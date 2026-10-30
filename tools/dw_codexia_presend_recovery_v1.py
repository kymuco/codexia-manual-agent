"""DW5 narrow operator-led reconciliation for verified Sentinel *pre-send* rejection.

No chat sends. No modification of a Gen2 Work. No retry and no automatic
reconciliation. The original active send fence is atomically copied to an
append-only audit table before its removal from the active turn table.

This is NOT a recovery path for timeouts, HTTP errors after a possible message
submission, ambiguous readbacks, or arbitrary CWA failures.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path
from contextlib import closing
from typing import Callable, Any

DEFAULT_REGISTRY = ".codexia/simple_work_v0.sqlite3"
FAILURE_KIND = "sentinel_prepare_rejected"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _database(path: str | Path, *, readonly: bool) -> sqlite3.Connection:
    source = Path(path).resolve(strict=True)
    if not source.is_file():
        raise ValueError("registry database is not a regular file")
    if readonly:
        db = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=10)
    else:
        db = sqlite3.connect(source, timeout=10)
    db.row_factory = sqlite3.Row
    return db


def _snapshot(db: sqlite3.Connection, project: str) -> tuple[dict, dict, dict]:
    binding = db.execute(
        "SELECT * FROM dw5_project_binding_v1 WHERE project=?", (project,)
    ).fetchone()
    if binding is None or binding["phase"] != "ready":
        raise ValueError("persistent project binding is not ready")
    turns = db.execute(
        "SELECT * FROM dw5_controller_turn_v1 WHERE project=? "
        "ORDER BY rowid DESC LIMIT 2", (project,)
    ).fetchall()
    if len(turns) != 2:
        raise ValueError("recovery requires previous verified Controller turn")
    failed, predecessor = (dict(row) for row in turns)
    if (failed["phase"] != "send_admitted"
            or failed["controller_message_id"] is not None):
        raise ValueError("latest turn is not an unresolved send_admitted fence")
    if (predecessor["phase"] != "ready"
            or predecessor["controller_message_id"] != failed["predecessor_message_id"]):
        raise ValueError("prior verified turn does not match send predecessor")
    if (failed["worker_message_id"] == predecessor["worker_message_id"]):
        raise ValueError("the same Worker message was already interpreted")
    if (db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='dw5_controller_presend_recovery_v1'"
    ).fetchone() is not None):
        prior = db.execute(
            "SELECT 1 FROM dw5_controller_presend_recovery_v1 "
            "WHERE project=? AND predecessor_message_id=?",
            (project, failed["predecessor_message_id"]),
        ).fetchone()
        if prior is not None:
            raise ValueError("this exact predecessor has already been reconciled once")
    return dict(binding), failed, predecessor


def inspect_fence(*, registry: str | Path, project: str,
                  expected_worker: str, expected_controller: str,
                  expected_controller_sha256: str,
                  latest: Callable[[str], Any]) -> dict:
    """No writes, including no schema migration; GET reads only via latest()."""
    if (not expected_worker or not expected_controller
            or len(expected_controller_sha256) != 64):
        raise ValueError("exact Worker/Controller IDs and Controller SHA-256 required")
    with closing(_database(registry, readonly=True)) as db:
        binding, fence, _ = _snapshot(db, project)
    if (fence["worker_message_id"] != expected_worker
            or fence["predecessor_message_id"] != expected_controller):
        raise ValueError("requested recovery identity differs from the active fence")
    worker = latest(binding["worker_conversation"])
    controller = latest(binding["controller_conversation"])
    if (worker.message_id != expected_worker
            or _sha(worker.text) != fence["worker_sha256"]):
        raise ValueError("Worker latest ID or text differs; STOP")
    if (controller.message_id != expected_controller
            or _sha(controller.text) != expected_controller_sha256):
        raise ValueError("Controller latest ID or text differs; STOP")
    grant = _sha(json.dumps([
        project, fence["predecessor_message_id"], expected_worker,
        fence["worker_sha256"], expected_controller_sha256, FAILURE_KIND,
    ], ensure_ascii=False, separators=(",", ":")))
    return {
        "status": "PRE_SEND_FENCE_CANDIDATE_READ_ONLY",
        "project": project, "worker_conversation": binding["worker_conversation"],
        "controller_conversation": binding["controller_conversation"],
        "worker_message_id": expected_worker,
        "worker_sha256": fence["worker_sha256"],
        "controller_message_id": expected_controller,
        "controller_sha256": expected_controller_sha256,
        "fence_phase": fence["phase"], "failure_kind": FAILURE_KIND,
        "reconciliation_digest": grant, "effects": "NONE",
    }


def reconcile_fence(*, registry: str | Path, project: str,
                    expected_worker: str, expected_controller: str,
                    expected_controller_sha256: str, error_kind: str,
                    latest: Callable[[str], Any], confirm: Callable[[str], str] = input) -> dict:
    """One human-confirmed atomic archive of the exact unresolved *pre-send* fence."""
    if error_kind != FAILURE_KIND:
        raise ValueError("only exact Sentinel prepare rejection is eligible")
    inspected = inspect_fence(
        registry=registry, project=project, expected_worker=expected_worker,
        expected_controller=expected_controller,
        expected_controller_sha256=expected_controller_sha256, latest=latest,
    )
    print("RECONCILIATION_PREFLIGHT=" + json.dumps(inspected, ensure_ascii=False),
          file=sys.stderr)
    print("Human attestation: CWA raised the exact error "
          "'browserless Sentinel prepare rejected' BEFORE any chat message submission. "
          "A timeout or uncertain send is NOT eligible. The old fence will be "
          "archived atomically; this operation will NOT send a model request.",
          file=sys.stderr)
    word = "RECONCILE " + inspected["reconciliation_digest"][:12]
    if confirm(f"Type {word}: ").strip() != word:
        raise ValueError("human did not authorize this exact pre-send reconciliation")

    # Recheck canonical latest after confirmation, before opening the write transaction.
    again = inspect_fence(
        registry=registry, project=project, expected_worker=expected_worker,
        expected_controller=expected_controller,
        expected_controller_sha256=expected_controller_sha256, latest=latest,
    )
    if again != inspected:
        raise ValueError("recovery preflight changed; no database mutation")

    db = _database(registry, readonly=False)
    try:
        db.execute("BEGIN IMMEDIATE")
        _, original, _ = _snapshot(db, project)
        if (original["predecessor_message_id"] != expected_controller
                or original["worker_message_id"] != expected_worker
                or original["worker_sha256"] != inspected["worker_sha256"]):
            raise ValueError("active fence changed; no database mutation")
        db.execute("""
            CREATE TABLE IF NOT EXISTS dw5_controller_presend_recovery_v1 (
                project TEXT NOT NULL,
                predecessor_message_id TEXT NOT NULL,
                worker_message_id TEXT NOT NULL,
                worker_sha256 TEXT NOT NULL,
                controller_message_id TEXT,
                original_phase TEXT NOT NULL,
                failure_kind TEXT NOT NULL,
                controller_sha256 TEXT NOT NULL,
                reconciliation_digest TEXT NOT NULL,
                reconciled_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(project, predecessor_message_id)
            )
        """)
        inserted = db.execute("""
            INSERT INTO dw5_controller_presend_recovery_v1 (
                project, predecessor_message_id, worker_message_id, worker_sha256,
                controller_message_id, original_phase, failure_kind,
                controller_sha256, reconciliation_digest
            ) SELECT project, predecessor_message_id, worker_message_id,
                worker_sha256, controller_message_id, phase, ?, ?, ?
              FROM dw5_controller_turn_v1
              WHERE project=? AND predecessor_message_id=?
                AND worker_message_id=? AND worker_sha256=?
                AND controller_message_id IS NULL AND phase='send_admitted'
        """, (FAILURE_KIND, expected_controller_sha256,
              inspected["reconciliation_digest"], project, expected_controller,
              expected_worker, inspected["worker_sha256"])).rowcount
        if inserted != 1:
            raise ValueError("failed to archive precisely one original fence")
        removed = db.execute("""
            DELETE FROM dw5_controller_turn_v1
            WHERE project=? AND predecessor_message_id=?
              AND worker_message_id=? AND worker_sha256=?
              AND controller_message_id IS NULL AND phase='send_admitted'
        """, (project, expected_controller, expected_worker,
              inspected["worker_sha256"])).rowcount
        if removed != 1:
            raise ValueError("failed to retire precisely one archived fence")
        new_latest = db.execute(
            "SELECT * FROM dw5_controller_turn_v1 WHERE project=? "
            "ORDER BY rowid DESC LIMIT 1", (project,)
        ).fetchone()
        if (new_latest is None or new_latest["phase"] != "ready"
                or new_latest["controller_message_id"] != expected_controller):
            raise ValueError("previous verified frontier not restored")
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()
    return {
        "status": "PRESEND_FENCE_ARCHIVED_NO_SEND",
        "project": project,
        "reconciliation_digest": inspected["reconciliation_digest"],
        "previous_controller_message_id": expected_controller,
        "worker_message_id": expected_worker,
        "next_action_expected": "interpret",
        "effects": "SQLITE_ARCHIVE_ONLY_NO_MODEL_SEND",
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("inspect", "reconcile-presend"))
    parser.add_argument("--project", required=True)
    parser.add_argument("--auth-file", required=True)
    parser.add_argument("--registry", default=DEFAULT_REGISTRY)
    parser.add_argument("--worker-message-id", required=True)
    parser.add_argument("--controller-message-id", required=True)
    parser.add_argument("--controller-sha256", required=True)
    parser.add_argument("--error-kind", choices=(FAILURE_KIND,))
    args = parser.parse_args(argv)
    try:
        from tools.dw_codexia_controller_command_v1 import _client, _latest
        client = _client(args.auth_file)
        def latest(conversation_id: str):
            return _latest(client, conversation_id)
        common = dict(
            registry=args.registry, project=args.project,
            expected_worker=args.worker_message_id,
            expected_controller=args.controller_message_id,
            expected_controller_sha256=args.controller_sha256,
            latest=latest,
        )
        if args.action == "inspect":
            result = inspect_fence(**common)
        else:
            if args.error_kind != FAILURE_KIND:
                raise ValueError("explicit --error-kind sentinel_prepare_rejected required")
            result = reconcile_fence(**common, error_kind=args.error_kind)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (KeyboardInterrupt, EOFError):
        print("DW5 RECOVERY INTERRUPTED: inspect durable state, no send", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"DW5 PRESEND RECOVERY STOPPED ({type(exc).__name__}): {exc}",
              file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
