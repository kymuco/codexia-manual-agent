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
    InvalidLabRecordError,
    LabPersistenceIntegrityError,
    LabRegistryEventReceipt,
    MetricRecord,
    SqliteLabRegistry,
)


_CANONICAL_UUID = "abcdefab-cdef-4abc-8def-abcdefabcdef"
_NONCANONICAL_UUID = _CANONICAL_UUID.upper()


class DurableLabRegistryReviewFindingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.db_path = Path(self.tempdir.name) / "lab-registry.sqlite3"
        self.hypothesis = Hypothesis.create(
            statement="Recovered registry receipts remain immutable and exact.",
            falsification_criterion="A recovered receipt or navigation index can hide drift.",
        )
        self.manifest = ExperimentManifest.create(
            hypothesis=self.hypothesis,
            procedure="Exercise final M4.2 review findings.",
            parameters={"grid": [1, 2, 3]},
        )
        self.run = ExperimentRun.create(manifest=self.manifest, ordinal=0, seed=19)
        self.metric = MetricRecord.create(
            run=self.run,
            name="score",
            value=0.5,
            unit="ratio",
        )
        self.artifact = ArtifactRecord.create(
            run=self.run,
            logical_path="results/score.json",
            size_bytes=8,
            sha256_digest="d" * 64,
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

    def _register_empty_owner(self, procedure: str) -> ExperimentManifest:
        owner = ExperimentManifest.create(
            hypothesis=self.hypothesis,
            procedure=procedure,
        )
        SqliteLabRegistry(self.db_path).register_experiment(self.hypothesis, owner)
        return owner

    def _add_space_after_identity_colon(
        self,
        *,
        sequence: int,
        field_name: str,
        record_id: str,
    ) -> None:
        canonical = f'"{field_name}":"{record_id}"'
        noncanonical = f'"{field_name}": "{record_id}"'
        self._execute(
            """
            UPDATE lab_registry_events
            SET payload_json = replace(payload_json, ?, ?)
            WHERE experiment_id = ? AND sequence = ?
            """,
            (canonical, noncanonical, self.manifest.experiment_id, sequence),
        )

    def test_recovered_event_payload_is_recursively_immutable(self) -> None:
        recovered = SqliteLabRegistry(self.db_path).recover_experiment(
            self.manifest.experiment_id
        )
        receipt = recovered.events[0]
        original_digest = receipt.event_digest

        with self.assertRaises(TypeError):
            receipt.payload["hypothesis"]["statement"] = "tampered"
        with self.assertRaises(TypeError):
            receipt.payload["manifest"]["parameters"]["grid"][0] = 99

        self.assertEqual(receipt.event_digest, original_digest)
        self.assertEqual(
            receipt.payload["hypothesis"]["statement"],
            self.hypothesis.statement,
        )
        self.assertEqual(
            receipt.payload["manifest"]["parameters"]["grid"],
            (1, 2, 3),
        )

    def test_missing_run_navigation_row_is_integrity_failure_not_unknown(self) -> None:
        self._execute(
            "DELETE FROM lab_registry_runs WHERE run_id = ?",
            (self.run.run_id,),
        )

        with self.assertRaises(LabPersistenceIntegrityError):
            SqliteLabRegistry(self.db_path).recover_for_run(self.run.run_id)

    def test_missing_metric_navigation_row_is_integrity_failure_not_unknown(self) -> None:
        self._execute(
            "DELETE FROM lab_registry_metrics WHERE metric_id = ?",
            (self.metric.metric_id,),
        )

        with self.assertRaises(LabPersistenceIntegrityError):
            SqliteLabRegistry(self.db_path).recover_for_metric(self.metric.metric_id)

    def test_missing_artifact_navigation_row_is_integrity_failure_not_unknown(self) -> None:
        self._execute(
            "DELETE FROM lab_registry_artifacts WHERE artifact_id = ?",
            (self.artifact.artifact_id,),
        )

        with self.assertRaises(LabPersistenceIntegrityError):
            SqliteLabRegistry(self.db_path).recover_for_artifact(self.artifact.artifact_id)

    def test_uppercase_run_uuid_cannot_alias_existing_global_identity(self) -> None:
        store = SqliteLabRegistry(self.db_path)
        canonical_run = ExperimentRun.create(
            manifest=self.manifest,
            ordinal=1,
            seed=20,
            run_id=_CANONICAL_UUID,
        )
        store.register_run(canonical_run)
        second_manifest = ExperimentManifest.create(
            hypothesis=self.hypothesis,
            procedure="Attempt a textual UUID alias in another experiment.",
        )
        store.register_experiment(self.hypothesis, second_manifest)
        aliased_run = ExperimentRun.create(
            manifest=second_manifest,
            ordinal=0,
            seed=21,
            run_id=_NONCANONICAL_UUID,
        )

        with self.assertRaisesRegex(
            InvalidLabRecordError,
            "canonical lowercase hyphenated UUID",
        ):
            store.register_run(aliased_run)

        recovered = store.recover_experiment(second_manifest.experiment_id)
        self.assertEqual(len(recovered.events), 1)
        self.assertEqual(recovered.runs, {})

    def test_all_durable_primary_uuid_fields_require_canonical_text(self) -> None:
        store = SqliteLabRegistry(self.db_path)

        noncanonical_hypothesis = Hypothesis.create(
            hypothesis_id=_NONCANONICAL_UUID,
            statement="A noncanonical hypothesis identity must not persist.",
            falsification_criterion="The registry accepts the textual alias.",
        )
        noncanonical_hypothesis_manifest = ExperimentManifest.create(
            hypothesis=noncanonical_hypothesis,
            procedure="Attempt noncanonical hypothesis registration.",
        )
        with self.assertRaises(InvalidLabRecordError):
            store.register_experiment(
                noncanonical_hypothesis,
                noncanonical_hypothesis_manifest,
            )

        noncanonical_experiment = ExperimentManifest.create(
            hypothesis=self.hypothesis,
            procedure="Attempt noncanonical experiment registration.",
            experiment_id=_NONCANONICAL_UUID,
        )
        with self.assertRaises(InvalidLabRecordError):
            store.register_experiment(self.hypothesis, noncanonical_experiment)

        noncanonical_metric = MetricRecord.create(
            run=self.run,
            name="noncanonical_metric_id",
            value=0.75,
            metric_id=_NONCANONICAL_UUID,
        )
        with self.assertRaises(InvalidLabRecordError):
            store.register_metric(noncanonical_metric)

        noncanonical_artifact = ArtifactRecord.create(
            run=self.run,
            logical_path="results/noncanonical-id.json",
            size_bytes=9,
            sha256_digest="e" * 64,
            media_type="application/json",
            artifact_id=_NONCANONICAL_UUID,
        )
        with self.assertRaises(InvalidLabRecordError):
            store.register_artifact(noncanonical_artifact)

        with self.assertRaises(InvalidLabRecordError):
            LabRegistryEventReceipt.create(
                experiment_id=self.manifest.experiment_id,
                sequence=0,
                kind="experiment_registered",
                payload={
                    "hypothesis": self.hypothesis.to_dict(),
                    "manifest": self.manifest.to_dict(),
                },
                previous_event_digest=None,
                event_id=_NONCANONICAL_UUID,
            )

    def test_stale_foreign_run_index_is_integrity_failure_not_identity_conflict(self) -> None:
        owner = self._register_empty_owner("Own a stale run navigation row only.")
        candidate = ExperimentRun.create(
            manifest=self.manifest,
            ordinal=1,
            seed=22,
        )
        before = SqliteLabRegistry(self.db_path).recover_experiment(
            self.manifest.experiment_id
        )
        self._execute(
            """
            INSERT INTO lab_registry_runs(
                run_id, experiment_id, run_digest, manifest_digest, ordinal
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                candidate.run_id,
                owner.experiment_id,
                candidate.run_digest,
                owner.manifest_digest,
                0,
            ),
        )

        with self.assertRaises(LabPersistenceIntegrityError):
            SqliteLabRegistry(self.db_path).register_run(candidate)

        after = SqliteLabRegistry(self.db_path).recover_experiment(
            self.manifest.experiment_id
        )
        self.assertEqual(after.events, before.events)
        self.assertNotIn(candidate.run_id, after.runs)

    def test_stale_foreign_metric_index_is_integrity_failure_not_identity_conflict(self) -> None:
        owner = self._register_empty_owner("Own a stale metric navigation row only.")
        candidate = MetricRecord.create(
            run=self.run,
            name="foreign_stale_metric",
            value=0.625,
        )
        before = SqliteLabRegistry(self.db_path).recover_experiment(
            self.manifest.experiment_id
        )
        self._execute(
            """
            INSERT INTO lab_registry_metrics(
                metric_id, experiment_id, run_id, run_digest,
                manifest_digest, metric_digest, name
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                candidate.metric_id,
                owner.experiment_id,
                self.run.run_id,
                candidate.run_digest,
                owner.manifest_digest,
                candidate.metric_digest,
                candidate.name,
            ),
        )

        with self.assertRaises(LabPersistenceIntegrityError):
            SqliteLabRegistry(self.db_path).register_metric(candidate)

        after = SqliteLabRegistry(self.db_path).recover_experiment(
            self.manifest.experiment_id
        )
        self.assertEqual(after.events, before.events)
        self.assertNotIn(candidate.metric_id, after.run(self.run.run_id).metrics)

    def test_stale_foreign_artifact_index_is_integrity_failure_not_identity_conflict(self) -> None:
        owner = self._register_empty_owner("Own a stale artifact navigation row only.")
        candidate = ArtifactRecord.create(
            run=self.run,
            logical_path="results/foreign-stale.json",
            size_bytes=10,
            sha256_digest="f" * 64,
            media_type="application/json",
        )
        before = SqliteLabRegistry(self.db_path).recover_experiment(
            self.manifest.experiment_id
        )
        self._execute(
            """
            INSERT INTO lab_registry_artifacts(
                artifact_id, experiment_id, run_id, run_digest,
                manifest_digest, artifact_digest, logical_path
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                candidate.artifact_id,
                owner.experiment_id,
                self.run.run_id,
                candidate.run_digest,
                owner.manifest_digest,
                candidate.artifact_digest,
                candidate.logical_path,
            ),
        )

        with self.assertRaises(LabPersistenceIntegrityError):
            SqliteLabRegistry(self.db_path).register_artifact(candidate)

        after = SqliteLabRegistry(self.db_path).recover_experiment(
            self.manifest.experiment_id
        )
        self.assertEqual(after.events, before.events)
        self.assertNotIn(candidate.artifact_id, after.run(self.run.run_id).artifacts)

    def test_missing_run_index_with_noncanonical_event_json_is_integrity_failure(self) -> None:
        self._execute(
            "DELETE FROM lab_registry_runs WHERE run_id = ?",
            (self.run.run_id,),
        )
        self._add_space_after_identity_colon(
            sequence=1,
            field_name="run_id",
            record_id=self.run.run_id,
        )

        with self.assertRaises(LabPersistenceIntegrityError):
            SqliteLabRegistry(self.db_path).recover_for_run(self.run.run_id)

    def test_missing_metric_index_with_noncanonical_event_json_is_integrity_failure(self) -> None:
        self._execute(
            "DELETE FROM lab_registry_metrics WHERE metric_id = ?",
            (self.metric.metric_id,),
        )
        self._add_space_after_identity_colon(
            sequence=2,
            field_name="metric_id",
            record_id=self.metric.metric_id,
        )

        with self.assertRaises(LabPersistenceIntegrityError):
            SqliteLabRegistry(self.db_path).recover_for_metric(self.metric.metric_id)

    def test_missing_artifact_index_with_noncanonical_event_json_is_integrity_failure(self) -> None:
        self._execute(
            "DELETE FROM lab_registry_artifacts WHERE artifact_id = ?",
            (self.artifact.artifact_id,),
        )
        self._add_space_after_identity_colon(
            sequence=3,
            field_name="artifact_id",
            record_id=self.artifact.artifact_id,
        )

        with self.assertRaises(LabPersistenceIntegrityError):
            SqliteLabRegistry(self.db_path).recover_for_artifact(self.artifact.artifact_id)

    def test_missing_run_index_with_noncanonical_uuid_event_is_integrity_failure(self) -> None:
        self._execute(
            "DELETE FROM lab_registry_runs WHERE run_id = ?",
            (self.run.run_id,),
        )
        self._execute(
            """
            UPDATE lab_registry_events
            SET payload_json = replace(payload_json, ?, ?)
            WHERE experiment_id = ? AND sequence = 1
            """,
            (
                self.run.run_id,
                self.run.run_id.upper(),
                self.manifest.experiment_id,
            ),
        )

        with self.assertRaises(LabPersistenceIntegrityError):
            SqliteLabRegistry(self.db_path).recover_for_run(self.run.run_id)


if __name__ == "__main__":
    unittest.main()
