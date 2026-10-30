"""DW5 bounded recovery for a *second* verified Sentinel pre-send failure.

Recovery v1 remains unchanged and explicitly refuses a second archive.
This operator requires the first v1 audit record and one new active
send_admitted fence for the same Controller predecessor. It archives that
second failure in a separate table. Third and ambiguous failures stay blocked.
It never sends, executes or posts anything.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from typing import Any, Callable

from tools import dw_codexia_presend_recovery_v1 as v1

FAILURE_KIND = v1.FAILURE_KIND
DEFAULT_REGISTRY = v1.DEFAULT_REGISTRY
V1_TABLE = "dw5_controller_presend_recovery_v1"
V2_TABLE = "dw5_controller_presend_recovery_v2"


def _snapshot(db: sqlite3.Connection, project: str) -> tuple[dict, dict, dict, dict]:
    binding = db.execute(
        "SELECT * FROM dw5_project_binding_v1 WHERE project=?", (project,)
    ).fetchone()
    if binding is None or binding["phase"] != "ready":
        raise ValueError("project binding is not ready")
    rows = db.execute(
        "SELECT * FROM dw5_controller_turn_v1 WHERE project=? "
        "ORDER BY rowid DESC LIMIT 2", (project,)
    ).fetchall()
    if len(rows) != 2:
        raise ValueError("second recovery requires one prior ready Controller turn")
    failed, ready = (dict(row) for row in rows)
    if failed["phase"] != "send_admitted" or failed["controller_message_id"] is not None:
        raise ValueError("latest turn is not an unresolved pre-send fence")
    if (
        ready["phase"] != "ready"
        or ready["controller_message_id"] != failed["predecessor_message_id"]
        or ready["worker_message_id"] == failed["worker_message_id"]
    ):
        raise ValueError("previous verified Controller identity differs")
    if db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (V1_TABLE,)
    ).fetchone() is None:
        raise ValueError("first recovery archive is missing")
    first = db.execute(
        f"SELECT * FROM {V1_TABLE} WHERE project=? AND predecessor_message_id=?",
        (project, failed["predecessor_message_id"]),
    ).fetchone()
    if first is None:
        raise ValueError("first recovery for this predecessor is missing")
    first = dict(first)
    if (
        first["worker_message_id"] != failed["worker_message_id"]
        or first["worker_sha256"] != failed["worker_sha256"]
        or first["controller_message_id"] is not None
        or first["original_phase"] != "send_admitted"
        or first["failure_kind"] != FAILURE_KIND
    ):
        raise ValueError("first recovery archive differs from current source")
    expected_first = v1._sha(json.dumps(
        [
            project, failed["predecessor_message_id"], failed["worker_message_id"],
            failed["worker_sha256"], first["controller_sha256"], FAILURE_KIND,
        ],
        ensure_ascii=False, separators=(",", ":"),
    ))
    if first["reconciliation_digest"] != expected_first:
        raise ValueError("first recovery audit digest differs")
    if db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (V2_TABLE,)
    ).fetchone() is not None:
        if db.execute(
            f"SELECT 1 FROM {V2_TABLE} WHERE project=? AND predecessor_message_id=?",
            (project, failed["predecessor_message_id"]),
        ).fetchone() is not None:
            raise ValueError("second recovery already recorded; further retries blocked")
    return dict(binding), failed, ready, first


def inspect_fence(
    *, registry: str | Path, project: str, expected_worker: str,
    expected_controller: str, expected_controller_sha256: str,
    latest: Callable[[str], Any],
) -> dict:
    """Canonical readback and immutable audit verification; never writes."""
    if not expected_worker or not expected_controller or len(expected_controller_sha256) != 64:
        raise ValueError("exact message identities and Controller SHA-256 required")
    with closing(v1._database(registry, readonly=True)) as db:
        binding, failed, _, first = _snapshot(db, project)
    if (
        failed["worker_message_id"] != expected_worker
        or failed["predecessor_message_id"] != expected_controller
        or first["controller_sha256"] != expected_controller_sha256
    ):
        raise ValueError("second recovery identity differs")
    worker = latest(binding["worker_conversation"])
    controller = latest(binding["controller_conversation"])
    if (
        worker.message_id != expected_worker
        or v1._sha(worker.text) != failed["worker_sha256"]
    ):
        raise ValueError("Worker canonical readback differs; STOP")
    if (
        controller.message_id != expected_controller
        or v1._sha(controller.text) != expected_controller_sha256
    ):
        raise ValueError("Controller canonical readback differs; STOP")
    digest = v1._sha(json.dumps(
        [
            "dw5-presend-recovery-v2", project, expected_controller,
            expected_worker, failed["worker_sha256"],
            expected_controller_sha256, first["reconciliation_digest"], FAILURE_KIND,
        ],
        ensure_ascii=False, separators=(",", ":"),
    ))
    return {
        "status": "SECOND_PRESEND_CANDIDATE_READ_ONLY",
        "project": project,
        "worker_conversation": binding["worker_conversation"],
        "controller_conversation": binding["controller_conversation"],
        "worker_message_id": expected_worker,
        "worker_sha256": failed["worker_sha256"],
        "controller_message_id": expected_controller,
        "controller_sha256": expected_controller_sha256,
        "first_recovery_digest": first["reconciliation_digest"],
        "second_recovery_digest": digest,
        "fence_phase": "send_admitted",
        "failure_kind": FAILURE_KIND,
        "recovery_limit": 2,
        "effects": "NONE",
    }


def reconcile_fence(
    *, registry: str | Path, project: str, expected_worker: str,
    expected_controller: str, expected_controller_sha256: str,
    error_kind: str, latest: Callable[[str], Any],
    confirm: Callable[[str], str] = input,
) -> dict:
    """Human-attested, single-use second archive; no model request."""
    if error_kind != FAILURE_KIND:
        raise ValueError("only Sentinel prepare rejection before send is eligible")
    inspected = inspect_fence(
        registry=registry, project=project, expected_worker=expected_worker,
        expected_controller=expected_controller,
        expected_controller_sha256=expected_controller_sha256, latest=latest,
    )
    print("SECOND_RECOVERY_PREFLIGHT=" + json.dumps(inspected, ensure_ascii=False),
          file=sys.stderr)
    print(
        "Human attestation: the SECOND invocation failed at exact "
        "'browserless Sentinel prepare rejected' BEFORE conversation submission. "
        "Timeout/unknown outcome is NOT eligible. This permanently records "
        "second recovery; any third same-predecessor failure stays blocked. "
        "This operation sends NO model request.", file=sys.stderr,
    )
    word = "RECONCILE-SECOND " + inspected["second_recovery_digest"][:12]
    if confirm(f"Type {word}: ").strip() != word:
        raise ValueError("human denied second recovery")

    again = inspect_fence(
        registry=registry, project=project, expected_worker=expected_worker,
        expected_controller=expected_controller,
        expected_controller_sha256=expected_controller_sha256, latest=latest,
    )
    if again != inspected:
        raise ValueError("second recovery frontier changed; no mutation")

    db = v1._database(registry, readonly=False)
    try:
        db.execute("BEGIN IMMEDIATE")
        _, failed, _, first = _snapshot(db, project)
        if (
            failed["predecessor_message_id"] != expected_controller
            or failed["worker_message_id"] != expected_worker
            or failed["worker_sha256"] != inspected["worker_sha256"]
            or first["reconciliation_digest"] != inspected["first_recovery_digest"]
            or first["controller_sha256"] != expected_controller_sha256
        ):
            raise ValueError("second recovery fence changed under transaction")
        db.execute(f"""
            CREATE TABLE IF NOT EXISTS {V2_TABLE} (
                project TEXT NOT NULL,
                predecessor_message_id TEXT NOT NULL,
                worker_message_id TEXT NOT NULL,
                worker_sha256 TEXT NOT NULL,
                controller_message_id TEXT,
                original_phase TEXT NOT NULL,
                failure_kind TEXT NOT NULL,
                controller_sha256 TEXT NOT NULL,
                previous_reconciliation_digest TEXT NOT NULL,
                second_reconciliation_digest TEXT NOT NULL,
                reconciled_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(project, predecessor_message_id)
            )
        """)
        inserted = db.execute(f"""
            INSERT INTO {V2_TABLE} (
                project, predecessor_message_id, worker_message_id, worker_sha256,
                controller_message_id, original_phase, failure_kind, controller_sha256,
                previous_reconciliation_digest, second_reconciliation_digest
            ) SELECT project, predecessor_message_id, worker_message_id,
                worker_sha256, controller_message_id, phase, ?, ?, ?, ?
              FROM dw5_controller_turn_v1
              WHERE project=? AND predecessor_message_id=?
                AND worker_message_id=? AND worker_sha256=?
                AND controller_message_id IS NULL AND phase='send_admitted'
        """, (
            FAILURE_KIND, expected_controller_sha256,
            inspected["first_recovery_digest"], inspected["second_recovery_digest"],
            project, expected_controller, expected_worker, inspected["worker_sha256"],
        )).rowcount
        if inserted != 1:
            raise ValueError("did not archive exactly one second pre-send fence")
        removed = db.execute("""
            DELETE FROM dw5_controller_turn_v1
             WHERE project=? AND predecessor_message_id=?
               AND worker_message_id=? AND worker_sha256=?
               AND controller_message_id IS NULL AND phase='send_admitted'
        """, (
            project, expected_controller, expected_worker, inspected["worker_sha256"],
        )).rowcount
        if removed != 1:
            raise ValueError("did not retire exactly one archived fence")
        restored = db.execute(
            "SELECT controller_message_id, phase FROM dw5_controller_turn_v1 "
            "WHERE project=? ORDER BY rowid DESC LIMIT 1", (project,)
        ).fetchone()
        if (
            restored is None or restored["phase"] != "ready"
            or restored["controller_message_id"] != expected_controller
        ):
            raise ValueError("previous verified Controller frontier not restored")
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()
    return {
        "status": "SECOND_PRESEND_FENCE_ARCHIVED_NO_SEND",
        "project": project,
        "second_recovery_digest": inspected["second_recovery_digest"],
        "previous_controller_message_id": expected_controller,
        "worker_message_id": expected_worker,
        "next_action_expected": "interpret",
        "effects": "SQLITE_ARCHIVE_ONLY_NO_MODEL_SEND",
        "third_recovery_blocked": True,
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

        kwargs = {
            "registry": args.registry,
            "project": args.project,
            "expected_worker": args.worker_message_id,
            "expected_controller": args.controller_message_id,
            "expected_controller_sha256": args.controller_sha256,
            "latest": latest,
        }
        if args.action == "inspect":
            result = inspect_fence(**kwargs)
        else:
            result = reconcile_fence(
                **kwargs, error_kind=args.error_kind,
            )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (KeyboardInterrupt, EOFError):
        print("DW5 SECOND RECOVERY INTERRUPTED: inspect durable state, no send",
              file=sys.stderr)
        return 130
    except Exception as exc:
        print(
            f"DW5 SECOND RECOVERY STOPPED ({type(exc).__name__}): {exc}",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
