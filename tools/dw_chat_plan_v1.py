"""Interpret a normal ChatGPT reply into an exact source-bound command plan.

This tool never executes commands or posts to the source chat. GPT-6 is used
only to interpret intent; Gen2 stores a RoleRun and one durable cognition
handoff, so a crash never silently resends the same model request.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from codexia_manual_agent.providers.chatgpt_http import ChatGPTHttpProvider
from codexia_manual_agent.providers.model_provider_cognition import (
    ModelProviderCognitionPort,
)
from codexia_manual_agent.role_core import RoleRunState, project_role_runs
from codexia_manual_agent.standalone_work import (
    StandaloneWorkHost,
    StandaloneWorkSelector,
    StandaloneWorkSurface,
)
from codexia_manual_agent.work_core import SqliteWorkStore
from codexia_manual_agent.workflow_runtime.chat_command_plan_v1 import (
    PACK_ID,
    WORKFLOW_ID,
    VERSION,
    ChatCommandSource,
    CommandContextMaterialPort,
    CommandInstructionsMaterialPort,
    CommandInterpretationWorkflowV1,
    CommandPlan,
    pack_binding,
    role_binding,
    workflow_binding,
)

_PROVIDER = "codexia:chat-command-interpret-http@0.1.0"


class _Plugin:
    def codexia_pack_distribution(self):
        return {
            "schema_version": 1,
            "pack": pack_binding().to_dict(),
            "workflows": [workflow_binding().to_dict()],
            "roles": [role_binding().to_dict()],
            "capabilities": [],
        }

    def codexia_workflow_implementation(self, binding):
        if binding != workflow_binding().to_dict():
            raise ValueError("wrong interpreter workflow")
        return CommandInterpretationWorkflowV1()

    def codexia_completion_criterion(self, binding):
        raise ValueError("interpreter is not WorkCompletion")


class _Plugins:
    def get(self, plugin_id):
        if plugin_id != _PROVIDER:
            raise KeyError("unknown interpreter provider")
        return _Plugin()


def _selector():
    return StandaloneWorkSelector(
        provider_ref=_PROVIDER,
        workflow_id=WORKFLOW_ID, workflow_version=VERSION,
    )


def _host(store, work_id=None, port=None):
    return StandaloneWorkHost(
        plugin_service=_Plugins(),
        cognition_port=port,
        instructions=CommandInstructionsMaterialPort() if work_id else None,
        context=CommandContextMaterialPort(store, work_id) if work_id else None,
    )


def _source_from_message(args, client):
    messages = client.get_messages(args.conversation, limit=1)
    if len(messages) != 1 or messages[0].role != "assistant":
        raise ValueError("latest current-branch message must be an assistant reply")
    message = messages[0]
    if not message.message_id:
        raise ValueError("assistant reply has no canonical message identity")
    if args.message_id and args.message_id != message.message_id:
        raise ValueError("selected assistant message is not the current chat head")
    from codexia_manual_agent.workflow_runtime.chat_command_plan_v1 import _sha

    return ChatCommandSource(
        project=args.project, repo=args.repo,
        conversation_id=args.conversation,
        source_message_id=message.message_id,
        source_text=message.text,
        source_sha256=_sha(message.text),
        workspace=str(Path(args.workspace).resolve()),
    )


def _read_port(args):
    from chatgpt_web_adapter.browserless_http import BrowserlessHTTPClient
    return BrowserlessHTTPClient(auth_file=args.auth_file)


def _port(args):
    from tools.dw_chatgpt_http_bootstrap_v0 import _assert_cwa_source
    from tools.dw_research_http import _VerifiedHTTPProvider

    _assert_cwa_source(Path(args.cwa_root))
    if not Path(args.auth_file).is_file():
        raise ValueError("CWA auth file missing")
    provider = _VerifiedHTTPProvider(
        auth_file=args.auth_file, model=args.model,
        reasoning_effort=args.reasoning_effort, timeout=240.0,
    )
    return ModelProviderCognitionPort(provider)


def _report(store, work_id):
    status = StandaloneWorkSurface(store).status(work_id)
    if len(status["workflow"]) != 1 or status["workflow"][0]["pack"] != pack_binding().to_dict():
        raise ValueError("Work is not bound to the exact command interpretation Pack")
    source = ChatCommandSource.parse(status["work"]["objective"])
    roles = project_role_runs(store.events(work_id))
    result = {
        "work_id": work_id,
        "project": source.project,
        "repo": source.repo,
        "conversation_id": source.conversation_id,
        "source_message_id": source.source_message_id,
        "source_sha256": source.source_sha256,
        "revision": status["work"]["revision"],
        "role_count": len(roles),
        "command_effects": "NONE",
        "status": "PENDING_ROLE",
    }
    if len(roles) > 1:
        raise ValueError("Interpreter Work contains multiple RoleRuns")
    if not roles:
        return result
    role = roles[0]
    if role.run.binding != role_binding():
        raise ValueError("Interpreter RoleBinding has changed")
    result["role_run_id"] = role.run.role_run_id
    result["role_state"] = role.state.value
    if role.state is RoleRunState.COMPLETED:
        try:
            plan = CommandPlan.parse(role.output_text, source=source)
        except (ValueError, TypeError) as exc:
            result.update({
                "status": "INVALID_PLAN_PRESERVED",
                "validation_error": str(exc),
                "recovery": "export-raw",
            })
        else:
            result.update({
                "status": "PLAN_READY_UNEXECUTED",
                "plan_digest": plan.digest,
                "plan": plan.to_dict(),
            })
    else:
        result["status"] = "ROLE_" + role.state.value.upper()
    return result


def _advance(store, work_id, port):
    pre = _report(store, work_id)
    if pre["status"] in {
        "INVALID_PLAN_PRESERVED", "PLAN_READY_UNEXECUTED",
    }:
        return pre
    try:
        StandaloneWorkSurface(store).advance(
            work_id, selector=_selector(),
            host_factory=lambda: _host(store, work_id, port),
            max_steps=4,
        )
    except Exception:
        # No resending, even after an ambiguous HTTP write. Caller can inspect.
        post = _report(store, work_id)
        if post["status"] == "INVALID_PLAN_PRESERVED":
            return post
        raise
    return _report(store, work_id)


def _export(store, work_id, output: Path, *, raw: bool):
    status = _report(store, work_id)
    roles = project_role_runs(store.events(work_id))
    if len(roles) != 1 or roles[0].state is not RoleRunState.COMPLETED:
        raise ValueError("no completed interpreter Role to export")
    if not raw and status["status"] != "PLAN_READY_UNEXECUTED":
        raise ValueError("plan invalid; use export-raw for exact original role output")
    if raw:
        data = roles[0].output_text
    else:
        data = json.dumps(status["plan"], ensure_ascii=False, indent=2)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(data)
        handle.write("\n" if not data.endswith("\n") else "")
    return {
        "status": "RAW_ROLE_EXPORT_ONLY" if raw else "PLAN_EXPORT_UNEXECUTED",
        "work_id": work_id, "file": str(output),
    }


def _confirm_send():
    print(
        "This sends ONE interpreter Role cognition request through CWA HTTP. "
        "It will not execute commands, modify files, or post to the source chat. "
        "The admitted model handoff is NEVER retried automatically.",
        file=sys.stderr,
    )
    if input("Type INTERPRET to authorize the model request: ").strip() != "INTERPRET":
        raise ValueError("interpreter send not authorized")


def _parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", default=".codexia/chat-plan-work.sqlite3")
    sub = parser.add_subparsers(dest="action", required=True)

    def add_send(p):
        p.add_argument("--send", action="store_true")
        p.add_argument("--cwa-root")
        p.add_argument("--auth-file")
        p.add_argument("--model", default="gpt-6")
        p.add_argument("--reasoning-effort", default="high", choices=(
            "instant", "medium", "high", "standard", "extended",
        ))

    begin = sub.add_parser("plan")
    begin.add_argument("--project", required=True)
    begin.add_argument("--repo", required=True)
    begin.add_argument("--conversation", required=True)
    begin.add_argument("--message-id")
    begin.add_argument("--workspace", required=True)
    add_send(begin)
    resume = sub.add_parser("resume")
    resume.add_argument("work_id")
    add_send(resume)
    status = sub.add_parser("status")
    status.add_argument("work_id")
    export = sub.add_parser("export")
    export.add_argument("work_id")
    export.add_argument("--out", type=Path, required=True)
    raw = sub.add_parser("export-raw")
    raw.add_argument("work_id")
    raw.add_argument("--out", type=Path, required=True)
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)
    try:
        store_path = Path(args.store)
        if args.action == "plan":
            if not args.cwa_root or not args.auth_file:
                raise ValueError("--cwa-root and --auth-file required for CWA read")
            from tools.dw_chatgpt_http_bootstrap_v0 import _assert_cwa_source
            _assert_cwa_source(Path(args.cwa_root))
            source = _source_from_message(args, _read_port(args))
            if args.send:
                _confirm_send()
                port = _port(args)
            else:
                port = None
            store_path.parent.mkdir(parents=True, exist_ok=True)
            store = SqliteWorkStore(store_path)
            started = StandaloneWorkSurface(store).start(
                objective=source.to_json(), selector=_selector(),
                host_factory=lambda: _host(store),
                source_id=source.conversation_id + ":" + source.source_message_id,
            )
            work_id = started["work"]["work_id"]
            print("WORK_ID=" + work_id, flush=True)
            result = (
                _advance(store, work_id, port) if port is not None
                else _report(store, work_id)
            )
        else:
            store = SqliteWorkStore(store_path)
            work_id = args.work_id
            if args.action == "status":
                result = _report(store, work_id)
            elif args.action == "export":
                result = _export(store, work_id, args.out, raw=False)
            elif args.action == "export-raw":
                result = _export(store, work_id, args.out, raw=True)
            else:
                pre = _report(store, work_id)
                if pre["status"] not in {"PENDING_ROLE", "ROLE_ACTIVE"}:
                    result = pre
                elif not args.send:
                    result = {"status": "NO_SEND", "work": pre}
                else:
                    _confirm_send()
                    result = _advance(store, work_id, _port(args))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 3 if result.get("status") == "INVALID_PLAN_PRESERVED" else 0
    except (KeyboardInterrupt, EOFError):
        print("INTERRUPTED: inspect Work before any retry", file=sys.stderr)
        return 130
    except Exception as exc:
        print(
            f"STOPPED ({type(exc).__name__}): {exc}; "
            "inspect Work, never automatically resend an existing handoff",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
