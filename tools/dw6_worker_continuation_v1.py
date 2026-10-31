"""DW6.1: one authorized, single-use Worker cognition continuation.

Uses the existing project's Worker/Controller chats. Never executes shell,
never grants terminal authority, and fences ambiguous sends before CWA calls.
A human must arm one bounded non-secret task at the exact no_commands frontier.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _goal(text: str) -> str:
    if type(text) is not str or not 5 <= len(text.strip()) <= 500:
        raise ValueError("worker continuation task must contain 5..500 characters")
    if any(ch in text for ch in "\r\n\x00"):
        raise ValueError("worker continuation task must be a single line")
    return text.strip()


class WorkerContinuationStore:
    """Single-use local grant plus durable pre-send no-replay fence."""

    def __init__(self, registry):
        self.registry = registry

    def _ensure_schema(self):
        with self.registry._open() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS dw6_worker_continuation_v1 (
                    project TEXT NOT NULL,
                    controller_message_id TEXT NOT NULL,
                    controller_sha256 TEXT NOT NULL,
                    worker_message_id TEXT NOT NULL,
                    worker_sha256 TEXT NOT NULL,
                    task TEXT NOT NULL,
                    task_sha256 TEXT NOT NULL,
                    phase TEXT NOT NULL CHECK(phase IN
                        ('armed', 'send_admitted', 'worker_known', 'verified', 'cancelled')),
                    result_worker_message_id TEXT,
                    PRIMARY KEY(project, controller_message_id)
                )
            """)

    def latest(self, project: str):
        with self.registry._open() as db:
            # Status is strictly read-only, even before the first grant.
            exists = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='dw6_worker_continuation_v1'"
            ).fetchone()
            if exists is None:
                return None
            return db.execute(
                "SELECT * FROM dw6_worker_continuation_v1 WHERE project=? "
                "ORDER BY rowid DESC LIMIT 1", (project,),
            ).fetchone()

    def arm(self, *, project: str, controller_message_id: str,
            controller_sha256: str, worker_message_id: str,
            worker_sha256: str, task: str):
        task = _goal(task)
        self._ensure_schema()
        with self.registry._open() as db:
            prior = db.execute(
                "SELECT phase FROM dw6_worker_continuation_v1 WHERE project=? "
                "ORDER BY rowid DESC LIMIT 1", (project,),
            ).fetchone()
            if prior is not None and prior["phase"] in {
                "armed", "send_admitted", "worker_known"
            }:
                raise ValueError("earlier Worker continuation is active or ambiguous")
            db.execute(
                "INSERT INTO dw6_worker_continuation_v1 "
                "(project,controller_message_id,controller_sha256,worker_message_id,"
                "worker_sha256,task,task_sha256,phase) VALUES (?,?,?,?,?,?,?,'armed')",
                (project, controller_message_id, controller_sha256,
                 worker_message_id, worker_sha256, task, _sha(task)),
            )

    def transition(self, *, project: str, controller_message_id: str,
                   from_phase: str, to_phase: str, result_id: str | None = None):
        if (from_phase, to_phase) not in {
            ("armed", "send_admitted"),
            ("armed", "cancelled"),
            ("send_admitted", "worker_known"),
            ("worker_known", "verified"),
        }:
            raise ValueError("invalid Worker continuation transition")
        requires_id = to_phase in {"worker_known", "verified"}
        if requires_id != (isinstance(result_id, str) and bool(result_id)):
            raise ValueError("result identity required exactly at readback transitions")
        with self.registry._open() as db:
            if to_phase == "verified":
                # The admitted send ID is durable before canonical readback.
                # Verification must never replace it with a different ID.
                updated = db.execute(
                    "UPDATE dw6_worker_continuation_v1 SET phase='verified' "
                    "WHERE project=? AND controller_message_id=? "
                    "AND phase='worker_known' AND result_worker_message_id=?",
                    (project, controller_message_id, result_id),
                ).rowcount
            else:
                updated = db.execute(
                    "UPDATE dw6_worker_continuation_v1 SET phase=?, "
                    "result_worker_message_id=? "
                    "WHERE project=? AND controller_message_id=? AND phase=?",
                    (to_phase, result_id, project, controller_message_id, from_phase),
                ).rowcount
            if updated != 1:
                raise ValueError("Worker continuation state changed; no retry")


