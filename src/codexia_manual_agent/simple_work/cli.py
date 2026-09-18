from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from codexia_manual_agent.domain.errors import CodexiaError
from codexia_manual_agent.providers.chatgpt_web import ChatGPTWebProvider
from codexia_manual_agent.simple_work.runtime import SimpleWorkRuntime
from codexia_manual_agent.simple_work.session import SimpleWorkSession, SimpleWorkStore


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m codexia_manual_agent.simple_work.cli",
        description=(
            "Simple Work v0: one persistent Codexia chat plus one persistent worker chat."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    start = sub.add_parser("start")
    start.add_argument("request")
    _common_run_args(start)

    resume = sub.add_parser("resume")
    resume.add_argument("work_id")
    _common_run_args(resume)

    answer = sub.add_parser("answer")
    answer.add_argument("work_id")
    answer.add_argument("answer")
    _common_run_args(answer)

    status = sub.add_parser("status")
    status.add_argument("work_id")
    status.add_argument("--database", default=".codexia/simple_work_v0.sqlite3")

    return parser


def _common_run_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--database", default=".codexia/simple_work_v0.sqlite3")
    parser.add_argument("--auth-file", default="auth_data.json")
    parser.add_argument("--timeout", type=float, default=900.0)
    parser.add_argument("--max-cycles", type=int, default=8)


def _runtime(args: argparse.Namespace) -> SimpleWorkRuntime:
    provider = ChatGPTWebProvider(
        auth_file=args.auth_file,
        timeout=args.timeout,
    )
    return SimpleWorkRuntime(
        provider=provider,
        store=SimpleWorkStore(Path(args.database)),
    )


def _session_payload(session: SimpleWorkSession) -> dict[str, object]:
    return {
        "work_id": session.work_id,
        "status": session.status.value,
        "user_request": session.user_request,
        "codexia_conversation_id": session.codexia_conversation_id,
        "worker_conversation_id": session.worker_conversation_id,
        "worker_turns": session.worker_turns,
        "next_worker_message": session.next_worker_message,
        "pending_human_question": session.pending_human_question,
        "final_text": session.final_text,
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "status":
            session = SimpleWorkStore(args.database).load(args.work_id)
            payload = {"action": "status", "session": _session_payload(session)}
        else:
            runtime = _runtime(args)
            if args.command == "start":
                result = runtime.start(args.request, max_cycles=args.max_cycles)
            elif args.command == "resume":
                result = runtime.resume(args.work_id, max_cycles=args.max_cycles)
            elif args.command == "answer":
                result = runtime.answer(
                    args.work_id,
                    args.answer,
                    max_cycles=args.max_cycles,
                )
            else:  # pragma: no cover
                raise RuntimeError(f"unknown command: {args.command}")
            payload = {
                "action": args.command,
                "stop": result.stop,
                "session": _session_payload(result.session),
            }
    except (CodexiaError, KeyError, RuntimeError, ValueError, OSError) as exc:
        print(f"simple-work: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
