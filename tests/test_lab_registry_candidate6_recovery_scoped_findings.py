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


_TARGET_EXPERIMENT_ID = "abcdef12-3456-4abc-8def-abcdef123456"
_TARGET_RUN_ID = "abcdefab-cdef-4abc-8def-abcdefabcdef"
_TARGET_METRIC_ID = "bcdefabc-defa-4bcd-8efa-bcdefabcdefa"
_TARGET_ARTIFACT_ID = "cdefabcd-efab-4cde-8fab-cdefabcdefab"
_FOREIGN_EXPERIMENT_ID = "fedcba98-7654-4321-8765-fedcba987654"
_FOREIGN_RUN_ID = "defabcde-fabc-4def-8abc-defabcdefabc"
_FOREIGN_METRIC_ID = "efabcdef-abcd-4efa-8bcd-efabcdefabcd"
_FOREIGN_ARTIFACT_ID = "fabcdefa-bcde-4fab-8cde-fabcdefabcde"


class DurableLabRegistryCandidate6RecoveryScopedFindingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)

    def _build_registry(
        self,
        case_name: str,
    ) -> tuple[
        SqliteLabRegistry,
        Path,
        ExperimentManifest,
        ExperimentRun,
        MetricRecord,
        ArtifactRecord,
    ]:
        db_path = Path(self.tempdir.name) / f"recovery-scoped-alias-{case_name}.sqlite3"
        store = SqliteLabRegistry(db_path)

        target_hypothesis = Hypothesis.create(
            statement="Experiment recovery rejects foreign scoped identity aliases.",
            falsification_criterion=(
                "A foreign scoped alias can remain invisible to experiment recovery."
            ),
        )
        target_manifest = ExperimentManifest.create(
            hypothesis=target_hypothesis,
            procedure="Recover target scoped evidence after foreign index corruption.",
            experiment_id=_TARGET_EXPERIMENT_ID,
        )
        target_run = ExperimentRun.create(
            manifest=target_manifest,
            ordinal=0,
            seed=1,
            run_id=_TARGET_RUN_ID,
        )
        target_metric = MetricRecord.create(
            run=target_run,
            name="target_metric",
            value=1.0,
            unit="score",
            metric_id=_TARGET_METRIC_ID,
        )
        target_artifact = ArtifactRecord.create(
            run=target_run,
            logical_path="target/result.bin",
            size_bytes=4,
            sha256_digest="a" * 64,
            media_type="application/octet-stream",
            artifact_id=_TARGET_ARTIFACT_ID,
        )
        store.register_experiment(target_hypothesis, target_manifest)
        store.register_run(target_run)
        store.register_metric(target_metric)
        store.register_artifact(target_artifact)

        foreign_hypothesis = Hypothesis.create(
            statement="A separate experiment owns independent scoped evidence.",
            falsification_criterion="Its scoped key is rebound to a target alias.",
        )
        foreign_manifest = ExperimentManifest.create(
            hypothesis=foreign_hypothesis,
            procedure="Provide valid foreign scoped evidence before corruption.",
            experiment_id=_FOREIGN_EXPERIMENT_ID,
        )
        foreign_run = ExperimentRun.create(
            manifest=foreign_manifest,
            ordinal=0,
            seed=2,
            run_id=_FOREIGN_RUN_ID,
        )
        foreign_metric = MetricRecord.create(
            run=foreign_run,
            name="foreign_metric",
            value=2.0,
            unit="score",
            metric_id=_FOREIGN_METRIC_ID,
        )
        foreign_artifact = ArtifactRecord.create(
            run=foreign_run,
            logical_path="foreign/result.bin",
            size_bytes=8,
            sha256_digest="b" * 64,
            media_type="application/octet-stream",
            artifact_id=_FOREIGN_ARTIFACT_ID,
        )
        store.register_experiment(foreign_hypothesis, foreign_manifest)
        store.register_run(foreign_run)
        store.register_metric(foreign_metric)
        store.register_artifact(foreign_artifact)

        return (
            store,
            db_path,
            target_manifest,
            target_run,
            target_metric,
            target_artifact,
        )

    @staticmethod
    def _rebind_scoped_identity(
        db_path: Path,
        *,
        table: str,
        id_column: str,
        record_id: str,
        alias_run_id: str,
        scope_column: str,
        scope_value: str,
    ) -> None:
        connection = sqlite3.connect(db_path)
        try:
            connection.execute(
                f"UPDATE {table} SET run_id = ?, {scope_column} = ? "
                f"WHERE {id_column} = ?",
                (alias_run_id, scope_value, record_id),
            )
            connection.commit()
        finally:
            connection.close()

    def test_foreign_metric_run_aliases_fail_closed_during_recovery(self) -> None:
        aliases = (
            ("uppercase", _TARGET_RUN_ID.upper()),
            ("unhyphenated", _TARGET_RUN_ID.replace("-", "")),
        )
        for alias_label, alias_run_id in aliases:
            with self.subTest(alias=alias_label):
                (
                    store,
                    db_path,
                    target_manifest,
                    _,
                    target_metric,
                    _,
                ) = self._build_registry(f"metric-{alias_label}")
                self._rebind_scoped_identity(
                    db_path,
                    table="lab_registry_metrics",
                    id_column="metric_id",
                    record_id=_FOREIGN_METRIC_ID,
                    alias_run_id=alias_run_id,
                    scope_column="name",
                    scope_value=target_metric.name,
                )

                with self.assertRaises(LabPersistenceIntegrityError):
                    store.recover_experiment(target_manifest.experiment_id)
                with self.assertRaises(LabPersistenceIntegrityError):
                    store.recover_for_metric(target_metric.metric_id)

    def test_foreign_artifact_run_aliases_fail_closed_during_recovery(self) -> None:
        aliases = (
            ("uppercase", _TARGET_RUN_ID.upper()),
            ("unhyphenated", _TARGET_RUN_ID.replace("-", "")),
        )
        for alias_label, alias_run_id in aliases:
            with self.subTest(alias=alias_label):
                (
                    store,
                    db_path,
                    target_manifest,
                    _,
                    _,
                    target_artifact,
                ) = self._build_registry(f"artifact-{alias_label}")
                self._rebind_scoped_identity(
                    db_path,
                    table="lab_registry_artifacts",
                    id_column="artifact_id",
                    record_id=_FOREIGN_ARTIFACT_ID,
                    alias_run_id=alias_run_id,
                    scope_column="logical_path",
                    scope_value=target_artifact.logical_path,
                )

                with self.assertRaises(LabPersistenceIntegrityError):
                    store.recover_experiment(target_manifest.experiment_id)
                with self.assertRaises(LabPersistenceIntegrityError):
                    store.recover_for_artifact(target_artifact.artifact_id)

    def test_registration_discovery_builds_experiment_root_snapshot_once(self) -> None:
        class CountingSqliteLabRegistry(SqliteLabRegistry):
            root_snapshot_builds = 0

            @classmethod
            def _experiment_root_snapshot(cls, connection, experiment_ids):
                cls.root_snapshot_builds += 1
                return super()._experiment_root_snapshot(connection, experiment_ids)

        db_path = Path(self.tempdir.name) / "registration-root-snapshot-once.sqlite3"
        store = CountingSqliteLabRegistry(db_path)
        manifests: list[ExperimentManifest] = []
        for index in range(4):
            hypothesis = Hypothesis.create(
                statement=f"Independent root snapshot experiment {index}.",
                falsification_criterion="Registration discovery rescans experiment roots.",
            )
            manifest = ExperimentManifest.create(
                hypothesis=hypothesis,
                procedure=f"Persist independent chronology {index}.",
            )
            store.register_experiment(hypothesis, manifest)
            manifests.append(manifest)

        CountingSqliteLabRegistry.root_snapshot_builds = 0
        recovery = store.recover_experiment(manifests[0].experiment_id)

        self.assertEqual(recovery.experiment_id, manifests[0].experiment_id)
        self.assertEqual(CountingSqliteLabRegistry.root_snapshot_builds, 1)


if __name__ == "__main__":
    unittest.main()
