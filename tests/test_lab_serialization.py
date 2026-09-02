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
    artifact_record_from_dict,
    conclusion_from_dict,
    experiment_manifest_from_dict,
    experiment_run_from_dict,
    hypothesis_from_dict,
    metric_record_from_dict,
)


class ComputationalLabSerializationTests(unittest.TestCase):
    def setUp(self) -> None:
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
        self.conclusion = Conclusion.create(
            hypothesis=self.hypothesis,
            manifest=self.manifest,
            verdict=ConclusionVerdict.SUPPORTED,
            summary="The bound evidence supports the declared hypothesis.",
            metrics=(self.metric,),
            artifacts=(self.artifact,),
        )

    def test_all_public_records_round_trip_through_strict_dict_decoders(self) -> None:
        cases = (
            (self.hypothesis, hypothesis_from_dict),
            (self.manifest, experiment_manifest_from_dict),
            (self.run, experiment_run_from_dict),
            (self.metric, metric_record_from_dict),
            (self.artifact, artifact_record_from_dict),
            (self.conclusion, conclusion_from_dict),
        )

        for record, decode in cases:
            with self.subTest(record=type(record).__name__):
                restored = decode(record.to_dict())
                self.assertEqual(restored.to_dict(), record.to_dict())

    def test_conclusion_decoder_normalizes_json_arrays_to_canonical_tuples(self) -> None:
        payload = self.conclusion.to_dict()
        self.assertIsInstance(payload["metric_digests"], list)
        self.assertIsInstance(payload["artifact_digests"], list)

        restored = conclusion_from_dict(payload)

        self.assertIsInstance(restored.metric_digests, tuple)
        self.assertIsInstance(restored.artifact_digests, tuple)
        self.assertEqual(restored.conclusion_digest, self.conclusion.conclusion_digest)

    def test_decoders_reject_missing_or_extra_fields(self) -> None:
        missing = self.hypothesis.to_dict()
        missing.pop("statement")
        extra = self.hypothesis.to_dict()
        extra["unexpected"] = "field"

        with self.assertRaises(InvalidLabRecordError):
            hypothesis_from_dict(missing)
        with self.assertRaises(InvalidLabRecordError):
            hypothesis_from_dict(extra)

    def test_decoder_revalidates_stale_digest_after_payload_tamper(self) -> None:
        payload = self.metric.to_dict()
        payload["value"] = 0.01

        with self.assertRaises(InvalidLabRecordError):
            metric_record_from_dict(payload)

    def test_conclusion_decoder_rejects_non_array_evidence_fields(self) -> None:
        payload = self.conclusion.to_dict()
        payload["metric_digests"] = "not-an-array"

        with self.assertRaises(InvalidLabRecordError):
            conclusion_from_dict(payload)


if __name__ == "__main__":
    unittest.main()
