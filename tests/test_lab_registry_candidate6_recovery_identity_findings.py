from __future__ import annotations

import sqlite3
import tempfile
import unittest
from hashlib import sha256
from pathlib import Path

from codexia_manual_agent.lab import (
    LAB_REGISTRY_EVENT_SCHEMA_VERSION,
    ArtifactRecord,
    ExperimentManifest,
    ExperimentRun,
    Hypothesis,
    LabPersistenceIntegrityError,
    MetricRecord,
    SqliteLabRegistry,
    canonical_registry_json,
)


_TARGET_EXPERIMENT_ID = "abcdef12-3456-4abc-8def-abcdef123456"
_TARGET_RUN_ID = "abcdefab-cdef-4abc-8def-abcdefabcdef"
_TARGET_METRIC_ID = "bcdefabc-defa-4bcd-8efa-bcdefabcdefa"
_TARGET_ARTIFACT_ID = "cdefabcd-efab-4cde-8fab-cdefabcdefab"

_FOREIGN_EXPERIMENT_ID = "fedcba98-7654-4321-8765-fedcba987654"
_FOREIGN_RUN_ID = "defabcde-fabc-4def-8abc-defabcdefabc"
_FOREIGN_METRIC_ID = "efabcdef-abcd-4efa-8bcd-efabcdefabcd"
_FOREIGN_ARTIFACT_ID = "fabcdefa-bcde-4fab-8cde-fabcdefabcde"
_REBOUND_EXPERIMENT_ID = "01234567-89ab-4cde-8fab-0123456789ab"


