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
    MetricRecord,
    SqliteLabRegistry,
)


_ORPHAN_EXPERIMENT_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


class DurableLabRegistryFinalReviewFindingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.db_path = Path(self.tempdir.name) / "lab-registry.sqlite3"
        self.hypothesis = Hypothesis.create(
            statement="Final registry review findings fail closed.",
            falsification_criterion="Persisted corruption is misclassified or leaks a raw error.",
        )
        self.manifest = ExperimentManifest.create(
            hypothesis=self.hypothesis,
            procedure="Exercise final persistence review findings.",
        )
        self.run = ExperimentRun.create(
            manifest=self.manifest,
            ordinal=0,
            seed=31,
        )
        self.store = SqliteLabRegistry(self.db_path)
        self.store.register_experiment(self.hypothesis, self.manifest)
        self.store.register_run(self.run)

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
        self.store.register_experiment(self.hypothesis, owner)
        return owner

    @staticmethod
    def _execute_on(
        db_path: Path,
        sql: str,
        parameters: tuple[object, ...] = (),
    ) -> None:
        connection = sqlite3.connect(db_path)
        try:
            connection.execute(sql, parameters)
            connection.commit()
        finally:
            connection.close()

    def test_stale_foreign_metric_scoped_key_is_integrity_failure(self) -> None:
        owner = self._register_empty_owner("Own only a stale metric scoped index row.")
        candidate = MetricRecord.create(
            run=self.run,
            name="scoped_score",
            value=0.75,
        )
        stale = MetricRecord.create(
            run=self.run,
            name=candidate.name,
            value=0.25,
        )
        before = self.store.recover_experiment(self.manifest.experiment_id)
        self._execute(
            """
            INSERT INTO lab_registry_metrics(
                metric_id, experiment_id, run_id, run_digest,
                manifest_digest, metric_digest, name
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                stale.metric_id,
                owner.experiment_id,
                self.run.run_id,
                stale.run_digest,
                owner.manifest_digest,
                stale.metric_digest,
                stale.name,
            ),
        )

        with self.assertRaises(LabPersistenceIntegrityError):
            self.store.register_metric(candidate)

        after = self.store.recover_experiment(self.manifest.experiment_id)
        self.assertEqual(after.events, before.events)
        self.assertNotIn(candidate.metric_id, after.run(self.run.run_id).metrics)

    def test_stale_foreign_artifact_scoped_key_is_integrity_failure(self) -> None:
        owner = self._register_empty_owner("Own only a stale artifact scoped index row.")
        candidate = ArtifactRecord.create(
            run=self.run,
            logical_path="results/scoped.json",
            size_bytes=12,
            sha256_digest="a" * 64,
            media_type="application/json",
        )
        stale = ArtifactRecord.create(
            run=self.run,
            logical_path=candidate.logical_path,
            size_bytes=13,
            sha256_digest="b" * 64,
            media_type="application/json",
        )
        before = self.store.recover_experiment(self.manifest.experiment_id)
        self._execute(
            """
            INSERT INTO lab_registry_artifacts(
                artifact_id, experiment_id, run_id, run_digest,
                manifest_digest, artifact_digest, logical_path
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                stale.artifact_id,
                owner.experiment_id,
                self.run.run_id,
                stale.run_digest,
                owner.manifest_digest,
                stale.artifact_digest,
                stale.logical_path,
            ),
        )

        with self.assertRaises(LabPersistenceIntegrityError):
            self.store.register_artifact(candidate)

        after = self.store.recover_experiment(self.manifest.experiment_id)
        self.assertEqual(after.events, before.events)
        self.assertNotIn(candidate.artifact_id, after.run(self.run.run_id).artifacts)

    def test_unhyphenated_persisted_run_uuid_cannot_hide_missing_index_corruption(self) -> None:
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
                self.run.run_id.replace("-", ""),
                self.manifest.experiment_id,
            ),
        )

        with self.assertRaises(LabPersistenceIntegrityError):
            SqliteLabRegistry(self.db_path).recover_for_run(self.run.run_id)

    def test_unicode_escaped_run_uuid_cannot_hide_missing_index_corruption(self) -> None:
        self._execute(
            "DELETE FROM lab_registry_runs WHERE run_id = ?",
            (self.run.run_id,),
        )
        first_character = self.run.run_id[0]
        escaped_run_id = f"\\u{ord(first_character):04x}{self.run.run_id[1:]}"
        self._execute(
            """
            UPDATE lab_registry_events
            SET payload_json = replace(payload_json, ?, ?)
            WHERE experiment_id = ? AND sequence = 1
            """,
            (
                self.run.run_id,
                escaped_run_id,
                self.manifest.experiment_id,
            ),
        )

        with self.assertRaises(LabPersistenceIntegrityError):
            SqliteLabRegistry(self.db_path).recover_for_run(self.run.run_id)

    def test_missing_navigation_event_orphan_owner_is_integrity_failure(self) -> None:
        self._execute(
            "DELETE FROM lab_registry_runs WHERE run_id = ?",
            (self.run.run_id,),
        )
        self._execute(
            """
            UPDATE lab_registry_events
            SET experiment_id = ?
            WHERE experiment_id = ? AND sequence = 1
            """,
            (
                _ORPHAN_EXPERIMENT_ID,
                self.manifest.experiment_id,
            ),
        )

        with self.assertRaises(LabPersistenceIntegrityError):
            SqliteLabRegistry(self.db_path).recover_for_run(self.run.run_id)

    def test_authoritative_global_identity_cannot_be_reused_when_index_is_missing(self) -> None:
        for record_type in ("run", "metric", "artifact"):
            with self.subTest(record_type=record_type):
                db_path = Path(self.tempdir.name) / f"authoritative-{record_type}.sqlite3"
                store = SqliteLabRegistry(db_path)
                source_manifest = ExperimentManifest.create(
                    hypothesis=self.hypothesis,
                    procedure=f"Own the original {record_type} identity.",
                )
                target_manifest = ExperimentManifest.create(
                    hypothesis=self.hypothesis,
                    procedure=f"Attempt to reuse the {record_type} identity.",
                )
                store.register_experiment(self.hypothesis, source_manifest)
                store.register_experiment(self.hypothesis, target_manifest)
                source_run = ExperimentRun.create(
                    manifest=source_manifest,
                    ordinal=0,
                    seed=40,
                )
                target_run = ExperimentRun.create(
                    manifest=target_manifest,
                    ordinal=0,
                    seed=41,
                    run_id=source_run.run_id if record_type == "run" else None,
                )
                store.register_run(source_run)
                if record_type != "run":
                    store.register_run(target_run)

                if record_type == "run":
                    self._execute_on(
                        db_path,
                        "DELETE FROM lab_registry_runs WHERE run_id = ?",
                        (source_run.run_id,),
                    )
                    candidate = target_run
                    register = lambda: store.register_run(candidate)
                elif record_type == "metric":
                    source = MetricRecord.create(
                        run=source_run,
                        name="source_metric",
                        value=0.5,
                    )
                    store.register_metric(source)
                    self._execute_on(
                        db_path,
                        "DELETE FROM lab_registry_metrics WHERE metric_id = ?",
                        (source.metric_id,),
                    )
                    candidate = MetricRecord.create(
                        run=target_run,
                        name="target_metric",
                        value=0.75,
                        metric_id=source.metric_id,
                    )
                    register = lambda: store.register_metric(candidate)
                else:
                    source = ArtifactRecord.create(
                        run=source_run,
                        logical_path="results/source.json",
                        size_bytes=7,
                        sha256_digest="e" * 64,
                        media_type="application/json",
                    )
                    store.register_artifact(source)
                    self._execute_on(
                        db_path,
                        "DELETE FROM lab_registry_artifacts WHERE artifact_id = ?",
                        (source.artifact_id,),
                    )
                    candidate = ArtifactRecord.create(
                        run=target_run,
                        logical_path="results/target.json",
                        size_bytes=8,
                        sha256_digest="f" * 64,
                        media_type="application/json",
                        artifact_id=source.artifact_id,
                    )
                    register = lambda: store.register_artifact(candidate)

                before = store.recover_experiment(target_manifest.experiment_id)
                with self.assertRaises(LabPersistenceIntegrityError):
                    register()
                after = store.recover_experiment(target_manifest.experiment_id)
                self.assertEqual(after.events, before.events)

    def test_deeply_nested_persisted_json_stays_in_integrity_error_taxonomy(self) -> None:
        deeply_nested = "[" * 10_000 + "0" + "]" * 10_000
        self._execute(
            """
            UPDATE lab_registry_events
            SET payload_json = ?
            WHERE experiment_id = ? AND sequence = 1
            """,
            (deeply_nested, self.manifest.experiment_id),
        )

        with self.assertRaises(LabPersistenceIntegrityError):
            SqliteLabRegistry(self.db_path).recover_experiment(
                self.manifest.experiment_id
            )

    def test_orphaned_navigation_owners_are_integrity_failures(self) -> None:
        for record_type in ("run", "metric", "artifact"):
            with self.subTest(record_type=record_type):
                db_path = Path(self.tempdir.name) / f"orphan-{record_type}.sqlite3"
                store = SqliteLabRegistry(db_path)
                store.register_experiment(self.hypothesis, self.manifest)
                store.register_run(self.run)

                if record_type == "run":
                    table = "lab_registry_runs"
                    id_column = "run_id"
                    record_id = self.run.run_id
                    recover = lambda: SqliteLabRegistry(db_path).recover_for_run(record_id)
                elif record_type == "metric":
                    metric = MetricRecord.create(
                        run=self.run,
                        name="orphan_owner_metric",
                        value=0.5,
                    )
                    store.register_metric(metric)
                    table = "lab_registry_metrics"
                    id_column = "metric_id"
                    record_id = metric.metric_id
                    recover = lambda: SqliteLabRegistry(db_path).recover_for_metric(record_id)
                else:
                    artifact = ArtifactRecord.create(
                        run=self.run,
                        logical_path="results/orphan-owner.json",
                        size_bytes=5,
                        sha256_digest="c" * 64,
                        media_type="application/json",
                    )
                    store.register_artifact(artifact)
                    table = "lab_registry_artifacts"
                    id_column = "artifact_id"
                    record_id = artifact.artifact_id
                    recover = lambda: SqliteLabRegistry(db_path).recover_for_artifact(record_id)

                self._execute_on(
                    db_path,
                    f"UPDATE {table} SET experiment_id = ? WHERE {id_column} = ?",
                    (_ORPHAN_EXPERIMENT_ID, record_id),
                )

                with self.assertRaises(LabPersistenceIntegrityError):
                    recover()

    def test_noncanonical_linked_run_uuid_is_rejected_before_lookup(self) -> None:
        aliased_run = ExperimentRun.create(
            manifest=self.manifest,
            ordinal=99,
            seed=32,
            run_id=self.run.run_id.upper(),
        )
        metric = MetricRecord.create(
            run=aliased_run,
            name="caller_alias_metric",
            value=0.25,
        )
        artifact = ArtifactRecord.create(
            run=aliased_run,
            logical_path="results/caller-alias.json",
            size_bytes=6,
            sha256_digest="d" * 64,
            media_type="application/json",
        )

        with self.assertRaisesRegex(
            InvalidLabRecordError,
            "canonical lowercase hyphenated UUID",
        ):
            self.store.register_metric(metric)
        with self.assertRaisesRegex(
            InvalidLabRecordError,
            "canonical lowercase hyphenated UUID",
        ):
            self.store.register_artifact(artifact)

    def test_nonascii_head_digest_is_domain_integrity_failure(self) -> None:
        self._execute(
            """
            UPDATE lab_registry_experiments
            SET head_event_digest = ?
            WHERE experiment_id = ?
            """,
            ("é" * 64, self.manifest.experiment_id),
        )

        with self.assertRaises(LabPersistenceIntegrityError):
            SqliteLabRegistry(self.db_path).recover_experiment(self.manifest.experiment_id)

    def test_nonascii_root_and_hypothesis_index_digests_fail_closed(self) -> None:
        corruptions = (
            (
                "UPDATE lab_registry_experiments SET hypothesis_digest = ? WHERE experiment_id = ?",
                self.manifest.experiment_id,
            ),
            (
                "UPDATE lab_registry_experiments SET manifest_digest = ? WHERE experiment_id = ?",
                self.manifest.experiment_id,
            ),
            (
                "UPDATE lab_registry_hypotheses SET hypothesis_digest = ? WHERE hypothesis_id = ?",
                self.hypothesis.hypothesis_id,
            ),
        )

        for index, (sql, identity) in enumerate(corruptions):
            with self.subTest(index=index):
                db_path = Path(self.tempdir.name) / f"digest-{index}.sqlite3"
                store = SqliteLabRegistry(db_path)
                store.register_experiment(self.hypothesis, self.manifest)
                store.register_run(self.run)
                connection = sqlite3.connect(db_path)
                try:
                    connection.execute(sql, ("é" * 64, identity))
                    connection.commit()
                finally:
                    connection.close()

                with self.assertRaises(LabPersistenceIntegrityError):
                    SqliteLabRegistry(db_path).recover_experiment(
                        self.manifest.experiment_id
                    )


if __name__ == "__main__":
    unittest.main()
