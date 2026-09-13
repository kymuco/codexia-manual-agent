from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from codexia_manual_agent.domain.errors import CodexiaError
from codexia_manual_agent.providers.chatgpt_web import ChatGPTWebProvider
from codexia_manual_agent.work.pilot_runtime import (
    daily_use_pilot_status,
    drive_daily_use_pilot,
    pilot_drive_summary,
    pilot_snapshot_summary,
    start_daily_use_pilot,
)
from codexia_manual_agent.work.supervisor import SupervisorPersistenceError


def _provider(args: argparse.Namespace) -> ChatGPTWebProvider:
    return ChatGPTWebProvider(
        auth_file=args.auth_file,
        model=args.model,
        reasoning_effort=args.reasoning_effort,
        timeout=args.timeout,
    )


def _add_provider_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--auth-file", default="auth_data.json")
    parser.add_argument("--model")
    parser.add_argument("--reasoning-effort")
    parser.add_argument("--timeout", type=float, default=90.0)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m codexia_manual_agent.work.pilot_cli",
        description="M6.6 first general daily-use delegated-work pilot",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    start = subparsers.add_parser(
        "start",
        help="Bind one existing ChatGPT conversation to an exact human WorkHandoff.",
    )
    start.add_argument("objective")
    start.add_argument("--conversation-id", required=True)
    start.add_argument("--database", default=".codexia/work-supervisor.sqlite3")
    start.add_argument("--context", action="append", default=[])
    start.add_argument("--constraint", action="append", default=[])
    start.add_argument("--attention-constraint", action="append", default=[])
    start.add_argument("--completion-expectation")
    start.add_argument("--scope")
    start.add_argument("--depth")
    start.add_argument("--human-actor", default="human")
    start.add_argument("--codexia-actor", default="codexia-pilot")
    start.add_argument("--worker-actor", default="chatgpt")
    _add_provider_arguments(start)

    drive = subparsers.add_parser(
        "drive",
        help="Advance one registered work until completion or a governed stop boundary.",
    )
    drive.add_argument("work_id")
    drive.add_argument("--database", default=".codexia/work-supervisor.sqlite3")
    drive.add_argument("--max-steps", type=int, default=32)
    drive.add_argument("--human-actor", default="human")
    drive.add_argument("--codexia-actor", default="codexia-pilot")
    drive.add_argument("--worker-actor", default="chatgpt")
    _add_provider_arguments(drive)

    status = subparsers.add_parser(
        "status",
        help="Recover the exact durable state for one pilot work item.",
    )
    status.add_argument("work_id")
    status.add_argument("--database", default=".codexia/work-supervisor.sqlite3")

    return parser


def _database_path(value: str) -> Path:
    path = Path(value).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        database = _database_path(args.database)
        if args.command == "start":
            snapshot = start_daily_use_pilot(
                database_path=database,
                provider=_provider(args),
                conversation_id=args.conversation_id,
                objective=args.objective,
                context=args.context,
                human_constraints=args.constraint,
                attention_constraints=args.attention_constraint,
                completion_expectation=args.completion_expectation,
                continuation_scope=args.scope,
                depth_interpretation=args.depth,
                human_actor=args.human_actor,
                codexia_actor=args.codexia_actor,
                worker_actor=args.worker_actor,
            )
            payload = {
                "pilot": "m6.6",
                "action": "start",
                "snapshot": pilot_snapshot_summary(snapshot),
            }
            code = 0
        elif args.command == "drive":
            result = drive_daily_use_pilot(
                database_path=database,
                provider=_provider(args),
                work_id=args.work_id,
                max_steps=args.max_steps,
                human_actor=args.human_actor,
                codexia_actor=args.codexia_actor,
                worker_actor=args.worker_actor,
            )
            payload = {
                "pilot": "m6.6",
                "action": "drive",
                "result": pilot_drive_summary(result),
            }
            code = 0 if result.stop.value in {"completed", "waiting_human"} else 2
        elif args.command == "status":
            snapshot = daily_use_pilot_status(
                database_path=database,
                work_id=args.work_id,
            )
            payload = {
                "pilot": "m6.6",
                "action": "status",
                "snapshot": pilot_snapshot_summary(snapshot),
            }
            code = 0
        else:  # pragma: no cover - argparse prevents this
            raise RuntimeError(f"Unknown pilot command: {args.command}")
    except (CodexiaError, SupervisorPersistenceError, OSError, ValueError) as exc:
        print(f"codexia-pilot: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