def continuation_frontier(state: dict, registry) -> dict:
    """An overlay on DW6 idle. No inferred task completion/continuation."""
    if state.get("next_action") != "idle" or state.get("work_id") is not None:
        return {}
    record = WorkerContinuationStore(registry).latest(state["project"])
    if record is None:
        return {}
    if record["phase"] in {"send_admitted", "worker_known"}:
        return {"next_action": "blocked_ambiguous_worker_send",
                "worker_continuation_phase": record["phase"]}
    if record["phase"] == "verified":
        # A verified Worker continuation only blocks reuse of its OWN
        # Controller grant. Once a later Controller turn has been verified,
        # this record is historical, not an active Worker-send fence.
        if record["controller_message_id"] == state["controller_message_id"]:
            return {"next_action": "blocked_worker_frontier_not_advanced"}
        return {}
    if record["phase"] == "cancelled":
        return {}
    if record["phase"] != "armed":
        return {"next_action": "blocked_invalid_worker_grant"}
    if (record["controller_message_id"] != state["controller_message_id"]
            or record["worker_message_id"] != state["worker_message_id"]
            or state.get("controller_phase") != "ready"
            or state.get("worker_advanced") is not False):
        return {"next_action": "blocked_stale_worker_grant"}
    return {"next_action": "continue_worker", "worker_continuation_phase": "armed",
            "worker_task_sha256": record["task_sha256"]}


def _frontier(*, state: dict, registry, client):
    """Verify Controller's no_commands against the exact latest Worker bytes."""
    from tools.dw_codexia_controller_command_v1 import _latest
    from tools.dw_codexia_controller_continue_v1 import ControllerTurnStore
    from codexia_manual_agent.workflow_runtime.chat_command_plan_v1 import (
        ChatCommandSource,
    )
    from codexia_manual_agent.workflow_runtime.controller_command_v1 import (
        ControllerCommandDecision,
    )

    binding = registry.require_ready(state["project"])
    if (state["next_action"] not in {"idle", "continue_worker"}
            or state["work_id"] is not None
            or state["worker_advanced"] is not False
            or state["controller_phase"] != "ready"):
        raise ValueError("Worker continuation requires idle no_commands frontier")
    previous = ControllerTurnStore(registry).latest(state["project"])
    if previous is None or previous["phase"] != "ready":
        raise ValueError("no completed Controller continuation to authorize")
    worker = _latest(client, binding.worker_conversation)
    controller = _latest(client, binding.controller_conversation)
    if (worker.message_id != state["worker_message_id"]
            or worker.message_id != previous["worker_message_id"]
            or controller.message_id != state["controller_message_id"]
            or controller.message_id != previous["controller_message_id"]):
        raise ValueError("Worker/Controller frontier changed; no send")
    source = ChatCommandSource(
        project=binding.project, repo=binding.repo, workspace=binding.workspace,
        conversation_id=binding.worker_conversation,
        source_message_id=worker.message_id,
        source_text=worker.text, source_sha256=_sha(worker.text),
    )
    decision = ControllerCommandDecision.from_messages(
        source=source, controller_conversation_id=binding.controller_conversation,
        controller_message_id=controller.message_id,
        controller_text=controller.text,
    )
    if decision.plan.decision != "no_commands":
        raise ValueError("Controller did not attest no_commands")
    return binding, worker, controller


