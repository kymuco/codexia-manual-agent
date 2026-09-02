from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence
from uuid import uuid4

from codexia_manual_agent import __version__
from codexia_manual_agent.adapters.filesystem_workspace import FilesystemWorkspace
from codexia_manual_agent.adapters.json_session_store import JsonSessionStore
from codexia_manual_agent.application.execute_process import ExecuteProcessService
from codexia_manual_agent.application.inspect_workspace import InspectWorkspaceService
from codexia_manual_agent.application.mutate_workspace import MutateWorkspaceService
from codexia_manual_agent.application.run_agent import RunAgentService
from codexia_manual_agent.application.run_session import RunSessionService
from codexia_manual_agent.application.session_queries import SessionQueryService
from codexia_manual_agent.authority import (
    ActionLifecycle,
    ActionPhase,
    ApprovalMode,
    LocalApprovalAuthority,
)
from codexia_manual_agent.domain.errors import (
    AuthorizationDeniedError,
    CodexiaError,
    InvalidWorkspaceMutationError,
)
from codexia_manual_agent.domain.models import AgentBudgets, ToolName, ToolRequest
from codexia_manual_agent.execution import ProcessLimits, ProcessTerminationReason
from codexia_manual_agent.git_mutation import (
    GitMutationOutcome,
    close_governed_git_push_preparation,
    execute_git_commit,
    execute_governed_git_push,
    prepare_git_commit_proposal,
    prepare_governed_git_push_proposal,
)
from codexia_manual_agent.mutation import (
    MAX_POSTIMAGE_BYTES,
    MutationOperation,
    MutationTerminationReason,
)
from codexia_manual_agent.providers.chatgpt_web import ChatGPTWebProvider


def _state_directory(workspace: Path, supplied: str | None) -> Path:
    if supplied:
        return Path(supplied).expanduser().resolve()
    return workspace.resolve() / ".codexia" / "sessions"


def _emit(data: Any, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True))
        return

    if isinstance(data, dict):
        for key, value in data.items():
            if isinstance(value, (list, dict)):
                rendered = json.dumps(value, ensure_ascii=False, indent=2)
                print(f"{key}: {rendered}")
            else:
                print(f"{key}: {value}")
        return

    if isinstance(data, list):
        for item in data:
            print(json.dumps(item, ensure_ascii=False, sort_keys=True))
        return

    print(data)


def _add_provider_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--provider", choices=("chatgpt-web",), default="chatgpt-web")
    parser.add_argument("--auth-file", default="auth_data.json")
    parser.add_argument("--model")
    parser.add_argument("--reasoning-effort")
    parser.add_argument("--timeout", type=float, default=90.0)
    parser.add_argument("--max-turns", type=int, default=8)
    parser.add_argument("--max-tool-calls", type=int, default=8)
    parser.add_argument("--max-response-chars", type=int, default=32_768)
    parser.add_argument("--max-total-model-chars", type=int, default=131_072)
    parser.add_argument("--max-observation-chars", type=int, default=131_072)