class DurableLabRegistryCandidate6RecoveryIdentityFindingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)

    def _build_registry(
        self,
        case_name: str,
    ) -> tuple[SqliteLabRegistry, Path, ExperimentManifest]:
        db_path = Path(self.tempdir.name) / f"recovery-global-alias-{case_name}.sqlite3"
        store = SqliteLabRegistry(db_path)

        target_hypothesis = Hypothesis.create(
            statement="Experiment recovery rejects foreign semantic global-ID aliases.",
            falsification_criterion=(
                "A foreign derived alias can remain invisible to experiment recovery."
            ),
        )
        target_manifest = ExperimentManifest.create(
            hypothesis=target_hypothesis,
            procedure="Recover the exact target experiment after foreign index corruption.",
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
            statement="A separate experiment owns independent derived identities.",
            falsification_criterion="Its derived identity is rebound to a target alias.",
        )
        foreign_manifest = ExperimentManifest.create(
            hypothesis=foreign_hypothesis,
            procedure="Provide a valid foreign chronology before out-of-band corruption.",
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

        return store, db_path, target_manifest

    @staticmethod
    def _rebind_primary_identity(
        db_path: Path,
        *,
        table: str,
        id_column: str,
        old_id: str,
        alias_id: str,
    ) -> None:
        connection = sqlite3.connect(db_path)
        try:
            connection.execute(
                f"UPDATE {table} SET {id_column} = ? WHERE {id_column} = ?",
                (alias_id, old_id),
            )
            connection.commit()
        finally:
            connection.close()

    @staticmethod
    def _rebind_foreign_hypothesis_owner(
        db_path: Path,
        *,
        hypothesis_id: str,
    ) -> None:
        connection = sqlite3.connect(db_path)
        try:
            connection.execute(
                """
                UPDATE lab_registry_experiments
                SET hypothesis_id = ?
                WHERE experiment_id = ?
                """,
                (hypothesis_id, _FOREIGN_EXPERIMENT_ID),
            )
            connection.commit()
        finally:
            connection.close()

    @staticmethod
    def _event_digest(row: sqlite3.Row, payload: dict[str, object]) -> str:
        base = {
            "schema_version": LAB_REGISTRY_EVENT_SCHEMA_VERSION,
            "event_id": row["event_id"],
            "experiment_id": row["experiment_id"],
            "sequence": row["sequence"],
            "created_at": row["created_at"],
            "kind": row["kind"],
            "payload": payload,
            "previous_event_digest": row["previous_event_digest"],
        }
        return sha256(canonical_registry_json(base).encode("utf-8")).hexdigest()

    @classmethod
    def _rewrite_tail_registration(
        cls,
        db_path: Path,
        *,
        experiment_id: str,
        sequence: int,
        payload: dict[str, object],
    ) -> None:
        connection = sqlite3.connect(db_path)
        connection.row_factory = sqlite3.Row
        try:
            row = connection.execute(
                """
                SELECT event_id, experiment_id, sequence, created_at, kind,
                       previous_event_digest
                FROM lab_registry_events
                WHERE experiment_id = ? AND sequence = ?
                """,
                (experiment_id, sequence),
            ).fetchone()
            assert row is not None
            head_sequence = connection.execute(
                "SELECT head_sequence FROM lab_registry_experiments WHERE experiment_id = ?",
                (experiment_id,),
            ).fetchone()
            assert head_sequence is not None
            assert head_sequence[0] == sequence

            payload_json = canonical_registry_json(payload)
            event_digest = cls._event_digest(row, payload)
            connection.execute(
                """
                UPDATE lab_registry_events
                SET payload_json = ?, event_digest = ?
                WHERE experiment_id = ? AND sequence = ?
                """,
                (payload_json, event_digest, experiment_id, sequence),
            )
            connection.execute(
                """
                UPDATE lab_registry_experiments
                SET head_event_digest = ?
                WHERE experiment_id = ?
                """,
                (event_digest, experiment_id),
            )
            connection.commit()
        finally:
            connection.close()

    @staticmethod
    def _rewrite_foreign_shared_hypothesis(
        db_path: Path,
        *,
        hypothesis: Hypothesis,
        manifest: ExperimentManifest,
    ) -> None:
        connection = sqlite3.connect(db_path)
        connection.row_factory = sqlite3.Row
        try:
            row = connection.execute(
                """
                SELECT event_id, experiment_id, sequence, created_at, kind,
                       previous_event_digest
                FROM lab_registry_events
                WHERE experiment_id = ? AND sequence = 0
                """,
                (_FOREIGN_EXPERIMENT_ID,),
            ).fetchone()
            assert row is not None

            payload = {
                "hypothesis": hypothesis.to_dict(),
                "manifest": manifest.to_dict(),
            }
            payload_json = canonical_registry_json(payload)
            event_digest = DurableLabRegistryCandidate6RecoveryIdentityFindingTests._event_digest(
                row,
                payload,
            )

            connection.execute(
                """
                UPDATE lab_registry_events
                SET payload_json = ?, event_digest = ?
                WHERE experiment_id = ? AND sequence = 0
                """,
                (payload_json, event_digest, _FOREIGN_EXPERIMENT_ID),
            )
            connection.execute(
                """
                UPDATE lab_registry_experiments
                SET hypothesis_id = ?, hypothesis_digest = ?,
                    manifest_digest = ?, head_event_digest = ?
                WHERE experiment_id = ?
                """,
                (
                    hypothesis.hypothesis_id,
                    hypothesis.hypothesis_digest,
                    manifest.manifest_digest,
                    event_digest,
                    _FOREIGN_EXPERIMENT_ID,
                ),
            )
            connection.commit()
        finally:
            connection.close()

    def _assert_foreign_alias_fails_closed(
        self,
        *,
        case_name: str,
        table: str,
        id_column: str,
        foreign_id: str,
        target_id: str,
        alias_id: str,
    ) -> None:
        self.assertNotEqual(alias_id, target_id)
        store, db_path, target_manifest = self._build_registry(case_name)
        self._rebind_primary_identity(
            db_path,
            table=table,
            id_column=id_column,
            old_id=foreign_id,
            alias_id=alias_id,
        )

        with self.assertRaises(LabPersistenceIntegrityError):
            store.recover_experiment(target_manifest.experiment_id)

    def test_foreign_run_aliases_fail_closed_during_experiment_recovery(self) -> None:
        aliases = (
            ("uppercase", _TARGET_RUN_ID.upper()),
            ("unhyphenated", _TARGET_RUN_ID.replace("-", "")),
        )
        for alias_label, alias_id in aliases:
            with self.subTest(alias=alias_label):
                self._assert_foreign_alias_fails_closed(
                    case_name=f"run-{alias_label}",
                    table="lab_registry_runs",
                    id_column="run_id",
                    foreign_id=_FOREIGN_RUN_ID,
                    target_id=_TARGET_RUN_ID,
                    alias_id=alias_id,
                )

    def test_foreign_metric_aliases_fail_closed_during_experiment_recovery(self) -> None:
        aliases = (
            ("uppercase", _TARGET_METRIC_ID.upper()),
            ("unhyphenated", _TARGET_METRIC_ID.replace("-", "")),
        )
        for alias_label, alias_id in aliases:
            with self.subTest(alias=alias_label):
                self._assert_foreign_alias_fails_closed(
                    case_name=f"metric-{alias_label}",
                    table="lab_registry_metrics",
                    id_column="metric_id",
                    foreign_id=_FOREIGN_METRIC_ID,
                    target_id=_TARGET_METRIC_ID,
                    alias_id=alias_id,
                )

    def test_foreign_artifact_aliases_fail_closed_during_experiment_recovery(self) -> None:
        aliases = (
            ("uppercase", _TARGET_ARTIFACT_ID.upper()),
            ("unhyphenated", _TARGET_ARTIFACT_ID.replace("-", "")),
        )
        for alias_label, alias_id in aliases:
            with self.subTest(alias=alias_label):
                self._assert_foreign_alias_fails_closed(
                    case_name=f"artifact-{alias_label}",
                    table="lab_registry_artifacts",
                    id_column="artifact_id",
                    foreign_id=_FOREIGN_ARTIFACT_ID,
                    target_id=_TARGET_ARTIFACT_ID,
                    alias_id=alias_id,
                )

    def test_foreign_hypothesis_owner_rebinds_fail_closed_during_recovery(self) -> None:
        for case_name, rebind in (
            ("exact", lambda hypothesis_id: hypothesis_id),
            ("unhyphenated", lambda hypothesis_id: hypothesis_id.replace("-", "")),
        ):
            with self.subTest(rebind=case_name):
                store, db_path, target_manifest = self._build_registry(
                    f"hypothesis-owner-{case_name}"
                )
                rebound_id = rebind(target_manifest.hypothesis_id)
                if case_name != "exact":
                    self.assertNotEqual(rebound_id, target_manifest.hypothesis_id)
                self._rebind_foreign_hypothesis_owner(
                    db_path,
                    hypothesis_id=rebound_id,
                )

                with self.assertRaises(LabPersistenceIntegrityError):
                    store.recover_experiment(target_manifest.experiment_id)
                with self.assertRaises(LabPersistenceIntegrityError):
                    store.recover_for_run(_TARGET_RUN_ID)

    def test_foreign_shared_hypothesis_digest_drift_fails_closed_during_recovery(
        self,
    ) -> None:
        db_path = Path(self.tempdir.name) / "recovery-shared-hypothesis-digest.sqlite3"
        store = SqliteLabRegistry(db_path)

        shared_hypothesis = Hypothesis.create(
            statement="Two experiments share one exact durable hypothesis.",
            falsification_criterion="Either owner binds another hypothesis digest.",
        )
        target_manifest = ExperimentManifest.create(
            hypothesis=shared_hypothesis,
            procedure="Recover the untouched owner after foreign digest drift.",
            experiment_id=_TARGET_EXPERIMENT_ID,
        )
        target_run = ExperimentRun.create(
            manifest=target_manifest,
            ordinal=0,
            seed=11,
            run_id=_TARGET_RUN_ID,
        )
        foreign_manifest = ExperimentManifest.create(
            hypothesis=shared_hypothesis,
            procedure="Provide a second exact owner before coherent corruption.",
            experiment_id=_FOREIGN_EXPERIMENT_ID,
        )
        store.register_experiment(shared_hypothesis, target_manifest)
        store.register_run(target_run)
        store.register_experiment(shared_hypothesis, foreign_manifest)

        altered_hypothesis = Hypothesis.create(
            statement="The foreign owner was coherently rebound to another digest.",
            falsification_criterion="Recovery accepts globally inconsistent ownership.",
            hypothesis_id=shared_hypothesis.hypothesis_id,
        )
        self.assertNotEqual(
            altered_hypothesis.hypothesis_digest,
            shared_hypothesis.hypothesis_digest,
        )
        altered_manifest = ExperimentManifest.create(
            hypothesis=altered_hypothesis,
            procedure="Coherently rewrite the foreign sequence-zero registration.",
            experiment_id=_FOREIGN_EXPERIMENT_ID,
        )
        self._rewrite_foreign_shared_hypothesis(
            db_path,
            hypothesis=altered_hypothesis,
            manifest=altered_manifest,
        )

        with self.assertRaises(LabPersistenceIntegrityError):
            store.recover_experiment(target_manifest.experiment_id)
        with self.assertRaises(LabPersistenceIntegrityError):
            store.recover_for_run(target_run.run_id)

    def test_foreign_shared_hypothesis_owner_semantic_replay_fails_closed(
        self,
    ) -> None:
        db_path = Path(self.tempdir.name) / "recovery-shared-hypothesis-owner-replay.sqlite3"
        store = SqliteLabRegistry(db_path)

        shared_hypothesis = Hypothesis.create(
            statement="Every authoritative hypothesis owner remains semantically replayable.",
            falsification_criterion=(
                "A foreign owner can preserve hypothesis identity while breaking root binding."
            ),
        )
        target_manifest = ExperimentManifest.create(
            hypothesis=shared_hypothesis,
            procedure="Recover the healthy owner after foreign semantic corruption.",
            experiment_id=_TARGET_EXPERIMENT_ID,
        )
        target_run = ExperimentRun.create(
            manifest=target_manifest,
            ordinal=0,
            seed=12,
            run_id=_TARGET_RUN_ID,
        )
        foreign_manifest = ExperimentManifest.create(
            hypothesis=shared_hypothesis,
            procedure="Provide a second exact owner before semantic corruption.",
            experiment_id=_FOREIGN_EXPERIMENT_ID,
        )
        store.register_experiment(shared_hypothesis, target_manifest)
        store.register_run(target_run)
        store.register_experiment(shared_hypothesis, foreign_manifest)

        rebound_manifest = ExperimentManifest.create(
            hypothesis=shared_hypothesis,
            procedure="Bind the foreign registration payload to another experiment root.",
            experiment_id=_REBOUND_EXPERIMENT_ID,
        )
        self.assertNotEqual(rebound_manifest.experiment_id, _FOREIGN_EXPERIMENT_ID)
        self._rewrite_foreign_shared_hypothesis(
            db_path,
            hypothesis=shared_hypothesis,
            manifest=rebound_manifest,
        )

        with self.assertRaises(LabPersistenceIntegrityError):
            store.recover_experiment(target_manifest.experiment_id)
        with self.assertRaises(LabPersistenceIntegrityError):
            store.recover_for_run(target_run.run_id)

    def test_foreign_authoritative_record_id_reuse_fails_closed_during_recovery(
        self,
    ) -> None:
        cases = ("run", "metric", "artifact")
        for record_type in cases:
            with self.subTest(record_type=record_type):
                db_path = (
                    Path(self.tempdir.name)
                    / f"recovery-authoritative-{record_type}-reuse.sqlite3"
                )
                store = SqliteLabRegistry(db_path)

                target_hypothesis = Hypothesis.create(
                    statement=f"Target recovery owns one durable {record_type} identity.",
                    falsification_criterion=(
                        "Another authoritative chronology reuses the same global identity."
                    ),
                )
                target_manifest = ExperimentManifest.create(
                    hypothesis=target_hypothesis,
                    procedure="Recover target state after foreign chronology corruption.",
                    experiment_id=_TARGET_EXPERIMENT_ID,
                )
                target_run = ExperimentRun.create(
                    manifest=target_manifest,
                    ordinal=0,
                    seed=21,
                    run_id=_TARGET_RUN_ID,
                )
                store.register_experiment(target_hypothesis, target_manifest)
                store.register_run(target_run)

                target_record_id = _TARGET_RUN_ID
                recover = lambda: store.recover_for_run(_TARGET_RUN_ID)
                if record_type == "metric":
                    target_metric = MetricRecord.create(
                        run=target_run,
                        name="target_metric",
                        value=1.0,
                        unit="score",
                        metric_id=_TARGET_METRIC_ID,
                    )
                    store.register_metric(target_metric)
                    target_record_id = target_metric.metric_id
                    recover = lambda: store.recover_for_metric(_TARGET_METRIC_ID)
                elif record_type == "artifact":
                    target_artifact = ArtifactRecord.create(
                        run=target_run,
                        logical_path="target/result.bin",
                        size_bytes=4,
                        sha256_digest="c" * 64,
                        media_type="application/octet-stream",
                        artifact_id=_TARGET_ARTIFACT_ID,
                    )
                    store.register_artifact(target_artifact)
                    target_record_id = target_artifact.artifact_id
                    recover = lambda: store.recover_for_artifact(_TARGET_ARTIFACT_ID)

                foreign_hypothesis = Hypothesis.create(
                    statement=f"Foreign chronology starts with an independent {record_type}.",
                    falsification_criterion="Its tail registration is coherently rebound.",
                )
                foreign_manifest = ExperimentManifest.create(
                    hypothesis=foreign_hypothesis,
                    procedure="Provide the foreign tail registration.",
                    experiment_id=_FOREIGN_EXPERIMENT_ID,
                )
                foreign_run = ExperimentRun.create(
                    manifest=foreign_manifest,
                    ordinal=0,
                    seed=22,
                    run_id=_FOREIGN_RUN_ID,
                )
                store.register_experiment(foreign_hypothesis, foreign_manifest)
                store.register_run(foreign_run)

                if record_type == "run":
                    rewritten = ExperimentRun.create(
                        manifest=foreign_manifest,
                        ordinal=0,
                        seed=22,
                        run_id=target_record_id,
                    )
                    sequence = 1
                    payload = {"run": rewritten.to_dict()}
                elif record_type == "metric":
                    foreign_metric = MetricRecord.create(
                        run=foreign_run,
                        name="foreign_metric",
                        value=2.0,
                        unit="score",
                        metric_id=_FOREIGN_METRIC_ID,
                    )
                    store.register_metric(foreign_metric)
                    rewritten = MetricRecord.create(
                        run=foreign_run,
                        name="foreign_metric",
                        value=2.0,
                        unit="score",
                        metric_id=target_record_id,
                    )
                    sequence = 2
                    payload = {"metric": rewritten.to_dict()}
                else:
                    foreign_artifact = ArtifactRecord.create(
                        run=foreign_run,
                        logical_path="foreign/result.bin",
                        size_bytes=8,
                        sha256_digest="d" * 64,
                        media_type="application/octet-stream",
                        artifact_id=_FOREIGN_ARTIFACT_ID,
                    )
                    store.register_artifact(foreign_artifact)
                    rewritten = ArtifactRecord.create(
                        run=foreign_run,
                        logical_path="foreign/result.bin",
                        size_bytes=8,
                        sha256_digest="d" * 64,
                        media_type="application/octet-stream",
                        artifact_id=target_record_id,
                    )
                    sequence = 2
                    payload = {"artifact": rewritten.to_dict()}

                self._rewrite_tail_registration(
                    db_path,
                    experiment_id=_FOREIGN_EXPERIMENT_ID,
                    sequence=sequence,
                    payload=payload,
                )

                with self.assertRaises(LabPersistenceIntegrityError):
                    store.recover_experiment(target_manifest.experiment_id)
                with self.assertRaises(LabPersistenceIntegrityError):
                    recover()

    def test_foreign_owner_root_manifest_drift_fails_closed_during_recovery(
        self,
    ) -> None:
        db_path = Path(self.tempdir.name) / "recovery-shared-hypothesis-root-manifest.sqlite3"
        store = SqliteLabRegistry(db_path)

        shared_hypothesis = Hypothesis.create(
            statement="Every shared-hypothesis owner root corroborates its chronology.",
            falsification_criterion=(
                "A foreign root manifest digest can drift while its chronology stays healthy."
            ),
        )
        target_manifest = ExperimentManifest.create(
            hypothesis=shared_hypothesis,
            procedure="Recover the healthy owner after foreign root-only corruption.",
            experiment_id=_TARGET_EXPERIMENT_ID,
        )
        target_run = ExperimentRun.create(
            manifest=target_manifest,
            ordinal=0,
            seed=31,
            run_id=_TARGET_RUN_ID,
        )
        foreign_manifest = ExperimentManifest.create(
            hypothesis=shared_hypothesis,
            procedure="Remain semantically healthy while root metadata is corrupted.",
            experiment_id=_FOREIGN_EXPERIMENT_ID,
        )
        store.register_experiment(shared_hypothesis, target_manifest)
        store.register_run(target_run)
        store.register_experiment(shared_hypothesis, foreign_manifest)

        corrupt_manifest_digest = "0" * 64
        self.assertNotEqual(corrupt_manifest_digest, foreign_manifest.manifest_digest)
        connection = sqlite3.connect(db_path)
        try:
            connection.execute(
                """
                UPDATE lab_registry_experiments
                SET manifest_digest = ?
                WHERE experiment_id = ?
                """,
                (corrupt_manifest_digest, _FOREIGN_EXPERIMENT_ID),
            )
            connection.commit()
        finally:
            connection.close()

        with self.assertRaises(LabPersistenceIntegrityError):
            store.recover_experiment(target_manifest.experiment_id)
        with self.assertRaises(LabPersistenceIntegrityError):
            store.recover_for_run(target_run.run_id)

    def test_recovery_builds_registration_discovery_once_per_load(self) -> None:
        class CountingSqliteLabRegistry(SqliteLabRegistry):
            discovery_builds = 0

            @classmethod
            def _registration_discovery_snapshot(cls, connection):
                cls.discovery_builds += 1
                return super()._registration_discovery_snapshot(connection)

        db_path = Path(self.tempdir.name) / "recovery-single-discovery.sqlite3"
        store = CountingSqliteLabRegistry(db_path)
        hypothesis = Hypothesis.create(
            statement="Recovery validates authoritative registration discovery once.",
            falsification_criterion=(
                "Recovery rebuilds the full registration snapshot per durable record."
            ),
        )
        manifest = ExperimentManifest.create(
            hypothesis=hypothesis,
            procedure="Register several records, then recover one experiment.",
            experiment_id=_TARGET_EXPERIMENT_ID,
        )
        run = ExperimentRun.create(
            manifest=manifest,
            ordinal=0,
            seed=41,
            run_id=_TARGET_RUN_ID,
        )
        store.register_experiment(hypothesis, manifest)
        store.register_run(run)
        for index in range(3):
            store.register_metric(
                MetricRecord.create(
                    run=run,
                    name=f"metric_{index}",
                    value=float(index),
                    unit="score",
                )
            )
            store.register_artifact(
                ArtifactRecord.create(
                    run=run,
                    logical_path=f"artifact/{index}.bin",
                    size_bytes=index + 1,
                    sha256_digest=f"{index + 1:x}" * 64,
                    media_type="application/octet-stream",
                )
            )

        CountingSqliteLabRegistry.discovery_builds = 0
        recovery = store.recover_experiment(manifest.experiment_id)

        self.assertEqual(len(recovery.run(run.run_id).metrics), 3)
        self.assertEqual(len(recovery.run(run.run_id).artifacts), 3)
        self.assertEqual(CountingSqliteLabRegistry.discovery_builds, 1)

    def test_recovery_builds_derived_index_snapshot_once_per_load(self) -> None:
        class CountingSqliteLabRegistry(SqliteLabRegistry):
            derived_snapshot_builds = 0

            @classmethod
            def _derived_index_snapshot(cls, connection, state):
                cls.derived_snapshot_builds += 1
                return super()._derived_index_snapshot(connection, state)

        db_path = Path(self.tempdir.name) / "recovery-single-derived-snapshot.sqlite3"
        store = CountingSqliteLabRegistry(db_path)
        hypothesis = Hypothesis.create(
            statement="Recovery validates derived indexes in one batched snapshot.",
            falsification_criterion=(
                "Recovery rescans a full derived table once per durable record."
            ),
        )
        manifest = ExperimentManifest.create(
            hypothesis=hypothesis,
            procedure="Register several evidence records, then recover once.",
            experiment_id=_TARGET_EXPERIMENT_ID,
        )
        run = ExperimentRun.create(
            manifest=manifest,
            ordinal=0,
            seed=51,
            run_id=_TARGET_RUN_ID,
        )
        store.register_experiment(hypothesis, manifest)
        store.register_run(run)
        for index in range(4):
            store.register_metric(
                MetricRecord.create(
                    run=run,
                    name=f"derived_metric_{index}",
                    value=float(index),
                    unit="score",
                )
            )
            store.register_artifact(
                ArtifactRecord.create(
                    run=run,
                    logical_path=f"derived/{index}.bin",
                    size_bytes=index + 1,
                    sha256_digest=f"{index + 5:x}" * 64,
                    media_type="application/octet-stream",
                )
            )

        CountingSqliteLabRegistry.derived_snapshot_builds = 0
        recovery = store.recover_experiment(manifest.experiment_id)

        self.assertEqual(len(recovery.run(run.run_id).metrics), 4)
        self.assertEqual(len(recovery.run(run.run_id).artifacts), 4)
        self.assertEqual(CountingSqliteLabRegistry.derived_snapshot_builds, 1)


if __name__ == "__main__":
    unittest.main()
