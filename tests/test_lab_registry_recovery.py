from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codexia_manual_agent.lab import (
    ArtifactRecord,
    EvidenceBindingError,
    ExperimentManifest,
    ExperimentRun,
    Hypothesis,
    LabIdentityConflictError,
    LabRegistryEventKind,
    LabRegistryStateError,
    MetricRecord,
    SqliteLabRegistry,
)


class DurableLabRegistryRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.db_path = Path(self.tempdir.name) / "lab-registry.sqlite3"
        self.hypothesis = Hypothesis.create(
            statement="The candidate improves the declared metric.",
            falsification_criterion="The declared metric does not improve.",
        )
        self.manifest = ExperimentManifest.create(
            hypothesis=self.hypothesis,
            procedure="Run the exact declared evaluator.",
            parameters={"split": "held-out", "repetitions": 3},
        )
        self.run = ExperimentRun.create(
            manifest=self.manifest,
            ordinal=0,
            seed=42,
        )
        self.metric = MetricRecord.create(
            run=self.run,
            name="accuracy",
            value=0.91,
            unit="ratio",
        )
        self.artifact = ArtifactRecord.create(
            run=self.run,
            logical_path="reports/metrics.json",
            size_bytes=64,
            sha256_digest="a" * 64,
            media_type="application/json",
        )

    def _registered_store(self) -> SqliteLabRegistry:
        store = SqliteLabRegistry(self.db_path)
        store.register_experiment(self.hypothesis, self.manifest)
        store.register_run(self.run)
        return store

    def test_exact_experiment_run_and_evidence_survive_restart(self) -> None:
        store = self._registered_store()
        store.register_metric(self.metric)
        store.register_artifact(self.artifact)
        store.seal_run(self.run.run_id, self.run.run_digest)
        store.seal_experiment(
            self.manifest.experiment_id,
            self.manifest.manifest_digest,
        )

        recovered = SqliteLabRegistry(self.db_path).recover_experiment(
            self.manifest.experiment_id
        )

        self.assertEqual(recovered.hypothesis, self.hypothesis)
        self.assertEqual(recovered.manifest, self.manifest)
        self.assertTrue(recovered.experiment_sealed)
        self.assertEqual(
            tuple(event.kind for event in recovered.events),
            (
                LabRegistryEventKind.EXPERIMENT_REGISTERED,
                LabRegistryEventKind.RUN_REGISTERED,
                LabRegistryEventKind.METRIC_REGISTERED,
                LabRegistryEventKind.ARTIFACT_REGISTERED,
                LabRegistryEventKind.RUN_SEALED,
                LabRegistryEventKind.EXPERIMENT_SEALED,
            ),
        )
        run_snapshot = recovered.run(self.run.run_id)
        self.assertTrue(run_snapshot.evidence_sealed)
        self.assertEqual(run_snapshot.metric(self.metric.metric_id), self.metric)
        self.assertEqual(run_snapshot.artifact(self.artifact.artifact_id), self.artifact)

    def test_recovery_by_run_metric_and_artifact_resolves_exact_experiment(self) -> None:
        store = self._registered_store()
        store.register_metric(self.metric)
        store.register_artifact(self.artifact)

        fresh = SqliteLabRegistry(self.db_path)
        by_run = fresh.recover_for_run(self.run.run_id)
        by_metric = fresh.recover_for_metric(self.metric.metric_id)
        by_artifact = fresh.recover_for_artifact(self.artifact.artifact_id)

        for recovered in (by_run, by_metric, by_artifact):
            with self.subTest(kind=recovered.events[-1].kind):
                self.assertEqual(recovered.experiment_id, self.manifest.experiment_id)
                self.assertEqual(recovered.run(self.run.run_id).run, self.run)

    def test_same_exact_hypothesis_can_anchor_multiple_experiments(self) -> None:
        store = SqliteLabRegistry(self.db_path)
        store.register_experiment(self.hypothesis, self.manifest)
        second_manifest = ExperimentManifest.create(
            hypothesis=self.hypothesis,
            procedure="Run a second exact evaluator.",
            parameters={"split": "second-held-out"},
        )

        second = store.register_experiment(self.hypothesis, second_manifest)

        self.assertEqual(second.hypothesis.hypothesis_digest, self.hypothesis.hypothesis_digest)
        self.assertEqual(second.manifest, second_manifest)

    def test_hypothesis_identity_cannot_be_rebound_across_experiments(self) -> None:
        store = SqliteLabRegistry(self.db_path)
        store.register_experiment(self.hypothesis, self.manifest)
        rebound = Hypothesis.create(
            hypothesis_id=self.hypothesis.hypothesis_id,
            statement="A different claim with the same identity.",
            falsification_criterion="A different criterion.",
        )
        rebound_manifest = ExperimentManifest.create(
            hypothesis=rebound,
            procedure="Attempt identity rebinding.",
        )

        with self.assertRaises(LabIdentityConflictError):
            store.register_experiment(rebound, rebound_manifest)

        recovered = store.recover_experiment(self.manifest.experiment_id)
        self.assertEqual(recovered.hypothesis, self.hypothesis)

    def test_run_ordinal_is_unique_inside_one_experiment(self) -> None:
        store = self._registered_store()
        duplicate_ordinal = ExperimentRun.create(
            manifest=self.manifest,
            ordinal=self.run.ordinal,
            seed=99,
        )

        with self.assertRaises(LabIdentityConflictError):
            store.register_run(duplicate_ordinal)

        recovered = store.recover_experiment(self.manifest.experiment_id)
        self.assertEqual(tuple(recovered.runs), (self.run.run_id,))

    def test_metric_name_and_artifact_path_are_unique_per_run(self) -> None:
        store = self._registered_store()
        store.register_metric(self.metric)
        store.register_artifact(self.artifact)
        duplicate_metric_name = MetricRecord.create(
            run=self.run,
            name=self.metric.name,
            value=0.92,
            unit="ratio",
        )
        duplicate_artifact_path = ArtifactRecord.create(
            run=self.run,
            logical_path=self.artifact.logical_path,
            size_bytes=65,
            sha256_digest="b" * 64,
            media_type="application/json",
        )

        with self.assertRaises(LabIdentityConflictError):
            store.register_metric(duplicate_metric_name)
        with self.assertRaises(LabIdentityConflictError):
            store.register_artifact(duplicate_artifact_path)

    def test_global_run_id_collision_rolls_back_second_experiment_append(self) -> None:
        store = self._registered_store()
        second_manifest = ExperimentManifest.create(
            hypothesis=self.hypothesis,
            procedure="Run an independent second experiment.",
        )
        store.register_experiment(self.hypothesis, second_manifest)
        colliding_run = ExperimentRun.create(
            manifest=second_manifest,
            ordinal=0,
            seed=17,
            run_id=self.run.run_id,
        )

        with self.assertRaises(LabIdentityConflictError):
            store.register_run(colliding_run)

        recovered = store.recover_experiment(second_manifest.experiment_id)
        self.assertEqual(len(recovered.events), 1)
        self.assertEqual(recovered.runs, {})

    def test_run_with_another_manifest_digest_is_rejected_without_append(self) -> None:
        store = self._registered_store()
        alternate_manifest = ExperimentManifest.create(
            hypothesis=self.hypothesis,
            procedure="Use another manifest under the same experiment identity.",
            experiment_id=self.manifest.experiment_id,
        )
        rebound_run = ExperimentRun.create(
            manifest=alternate_manifest,
            ordinal=1,
            seed=18,
        )
        before = store.recover_experiment(self.manifest.experiment_id)

        with self.assertRaises(EvidenceBindingError):
            store.register_run(rebound_run)

        after = store.recover_experiment(self.manifest.experiment_id)
        self.assertEqual(after.events, before.events)

    def test_run_seal_is_irreversible_and_blocks_later_evidence(self) -> None:
        store = self._registered_store()
        store.seal_run(self.run.run_id, self.run.run_digest)

        with self.assertRaises(LabRegistryStateError):
            store.register_metric(self.metric)
        with self.assertRaises(LabRegistryStateError):
            store.register_artifact(self.artifact)
        with self.assertRaises(LabRegistryStateError):
            store.seal_run(self.run.run_id, self.run.run_digest)

        recovered = SqliteLabRegistry(self.db_path).recover_for_run(self.run.run_id)
        self.assertTrue(recovered.run(self.run.run_id).evidence_sealed)
        self.assertEqual(recovered.run(self.run.run_id).metrics, {})
        self.assertEqual(recovered.run(self.run.run_id).artifacts, {})

    def test_experiment_seal_requires_all_runs_sealed_and_blocks_new_runs(self) -> None:
        store = self._registered_store()

        with self.assertRaises(LabRegistryStateError):
            store.seal_experiment(
                self.manifest.experiment_id,
                self.manifest.manifest_digest,
            )

        store.seal_run(self.run.run_id, self.run.run_digest)
        sealed = store.seal_experiment(
            self.manifest.experiment_id,
            self.manifest.manifest_digest,
        )
        self.assertTrue(sealed.experiment_sealed)

        later_run = ExperimentRun.create(
            manifest=self.manifest,
            ordinal=1,
            seed=7,
        )
        with self.assertRaises(LabRegistryStateError):
            store.register_run(later_run)
        with self.assertRaises(LabRegistryStateError):
            store.seal_experiment(
                self.manifest.experiment_id,
                self.manifest.manifest_digest,
            )

    def test_failed_transition_does_not_append_an_event(self) -> None:
        store = self._registered_store()
        before = store.recover_experiment(self.manifest.experiment_id)

        with self.assertRaises(EvidenceBindingError):
            store.seal_run(self.run.run_id, "0" * 64)

        after = store.recover_experiment(self.manifest.experiment_id)
        self.assertEqual(after.events, before.events)
        self.assertFalse(after.run(self.run.run_id).evidence_sealed)


if __name__ == "__main__":
    unittest.main()
