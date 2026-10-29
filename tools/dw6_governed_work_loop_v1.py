"""DW6 v1: one-step, recoverable operator over the proven DW5 lifecycle.

No background polling, inferred authorization, or generic command execution.
Each invocation re-reads the durable Controller/Work frontiers. Safe capture and
review can advance without a flag; execute/post/send require explicit intent
and retain their own DW5 exact-digest human confirmation.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

DEFAULT_WORK_STORE = ".codexia/controller-shell-work.sqlite3"


def choose_action(*, controller_phase: str, controller_matches: bool,
                  worker_advanced: bool, controller_decision: str,
                  work: dict | None, review_response: str | None = None) -> str:
    """Pure, fail-closed transition selector. Never performs an effect."""
    if controller_phase != "ready" or not controller_matches:
        return "blocked_controller_frontier"
    if work is None:
        if controller_decision == "commands":
            return "blocked_uncaptured_worker_moved" if worker_advanced else "capture"
        if controller_decision == "no_commands":
            return "interpret" if worker_advanced else "idle"
        return "blocked_controller_decision"
    if work.get("post_handoff") and not work.get("post_receipt"):
        return "blocked_ambiguous_post"
    if work.get("post_receipt"):
        return "interpret" if worker_advanced else "idle"
    if worker_advanced:
        return "blocked_worker_frontier_moved"
    plan = work.get("plan")
    if not isinstance(plan, dict) or plan.get("decision") != "commands":
        return "blocked_invalid_work"
    digest = work.get("plan_sha256")
    steps = plan.get("steps")
    results = work.get("results")
    if not isinstance(digest, str) or len(digest) != 64:
        return "blocked_invalid_work"
    if not isinstance(steps, list) or not 1 <= len(steps) <= 8:
        return "blocked_invalid_work"
    if not isinstance(results, list) or len(results) > len(steps):
        return "blocked_invalid_work"
    if work.get("review_recorded"):
        if review_response == f"REJECT {digest}":
            return "rejected"
        if review_response != f"APPROVE {digest}":
            return "blocked_review_inconsistent"
    else:
        if review_response is not None:
            return "blocked_review_inconsistent"
        if results:
            return "blocked_unapproved_execution"
        if work.get("yield", {}).get("kind") == "attention":
            return "await_approval"
        if work.get("yield", {}).get("kind") == "none":
            return "review"
        return "blocked_unexpected_yield"
    if any(item.get("state") != "succeeded" for item in results
           if isinstance(item, dict)) or any(not isinstance(item, dict) for item in results):
        return "blocked_non_success"
    if len(results) < len(steps):
        return "await_execution"
    return "await_post"


def _work_for_controller(path: str, conversation: str, message: str) -> str | None:
    """Find exact DW5 idempotent ingress. Never create a new Work on lookup."""
    database = Path(path)
    if not database.is_file():
        return None
    uri = database.resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True) as db:
        rows = db.execute(
            "SELECT work_id FROM g2_work_v1 WHERE source_namespace=? AND source_id=?",
            ("codexia.standalone.work", f"shell:{conversation}:{message}"),
        ).fetchall()
    if len(rows) > 1:
        raise ValueError("duplicate Work ingress identity")
    return rows[0][0] if rows else None


def _last_review(store, work_id: str) -> str | None:
    from codexia_manual_agent.attention_core import project_attention_responses

    responses = project_attention_responses(store.events(work_id))
    if len(responses) > 1:
        raise ValueError("multiple human responses for one Work")
    return responses[0].response_text if responses else None


def probe(*, project: str, registry, client, store_path: str) -> dict:
    """Observe the current DW5 frontiers. Does not send, execute, or post."""
    from tools.dw_codexia_controller_command_v1 import _latest
    from tools.dw_codexia_controller_continue_v1 import ControllerTurnStore
    from tools.dw_codexia_shell_plan_v1 import _status
    from codexia_manual_agent.work_core import SqliteWorkStore

    binding = registry.require_ready(project)
    previous = ControllerTurnStore(registry).latest(project)
    phase = previous["phase"] if previous is not None else binding.phase
    expected_controller = (
        previous["controller_message_id"] if previous is not None
        else binding.controller_message_id
    )
    source_worker_id = (
        previous["worker_message_id"] if previous is not None
        else binding.worker_message_id
    )
    worker = _latest(client, binding.worker_conversation)
    controller = _latest(client, binding.controller_conversation)
    matches = bool(expected_controller and controller.message_id == expected_controller)
    advanced = worker.message_id != source_worker_id
    result = {
        "project": project,
        "worker_conversation": binding.worker_conversation,
        "controller_conversation": binding.controller_conversation,
        "controller_message_id": expected_controller,
        "worker_message_id": worker.message_id,
        "controller_phase": phase,
        "worker_advanced": advanced,
        "work_id": None,
        "next_action": None,
        "effects": "NONE",
    }
    if not matches or phase != "ready":
        result["next_action"] = choose_action(
            controller_phase=phase, controller_matches=matches,
            worker_advanced=advanced, controller_decision="unknown", work=None,
        )
        return result
    work_id = _work_for_controller(store_path, binding.controller_conversation,
                                   expected_controller)
    work = None
    response = None
    if work_id:
        store = SqliteWorkStore(store_path)
        work = _status(store, work_id)
        if (
            work["controller_conversation"] != binding.controller_conversation
            or work["controller_message_id"] != expected_controller
            or work["worker_message_id"] != source_worker_id
            or work["project"] != project
            or work["repo"] != binding.repo
            or work["workspace"] != binding.workspace
        ):
            raise ValueError("Gen2 Work scope differs from project binding")
        response = _last_review(store, work_id)
        result["work_id"] = work_id
        result["plan_sha256"] = work["plan_sha256"]
        result["completed_steps"] = sum(
            row.get("state") == "succeeded" for row in work["results"]
        )
        result["total_steps"] = len(work["plan"]["steps"])
    # The saved Controller reply was checked at the DW5 ready transition.
    # Re-parse only the narrow decision type when there is no captured Work.
    decision = "commands"
    if work is None:
        from codexia_manual_agent.workflow_runtime.chat_command_plan_v1 import _json_object

        clean = controller.text.strip()
        if clean.startswith("RUN_COMMANDS:\n"):
            clean = clean[len("RUN_COMMANDS:\n"):].strip()
        data = _json_object(clean, {
            "schema_version", "source_sha256", "decision", "steps", "reason",
        })
        decision = data["decision"]
    result["next_action"] = choose_action(
        controller_phase=phase, controller_matches=matches,
        worker_advanced=advanced, controller_decision=decision,
        work=work, review_response=response,
    )
    if result["next_action"] == "idle":
        from tools.dw6_worker_continuation_v1 import continuation_frontier
        result.update(continuation_frontier(result, registry))
    return result


def advance(*, state: dict, registry, client, store_path: str,
            requested: str | None, model: str, reasoning_effort: str,
            reason: str | None) -> dict:
    """Apply exactly one permitted DW5 transition; never silently authorize."""
    action = state["next_action"]
    work_id = state["work_id"]
    project = state["project"]
    privileged = {"approve": "await_approval", "reject": "await_approval",
                  "execute": "await_execution", "post": "await_post",
                  "interpret": "interpret", "continue_worker": "continue_worker"}
    if requested is not None and privileged[requested] != action:
        raise ValueError(f"{requested} does not match verified frontier {action}")
    if action.startswith("blocked_") or action == "rejected":
        raise ValueError(f"DW6 stops at {action}; inspect durable history")
    if requested is None and action in privileged.values():
        return {**state, "status": "WAITING_FOR_EXPLICIT_HUMAN_ACTION"}
    if action == "idle":
        return {**state, "status": "NO_NEW_WORKER_REQUEST"}
    from codexia_manual_agent.work_core import SqliteWorkStore
    from tools import dw_codexia_shell_plan_v1 as shell
    from tools.dw_codexia_controller_continue_v1 import continue_controller

    if action == "capture" and requested is None:
        binding = registry.require_ready(project)
        args = SimpleNamespace(
            project=project, repo=binding.repo, workspace=binding.workspace,
            registry=str(registry.database),
            worker_conversation=binding.worker_conversation,
            codexia_conversation=binding.controller_conversation,
            worker_message_id=state["worker_message_id"],
            codexia_message_id=state["controller_message_id"],
        )
        return shell._capture(args, client, SqliteWorkStore(store_path))
    if action == "review" and requested is None:
        return shell._review(SqliteWorkStore(store_path), work_id)
    if requested in ("approve", "reject"):
        return shell._answer(SqliteWorkStore(store_path), work_id, requested)
    if requested == "execute":
        return shell._run(SqliteWorkStore(store_path), work_id, client)
    if requested == "post":
        return shell._post(SqliteWorkStore(store_path), work_id, client, model)
    if requested == "interpret":
        return continue_controller(
            registry=registry, client=client, project=project,
            reason=reason or "New worker assistant reply after a verified prior handoff; interpret only current requested commands. No execution authority.",
            model=model, effort=reasoning_effort,
        )
    if requested == "continue_worker":
        from tools.dw6_worker_continuation_v1 import continue_worker
        return continue_worker(state=state, registry=registry, client=client,
                               model=model, effort=reasoning_effort)
    raise ValueError(f"unsupported DW6 transition: {action}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("status", "advance"))
    parser.add_argument("--project", required=True)
    parser.add_argument("--auth-file", required=True)
    parser.add_argument("--registry", default=".codexia/simple_work_v0.sqlite3")
    parser.add_argument("--store", default=DEFAULT_WORK_STORE)
    parser.add_argument("--model", default="gpt-6")
    parser.add_argument("--reasoning-effort", default="high", choices=("medium", "high"))
    parser.add_argument("--reason")
    flags = parser.add_mutually_exclusive_group()
    for name in ("approve", "reject", "execute", "post", "interpret", "continue_worker"):
        flags.add_argument(f"--{name.replace('_', '-')}", action="store_true")
    args = parser.parse_args(argv)
    requested = next((name for name in ("approve", "reject", "execute", "post", "interpret", "continue_worker")
                      if getattr(args, name)), None)
    if args.action == "status" and requested is not None:
        parser.error("status is read-only and accepts no action flags")
    try:
        from tools.dw_codexia_controller_command_v1 import _client
        from tools.dw_codexia_project_bootstrap_v1 import DW5ProjectRegistry

        registry = DW5ProjectRegistry(args.registry)
        client = _client(args.auth_file)
        state = probe(project=args.project, registry=registry,
                      client=client, store_path=args.store)
        result = state if args.action == "status" else advance(
            state=state, registry=registry, client=client,
            store_path=args.store, requested=requested,
            model=args.model, reasoning_effort=args.reasoning_effort,
            reason=args.reason,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (KeyboardInterrupt, EOFError):
        print("DW6 INTERRUPTED: inspect existing Work; do not retry effects blindly", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"DW6 STOPPED ({type(exc).__name__}): {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