def arm_worker(*, state: dict, registry, client, task: str,
               expected_worker: str, expected_controller: str, confirm=input) -> dict:
    """Human-supplied explicit one-turn task; no model call or shell effect."""
    if state["next_action"] != "idle":
        raise ValueError("only an idle project can receive a new Worker task")
    task = _goal(task)
    binding, worker, controller = _frontier(state=state, registry=registry, client=client)
    if (worker.message_id != expected_worker
            or controller.message_id != expected_controller):
        raise ValueError("expected message frontier differs; no grant")
    brief_loaded = _project_brief(binding) is not None
    digest = _sha(json.dumps(
        [binding.project, worker.message_id, controller.message_id, task],
        ensure_ascii=False, separators=(",", ":"),
    ))
    print(f"PROJECT_GOAL_CONTEXT={'LOADED' if brief_loaded else 'ABSENT'}",\n          file=sys.stderr)\n    print(f"PROJECT={binding.project}\nWORKER={worker.message_id}\n"
          f"CONTROLLER={controller.message_id}\nTASK={task}\n"
          f"WORKER_CONTINUATION_GRANT_SHA256={digest}\n"
          "One Worker cognition turn only; no local process or shell authority.",
          file=sys.stderr)
    if confirm(f"Type ARM {digest[:12]}: ").strip() != f"ARM {digest[:12]}":
        raise ValueError("human did not authorize Worker continuation")
    WorkerContinuationStore(registry).arm(
        project=binding.project, controller_message_id=controller.message_id,
        controller_sha256=_sha(controller.text),
        worker_message_id=worker.message_id,
        worker_sha256=_sha(worker.text), task=task,
    )
    return {"status": "WORKER_CONTINUATION_ARMED_NO_SEND", "project": binding.project,
            "controller_message_id": controller.message_id,
            "worker_message_id": worker.message_id, "task_sha256": _sha(task)}


def _project_brief(binding) -> str | None:
    """Read one opt-in project goal from the bound workspace, never from chat.

    There is no separate project-memory database or automatic network fetch.
    The file is guidance, not a source of execution or approval authority.
    """
    root = Path(binding.workspace)
    folder = root / ".codexia"
    path = folder / "PROJECT_BRIEF.md"
    if not path.exists() and not path.is_symlink():
        return None
    if folder.is_symlink() or path.is_symlink() or not path.is_file():
        raise ValueError("project brief must be a regular local file")
    if path.stat().st_size > 8192:
        raise ValueError("project brief too large (max 8192 bytes)")
    try:
        value = path.read_text(encoding="utf-8").strip()
    except UnicodeError as exc:
        raise ValueError("project brief must be UTF-8 text") from exc
    if not 80 <= len(value) <= 8192 or "\x00" in value:
        raise ValueError("project brief must contain 80..8192 readable characters")
    return value


def _prompt(*, binding, record) -> str:
    """Keep human task, product goal and authority in their separate roles."""
    brief = _project_brief(binding)
    goal_context = ""
    if brief is not None:
        goal_context = (
            "\n\n[Project goal — local reference, not execution authority]\n"
            + brief
            + "\n[End project goal]\n"
            "Use this goal to choose work that moves the usable product forward. "
            "Prefer implementing a concrete user-visible capability over repeating "
            "an already-sufficient audit. Compare feasible approaches by value, "
            "reuse, effort, evidence and risk. Pick one justified next step rather "
            "than asking the human to choose without a genuine decision. "
            "Treat historical plans as revisable, not mandatory steps. "
            "Do not imply this reference authorizes any effect."
        )
    return (
        "Continue the agreed task in this existing project conversation:\n"
        + record["task"]
        + goal_context
        + "\n\nUse the existing context. Do not repeat completed checks or "
        "restate known project facts. Reply in the language of the human "
        "discussion, not the language of Codexia automation messages. "
        "Give the result and next useful decision briefly; expand only "
        "when the task genuinely needs detail.\n"
        "This is a reasoning turn, not permission to execute commands, "
        "edit files, commit, or post externally. Propose exact local commands "
        "only if needed; they require separate human approval."
    )


