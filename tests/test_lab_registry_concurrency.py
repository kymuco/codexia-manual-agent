from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from codexia_manual_agent.lab import (
    ExperimentManifest,
    ExperimentRun,
    Hypothesis,
    LabIdentityConflictError,
    LabRegistryStateError,
    MetricRecord,
    SqliteLabRegistry,
)


class DurableLabRegistryConcurrencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.db_path = Path(self.tempdir.name) / "lab-registry.sqlite3"
        self.hypothesis = Hypothesis.create(
            statement="Concurrent durable registry mutations remain serializable.",
            falsification_criterion="A race admits mutually incompatible registry state.",
        )
        self.manifest = ExperimentManifest.create(
            hypothesis=self.hypothesis,
            procedure="Exercise concurrent registry mutation.",
        )
        store = SqliteLabRegistry(self.db_path)
        store.register_experiment(self.hypothesis, self.manifest)

    def _race(self, callables: tuple[object, object]) -> list[BaseException | None]:
        barrier = threading.Barrier(2)
        results: list[BaseException | None] = [None, None]

        def worker(index: int) -> None:
            try:
                barrier.wait(timeout=5)
                callables[index]()  # type: ignore[operator]
            except BaseException as exc:  # capture thread result for assertions
                results[index] = exc

        threads = [threading.Thread(target=worker, args=(index,)) for index in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive(), "registry mutation thread did not finish")
        return results

    def test_competing_same_ordinal_run_registrations_admit_exactly_one(self) -> None:
        first = ExperimentRun.create(manifest=self.manifest, ordinal=0, seed=1)
        second = ExperimentRun.create(manifest=self.manifest, ordinal=0, seed=2)

        results = self._race(
            (
                lambda: SqliteLabRegistry(self.db_path).register_run(first),
                lambda: SqliteLabRegistry(self.db_path).register_run(second),
            )
        )

        self.assertEqual(sum(result is None for result in results), 1)
        failures = [result for result in results if result is not None]
        self.assertEqual(len(failures), 1)
        self.assertIsInstance(failures[0], LabIdentityConflictError)
        recovered = SqliteLabRegistry(self.db_path).recover_experiment(
            self.manifest.experiment_id
        )
        self.assertEqual(len(recovered.runs), 1)
        only_run = next(iter(recovered.runs.values())).run
        self.assertIn(only_run.run_id, {first.run_id, second.run_id})

    def test_competing_same_metric_name_registrations_admit_exactly_one(self) -> None:
        run = ExperimentRun.create(manifest=self.manifest, ordinal=0, seed=3)
        SqliteLabRegistry(self.db_path).register_run(run)
        first = MetricRecord.create(run=run, name="accuracy", value=0.8)
        second = MetricRecord.create(run=run, name="accuracy", value=0.9)

        results = self._race(
            (
                lambda: SqliteLabRegistry(self.db_path).register_metric(first),
                lambda: SqliteLabRegistry(self.db_path).register_metric(second),
            )
        )

        self.assertEqual(sum(result is None for result in results), 1)
        failures = [result for result in results if result is not None]
        self.assertEqual(len(failures), 1)
        self.assertIsInstance(failures[0], LabIdentityConflictError)
        recovered = SqliteLabRegistry(self.db_path).recover_for_run(run.run_id)
        metrics = recovered.run(run.run_id).metrics
        self.assertEqual(len(metrics), 1)
        self.assertIn(next(iter(metrics)), {first.metric_id, second.metric_id})

    def test_evidence_registration_and_run_seal_are_serialized(self) -> None:
        run = ExperimentRun.create(manifest=self.manifest, ordinal=0, seed=4)
        metric = MetricRecord.create(run=run, name="score", value=1.0)
        SqliteLabRegistry(self.db_path).register_run(run)

        results = self._race(
            (
                lambda: SqliteLabRegistry(self.db_path).register_metric(metric),
                lambda: SqliteLabRegistry(self.db_path).seal_run(
                    run.run_id,
                    run.run_digest,
                ),
            )
        )

        seal_failures = [
            result
            for index, result in enumerate(results)
            if index == 1 and result is not None
        ]
        self.assertEqual(seal_failures, [])
        if results[0] is not None:
            self.assertIsInstance(results[0], LabRegistryStateError)

        recovered = SqliteLabRegistry(self.db_path).recover_for_run(run.run_id)
        snapshot = recovered.run(run.run_id)
        self.assertTrue(snapshot.evidence_sealed)
        if metric.metric_id in snapshot.metrics:
            kinds = [event.kind.value for event in recovered.events]
            self.assertLess(
                kinds.index("metric_registered"),
                kinds.index("run_sealed"),
            )
        else:
            self.assertIsInstance(results[0], LabRegistryStateError)

    def test_run_registration_and_experiment_seal_cannot_cross_the_seal_boundary(self) -> None:
        # An experiment with no runs is immediately sealable. A concurrent run
        # registration either commits first, forcing seal to reject because the
        # run is open, or seal commits first and the run registration rejects.
        run = ExperimentRun.create(manifest=self.manifest, ordinal=0, seed=5)

        results = self._race(
            (
                lambda: SqliteLabRegistry(self.db_path).register_run(run),
                lambda: SqliteLabRegistry(self.db_path).seal_experiment(
                    self.manifest.experiment_id,
                    self.manifest.manifest_digest,
                ),
            )
        )

        recovered = SqliteLabRegistry(self.db_path).recover_experiment(
            self.manifest.experiment_id
        )
        if recovered.experiment_sealed:
            self.assertNotIn(run.run_id, recovered.runs)
            self.assertIsInstance(results[0], LabRegistryStateError)
            self.assertIsNone(results[1])
        else:
            self.assertIn(run.run_id, recovered.runs)
            self.assertIsNone(results[0])
            self.assertIsInstance(results[1], LabRegistryStateError)


if __name__ == "__main__":
    unittest.main()
