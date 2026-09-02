from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codexia_manual_agent.lab import (
    ExperimentManifest,
    ExperimentRun,
    Hypothesis,
    LabPersistenceError,
    SqliteLabRegistry,
)


class _ConnectionFaultProxy:
    def __init__(self, connection: sqlite3.Connection, *, fail_sql: str) -> None:
        self._connection = connection
        self._fail_sql = fail_sql

    @property
    def in_transaction(self) -> bool:
        return self._connection.in_transaction

    def execute(self, sql: str, parameters=()):
        normalized = " ".join(sql.split()).upper()
        if normalized == self._fail_sql:
            raise sqlite3.OperationalError(f"forced SQLite failure at {self._fail_sql}")
        return self._connection.execute(sql, parameters)

    def __getattr__(self, name: str):
        return getattr(self._connection, name)

    def close(self) -> None:
        self._connection.close()


class DurableLabRegistryCandidate6StorageFindingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.db_path = Path(self.tempdir.name) / "lab-registry.sqlite3"
        self.hypothesis = Hypothesis.create(
            statement="SQLite operation failures remain in the lab persistence domain.",
            falsification_criterion="A raw sqlite3.Error escapes a registry operation.",
        )
        self.manifest = ExperimentManifest.create(
            hypothesis=self.hypothesis,
            procedure="Exercise the exact durable SQLite operation boundary.",
        )

    @staticmethod
    def _fault_statement(store: SqliteLabRegistry, statement: str):
        original_connect = store._connect

        def connect():
            return _ConnectionFaultProxy(
                original_connect(),
                fail_sql=statement,
            )

        return patch.object(store, "_connect", side_effect=connect)

    def test_standard_sqlite_memory_path_is_rejected_fail_fast(self) -> None:
        for database_path in (":memory:", Path(":memory:")):
            with self.subTest(database_path=database_path):
                with self.assertRaisesRegex(
                    LabPersistenceError,
                    "filesystem-backed SQLite database path",
                ):
                    SqliteLabRegistry(database_path)

    def test_parent_directory_creation_failure_is_normalized(self) -> None:
        blocker = Path(self.tempdir.name) / "not-a-directory"
        blocker.write_text("block parent creation", encoding="utf-8")

        with self.assertRaises(LabPersistenceError) as raised:
            SqliteLabRegistry(blocker / "lab-registry.sqlite3")

        self.assertIsInstance(raised.exception.__cause__, OSError)

    def test_write_lock_begin_failure_is_normalized(self) -> None:
        store = SqliteLabRegistry(self.db_path)

        with self._fault_statement(store, "BEGIN IMMEDIATE"):
            with self.assertRaises(LabPersistenceError) as raised:
                store.register_experiment(self.hypothesis, self.manifest)

        self.assertIsInstance(raised.exception.__cause__, sqlite3.OperationalError)
        recovered = store.register_experiment(self.hypothesis, self.manifest)
        self.assertEqual(recovered.experiment_id, self.manifest.experiment_id)

    def test_commit_failure_is_normalized_after_rollback(self) -> None:
        store = SqliteLabRegistry(self.db_path)
        store.register_experiment(self.hypothesis, self.manifest)
        run = ExperimentRun.create(manifest=self.manifest, ordinal=0, seed=17)
        before_events = store.recover_experiment(self.manifest.experiment_id).events

        with self._fault_statement(store, "COMMIT"):
            with self.assertRaises(LabPersistenceError) as raised:
                store.register_run(run)

        self.assertIsInstance(raised.exception.__cause__, sqlite3.OperationalError)
        recovered = store.recover_experiment(self.manifest.experiment_id)
        self.assertEqual(recovered.events, before_events)
        self.assertNotIn(run.run_id, recovered.runs)

    def test_connection_failure_is_normalized(self) -> None:
        store = SqliteLabRegistry(self.db_path)
        store.register_experiment(self.hypothesis, self.manifest)

        with patch.object(
            store,
            "_connect",
            side_effect=sqlite3.OperationalError("unable to open database file"),
        ):
            with self.assertRaises(LabPersistenceError) as raised:
                store.recover_experiment(self.manifest.experiment_id)

        self.assertIsInstance(raised.exception.__cause__, sqlite3.OperationalError)

    def test_shared_hypothesis_admission_builds_one_derived_snapshot(self) -> None:
        class CountingSqliteLabRegistry(SqliteLabRegistry):
            derived_snapshot_builds = 0

            @classmethod
            def _derived_index_snapshot(cls, connection, state):
                cls.derived_snapshot_builds += 1
                return super()._derived_index_snapshot(connection, state)

        store = CountingSqliteLabRegistry(self.db_path)
        for ordinal in range(4):
            manifest = ExperimentManifest.create(
                hypothesis=self.hypothesis,
                procedure=f"Existing shared-hypothesis owner {ordinal}.",
            )
            store.register_experiment(self.hypothesis, manifest)
            store.register_run(
                ExperimentRun.create(
                    manifest=manifest,
                    ordinal=0,
                    seed=ordinal,
                )
            )

        candidate = ExperimentManifest.create(
            hypothesis=self.hypothesis,
            procedure="Admit another exact shared-hypothesis owner.",
        )
        CountingSqliteLabRegistry.derived_snapshot_builds = 0

        recovered = store.register_experiment(self.hypothesis, candidate)

        self.assertEqual(recovered.experiment_id, candidate.experiment_id)
        self.assertEqual(CountingSqliteLabRegistry.derived_snapshot_builds, 1)


if __name__ == "__main__":
    unittest.main()
