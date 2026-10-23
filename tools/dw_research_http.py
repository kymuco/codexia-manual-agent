"""Run one bounded Gen2 Research Work through CWA Direct HTTP.

Local WorkStore owns authority. No browser fallback, generic tool execution,
automatic retry of an admitted handoff, or model-driven filesystem writes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from codexia_manual_agent.artifact_core import project_artifact_refs
from codexia_manual_agent.providers.chatgpt_http import ChatGPTHttpProvider
from codexia_manual_agent.providers.model_provider_cognition import (
    ModelProviderCognitionPort,
)
from codexia_manual_agent.research_work import (
    ResearchContextMaterialPort,
    ResearchInstructionsMaterialPort,
    ResearchWorkMaterializer,
)
from codexia_manual_agent.role_core import RoleRunState, project_role_runs
from codexia_manual_agent.standalone_work import (
    StandaloneWorkHost,
    StandaloneWorkSelector,
    StandaloneWorkSurface,
)
from codexia_manual_agent.work_core import SqliteWorkStore
from codexia_manual_agent.workflow_runtime.research_v1 import (
    RESEARCH_WORKFLOW_ID,
    RESEARCH_WORKFLOW_VERSION,
    STAGE_SYNTHESIS,
    SYNTHESIZER_ROLE_ID,
    ResearchCompletionCriterionV1,
    ResearchRoleOutput,
    ResearchWorkflowImplementationV1,
    research_pack_binding,
    research_role_bindings,
    research_workflow_binding,
)

PROVIDER_REF = "codexia:research-http-operator@0.1.0"
MAX_OBJECTIVE_CHARS = 16_384


class _ResearchProvider:
    def codexia_pack_distribution(self):
        return {
            "schema_version": 1,
            "pack": research_pack_binding().to_dict(),
            "workflows": [research_workflow_binding().to_dict()],
            "roles": [role.to_dict() for role in research_role_bindings()],
            "capabilities": [],
        }

    def codexia_workflow_implementation(self, raw_binding):
        if raw_binding != research_workflow_binding().to_dict():
            raise ValueError("Research WorkflowBinding changed")
        return ResearchWorkflowImplementationV1()

    def codexia_completion_criterion(self, raw_binding):
        if raw_binding != research_workflow_binding().to_dict():
            raise ValueError("Research completion binding changed")
        return ResearchCompletionCriterionV1()


class _PluginService:
    def get(self, plugin_id):
        if plugin_id != PROVIDER_REF:
            raise KeyError("Unsupported Research Pack provider")
        return _ResearchProvider()


def _selector() -> StandaloneWorkSelector:
    return StandaloneWorkSelector(
        provider_ref=PROVIDER_REF,
        workflow_id=RESEARCH_WORKFLOW_ID,
        workflow_version=RESEARCH_WORKFLOW_VERSION,
    )


def _host(store, work_id, *, cognition_port=None) -> StandaloneWorkHost:
    return StandaloneWorkHost(
        plugin_service=_PluginService(),
        cognition_port=cognition_port,
        instructions=ResearchInstructionsMaterialPort() if work_id else None,
        context=(
            ResearchContextMaterialPort(store=store, work_id=work_id)
            if work_id else None
        ),
    )


def _objective(text: str, files: list[str]) -> str:
    if not text.strip():
        raise ValueError("objective is empty")
    if len(files) > 12:
        raise ValueError("at most twelve context files")
    parts = [text.strip()]
    names: set[str] = set()
    for raw in files:
        source = Path(raw)
        if (
            source.is_symlink()
            or not source.is_file()
            or source.suffix.lower() not in {".md", ".txt"}
        ):
            raise ValueError("context input must be a regular Markdown/TXT file")
        if source.name in names:
            raise ValueError("context file basenames must be unique")
        names.add(source.name)
        data = source.read_bytes()
        if b"\x00" in data:
            raise ValueError("binary context is not supported")
        content = data.decode("utf-8")
        digest = hashlib.sha256(data).hexdigest()
        parts.append(
            f"\n[Untrusted reference document: {source.name}; sha256={digest}]\n"
            f"{content}\n[End reference document: {source.name}]"
        )
    result = "\n".join(parts)
    if len(result) > MAX_OBJECTIVE_CHARS:
        raise ValueError("context exceeds Gen2 16384-character objective budget")
    return result


def _report(status: dict, *, progression: dict | None = None) -> dict:
    work = status["work"]
    yield_data = status["yield"]
    attention = yield_data.get("attention") or {}
    return {
        "work_id": work["work_id"],
        "revision": work["revision"],
        "state": work["state"],
        "yield": yield_data["kind"],
        "human_question": attention.get("question"),
        "roles_total": len(status.get("unresolved", {}).get("roles", [])),
        "progression": progression["status"] if progression else None,
    }


def _export(store, work_id: str, destination: Path) -> str:
    events = store.events(work_id)
    completed = [
        role
        for role in project_role_runs(events)
        if role.run.binding.role_id == SYNTHESIZER_ROLE_ID
        and role.state is RoleRunState.COMPLETED
    ]
    if len(completed) != 1 or completed[0].output_text is None:
        raise ValueError("no completed synthesis to export")
    final = ResearchRoleOutput.parse(
        completed[0].output_text, expected_stage=STAGE_SYNTHESIS
    )
    digest = hashlib.sha256(final.content.encode("utf-8")).hexdigest()
    if not any(
        item.content_sha256 == digest for item in project_artifact_refs(events)
    ):
        raise ValueError("synthesis artifact not yet materialized")
    # This is an explicit operator-chosen output; never clobber an existing file.
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("x", encoding="utf-8", newline="\n") as output:
        output.write(final.content)
    return digest


def _execute(store, work_id: str, port, *, out: Path | None = None) -> dict:
    surface = StandaloneWorkSurface(store)
    result = surface.advance(
        work_id,
        selector=_selector(),
        host_factory=lambda: _host(store, work_id, cognition_port=port),
        max_steps=8,
    )
    roles = project_role_runs(store.events(work_id))
    if len(roles) == 4 and all(role.state is RoleRunState.COMPLETED for role in roles):
        # Materialization is a normal deterministic Gen2 Work admission, not
        # a file write; only a complete four-role result reaches this branch.
        ResearchWorkMaterializer(store).materialize(work_id)
        result = surface.advance(
            work_id,
            selector=_selector(),
            host_factory=lambda: _host(store, work_id, cognition_port=port),
            max_steps=2,
        )
    payload = _report(surface.status(work_id), progression=result["progression"])
    if out is not None:
        try:
            payload["export_sha256"] = _export(store, work_id, out)
            payload["output"] = str(out)
        except ValueError as exc:
            payload["export_pending"] = str(exc)
    return payload


def _authorize() -> None:
    print(
        "This run can submit up to FOUR Research Role messages through CWA HTTP. "
        "Each admitted handoff is never resent. Selected context is sent to "
        "ChatGPT. It does not edit project files or complete Work for you.",
        file=sys.stderr,
    )
    if input("Type SEND to authorize this bounded run: ").strip() != "SEND":
        raise ValueError("cancelled before any HTTP send")


class _VerifiedHTTPProvider(ChatGPTHttpProvider):
    """Require exact canonical assistant readback before role admission."""

    def send(self, request, *, media=None):
        from tools.dw_chatgpt_http_bootstrap_v0 import _verify_canonical

        response = super().send(request, media=media)
        _verify_canonical(self, response)
        return response


def _http_port(args):
    if not args.cwa_root or not args.auth_file:
        raise ValueError("--cwa-root and --auth-file are required for sending")
    from tools.dw_chatgpt_http_bootstrap_v0 import _assert_cwa_source

    _assert_cwa_source(Path(args.cwa_root))
    if not Path(args.auth_file).is_file():
        raise ValueError("auth file missing before any handoff")
    provider = _VerifiedHTTPProvider(
        auth_file=args.auth_file,
        model=args.model,
        reasoning_effort=args.reasoning_effort,
        timeout=240.0,
    )
    return ModelProviderCognitionPort(provider)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", default=".codexia/research-work.sqlite3")
    actions = parser.add_subparsers(dest="action", required=True)

    def send_options(p):
        p.add_argument("--send", action="store_true")
        p.add_argument("--cwa-root")
        p.add_argument("--auth-file")
        p.add_argument("--model", default="gpt-6")
        p.add_argument("--reasoning-effort", default="high", choices=("instant", "medium", "high", "standard", "extended"))
        p.add_argument("--out", type=Path)

    start = actions.add_parser("run", help="Create a new Work, optionally advance via HTTP")
    start.add_argument("objective")
    start.add_argument("--file", action="append", default=[])
    send_options(start)
    resume = actions.add_parser("resume", help="Continue an existing Work")
    resume.add_argument("work_id")
    send_options(resume)
    status = actions.add_parser("status", help="Inspect current Work")
    status.add_argument("work_id")
    answer = actions.add_parser("answer", help="Answer one durable AttentionNeed")
    answer.add_argument("work_id")
    answer.add_argument("response")
    export = actions.add_parser("export", help="Write validated synthesis to a new Markdown file")
    export.add_argument("work_id")
    export.add_argument("--out", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        path = Path(args.store)
        if args.action == "run":
            objective = _objective(args.objective, args.file)
            port = None
            if args.send:
                _authorize()
                port = _http_port(args)
            path.parent.mkdir(parents=True, exist_ok=True)
            store = SqliteWorkStore(path)
            surface = StandaloneWorkSurface(store)
            started = surface.start(
                objective=objective,
                selector=_selector(),
                host_factory=lambda: _host(store, None),
            )
            work_id = started["work"]["work_id"]
            print(f"WORK_ID={work_id}", flush=True)
            result = (
                _execute(store, work_id, port, out=args.out)
                if port is not None
                else _report(started)
            )
        else:
            store = SqliteWorkStore(path)
            surface = StandaloneWorkSurface(store)
            work_id = args.work_id
            if args.action == "status":
                result = _report(surface.status(work_id))
            elif args.action == "answer":
                result = _report(
                    surface.answer(work_id, response_text=args.response)["status"]
                )
            elif args.action == "export":
                result = {
                    "work_id": work_id,
                    "output": str(args.out),
                    "export_sha256": _export(store, work_id, args.out),
                }
            else:
                status = surface.status(work_id)
                if status["yield"]["kind"] != "none" or status["work"]["state"] != "active":
                    result = _report(status)
                elif not args.send:
                    result = {"status": "NO_SEND", **_report(status)}
                else:
                    _authorize()
                    port = _http_port(args)
                    result = _execute(store, work_id, port, out=args.out)

        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (KeyboardInterrupt, EOFError):
        print("Cancelled; inspect WorkStore before any retry.", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 - CLI boundary must not leak CWA secrets
        print(
            f"STOPPED ({type(exc).__name__}); inspect WorkStore and "
            "never redispatch an existing CognitionHandoff.",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
