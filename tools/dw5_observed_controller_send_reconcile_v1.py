"""Reconcile one completed Controller send after an ambiguous streaming timeout.

This is NOT a presend recovery. A sent request and completed Controller response
must both exist in the exact canonical chat branch. No send, shell or Gen2 Work
operation occurs. Reconciliation is human-attested and audit-anchored.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from typing import Any

from codexia_manual_agent.workflow_runtime.chat_command_plan_v1 import (
    ChatCommandSource,
    _sha,
)
from codexia_manual_agent.workflow_runtime.controller_command_v1 import (
    ControllerCommandDecision,
)
from tools.dw_codexia_controller_continue_v1 import _prompt
from tools.dw_codexia_presend_recovery_v1 import _database
from tools.dw_codexia_controller_command_v1 import _latest

TABLE = "dw5_controller_observed_send_reconciliation_v1"
DEFAULT_REGISTRY = ".codexia/simple_work_v0.sqlite3"
DEFAULT_REASON = (
    "New worker assistant reply after a verified prior handoff; interpret only "
    "current requested commands. No execution authority."
)


def _snapshot(db: sqlite3.Connection, project: str, predecessor: str) -> dict:
    binding = db.execute(
        "SELECT * FROM dw5_project_binding_v1 WHERE project=?", (project,)
    ).fetchone()
    if binding is None or binding["phase"] != "ready":
        raise ValueError("project binding is not ready")

    turns = db.execute(
        "SELECT * FROM dw5_controller_turn_v1 WHERE project=? "
        "ORDER BY rowid DESC LIMIT 2", (project,)
    ).fetchall()
    if len(turns) != 2:
        raise ValueError("expected active admitted send and prior ready turn")
    fence, prior = (dict(row) for row in turns)
    if (
        fence["phase"] != "send_admitted"
        or fence["controller_message_id"] is not None
        or fence["predecessor_message_id"] != predecessor
        or prior["phase"] != "ready"
        or prior["controller_message_id"] != predecessor
        or prior["worker_message_id"] == fence["worker_message_id"]
    ):
        raise ValueError("exact ambiguous Controller send fence differs")
    if db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)
    ).fetchone() is not None:
        found = db.execute(
            f"SELECT 1 FROM {TABLE} WHERE project=? AND predecessor_message_id=?",
            (project, predecessor),
        ).fetchone()
        if found:
            raise ValueError("observed send already reconciled")

    archives = []
    for table in (
        "dw5_controller_presend_recovery_v1",
        "dw5_controller_presend_recovery_v2",
    ):
        row = db.execute(
            f"SELECT * FROM {table} WHERE project=? AND predecessor_message_id=?",
            (project, predecessor),
        ).fetchone()
        if row is None:
            raise ValueError("expected prior presend recovery audit missing")
        item = dict(row)
        if (
            item["worker_message_id"] != fence["worker_message_id"]
            or item["worker_sha256"] != fence["worker_sha256"]
            or item["original_phase"] != "send_admitted"
            or item["controller_message_id"] is not None
        ):
            raise ValueError("prior presend audit identity differs")
        archives.append(item)
    if (
        archives[1]["previous_reconciliation_digest"]
        != archives[0]["reconciliation_digest"]
    ):
        raise ValueError("prior audit chain differs")

    return {
        "binding": dict(binding),
        "fence": fence,
        "prior": prior,
        "v1_digest": archives[0]["reconciliation_digest"],
        "v2_digest": archives[1]["second_reconciliation_digest"],
    }


def inspect_observed(
    *, registry: str | Path, project: str, predecessor: str,
    worker_message_id: str, request_message_id: str,
    response_message_id: str, response_sha256_prefix: str, client: Any,
) -> dict:
    """Only read-only SQLite and canonical GET operations. Never send."""
    if len(response_sha256_prefix) < 16 or len(response_sha256_prefix) > 64:
        raise ValueError("response SHA prefix must contain 16..64 hex characters")
    if any(x not in "0123456789abcdef" for x in response_sha256_prefix):
        raise ValueError("response SHA prefix must be lowercase hexadecimal")
    with closing(_database(registry, readonly=True)) as db:
        snapshot = _snapshot(db, project, predecessor)
    binding = snapshot["binding"]
    fence = snapshot["fence"]
    if fence["worker_message_id"] != worker_message_id:
        raise ValueError("worker fence identity mismatch")
    if binding["controller_conversation"] == binding["worker_conversation"]:
        raise ValueError("controller and worker conversations must differ")

    worker = _latest(client, binding["worker_conversation"])
    if (
        worker.message_id != worker_message_id
        or _sha(worker.text) != fence["worker_sha256"]
    ):
        raise ValueError("canonical Worker message differs from admitted source")

    source = ChatCommandSource(
        project=project,
        repo=binding["repo"],
        workspace=binding["workspace"],
        conversation_id=binding["worker_conversation"],
        source_message_id=worker.message_id,
        source_text=worker.text,
        source_sha256=fence["worker_sha256"],
    )
    messages = client.get_messages(
        binding["controller_conversation"], limit=3, include_empty=True
    )
    if len(messages) != 3:
        raise ValueError("canonical Controller tail is not three messages")
    prior, request, response = messages
    if (
        prior.role != "assistant"
        or prior.message_id != predecessor
        or request.role != "user"
        or request.message_id != request_message_id
        or response.role != "assistant"
        or response.message_id != response_message_id
        or response.finish_reason != "stop"
    ):
        raise ValueError("Controller request/response canonical branch differs")
    expected_prompt = _prompt(source, DEFAULT_REASON)
    if request.text != expected_prompt:
        raise ValueError("canonical request does not match exact intended prompt")
    response_sha256 = _sha(response.text)
    if not response_sha256.startswith(response_sha256_prefix):
        raise ValueError("canonical response hash differs from prior observation")

    decision = ControllerCommandDecision.from_messages(
        source=source,
        controller_conversation_id=binding["controller_conversation"],
        controller_message_id=response.message_id,
        controller_text=response.text,
    )

    facts = [
        "dw5-observed-send-reconciliation-v1", project, predecessor,
        worker_message_id, source.source_sha256,
        request.message_id, _sha(request.text),
        response.message_id, response_sha256,
        decision.plan.decision, decision.plan.digest,
        snapshot["v1_digest"], snapshot["v2_digest"],
    ]
    digest = _sha(json.dumps(facts, ensure_ascii=False, separators=(",", ":")))
    return {
        "status": "COMPLETED_CONTROLLER_SEND_OBSERVED_READ_ONLY",
        "project": project,
        "predecessor_message_id": predecessor,
        "worker_message_id": worker_message_id,
        "worker_sha256": source.source_sha256,
        "request_message_id": request.message_id,
        "request_sha256": _sha(request.text),
        "response_message_id": response.message_id,
        "response_sha256": response_sha256,
        "response_finish_reason": response.finish_reason,
        "decision": decision.plan.decision,
        "steps": len(decision.plan.steps),
        "plan_sha256": decision.plan.digest,
        "v1_archive_digest": snapshot["v1_digest"],
        "v2_archive_digest": snapshot["v2_digest"],
        "reconciliation_digest": digest,
        "effects": "NONE",
    }


def reconcile_observed(
    *, registry: str | Path, project: str, predecessor: str,
    worker_message_id: str, request_message_id: str,
    response_message_id: str, response_sha256_prefix: str,
    client: Any, confirm=input,
) -> dict:
    """Explicit human-approved audit+state advance for an already-completed send."""
    kwargs = dict(
        registry=registry, project=project, predecessor=predecessor,
        worker_message_id=worker_message_id, request_message_id=request_message_id,
        response_message_id=response_message_id,
        response_sha256_prefix=response_sha256_prefix, client=client,
    )
    observed = inspect_observed(**kwargs)
    print("OBSERVED_SEND_PREFLIGHT=" + json.dumps(observed, ensure_ascii=False),
          file=sys.stderr)
    print(
        "Attestation: the request and final assistant response already exist in "
        "the canonical Controller branch. This only reconciles durable state; "
        "it DOES NOT submit another prompt. Do not approve if evidence is ambiguous.",
        file=sys.stderr,
    )
    approval = "RECONCILE-OBSERVED " + observed["reconciliation_digest"][:12]
    if confirm(f"Type {approval}: ").strip() != approval:
        raise ValueError("human declined observed-send reconciliation")
    if inspect_observed(**kwargs) != observed:
        raise ValueError("canonical Controller evidence changed before transaction")

    db = _database(registry, readonly=False)
    try:
        db.execute("BEGIN IMMEDIATE")
        snapshot = _snapshot(db, project, predecessor)
        fence = snapshot["fence"]
        if (
            fence["worker_message_id"] != observed["worker_message_id"]
            or fence["worker_sha256"] != observed["worker_sha256"]
            or snapshot["v1_digest"] != observed["v1_archive_digest"]
            or snapshot["v2_digest"] != observed["v2_archive_digest"]
        ):
            raise ValueError("durable send frontier changed during reconciliation")
        db.execute(f"""
            CREATE TABLE IF NOT EXISTS {TABLE} (
                project TEXT NOT NULL,
                predecessor_message_id TEXT NOT NULL,
                worker_message_id TEXT NOT NULL,
                worker_sha256 TEXT NOT NULL,
                request_message_id TEXT NOT NULL,
                request_sha256 TEXT NOT NULL,
                response_message_id TEXT NOT NULL,
                response_sha256 TEXT NOT NULL,
                decision TEXT NOT NULL,
                plan_sha256 TEXT NOT NULL,
                v1_archive_digest TEXT NOT NULL,
                v2_archive_digest TEXT NOT NULL,
                reconciliation_digest TEXT NOT NULL,
                reconciled_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (project, predecessor_message_id)
            )
        """)
        values = (
            project, predecessor, observed["worker_message_id"],
            observed["worker_sha256"], observed["request_message_id"],
            observed["request_sha256"], observed["response_message_id"],
            observed["response_sha256"], observed["decision"],
            observed["plan_sha256"], observed["v1_archive_digest"],
            observed["v2_archive_digest"], observed["reconciliation_digest"],
        )
        db.execute(f"""
            INSERT INTO {TABLE} (
                project, predecessor_message_id, worker_message_id,
                worker_sha256, request_message_id, request_sha256,
                response_message_id, response_sha256, decision, plan_sha256,
                v1_archive_digest, v2_archive_digest, reconciliation_digest
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, values)
        recorded = db.execute("""
            UPDATE dw5_controller_turn_v1
            SET controller_message_id=?, phase='controller_known'
            WHERE project=? AND predecessor_message_id=?
              AND worker_message_id=? AND worker_sha256=?
              AND controller_message_id IS NULL AND phase='send_admitted'
        """, (
            observed["response_message_id"], project, predecessor,
            observed["worker_message_id"], observed["worker_sha256"],
        )).rowcount
        if recorded != 1:
            raise ValueError("could not record exactly one observed Controller send")
        ready = db.execute("""
            UPDATE dw5_controller_turn_v1 SET phase='ready'
            WHERE project=? AND predecessor_message_id=?
              AND controller_message_id=? AND phase='controller_known'
        """, (project, predecessor, observed["response_message_id"])).rowcount
        if ready != 1:
            raise ValueError("could not mark observed Controller answer ready")
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()

    return {
        "status": "OBSERVED_CONTROLLER_SEND_RECONCILED_NO_SEND",
        "project": project,
        "controller_message_id": observed["response_message_id"],
        "decision": observed["decision"],
        "steps": observed["steps"],
        "plan_sha256": observed["plan_sha256"],
        "reconciliation_digest": observed["reconciliation_digest"],
        "effects": "SQLITE_AUDIT_AND_READY_ONLY",
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("inspect", "reconcile"))
    parser.add_argument("--project", required=True)
    parser.add_argument("--registry", default=DEFAULT_REGISTRY)
    parser.add_argument("--auth-file", required=True)
    parser.add_argument("--predecessor", required=True)
    parser.add_argument("--worker-message-id", required=True)
    parser.add_argument("--request-message-id", required=True)
    parser.add_argument("--response-message-id", required=True)
    parser.add_argument("--response-sha256-prefix", required=True)
    args = parser.parse_args(argv)
    try:
        from chatgpt_web_adapter.browserless_http import BrowserlessHTTPClient

        client = BrowserlessHTTPClient(auth_file=args.auth_file, timeout=180)
        kwargs = dict(
            registry=args.registry, project=args.project,
            predecessor=args.predecessor,
            worker_message_id=args.worker_message_id,
            request_message_id=args.request_message_id,
            response_message_id=args.response_message_id,
            response_sha256_prefix=args.response_sha256_prefix,
            client=client,
        )
        result = (
            inspect_observed(**kwargs) if args.action == "inspect"
            else reconcile_observed(**kwargs)
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as error:
        print(
            f"OBSERVED_SEND_STOPPED ({type(error).__name__}): {error}",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
