from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from codexia_manual_agent.lab import (
    ExperimentManifest,
    ExperimentRun,
    Hypothesis,
    LabPersistenceIntegrityError,
    SqliteLabRegistry,
)


_MALFORMED_OWNER_ID = "not-a-uuid"


class DurableLabRegistryCandidate3TaxonomyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.db_path = Path(self.tempdir.name) / "lab-registry.sqlite3"
        self.hypothesis = Hypothesis.create(
            statement="Malformed persisted owner UUIDs remain persistence corruption.",
            falsification_criterion="A persisted owner UUID leaks a built-in parser error.",
        )
        self.manifest = ExperimentManifest.create(
            hypothesis=self.hypothesis,
            procedure="Exercise malformed persisted owner UUID taxonomy.",
        )
        self.run = ExperimentRun.create(
            manifest=self.manifest,
            ordinal=0,
            seed=71,
        )
        self.store = SqliteLabRegistry(self.db_path)
        self.store.register_experiment(self.hypothesis, self.manifest)
        self.store.register_run(self.run)

    @staticmethod
    def _execute(
        db_path: Path,
        sql: str,
        parameters: tuple[object, ...] = (),
    ) -> None:
        # Foreign keys are intentionally left disabled to model out-of-band
        # persistence corruption that the supported registry writer cannot create.
        connection = sqlite3.connect(db_path)
        try:
            connection.execute(sql, parameters)
            connection.commit()
        finally:
            connection.close()

    @staticmethod
    def _root_count(db_path: Path, experiment_id: str) -> int:
        connection = sqlite3.connect(db_path)
        try:
            row = connection.execute(
                "SELECT COUNT(*) FROM lab_registry_experiments WHERE experiment_id = ?",
                (experiment_id,),
            ).fetchone()
            assert row is not None
            return int(row[0])
        finally:
            connection.close()

    def test_malformed_derived_navigation_owner_is_integrity_failure(self) -> None:
        self._execute(
            self.db_path,
            "UPDATE lab_registry_runs SET experiment_id = ? WHERE run_id = ?",
            (_MALFORMED_OWNER_ID, self.run.run_id),
        )

        with self.assertRaises(LabPersistenceIntegrityError):
            SqliteLabRegistry(self.db_path).recover_for_run(self.run.run_id)

    def test_malformed_authoritative_fallback_owner_is_integrity_failure(self) -> None:
        self._execute(
            self.db_path,
            "DELETE FROM lab_registry_runs WHERE run_id = ?",
            (self.run.run_id,),
        )
        self._execute(
            self.db_path,
            """
            UPDATE lab_registry_events
            SET experiment_id = ?
            WHERE experiment_id = ? AND kind = 'run_registered'
            """,
            (_MALFORMED_OWNER_ID, self.manifest.experiment_id),
        )

        with self.assertRaises(LabPersistenceIntegrityError):
            SqliteLabRegistry(self.db_path).recover_for_run(self.run.run_id)

    def test_malformed_hypothesis_root_owner_blocks_new_registration(self) -> None:
        candidate = ExperimentManifest.create(
            hypothesis=self.hypothesis,
            procedure="A later experiment must not bypass a malformed hypothesis owner.",
        )
        self._execute(
            self.db_path,
            """
            UPDATE lab_registry_experiments
            SET experiment_id = ?
            WHERE experiment_id = ?
            """,
            (_MALFORMED_OWNER_ID, self.manifest.experiment_id),
        )

        with self.assertRaises(LabPersistenceIntegrityError):
            self.store.register_experiment(self.hypothesis, candidate)

        self.assertEqual(self._root_count(self.db_path, candidate.experiment_id), 0)


if __name__ == "__main__":
    unittest.main()
