from __future__ import annotations

import json
import unittest

from codexia_manual_agent.lab import (
    ArtifactRecord,
    Conclusion,
    ConclusionVerdict,
    EvidenceBindingError,
    ExperimentManifest,
    ExperimentRun,
    Hypothesis,
    InvalidLabRecordError,
    MetricRecord,
)


FIXED_TIME = "2026-08-27T00:00:00+00:00"
HYPOTHESIS_ID = "11111111-1111-4111-8111-111111111111"
EXPERIMENT_ID = "22222222-2222-4222-8222-222222222222"
RUN_ID = "33333333-3333-4333-8333-333333333333"
METRIC_A_ID = "44444444-4444-4444-8444-444444444444"
METRIC_B_ID = "55555555-5555-4555-8555-555555555555"
ARTIFACT_ID = "66666666-6666-4666-8666-666666666666"
CONCLUSION_ID = "77777777-7777-4777-8777-777777777777"


class ComputationalLabContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.hypothesis = Hypothesis.create(
            statement="Calibration improves held-out accuracy.",
            falsification_criterion="Mean held-out accuracy does not improve.",
            hypothesis_id=HYPOTHESIS_ID,
            created_at=FIXED_TIME,
        )
        self.manifest = ExperimentManifest.create(
            hypothesis=self.hypothesis,
            procedure="Run the frozen evaluator on the declared dataset split.",
            parameters={
                "dataset": "held-out-v1",
                "calibration": {"enabled": True, "temperature": 0.8},
            },
            experiment_id=EXPERIMENT_ID,
            created_at=FIXED_TIME,
        )
        self.run = ExperimentRun.create(
            manifest=self.manifest,
            ordinal=0,
            seed=7,
            run_id=RUN_ID,
            created_at=FIXED_TIME,
        )

    def test_hypothesis_digest_binds_exact_payload(self) -> None:
        tampered = self.hypothesis.to_dict()
        tampered["statement"] = "Calibration always improves accuracy."

        with self.assertRaises(InvalidLabRecordError):
            Hypothesis(**tampered)

    def test_manifest_parameter_order_is_canonical_and_deeply_detached(self) -> None:
        source = {
            "z": [1, {"inner": "stable"}],
            "a": {"value": 2},
        }
        first = ExperimentManifest.create(
            hypothesis=self.hypothesis,
            procedure="Run the same deterministic procedure.",
            parameters=source,
            experiment_id=EXPERIMENT_ID,
            created_at=FIXED_TIME,
        )
        second = ExperimentManifest.create(
            hypothesis=self.hypothesis,
            procedure="Run the same deterministic procedure.",
            parameters={"a": {"value": 2}, "z": [1, {"inner": "stable"}]},
            experiment_id=EXPERIMENT_ID,
            created_at=FIXED_TIME,
        )

        source["z"][1]["inner"] = "mutated later"

        self.assertEqual(first.manifest_digest, second.manifest_digest)
        self.assertEqual(
            first.to_dict()["parameters"],
            {"a": {"value": 2}, "z": [1, {"inner": "stable"}]},
        )
        json.dumps(first.to_dict(), allow_nan=False)

    def test_manifest_rejects_non_finite_parameters(self) -> None:
        with self.assertRaises(InvalidLabRecordError):
            ExperimentManifest.create(
                hypothesis=self.hypothesis,
                procedure="Invalid numeric input.",
                parameters={"threshold": float("nan")},
            )

    def test_run_identity_is_bound_to_exact_manifest(self) -> None:
        payload = self.run.to_dict()
        payload["manifest_digest"] = "0" * 64

        with self.assertRaises(InvalidLabRecordError):
            ExperimentRun(**payload)

    def test_artifact_record_rejects_noncanonical_or_traversal_paths(self) -> None:
        for logical_path in (
            "../escape.bin",
            "/absolute/result.bin",
            "nested\\windows.bin",
            "nested//duplicate.bin",
        ):
            with self.subTest(logical_path=logical_path):
                with self.assertRaises(InvalidLabRecordError):
                    ArtifactRecord.create(
                        run=self.run,
                        logical_path=logical_path,
                        size_bytes=1,
                        sha256_digest="a" * 64,
                    )

    def test_metric_rejects_boolean_and_non_finite_values(self) -> None:
        for value in (True, float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value):
                with self.assertRaises(InvalidLabRecordError):
                    MetricRecord.create(
                        run=self.run,
                        name="accuracy",
                        value=value,
                    )

    def test_supported_or_refuted_conclusion_requires_bound_evidence(self) -> None:
        for verdict in (ConclusionVerdict.SUPPORTED, ConclusionVerdict.REFUTED):
            with self.subTest(verdict=verdict):
                with self.assertRaises(EvidenceBindingError):
                    Conclusion.create(
                        hypothesis=self.hypothesis,
                        manifest=self.manifest,
                        verdict=verdict,
                        summary="This claim has no evidence and must be rejected.",
                    )

        conclusion = Conclusion.create(
            hypothesis=self.hypothesis,
            manifest=self.manifest,
            verdict=ConclusionVerdict.INCONCLUSIVE,
            summary="No evidence has been collected yet.",
        )
        self.assertEqual(conclusion.metric_digests, ())
        self.assertEqual(conclusion.artifact_digests, ())

    def test_conclusion_rejects_evidence_from_another_manifest(self) -> None:
        other_hypothesis = Hypothesis.create(
            statement="A different claim.",
            falsification_criterion="A different falsification criterion.",
        )
        other_manifest = ExperimentManifest.create(
            hypothesis=other_hypothesis,
            procedure="Run a different experiment.",
        )
        other_run = ExperimentRun.create(manifest=other_manifest, ordinal=0)
        foreign_metric = MetricRecord.create(
            run=other_run,
            name="accuracy",
            value=0.5,
        )

        with self.assertRaises(EvidenceBindingError):
            Conclusion.create(
                hypothesis=self.hypothesis,
                manifest=self.manifest,
                verdict=ConclusionVerdict.SUPPORTED,
                summary="Foreign evidence must not support this experiment.",
                metrics=(foreign_metric,),
            )

    def test_conclusion_rejects_manifest_hypothesis_rebinding(self) -> None:
        other_hypothesis = Hypothesis.create(
            statement="Another claim.",
            falsification_criterion="Another criterion.",
        )

        with self.assertRaises(EvidenceBindingError):
            Conclusion.create(
                hypothesis=other_hypothesis,
                manifest=self.manifest,
                verdict=ConclusionVerdict.INCONCLUSIVE,
                summary="The manifest belongs to a different hypothesis.",
            )

    def test_conclusion_evidence_is_duplicate_free_and_order_canonical(self) -> None:
        first_metric = MetricRecord.create(
            run=self.run,
            name="accuracy",
            value=0.91,
            unit="ratio",
            metric_id=METRIC_A_ID,
            created_at=FIXED_TIME,
        )
        second_metric = MetricRecord.create(
            run=self.run,
            name="loss",
            value=0.12,
            unit="ratio",
            metric_id=METRIC_B_ID,
            created_at=FIXED_TIME,
        )
        artifact = ArtifactRecord.create(
            run=self.run,
            logical_path="reports/result.json",
            size_bytes=128,
            sha256_digest="b" * 64,
            media_type="application/json",
            artifact_id=ARTIFACT_ID,
            created_at=FIXED_TIME,
        )

        first = Conclusion.create(
            hypothesis=self.hypothesis,
            manifest=self.manifest,
            verdict=ConclusionVerdict.SUPPORTED,
            summary="The bound evidence supports the stated hypothesis.",
            metrics=(first_metric, second_metric),
            artifacts=(artifact,),
            conclusion_id=CONCLUSION_ID,
            created_at=FIXED_TIME,
        )
        second = Conclusion.create(
            hypothesis=self.hypothesis,
            manifest=self.manifest,
            verdict=ConclusionVerdict.SUPPORTED,
            summary="The bound evidence supports the stated hypothesis.",
            metrics=(second_metric, first_metric),
            artifacts=(artifact,),
            conclusion_id=CONCLUSION_ID,
            created_at=FIXED_TIME,
        )

        self.assertEqual(first.conclusion_digest, second.conclusion_digest)
        self.assertEqual(first.metric_digests, tuple(sorted(first.metric_digests)))

        with self.assertRaises(EvidenceBindingError):
            Conclusion.create(
                hypothesis=self.hypothesis,
                manifest=self.manifest,
                verdict=ConclusionVerdict.SUPPORTED,
                summary="Duplicate evidence must fail closed.",
                metrics=(first_metric, first_metric),
            )

    def test_wrong_evidence_record_type_is_rejected(self) -> None:
        artifact = ArtifactRecord.create(
            run=self.run,
            logical_path="result.bin",
            size_bytes=4,
            sha256_digest="c" * 64,
        )

        with self.assertRaises(TypeError):
            Conclusion.create(
                hypothesis=self.hypothesis,
                manifest=self.manifest,
                verdict=ConclusionVerdict.SUPPORTED,
                summary="Artifact evidence cannot masquerade as a metric.",
                metrics=(artifact,),  # type: ignore[arg-type]
            )


if __name__ == "__main__":
    unittest.main()