def _add_git_authority_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--workspace", default=".")
    parser.add_argument(
        "--approval-mode",
        choices=tuple(mode.value for mode in ApprovalMode),
        default=ApprovalMode.RISKY.value,
    )
    parser.add_argument(
        "--approve",
        action="store_true",
        help=(
            "Display the exact Git proposal/preview, then request a local YES before "
            "issuing the one-shot human authorization receipt."
        ),
    )
    parser.add_argument("--json", action="store_true")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="codexia",
        description="Codexia human-governed coding runtime",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser(
        "run",
        help="Create a read-only session and optionally run a bounded agent task.",
    )
    run_parser.add_argument("task", nargs="?")
    run_parser.add_argument("--workspace", default=".")
    run_parser.add_argument("--state-dir")
    run_parser.add_argument("--prompt-version", default="v0.3")
    run_parser.add_argument("--title")
    run_parser.add_argument("--json", action="store_true")
    _add_provider_arguments(run_parser)

    resume_parser = subparsers.add_parser(
        "resume",
        help="Read a session manifest or continue its ChatGPT conversation.",
    )
    resume_parser.add_argument("session_id")
    resume_parser.add_argument("task", nargs="?")
    resume_parser.add_argument("--state-dir", default=".codexia/sessions")
    resume_parser.add_argument("--json", action="store_true")
    _add_provider_arguments(resume_parser)

    sessions_parser = subparsers.add_parser(
        "sessions",
        help="List local session manifests.",
    )
    sessions_parser.add_argument("--state-dir", default=".codexia/sessions")
    sessions_parser.add_argument("--json", action="store_true")

    inspect_parser = subparsers.add_parser(
        "inspect",
        help="Run one workspace-bounded read-only inspection.",
    )
    inspect_parser.add_argument("--workspace", default=".")
    inspect_parser.add_argument("--json", action="store_true")
    inspect_subparsers = inspect_parser.add_subparsers(dest="inspection", required=True)

    read_parser = inspect_subparsers.add_parser("read")
    read_parser.add_argument("path")
    read_parser.add_argument("--max-bytes", type=int, default=131_072)

    list_parser = inspect_subparsers.add_parser("list")
    list_parser.add_argument("path", nargs="?", default=".")
    list_parser.add_argument("--recursive", action="store_true")
    list_parser.add_argument("--limit", type=int, default=200)

    search_parser = inspect_subparsers.add_parser("search")
    search_parser.add_argument("query")
    search_parser.add_argument("path", nargs="?", default=".")
    search_parser.add_argument("--max-matches", type=int, default=100)

    inspect_subparsers.add_parser("git-status")

    exec_parser = subparsers.add_parser(
        "exec",
        help="Run one explicitly human-approved bounded local process.",
    )
    exec_parser.add_argument("--workspace", default=".")
    exec_parser.add_argument("--cwd", default=".")
    exec_parser.add_argument(
        "--approval-mode",
        choices=tuple(mode.value for mode in ApprovalMode),
        default=ApprovalMode.RISKY.value,
    )
    exec_parser.add_argument(
        "--approve",
        action="store_true",
        help="Explicitly approve this exact process proposal.",
    )
    exec_parser.add_argument("--timeout", type=float, default=30.0)
    exec_parser.add_argument("--max-stdout-bytes", type=int, default=65_536)
    exec_parser.add_argument("--max-stderr-bytes", type=int, default=65_536)
    exec_parser.add_argument("--json", action="store_true")
    exec_parser.add_argument(
        "argv",
        nargs=argparse.REMAINDER,
        help="Structured argv after --; no shell string is evaluated.",
    )

    write_parser = subparsers.add_parser(
        "write",
        help="Create or replace one workspace file through M2.3 human authorization.",
    )
    write_parser.add_argument("--workspace", default=".")
    operation = write_parser.add_mutually_exclusive_group(required=True)
    operation.add_argument("--create", metavar="PATH")
    operation.add_argument("--replace", metavar="PATH")
    write_parser.add_argument(
        "--content-file",
        required=True,
        help="Local file whose exact bytes become the proposed postimage.",
    )
    write_parser.add_argument(
        "--approval-mode",
        choices=tuple(mode.value for mode in ApprovalMode),
        default=ApprovalMode.RISKY.value,
    )
    write_parser.add_argument(
        "--approve",
        action="store_true",
        help="Explicitly approve this exact digest-bound mutation proposal.",
    )
    write_parser.add_argument("--json", action="store_true")

    git_parser = subparsers.add_parser(
        "git",
        help="Run one explicit M2.5/M2.5.1 Git commit or push mutation.",
    )
    git_subparsers = git_parser.add_subparsers(dest="git_action", required=True)

    git_commit_parser = git_subparsers.add_parser(
        "commit",
        help="Create one exact commit from the currently staged index.",
    )
    _add_git_authority_arguments(git_commit_parser)
    git_commit_parser.add_argument(
        "--message",
        required=True,
        help="Exact UTF-8 commit message bound into the proposal.",
    )

    git_push_parser = git_subparsers.add_parser(
        "push",
        help="Push one exact commit OID to one explicit branch ref.",
    )
    _add_git_authority_arguments(git_push_parser)
    git_push_parser.add_argument(
        "--remote",
        required=True,
        help="Configured remote name whose exact push URL is bound into the proposal.",
    )
    git_push_parser.add_argument(
        "--destination-ref",
        required=True,
        help="Explicit refs/heads/... destination; implicit upstreams are not allowed.",
    )
    git_push_parser.add_argument(
        "--transport",
        choices=("file", "ssh", "https"),
        default="file",
        help="Explicit governed push backend. file preserves the M2.5 default.",
    )
    git_push_parser.add_argument(
        "--ssh-identity-file",
        help="Absolute private-key source path required by --transport ssh.",
    )
    git_push_parser.add_argument(
        "--ssh-host-key-file",
        help="Absolute one-host known-hosts file required by --transport ssh.",
    )
    git_push_parser.add_argument(
        "--https-credential-file",
        help=(
            "Absolute credential source file containing exactly one HTTPS credential URL "
            "for the reviewed repository; required by --transport https."
        ),
    )
    git_push_parser.add_argument(
        "--https-ca-bundle",
        help="Absolute CA bundle path required by --transport https.",
    )

    return parser