def continue_worker(*, state: dict, registry, client,
                    model: str = "gpt-6", effort: str = "high") -> dict:
    """Admit send BEFORE CWA call; ambiguities require manual reconciliation."""
    if state.get("next_action") != "continue_worker":
        raise ValueError("no authorized Worker continuation at this frontier")
    binding, worker, controller = _frontier(state=state, registry=registry, client=client)
    store = WorkerContinuationStore(registry)
    record = store.latest(binding.project)
    if (record is None or record["phase"] != "armed"
            or record["worker_message_id"] != worker.message_id
            or record["controller_message_id"] != controller.message_id
            or record["worker_sha256"] != _sha(worker.text)
            or record["controller_sha256"] != _sha(controller.text)
            or record["task_sha256"] != _sha(record["task"])):
        raise ValueError("Worker continuation grant is missing or stale")
    prompt = _prompt(binding=binding, record=record)
    # Critical durable no-replay boundary before an external model send.
    store.transition(project=binding.project,
                     controller_message_id=controller.message_id,
                     from_phase="armed", to_phase="send_admitted")
    response = client.send(prompt, conversation=binding.worker_conversation,
                           model=model, reasoning_effort=effort, temporary=False)
    identity = getattr(response, "conversation", None)
    cid = getattr(identity, "conversation_id", None)
    mid = getattr(identity, "message_id", None)
    if cid != binding.worker_conversation or not mid or mid == worker.message_id:
        raise ValueError("ambiguous Worker send; DO NOT RETRY")
    store.transition(project=binding.project,
                     controller_message_id=controller.message_id,
                     from_phase="send_admitted", to_phase="worker_known",
                     result_id=mid)
    from tools.dw_codexia_controller_command_v1 import _latest
    observed = _latest(client, binding.worker_conversation)
    if observed.message_id != mid or observed.text != response.text:
        raise ValueError("Worker canonical readback differs; DO NOT RETRY")
    store.transition(project=binding.project,
                     controller_message_id=controller.message_id,
                     from_phase="worker_known", to_phase="verified",
                     result_id=mid)
    return {"status": "WORKER_CONTINUED_READBACK_VERIFIED",
            "project": binding.project, "worker_conversation": cid,
            "worker_message_id": mid, "task_sha256": record["task_sha256"],
            "effects": "MODEL_SEND_ONLY_NO_LOCAL_EXECUTION"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("status", "arm"))
    parser.add_argument("--project", required=True)
    parser.add_argument("--auth-file", required=True)
    parser.add_argument("--registry", default=".codexia/simple_work_v0.sqlite3")
    parser.add_argument("--store", default=".codexia/controller-shell-work.sqlite3")
    parser.add_argument("--task")
    parser.add_argument("--worker-message-id")
    parser.add_argument("--controller-message-id")
    args = parser.parse_args(argv)
    try:
        from tools.dw_codexia_controller_command_v1 import _client
        from tools.dw_codexia_project_bootstrap_v1 import DW5ProjectRegistry
        from tools.dw6_governed_work_loop_v1 import probe

        registry = DW5ProjectRegistry(args.registry)
        client = _client(args.auth_file)
        state = probe(project=args.project, registry=registry,
                      client=client, store_path=args.store)
        if args.action == "arm":
            if not (args.task and args.worker_message_id and args.controller_message_id):
                raise ValueError("arm requires task and exact worker/controller message IDs")
            result = arm_worker(
                state=state, registry=registry, client=client,
                task=args.task, expected_worker=args.worker_message_id,
                expected_controller=args.controller_message_id,
            )
        else:
            result = state
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (KeyboardInterrupt, EOFError):
        print("DW6.1 INTERRUPTED: inspect durable state, no blind retry", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"DW6.1 STOPPED ({type(exc).__name__}): {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
