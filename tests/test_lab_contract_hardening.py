from __future__ import annotations

import unittest

from codexia_manual_agent.lab import (
    ArtifactRecord,
    Conclusion,
    ConclusionVerdict,
    ExperimentManifest,
    ExperimentRun,
    Hypothesis,
    InvalidLabRecordError,
    MetricRecord,
)


class ComputationalLabContractHardeningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.hypothesis = Hypothesis.create(
            statement="A bounded experiment can produce reproducible evidence.",
            falsification_criterion="The declared evidence cannot be reproduced.",
        )
        self.manifest = ExperimentManifest.create(
            hypothesis=self.hypothesis,
            procedure="Run the declared bounded procedure.",
        )
        self.run = ExperimentRun.create(manifest=self.manifest, ordinal=0, seed=0)

    def test_single_dot_is_not_an_artifact_path(self) -> None:
        with self.assertRaises(InvalidLabRecordError):
            ArtifactRecord.create(
                run=self.run,
                logical_path=".",
                size_bytes=0,
                sha256_digest="0" * 64,
            )

    def test_parameter_nesting_depth_is_bounded_before_digesting(self) -> None:
        nested: object = "leaf"
        for _ in range(40):
            nested = [nested]

        with self.assertRaises(InvalidLabRecordError):
            ExperimentManifest.create(
                hypothesis=self.hypothesis,
                procedure="Reject an excessively nested parameter object.",
                parameters={"nested": nested},
            )

    def test_large_python_integers_are_not_admitted_into_bounded_numeric_fields(self) -> None:
        too_large = 1 << 80

        with self.assertRaises(InvalidLabRecordError):
            ExperimentRun.create(
                manifest=self.manifest,
                ordinal=too_large,
            )
        with self.assertRaises(InvalidLabRecordError):
            MetricRecord.create(
                run=self.run,
                name="count",
                value=too_large,
            )
        with self.assertRaises(InvalidLabRecordError):
            ArtifactRecord.create(
                run=self.run,
                logical_path="artifact.bin",
                size_bytes=too_large,
                sha256_digest="1" * 64,
            )
        with self.assertRaises(InvalidLabRecordError):
            ExperimentManifest.create(
                hypothesis=self.hypothesis,
                procedure="Reject an unbounded parameter integer.",
                parameters={"count": too_large},
            )

    def test_evidence_count_budget_stops_an_unbounded_iterable(self) -> None:
        metric = MetricRecord.create(
            run=self.run,
            name="accuracy",
            value=1.0,
        )
        produced = 0

        def endless_metrics():
            nonlocal produced
            while True:
                produced += 1
                yield MetricRecord.create(
                    run=self.run,
                    name=f"m{produced}",
                    value=produced,
                )

        with self.assertRaises(InvalidLabRecordError):
            Conclusion.create(
                hypothesis=self.hypothesis,
                manifest=self.manifest,
                verdict=ConclusionVerdict.SUPPORTED,
                summary="The evidence iterable must remain bounded.",
                metrics=endless_metrics(),
            )

        self.assertEqual(produced, 257)
        self.assertIsNotNone(metric.metric_digest)


if __name__ == "__main__":
    unittest.main()
