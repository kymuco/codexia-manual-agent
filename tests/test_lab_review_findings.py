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


class ComputationalLabPostReadyFindingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.hypothesis = Hypothesis.create(
            statement="Strict record metadata remains bounded and typed.",
            falsification_criterion="A malformed metadata value is admitted.",
        )
        self.manifest = ExperimentManifest.create(
            hypothesis=self.hypothesis,
            procedure="Exercise strict record metadata validation.",
        )
        self.run = ExperimentRun.create(manifest=self.manifest, ordinal=0, seed=1)
        self.metric = MetricRecord.create(
            run=self.run,
            name="score",
            value=1.0,
        )
        self.artifact = ArtifactRecord.create(
            run=self.run,
            logical_path="result.json",
            size_bytes=2,
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

    def test_boolean_schema_version_is_rejected_for_every_record_type(self) -> None:
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
                payload = record.to_dict()
                payload["schema_version"] = True
                with self.assertRaisesRegex(InvalidLabRecordError, "schema version"):
                    decode(payload)

    def test_oversized_timestamp_is_rejected_before_digest_validation(self) -> None:
        payload = self.hypothesis.to_dict()
        payload["created_at"] = "2026-08-27T00:00:00." + ("1" * 1_000_000) + "+00:00"

        with self.assertRaisesRegex(
            InvalidLabRecordError,
            "bounded canonical ISO-8601",
        ):
            hypothesis_from_dict(payload)

    def test_noncanonical_but_parseable_timestamp_is_rejected(self) -> None:
        payload = self.hypothesis.to_dict()
        payload["created_at"] = "2026-08-27 00:00:00+00:00"

        with self.assertRaisesRegex(InvalidLabRecordError, "canonical ISO-8601"):
            hypothesis_from_dict(payload)


if __name__ == "__main__":
    unittest.main()
