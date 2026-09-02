from __future__ import annotations

import json
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


class DurableLabRegistryIntegrityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.db_path = Path(self.tempdir.name) / "lab-registry.sqlite3"
        self.hypothesis = Hypothesis.create(
            statement="Durable evidence remains exactly recoverable.",
            falsification_criterion="Persisted lineage cannot be recovered exactly.",
        )
        self.manifest = ExperimentManifest.create(
            hypothesis=self.hypothesis,
            procedure="Produce one metric and one artifact.",
        )
        self.run = ExperimentRun.create(manifest=self.manifest, ordinal=0, seed=5)
        self.metric = MetricRecord.create(
            run=self.run,
            name="loss",
            value=0.25,
            unit="ratio",
        )
        self.artifact = ArtifactRecord.create(
            run=self.run,
            logical_path="results/loss.json",
            size_bytes=12,
            sha256_digest="c" * 64,
            media_type="application/json",
        )
        store = SqliteLabRegistry(self.db_path)
        store.register_experiment(self.hypothesis, self.manifest)
        store.register_run(self.run)
        store.register_metric(self.metric)
        store.register_artifact(self.artifact)

    def _execute(self, sql: str, parameters: tuple[object, ...] = ()) -> None:
        connection = sqlite3.connect(self.db_path)
        try:
            connection.execute(sql, parameters)
            connection.commit()
        finally:
            connection.close()

    def test_root_head_digest_tamper_fails_closed(self) -> None:
        self._execute(
            """
            UPDATE lab_registry_experiments
            SET head_event_digest = ?
            WHERE experiment_id = ?
            """,
            ("0" * 64, self.manifest.experiment_id),
        )

        with self.assertRaises(LabPersistenceIntegrityError):
            SqliteLabRegistry(self.db_path).recover_experiment(self.manifest.experiment_id)

    def test_tail_event_deletion_fails_closed(self) -> None:
        self._execute(
            """
            DELETE FROM lab_registry_events
            WHERE experiment_id = ? AND sequence = 3
            """,
            (self.manifest.experiment_id,),
        )

        with self.assertRaises(LabPersistenceIntegrityError):
            SqliteLabRegistry(self.db_path).recover_experiment(self.manifest.experiment_id)

    def test_event_payload_tamper_fails_closed(self) -> None:
        connection = sqlite3.connect(self.db_path)
        try:
            row = connection.execute(
                """
                SELECT payload_json
                FROM lab_registry_events
                WHERE experiment_id = ? AND sequence = 2
                """,
                (self.manifest.experiment_id,),
            ).fetchone()
            payload = json.loads(row[0])
            payload["metric"]["value"] = 0.75
            tampered = json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            connection.execute(
                """
                UPDATE lab_registry_events
                SET payload_json = ?
                WHERE experiment_id = ? AND sequence = 2
                """,
                (tampered, self.manifest.experiment_id),
            )
            connection.commit()
        finally:
            connection.close()

        with self.assertRaises(LabPersistenceIntegrityError):
            SqliteLabRegistry(self.db_path).recover_experiment(self.manifest.experiment_id)

    def test_noncanonical_persisted_json_fails_closed(self) -> None:
        connection = sqlite3.connect(self.db_path)
        try:
            row = connection.execute(
                """
                SELECT payload_json
                FROM lab_registry_events
                WHERE experiment_id = ? AND sequence = 1
                """,
                (self.manifest.experiment_id,),
            ).fetchone()
            noncanonical = json.dumps(json.loads(row[0]), indent=2)
            connection.execute(
                """
                UPDATE lab_registry_events
                SET payload_json = ?
                WHERE experiment_id = ? AND sequence = 1
                """,
                (noncanonical, self.manifest.experiment_id),
            )
            connection.commit()
        finally:
            connection.close()

        with self.assertRaisesRegex(
            LabPersistenceIntegrityError,
            "not canonical JSON",
        ):
            SqliteLabRegistry(self.db_path).recover_experiment(self.manifest.experiment_id)

    def test_hash_chain_previous_digest_tamper_fails_closed(self) -> None:
        self._execute(
            """
            UPDATE lab_registry_events
            SET previous_event_digest = ?
            WHERE experiment_id = ? AND sequence = 2
            """,
            ("1" * 64, self.manifest.experiment_id),
        )

        with self.assertRaises(LabPersistenceIntegrityError):
            SqliteLabRegistry(self.db_path).recover_experiment(self.manifest.experiment_id)

    def test_unknown_event_kind_is_normalized_to_integrity_failure(self) -> None:
        self._execute(
            """
            UPDATE lab_registry_events
            SET kind = 'future_unknown_kind'
            WHERE experiment_id = ? AND sequence = 2
            """,
            (self.manifest.experiment_id,),
        )

        with self.assertRaises(LabPersistenceIntegrityError):
            SqliteLabRegistry(self.db_path).recover_experiment(self.manifest.experiment_id)

    def test_root_identity_metadata_tamper_fails_closed(self) -> None:
        self._execute(
            """
            UPDATE lab_registry_experiments
            SET manifest_digest = ?
            WHERE experiment_id = ?
            """,
            ("2" * 64, self.manifest.experiment_id),
        )

        with self.assertRaises(LabPersistenceIntegrityError):
            SqliteLabRegistry(self.db_path).recover_experiment(self.manifest.experiment_id)

    def test_hypothesis_identity_index_tamper_fails_closed(self) -> None:
        self._execute(
            """
            UPDATE lab_registry_hypotheses
            SET hypothesis_digest = ?
            WHERE hypothesis_id = ?
            """,
            ("3" * 64, self.hypothesis.hypothesis_id),
        )

        with self.assertRaises(LabPersistenceIntegrityError):
            SqliteLabRegistry(self.db_path).recover_experiment(self.manifest.experiment_id)

    def test_run_index_tamper_fails_closed(self) -> None:
        self._execute(
            """
            UPDATE lab_registry_runs
            SET ordinal = 9
            WHERE run_id = ?
            """,
            (self.run.run_id,),
        )

        with self.assertRaises(LabPersistenceIntegrityError):
            SqliteLabRegistry(self.db_path).recover_for_run(self.run.run_id)

    def test_metric_index_tamper_fails_closed(self) -> None:
        self._execute(
            """
            UPDATE lab_registry_metrics
            SET metric_digest = ?
            WHERE metric_id = ?
            """,
            ("4" * 64, self.metric.metric_id),
        )

        with self.assertRaises(LabPersistenceIntegrityError):
            SqliteLabRegistry(self.db_path).recover_for_metric(self.metric.metric_id)

    def test_artifact_index_tamper_fails_closed(self) -> None:
        self._execute(
            """
            UPDATE lab_registry_artifacts
            SET logical_path = 'other/result.json'
            WHERE artifact_id = ?
            """,
            (self.artifact.artifact_id,),
        )

        with self.assertRaises(LabPersistenceIntegrityError):
            SqliteLabRegistry(self.db_path).recover_for_artifact(self.artifact.artifact_id)

    def test_database_handles_are_released_after_recovery_failure(self) -> None:
        self._execute(
            """
            UPDATE lab_registry_experiments
            SET head_event_digest = ?
            WHERE experiment_id = ?
            """,
            ("5" * 64, self.manifest.experiment_id),
        )
        with self.assertRaises(LabPersistenceIntegrityError):
            SqliteLabRegistry(self.db_path).recover_experiment(self.manifest.experiment_id)

        renamed = self.db_path.with_suffix(".moved")
        self.db_path.rename(renamed)
        self.assertTrue(renamed.exists())


if __name__ == "__main__":
    unittest.main()
