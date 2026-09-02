from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from hashlib import sha256
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
_STALE_METRIC_ID = "dabcdefa-bcde-4def-8abc-defabcdefabc"
_STALE_ARTIFACT_ID = "eabcdefa-bcde-4efa-8bcd-efabcdefabcd"
_HYPOTHESIS_ID = "fabcdefa-bcde-4fab-8cde-fabcdefabcde"
_EXPERIMENT_ID = "abcdef12-3456-4abc-8def-abcdef123456"
_SECOND_EXPERIMENT_ID = "12345678-90ab-4cde-8fab-1234567890ab"
_FOREIGN_EXPERIMENT_ID = "fedcba98-7654-4321-8765-fedcba987654"


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


class DurableLabRegistryCandidate6FindingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.db_path = Path(self.tempdir.name) / "non-text-uuid-identities.sqlite3"
        self.store = SqliteLabRegistry(self.db_path)
        self.hypothesis = Hypothesis.create(
            statement="Persisted durable UUID identities remain text-only.",
            falsification_criterion=(
                "A non-text SQLite UUID key can coexist with the canonical text identity."
            ),
        )
        self.manifest = ExperimentManifest.create(
            hypothesis=self.hypothesis,
            procedure="Exercise durable identity scans.",
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
        return self._event_count_for(self.manifest.experiment_id)

    def _event_count_for(self, experiment_id: str) -> int:
        connection = sqlite3.connect(self.db_path)
        try:
            row = connection.execute(
                "SELECT COUNT(*) FROM lab_registry_events WHERE experiment_id = ?",
                (experiment_id,),
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

    def _latest_run_event_sequence(self, experiment_id: str) -> int:
        connection = sqlite3.connect(self.db_path)
        try:
            row = connection.execute(
                """
                SELECT sequence
                FROM lab_registry_events
                WHERE experiment_id = ? AND kind = 'run_registered'
                ORDER BY sequence DESC
                LIMIT 1
                """,
                (experiment_id,),
            ).fetchone()
            assert row is not None
            return int(row[0])
        finally:
            connection.close()

    def test_blob_global_run_id_fails_registration_and_direct_recovery(self) -> None:
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
                sqlite3.Binary(candidate.run_id.encode("utf-8")),
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

    def test_blob_global_metric_id_fails_registration_and_direct_recovery(self) -> None:
        candidate = MetricRecord.create(
            run=self.run,
            name="candidate-metric",
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
                sqlite3.Binary(candidate.metric_id.encode("utf-8")),
                _FOREIGN_EXPERIMENT_ID,
                self.run.run_id,
                candidate.run_digest,
                candidate.manifest_digest,
                candidate.metric_digest,
                "stale-other-metric",
            ),
        )

        with self.assertRaises(LabPersistenceIntegrityError):
            self.store.register_metric(candidate)
        self.assertEqual(self._event_count(), before)
        with self.assertRaises(LabPersistenceIntegrityError):
            self.store.recover_for_metric(candidate.metric_id)

    def test_blob_global_artifact_id_fails_registration_and_direct_recovery(self) -> None:
        candidate = ArtifactRecord.create(
            run=self.run,
            logical_path="reports/candidate.bin",
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
                sqlite3.Binary(candidate.artifact_id.encode("utf-8")),
                _FOREIGN_EXPERIMENT_ID,
                self.run.run_id,
                candidate.run_digest,
                candidate.manifest_digest,
                candidate.artifact_digest,
                "reports/stale-other.bin",
            ),
        )

        with self.assertRaises(LabPersistenceIntegrityError):
            self.store.register_artifact(candidate)
        self.assertEqual(self._event_count(), before)
        with self.assertRaises(LabPersistenceIntegrityError):
            self.store.recover_for_artifact(candidate.artifact_id)

    def test_blob_scoped_metric_run_id_cannot_bypass_name_uniqueness(self) -> None:
        candidate = MetricRecord.create(
            run=self.run,
            name="scoped-metric",
            value=0.75,
            unit="ratio",
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
                _STALE_METRIC_ID,
                _FOREIGN_EXPERIMENT_ID,
                sqlite3.Binary(self.run.run_id.encode("utf-8")),
                self.run.run_digest,
                self.run.manifest_digest,
                "b" * 64,
                candidate.name,
            ),
        )

        with self.assertRaises(LabPersistenceIntegrityError):
            self.store.register_metric(candidate)
        self.assertEqual(self._event_count(), before)

    def test_blob_scoped_artifact_run_id_cannot_bypass_path_uniqueness(self) -> None:
        candidate = ArtifactRecord.create(
            run=self.run,
            logical_path="reports/scoped.bin",
            size_bytes=4,
            sha256_digest="c" * 64,
            media_type="application/octet-stream",
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
                _STALE_ARTIFACT_ID,
                _FOREIGN_EXPERIMENT_ID,
                sqlite3.Binary(self.run.run_id.encode("utf-8")),
                self.run.run_digest,
                self.run.manifest_digest,
                "d" * 64,
                candidate.logical_path,
            ),
        )

        with self.assertRaises(LabPersistenceIntegrityError):
            self.store.register_artifact(candidate)
        self.assertEqual(self._event_count(), before)

    def test_blob_hypothesis_index_cannot_admit_canonical_text_identity(self) -> None:
        candidate_hypothesis = Hypothesis.create(
            hypothesis_id=_HYPOTHESIS_ID,
            statement="A second hypothesis identity.",
            falsification_criterion="The identity can be duplicated by SQLite type.",
        )
        candidate_manifest = ExperimentManifest.create(
            hypothesis=candidate_hypothesis,
            procedure="Attempt BLOB/TEXT hypothesis identity coexistence.",
            experiment_id=_EXPERIMENT_ID,
        )
        self._execute(
            """
            INSERT INTO lab_registry_hypotheses(hypothesis_id, hypothesis_digest)
            VALUES (?, ?)
            """,
            (
                sqlite3.Binary(candidate_hypothesis.hypothesis_id.encode("utf-8")),
                candidate_hypothesis.hypothesis_digest,
            ),
        )

        with self.assertRaises(LabPersistenceIntegrityError):
            self.store.register_experiment(candidate_hypothesis, candidate_manifest)

        self.assertEqual(self._root_count(candidate_manifest.experiment_id), 0)

    def test_non_text_authoritative_primary_id_is_integrity_not_unknown(self) -> None:
        second = ExperimentRun.create(
            manifest=self.manifest,
            ordinal=1,
            seed=3,
            run_id=_RUN_ID,
        )
        self.store.register_run(second)

        connection = sqlite3.connect(self.db_path)
        try:
            row = connection.execute(
                """
                SELECT sequence, payload_json
                FROM lab_registry_events
                WHERE experiment_id = ? AND kind = 'run_registered'
                ORDER BY sequence DESC
                LIMIT 1
                """,
                (self.manifest.experiment_id,),
            ).fetchone()
            assert row is not None
            payload = json.loads(str(row[1]))
            payload["run"]["run_id"] = 7
            non_text_payload = json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            connection.execute(
                "DELETE FROM lab_registry_runs WHERE run_id = ?",
                (second.run_id,),
            )
            connection.execute(
                """
                UPDATE lab_registry_events
                SET payload_json = ?
                WHERE experiment_id = ? AND sequence = ?
                """,
                (non_text_payload, self.manifest.experiment_id, int(row[0])),
            )
            connection.commit()
        finally:
            connection.close()

        with self.assertRaises(LabPersistenceIntegrityError):
            self.store.recover_for_run(second.run_id)

    def test_blob_event_kind_cannot_hide_authoritative_run_identity(self) -> None:
        original = ExperimentRun.create(
            manifest=self.manifest,
            ordinal=1,
            seed=4,
            run_id=_RUN_ID,
        )
        self.store.register_run(original)
        second_manifest = ExperimentManifest.create(
            hypothesis=self.hypothesis,
            procedure="Second experiment for duplicate run admission.",
            experiment_id=_SECOND_EXPERIMENT_ID,
        )
        self.store.register_experiment(self.hypothesis, second_manifest)
        duplicate = ExperimentRun.create(
            manifest=second_manifest,
            ordinal=0,
            seed=5,
            run_id=original.run_id,
        )
        original_sequence = self._latest_run_event_sequence(self.manifest.experiment_id)
        before_target = self._event_count_for(second_manifest.experiment_id)

        self._execute(
            "DELETE FROM lab_registry_runs WHERE run_id = ?",
            (original.run_id,),
        )
        self._execute(
            """
            UPDATE lab_registry_events
            SET kind = ?
            WHERE experiment_id = ? AND sequence = ?
            """,
            (
                sqlite3.Binary(b"run_registered"),
                self.manifest.experiment_id,
                original_sequence,
            ),
        )

        with self.assertRaises(LabPersistenceIntegrityError):
            self.store.register_run(duplicate)
        self.assertEqual(
            self._event_count_for(second_manifest.experiment_id),
            before_target,
        )
        with self.assertRaises(LabPersistenceIntegrityError):
            self.store.recover_for_run(original.run_id)

    def test_known_text_wrong_event_kind_is_validated_before_filtering(self) -> None:
        original = ExperimentRun.create(
            manifest=self.manifest,
            ordinal=1,
            seed=6,
            run_id=_RUN_ID,
        )
        self.store.register_run(original)
        original_sequence = self._latest_run_event_sequence(self.manifest.experiment_id)
        self._execute(
            "DELETE FROM lab_registry_runs WHERE run_id = ?",
            (original.run_id,),
        )
        self._execute(
            """
            UPDATE lab_registry_events
            SET kind = 'metric_registered'
            WHERE experiment_id = ? AND sequence = ?
            """,
            (self.manifest.experiment_id, original_sequence),
        )

        with self.assertRaises(LabPersistenceIntegrityError):
            self.store.recover_for_run(original.run_id)

    def test_chain_head_is_verified_before_valid_kind_filtering(self) -> None:
        original = ExperimentRun.create(
            manifest=self.manifest,
            ordinal=1,
            seed=7,
            run_id=_RUN_ID,
        )
        self.store.register_run(original)
        second_manifest = ExperimentManifest.create(
            hypothesis=self.hypothesis,
            procedure="Second experiment for chain-aware duplicate admission.",
            experiment_id=_SECOND_EXPERIMENT_ID,
        )
        self.store.register_experiment(self.hypothesis, second_manifest)
        duplicate = ExperimentRun.create(
            manifest=second_manifest,
            ordinal=0,
            seed=8,
            run_id=original.run_id,
        )
        before_target = self._event_count_for(second_manifest.experiment_id)

        connection = sqlite3.connect(self.db_path)
        try:
            row = connection.execute(
                """
                SELECT sequence, event_id, created_at, previous_event_digest
                FROM lab_registry_events
                WHERE experiment_id = ? AND kind = 'run_registered'
                ORDER BY sequence DESC
                LIMIT 1
                """,
                (self.manifest.experiment_id,),
            ).fetchone()
            assert row is not None
            sequence = int(row[0])
            replacement_payload = {
                "run_id": original.run_id,
                "run_digest": original.run_digest,
            }
            replacement_base = {
                "schema_version": 1,
                "event_id": str(row[1]),
                "experiment_id": self.manifest.experiment_id,
                "sequence": sequence,
                "created_at": str(row[2]),
                "kind": "run_sealed",
                "payload": replacement_payload,
                "previous_event_digest": row[3],
            }
            replacement_digest = sha256(
                _canonical_json(replacement_base).encode("utf-8")
            ).hexdigest()
            connection.execute(
                "DELETE FROM lab_registry_runs WHERE run_id = ?",
                (original.run_id,),
            )
            connection.execute(
                """
                UPDATE lab_registry_events
                SET kind = 'run_sealed', payload_json = ?, event_digest = ?
                WHERE experiment_id = ? AND sequence = ?
                """,
                (
                    _canonical_json(replacement_payload),
                    replacement_digest,
                    self.manifest.experiment_id,
                    sequence,
                ),
            )
            connection.commit()
        finally:
            connection.close()

        with self.assertRaises(LabPersistenceIntegrityError):
            self.store.register_run(duplicate)
        self.assertEqual(
            self._event_count_for(second_manifest.experiment_id),
            before_target,
        )
        with self.assertRaises(LabPersistenceIntegrityError):
            self.store.recover_for_run(original.run_id)

    def test_blob_experiment_registered_kind_blocks_hypothesis_admission(self) -> None:
        candidate_manifest = ExperimentManifest.create(
            hypothesis=self.hypothesis,
            procedure="A corrupted registration kind must not hide hypothesis ownership.",
            experiment_id=_EXPERIMENT_ID,
        )
        self._execute(
            """
            UPDATE lab_registry_events
            SET kind = ?
            WHERE experiment_id = ? AND sequence = 0
            """,
            (
                sqlite3.Binary(b"experiment_registered"),
                self.manifest.experiment_id,
            ),
        )

        with self.assertRaises(LabPersistenceIntegrityError):
            self.store.register_experiment(self.hypothesis, candidate_manifest)
        self.assertEqual(self._root_count(candidate_manifest.experiment_id), 0)

    def test_blob_metric_name_cannot_bypass_scoped_uniqueness(self) -> None:
        candidate = MetricRecord.create(
            run=self.run,
            name="storage-class-metric",
            value=0.8,
            unit="ratio",
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
                _STALE_METRIC_ID,
                _FOREIGN_EXPERIMENT_ID,
                self.run.run_id,
                self.run.run_digest,
                self.run.manifest_digest,
                "e" * 64,
                sqlite3.Binary(candidate.name.encode("utf-8")),
            ),
        )

        with self.assertRaises(LabPersistenceIntegrityError):
            self.store.register_metric(candidate)
        self.assertEqual(self._event_count(), before)

    def test_blob_artifact_path_cannot_bypass_scoped_uniqueness(self) -> None:
        candidate = ArtifactRecord.create(
            run=self.run,
            logical_path="reports/storage-class.bin",
            size_bytes=5,
            sha256_digest="f" * 64,
            media_type="application/octet-stream",
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
                _STALE_ARTIFACT_ID,
                _FOREIGN_EXPERIMENT_ID,
                self.run.run_id,
                self.run.run_digest,
                self.run.manifest_digest,
                "1" * 64,
                sqlite3.Binary(candidate.logical_path.encode("utf-8")),
            ),
        )

        with self.assertRaises(LabPersistenceIntegrityError):
            self.store.register_artifact(candidate)
        self.assertEqual(self._event_count(), before)

    def test_malformed_text_scoped_metric_run_id_fails_closed(self) -> None:
        candidate = MetricRecord.create(
            run=self.run,
            name="malformed-text-run-metric",
            value=0.9,
            unit="ratio",
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
                _STALE_METRIC_ID,
                _FOREIGN_EXPERIMENT_ID,
                "not-a-uuid",
                self.run.run_digest,
                self.run.manifest_digest,
                "2" * 64,
                candidate.name,
            ),
        )

        with self.assertRaises(LabPersistenceIntegrityError):
            self.store.register_metric(candidate)
        self.assertEqual(self._event_count(), before)

    def test_malformed_text_scoped_artifact_run_id_fails_closed(self) -> None:
        candidate = ArtifactRecord.create(
            run=self.run,
            logical_path="reports/malformed-text-run.bin",
            size_bytes=6,
            sha256_digest="3" * 64,
            media_type="application/octet-stream",
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
                _STALE_ARTIFACT_ID,
                _FOREIGN_EXPERIMENT_ID,
                "not-a-uuid",
                self.run.run_digest,
                self.run.manifest_digest,
                "4" * 64,
                candidate.logical_path,
            ),
        )

        with self.assertRaises(LabPersistenceIntegrityError):
            self.store.register_artifact(candidate)
        self.assertEqual(self._event_count(), before)


if __name__ == "__main__":
    unittest.main()
