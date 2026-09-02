from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from codexia_manual_agent.lab import (
    ArtifactRecord,
    ExperimentManifest,
    ExperimentRun,
    Hypothesis,
    LabPersistenceIntegrityError,
    MetricRecord,
    SqliteLabRegistry,
)


_RUN_ID = "abcdefab-cdef-4abc-8def-abcdefabcdef"
_METRIC_ID = "bcdefabc-defa-4bcd-8efa-bcdefabcdefa"
_ARTIFACT_ID = "cdefabcd-efab-4cde-8fab-cdefabcdefab"
_EXPERIMENT_ID = "abcdef12-3456-4abc-8def-abcdef123456"
_HYPOTHESIS_ID = "fabcdefa-bcde-4fab-8cde-fabcdefabcde"
_FOREIGN_EXPERIMENT_ID = "fedcba98-7654-4321-8765-fedcba987654"


class DurableLabRegistryCandidate6GlobalUuidFindingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.db_path = Path(self.tempdir.name) / "malformed-global-uuid.sqlite3"
        self.store = SqliteLabRegistry(self.db_path)
        self.hypothesis = Hypothesis.create(
            statement="Persisted UUID identity scans reject malformed text.",
            falsification_criterion=(
                "A malformed TEXT UUID can be skipped as an unrelated durable identity."
            ),
        )
        self.manifest = ExperimentManifest.create(
            hypothesis=self.hypothesis,
            procedure="Exercise malformed global UUID identity state.",
        )
        self.run = ExperimentRun.create(
            manifest=self.manifest,
            ordinal=0,
            seed=1,
        )
        self.store.register_experiment(self.hypothesis, self.manifest)
        self.store.register_run(self.run)

    def _execute(self, sql: str, parameters: tuple[object, ...]) -> None:
        connection = sqlite3.connect(self.db_path)
        try:
            connection.execute(sql, parameters)
            connection.commit()
        finally:
            connection.close()

    def _event_count(self) -> int:
        connection = sqlite3.connect(self.db_path)
        try:
            row = connection.execute(
                "SELECT COUNT(*) FROM lab_registry_events WHERE experiment_id = ?",
                (self.manifest.experiment_id,),
            ).fetchone()
            assert row is not None
            return int(row[0])
        finally:
            connection.close()

    def _root_count(self, experiment_id: str) -> int:
        connection = sqlite3.connect(self.db_path)
        try:
            row = connection.execute(
                "SELECT COUNT(*) FROM lab_registry_experiments WHERE experiment_id = ?",
                (experiment_id,),
            ).fetchone()
            assert row is not None
            return int(row[0])
        finally:
            connection.close()

    def test_malformed_text_global_run_id_fails_closed(self) -> None:
        candidate = ExperimentRun.create(
            manifest=self.manifest,
            ordinal=1,
            seed=2,
            run_id=_RUN_ID,
        )
        before = self._event_count()
        self._execute(
            """
            INSERT INTO lab_registry_runs(
                run_id, experiment_id, run_digest, manifest_digest, ordinal
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                "not-a-uuid",
                _FOREIGN_EXPERIMENT_ID,
                candidate.run_digest,
                candidate.manifest_digest,
                99,
            ),
        )

        with self.assertRaises(LabPersistenceIntegrityError):
            self.store.register_run(candidate)
        self.assertEqual(self._event_count(), before)
        with self.assertRaises(LabPersistenceIntegrityError):
            self.store.recover_for_run(candidate.run_id)

    def test_malformed_text_global_metric_id_fails_closed(self) -> None:
        candidate = MetricRecord.create(
            run=self.run,
            name="candidate-global-metric",
            value=0.5,
            unit="ratio",
            metric_id=_METRIC_ID,
        )
        before = self._event_count()
        self._execute(
            """
            INSERT INTO lab_registry_metrics(
                metric_id, experiment_id, run_id, run_digest,
                manifest_digest, metric_digest, name
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "not-a-uuid",
                _FOREIGN_EXPERIMENT_ID,
                self.run.run_id,
                candidate.run_digest,
                candidate.manifest_digest,
                candidate.metric_digest,
                "stale-unrelated-metric",
            ),
        )

        with self.assertRaises(LabPersistenceIntegrityError):
            self.store.register_metric(candidate)
        self.assertEqual(self._event_count(), before)
        with self.assertRaises(LabPersistenceIntegrityError):
            self.store.recover_for_metric(candidate.metric_id)

    def test_malformed_text_global_artifact_id_fails_closed(self) -> None:
        candidate = ArtifactRecord.create(
            run=self.run,
            logical_path="reports/candidate-global.bin",
            size_bytes=8,
            sha256_digest="a" * 64,
            media_type="application/octet-stream",
            artifact_id=_ARTIFACT_ID,
        )
        before = self._event_count()
        self._execute(
            """
            INSERT INTO lab_registry_artifacts(
                artifact_id, experiment_id, run_id, run_digest,
                manifest_digest, artifact_digest, logical_path
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "not-a-uuid",
                _FOREIGN_EXPERIMENT_ID,
                self.run.run_id,
                candidate.run_digest,
                candidate.manifest_digest,
                candidate.artifact_digest,
                "reports/stale-unrelated.bin",
            ),
        )

        with self.assertRaises(LabPersistenceIntegrityError):
            self.store.register_artifact(candidate)
        self.assertEqual(self._event_count(), before)
        with self.assertRaises(LabPersistenceIntegrityError):
            self.store.recover_for_artifact(candidate.artifact_id)

    def test_malformed_text_experiment_root_id_blocks_new_root(self) -> None:
        candidate_hypothesis = Hypothesis.create(
            statement="A candidate experiment must not cross malformed root identity state.",
            falsification_criterion="A malformed root UUID can be ignored during admission.",
        )
        candidate_manifest = ExperimentManifest.create(
            hypothesis=candidate_hypothesis,
            procedure="Attempt admission beside malformed experiment identity.",
            experiment_id=_EXPERIMENT_ID,
        )
        self._execute(
            """
            INSERT INTO lab_registry_experiments(
                experiment_id, hypothesis_id, hypothesis_digest,
                manifest_digest, head_sequence, head_event_digest, registered_at
            ) VALUES (?, ?, ?, ?, -1, NULL, ?)
            """,
            (
                "not-a-uuid",
                _HYPOTHESIS_ID,
                "1" * 64,
                "2" * 64,
                "2026-01-01T00:00:00+00:00",
            ),
        )

        with self.assertRaises(LabPersistenceIntegrityError):
            self.store.register_experiment(candidate_hypothesis, candidate_manifest)
        self.assertEqual(self._root_count(candidate_manifest.experiment_id), 0)

    def test_malformed_text_hypothesis_index_id_blocks_new_root(self) -> None:
        candidate_hypothesis = Hypothesis.create(
            hypothesis_id=_HYPOTHESIS_ID,
            statement="A candidate hypothesis must not cross malformed index identity state.",
            falsification_criterion="A malformed hypothesis UUID can be ignored during admission.",
        )
        candidate_manifest = ExperimentManifest.create(
            hypothesis=candidate_hypothesis,
            procedure="Attempt admission beside malformed hypothesis identity.",
            experiment_id=_EXPERIMENT_ID,
        )
        self._execute(
            """
            INSERT INTO lab_registry_hypotheses(hypothesis_id, hypothesis_digest)
            VALUES (?, ?)
            """,
            ("not-a-uuid", "3" * 64),
        )

        with self.assertRaises(LabPersistenceIntegrityError):
            self.store.register_experiment(candidate_hypothesis, candidate_manifest)
        self.assertEqual(self._root_count(candidate_manifest.experiment_id), 0)


if __name__ == "__main__":
    unittest.main()
