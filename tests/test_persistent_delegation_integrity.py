from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from codexia_manual_agent.delegation import (
    DelegationBudget,
    DelegationPersistenceIntegrityError,
    SqliteDelegationCoordinator,
    SqliteDelegationEventStore,
)
from codexia_manual_agent.domain.capabilities import Capability


class PersistentDelegationIntegrityTests(unittest.TestCase):
    def _case(self):
        temp = tempfile.TemporaryDirectory()
        root = Path(temp.name)
        workspace = root / "workspace"
        workspace.mkdir()
        database = root / "delegation.sqlite3"
        return temp, workspace, database

    def test_root_head_digest_tamper_blocks_recovery(self) -> None:
        temp, workspace, database = self._case()
        try:
            coordinator = SqliteDelegationCoordinator(database)
            root = coordinator.create_root(
                workspace_root=workspace,
                task="root",
                budget=DelegationBudget(turns=5, tool_calls=2, model_chars=500),
            )
            with closing(sqlite3.connect(database)) as connection:
                connection.execute(
                    "UPDATE delegation_roots SET head_event_digest = ? WHERE root_delegation_id = ?",
                    ("0" * 64, root.delegation_id),
                )
                connection.commit()

            with self.assertRaises(DelegationPersistenceIntegrityError):
                SqliteDelegationCoordinator(database).recover(root.delegation_id)
        finally:
            temp.cleanup()

    def test_tail_deletion_is_detected_by_root_head_metadata(self) -> None:
        temp, workspace, database = self._case()
        try:
            coordinator = SqliteDelegationCoordinator(database)
            root = coordinator.create_root(
                workspace_root=workspace,
                task="root",
                budget=DelegationBudget(turns=5, tool_calls=2, model_chars=500),
            )
            coordinator.create_child(
                root.delegation_id,
                task="child",
                capabilities=(Capability.READ_WORKSPACE,),
                budget=DelegationBudget(turns=1, tool_calls=1, model_chars=100),
            )
            with closing(sqlite3.connect(database)) as connection:
                connection.execute(
                    "DELETE FROM delegation_events WHERE root_delegation_id = ? AND sequence = 1",
                    (root.delegation_id,),
                )
                connection.commit()

            with self.assertRaises(DelegationPersistenceIntegrityError):
                SqliteDelegationCoordinator(database).recover(root.delegation_id)
        finally:
            temp.cleanup()

    def test_derived_delegation_index_must_match_event_replay(self) -> None:
        temp, workspace, database = self._case()
        try:
            coordinator = SqliteDelegationCoordinator(database)
            root = coordinator.create_root(
                workspace_root=workspace,
                task="root",
                budget=DelegationBudget(turns=5, tool_calls=2, model_chars=500),
            )
            child = coordinator.create_child(
                root.delegation_id,
                task="child",
                capabilities=(Capability.READ_WORKSPACE,),
                budget=DelegationBudget(turns=1, tool_calls=1, model_chars=100),
            )
            with closing(sqlite3.connect(database)) as connection:
                connection.execute(
                    "DELETE FROM delegation_index WHERE delegation_id = ?",
                    (child.delegation_id,),
                )
                connection.commit()

            with self.assertRaises(DelegationPersistenceIntegrityError):
                SqliteDelegationCoordinator(database).recover(root.delegation_id)
        finally:
            temp.cleanup()

    def test_event_payload_tamper_breaks_exact_event_digest(self) -> None:
        temp, workspace, database = self._case()
        try:
            coordinator = SqliteDelegationCoordinator(database)
            root = coordinator.create_root(
                workspace_root=workspace,
                task="root",
                budget=DelegationBudget(turns=5, tool_calls=2, model_chars=500),
            )
            coordinator.consume_budget(root.delegation_id, turns=1)
            with closing(sqlite3.connect(database)) as connection:
                row = connection.execute(
                    """
                    SELECT payload_json FROM delegation_events
                    WHERE root_delegation_id = ? AND sequence = 1
                    """,
                    (root.delegation_id,),
                ).fetchone()
                assert row is not None
                payload = json.loads(row[0])
                payload["amount"]["turns"] = 2
                connection.execute(
                    """
                    UPDATE delegation_events SET payload_json = ?
                    WHERE root_delegation_id = ? AND sequence = 1
                    """,
                    (
                        json.dumps(payload, sort_keys=True, separators=(",", ":")),
                        root.delegation_id,
                    ),
                )
                connection.commit()

            with self.assertRaises(DelegationPersistenceIntegrityError):
                SqliteDelegationCoordinator(database).recover(root.delegation_id)
        finally:
            temp.cleanup()

    def test_unknown_persisted_event_kind_is_normalized_to_integrity_failure(self) -> None:
        temp, workspace, database = self._case()
        try:
            coordinator = SqliteDelegationCoordinator(database)
            root = coordinator.create_root(
                workspace_root=workspace,
                task="root",
                budget=DelegationBudget(turns=5, tool_calls=2, model_chars=500),
            )
            with closing(sqlite3.connect(database)) as connection:
                connection.execute(
                    """
                    UPDATE delegation_events SET kind = 'not_an_m3_2_event'
                    WHERE root_delegation_id = ? AND sequence = 0
                    """,
                    (root.delegation_id,),
                )
                connection.commit()

            with self.assertRaises(DelegationPersistenceIntegrityError):
                SqliteDelegationCoordinator(database).recover(root.delegation_id)
        finally:
            temp.cleanup()

    def test_missing_workspace_identity_blocks_recovery_instead_of_weakening_envelope(self) -> None:
        temp, workspace, database = self._case()
        try:
            coordinator = SqliteDelegationCoordinator(database)
            root = coordinator.create_root(
                workspace_root=workspace,
                task="root",
                budget=DelegationBudget(turns=5, tool_calls=2, model_chars=500),
            )
            workspace.rmdir()

            with self.assertRaises(DelegationPersistenceIntegrityError):
                SqliteDelegationCoordinator(database).recover(root.delegation_id)
        finally:
            temp.cleanup()

    def test_prepare_value_error_is_not_misclassified_as_persisted_corruption(self) -> None:
        temp, workspace, database = self._case()
        try:
            coordinator = SqliteDelegationCoordinator(database)
            root = coordinator.create_root(
                workspace_root=workspace,
                task="root",
                budget=DelegationBudget(turns=5, tool_calls=2, model_chars=500),
            )
            store = SqliteDelegationEventStore(database)

            def reject_prepare(_state):
                raise ValueError("caller-side prepare failure")

            with self.assertRaisesRegex(ValueError, "caller-side prepare failure"):
                store.mutate_delegation(root.delegation_id, reject_prepare)

            recovered = SqliteDelegationCoordinator(database).recover(root.delegation_id)
            self.assertEqual(len(recovered.events), 1)
        finally:
            temp.cleanup()

    def test_public_operations_release_sqlite_handles_for_windows_cleanup(self) -> None:
        temp, workspace, database = self._case()
        coordinator = SqliteDelegationCoordinator(database)
        root = coordinator.create_root(
            workspace_root=workspace,
            task="root",
            budget=DelegationBudget(turns=3, tool_calls=1, model_chars=300),
        )
        coordinator.snapshot(root.delegation_id)
        coordinator.recover(root.delegation_id)
        del coordinator
        # Windows TemporaryDirectory cleanup fails with WinError 32 if any public
        # operation leaked a SQLite file handle.
        temp.cleanup()

    def test_persistent_coordinator_exposes_no_action_authority_surface(self) -> None:
        temp, workspace, database = self._case()
        try:
            coordinator = SqliteDelegationCoordinator(database)
            coordinator.create_root(
                workspace_root=workspace,
                task="root",
                budget=DelegationBudget(turns=3, tool_calls=1, model_chars=300),
            )
            for forbidden in (
                "authorize",
                "verify_authorization",
                "consume_authorization",
                "execute_process",
                "write_workspace",
                "git_push",
            ):
                self.assertFalse(hasattr(coordinator, forbidden), forbidden)
        finally:
            temp.cleanup()


if __name__ == "__main__":
    unittest.main()