def _budgets(args: argparse.Namespace) -> AgentBudgets:
    return AgentBudgets(
        max_turns=args.max_turns,
        max_tool_calls=args.max_tool_calls,
        max_response_chars=args.max_response_chars,
        max_total_model_chars=args.max_total_model_chars,
        max_observation_chars=args.max_observation_chars,
    )


def _provider(args: argparse.Namespace) -> ChatGPTWebProvider:
    return ChatGPTWebProvider(
        auth_file=args.auth_file,
        model=args.model,
        reasoning_effort=args.reasoning_effort,
        timeout=args.timeout,
    )


def _execute_live_task(
    *,
    manifest: Any,
    task: str,
    store: JsonSessionStore,
    args: argparse.Namespace,
) -> tuple[dict[str, Any], int]:
    provider = _provider(args)
    result, updated = RunAgentService(
        provider=provider,
        store=store,
        budgets=_budgets(args),
        model=args.model,
        reasoning_effort=args.reasoning_effort,
    ).run(manifest=manifest, task=task)
    data = {"session": updated.to_dict(), "result": result.to_dict()}
    return data, 0 if result.completed else 1


def _run_command(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    workspace = Path(args.workspace).expanduser()
    store = JsonSessionStore(_state_directory(workspace, args.state_dir))

    if args.task:
        provider = _provider(args)
        manifest = RunSessionService(store).start(
            workspace=workspace,
            prompt_version=args.prompt_version,
            title=args.title,
            provider=provider.provider_id,
            model=args.model,
            reasoning_effort=args.reasoning_effort,
        )
        result, updated = RunAgentService(
            provider=provider,
            store=store,
            budgets=_budgets(args),
            model=args.model,
            reasoning_effort=args.reasoning_effort,
        ).run(manifest=manifest, task=args.task)
        return {"session": updated.to_dict(), "result": result.to_dict()}, 0 if result.completed else 1

    manifest = RunSessionService(store).start(
        workspace=workspace,
        prompt_version=args.prompt_version,
        title=args.title,
    )
    data = manifest.to_dict()
    data["message"] = "Read-only session initialized. No model provider was contacted."
    return data, 0


def _resume_command(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    store = JsonSessionStore(args.state_dir)
    manifest = SessionQueryService(store).resume(args.session_id)
    if not args.task:
        return manifest.to_dict(), 0
    if not args.model:
        args.model = manifest.model
    if not args.reasoning_effort:
        args.reasoning_effort = manifest.reasoning_effort
    return _execute_live_task(manifest=manifest, task=args.task, store=store, args=args)


def _sessions_command(args: argparse.Namespace) -> tuple[list[dict[str, Any]], int]:
    store = JsonSessionStore(args.state_dir)
    return [manifest.to_dict() for manifest in SessionQueryService(store).list()], 0


def _inspect_command(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    workspace = FilesystemWorkspace(args.workspace)
    service = InspectWorkspaceService(workspace)

    if args.inspection == "read":
        tool = ToolName.READ_FILE
        arguments = {"path": args.path, "max_bytes": args.max_bytes}
    elif args.inspection == "list":
        tool = ToolName.LIST_FILES
        arguments = {"path": args.path, "recursive": args.recursive, "max_entries": args.limit}
    elif args.inspection == "search":
        tool = ToolName.SEARCH_TEXT
        arguments = {"query": args.query, "path": args.path, "max_matches": args.max_matches}
    elif args.inspection == "git-status":
        tool = ToolName.GIT_STATUS
        arguments = {}
    else:  # pragma: no cover - argparse prevents this
        raise RuntimeError(f"Unknown inspection: {args.inspection}")

    observation = service.execute(
        ToolRequest(request_id=str(uuid4()), name=tool, arguments=arguments)
    )
    if not observation.success:
        raise CodexiaError(observation.error or "Inspection failed")
    return observation.to_dict(), 0


def _exec_command(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    argv = list(args.argv)
    if argv and argv[0] == "--":
        argv = argv[1:]
    if not argv:
        raise ValueError("codexia exec requires structured argv after --")

    limits = ProcessLimits(
        timeout_seconds=args.timeout,
        max_stdout_bytes=args.max_stdout_bytes,
        max_stderr_bytes=args.max_stderr_bytes,
    )
    result = ExecuteProcessService().run(
        workspace=args.workspace,
        argv=argv,
        cwd=args.cwd,
        mode=ApprovalMode(args.approval_mode),
        approved=True if args.approve else None,
        limits=limits,
        actor="local-cli",
    )
    observation = result.observation
    success = observation.termination_reason is ProcessTerminationReason.EXITED and observation.exit_code == 0
    return result.to_dict(), 0 if success else 1


def _read_bounded_content_file(source: Path) -> bytes:
    size = source.stat().st_size
    if size > MAX_POSTIMAGE_BYTES:
        raise InvalidWorkspaceMutationError(f"Mutation postimage exceeds {MAX_POSTIMAGE_BYTES} bytes")
    with source.open("rb") as handle:
        content = handle.read(MAX_POSTIMAGE_BYTES + 1)
    if len(content) > MAX_POSTIMAGE_BYTES:
        raise InvalidWorkspaceMutationError(f"Mutation postimage exceeds {MAX_POSTIMAGE_BYTES} bytes")
    return content


def _write_command(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    source = Path(args.content_file).expanduser()
    content = _read_bounded_content_file(source)
    if args.create is not None:
        operation = MutationOperation.CREATE
        target = args.create
    else:
        operation = MutationOperation.REPLACE
        target = args.replace
    result = MutateWorkspaceService().run(
        workspace=args.workspace,
        operation=operation,
        target=target,
        content=content,
        mode=ApprovalMode(args.approval_mode),
        approved=True if args.approve else None,
        actor="local-cli",
    )
    success = result.observation.termination_reason is MutationTerminationReason.APPLIED
    return result.to_dict(), 0 if success else 1


def _git_preview_payload(preparation) -> dict[str, object]:
    return {
        "proposal": preparation.proposal.to_dict(),
        "approval_preview": preparation.approval_preview.to_dict(),
    }


def _confirm_git_preview(preparation) -> bool:
    preview = _git_preview_payload(preparation)
    print("M2.5 exact Git mutation approval preview:", file=sys.stderr)
    print(
        json.dumps(preview, ensure_ascii=False, indent=2, sort_keys=True),
        file=sys.stderr,
    )
    print("Type YES to authorize this exact proposal: ", end="", file=sys.stderr, flush=True)
    response = sys.stdin.readline()
    return response.rstrip("\r\n") == "YES"


def _git_command(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    mode = ApprovalMode(args.approval_mode)
    is_push = args.git_action == "push"
    if args.git_action == "commit":
        preparation = prepare_git_commit_proposal(
            workspace=args.workspace,
            message=args.message,
        )
    elif is_push:
        preparation = prepare_governed_git_push_proposal(
            workspace=args.workspace,
            remote=args.remote,
            destination_ref=args.destination_ref,
            transport=args.transport,
            ssh_identity_file=args.ssh_identity_file,
            ssh_host_key_file=args.ssh_host_key_file,
            https_credential_file=args.https_credential_file,
            https_ca_bundle=args.https_ca_bundle,
        )
    else:  # pragma: no cover - argparse prevents this
        raise RuntimeError(f"Unknown Git action: {args.git_action}")

    try:
        preview = _git_preview_payload(preparation)
        if not args.approve:
            preview["status"] = "approval_required"
            return preview, 2

        approved = _confirm_git_preview(preparation)
        authority = LocalApprovalAuthority()
        receipt = authority.decide(
            preparation.proposal,
            mode=mode,
            approved=approved,
            actor="local-cli",
        )
        lifecycle = ActionLifecycle(preparation.proposal, mode)
        phase = lifecycle.apply_receipt(receipt, authority=authority)
        if phase is ActionPhase.DENIED:
            raise AuthorizationDeniedError(receipt.reason or "Local human denied Git mutation")

        if args.git_action == "commit":
            observation = execute_git_commit(
                preparation,
                lifecycle=lifecycle,
                authority=authority,
            )
        else:
            observation = execute_governed_git_push(
                preparation,
                lifecycle=lifecycle,
                authority=authority,
            )
        result = {
            **preview,
            "authorization": receipt.to_dict(),
            "observation": observation.to_dict(),
        }
        return result, 0 if observation.outcome is GitMutationOutcome.APPLIED else 1
    finally:
        if is_push:
            close_governed_git_push_preparation(preparation)


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "run":
            data, exit_code = _run_command(args)
        elif args.command == "resume":
            data, exit_code = _resume_command(args)
        elif args.command == "sessions":
            data, exit_code = _sessions_command(args)
        elif args.command == "inspect":
            data, exit_code = _inspect_command(args)
        elif args.command == "exec":
            data, exit_code = _exec_command(args)
        elif args.command == "write":
            data, exit_code = _write_command(args)
        elif args.command == "git":
            data, exit_code = _git_command(args)
        else:  # pragma: no cover - argparse prevents this
            parser.error(f"Unknown command: {args.command}")
            return 2
    except (CodexiaError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"codexia: {exc}", file=sys.stderr)
        return 1

    _emit(data, as_json=bool(getattr(args, "json", False)))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
