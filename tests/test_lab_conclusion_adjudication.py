from __future__ import annotations

import inspect
import tempfile
import unittest
from pathlib import Path

from codexia_manual_agent.lab.comparison import (
    ComparisonDirection,
    ComparisonMissingPolicy,
    ComparisonPolicy,
    FrozenComparisonPolicy,
)
from codexia_manual_agent.lab.comparison_result import (
    ComparisonOutcome,
    ComparisonResult,
)
from codexia_manual_agent.lab.conclusion_adjudication import (
    AdjudicatedConclusion,
    ConclusionScope,
    adjudicated_conclusion_from_dict,
)
from codexia_manual_agent.lab.errors import EvidenceBindingError, InvalidLabRecordError
from codexia_manual_agent.lab.models import (
    ConclusionVerdict,
    ExperimentManifest,
    Hypothesis,
)
from codexia_manual_agent.lab.registry import SqliteLabRegistry


class AdjudicatedConclusionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name).resolve()
        self.lab = SqliteLabRegistry(self.root / "adjudication.sqlite3")

    def _bundle(self, outcome: ComparisonOutcome):
        hypothesis = Hypothesis.create(
            statement="The candidate satisfies the exact frozen comparison claim.",
            falsification_criterion="The frozen comparison result is not supported.",
        )
        baseline = ExperimentManifest.create(
            hypothesis=hypothesis,
            procedure="Produce the baseline comparison metric.",
            parameters={"arm": "baseline"},
        )
        candidate = ExperimentManifest.create(
            hypothesis=hypothesis,
            procedure="Produce the candidate comparison metric.",
            parameters={"arm": "candidate"},
        )
        baseline_recovery = self.lab.register_experiment(hypothesis, baseline)
        candidate_recovery = self.lab.register_experiment(hypothesis, candidate)
        policy = ComparisonPolicy.create(
            hypothesis=hypothesis,
            baseline_manifest=baseline,
            candidate_manifest=candidate,
            metric_name="score",
            metric_unit="points",
            direction=ComparisonDirection.LOWER_IS_BETTER,
            minimum_effect=2,
            seeds=(11,),
            missing_run_policy=ComparisonMissingPolicy.INCONCLUSIVE,
        )
        baseline_event = baseline_recovery.events[-1]
        candidate_event = candidate_recovery.events[-1]
        frozen = FrozenComparisonPolicy.create(
            policy=policy,
            baseline_event_id=baseline_event.event_id,
            baseline_event_sequence=baseline_event.sequence,
            baseline_event_digest=baseline_event.event_digest,
            candidate_event_id=candidate_event.event_id,
            candidate_event_sequence=candidate_event.sequence,
            candidate_event_digest=candidate_event.event_digest,
        )

        if outcome is ComparisonOutcome.INCONCLUSIVE:
            result = ComparisonResult.create(
                frozen=frozen,
                baseline=baseline_recovery,
                candidate=candidate_recovery,
                baseline_evidence=(),
                candidate_evidence=(),
                missing_baseline_seeds=(11,),
                missing_candidate_seeds=(11,),
                baseline_mean=None,
                candidate_mean=None,
                effect=None,
                outcome=outcome,
            )
        else:
            result = ComparisonResult.create(
                frozen=frozen,
                baseline=baseline_recovery,
                candidate=candidate_recovery,
                baseline_evidence=(),
                candidate_evidence=(),
                missing_baseline_seeds=(),
                missing_candidate_seeds=(),
                baseline_mean="10",
                candidate_mean="7",
                effect="3",
                outcome=outcome,
            )
        return hypothesis, baseline, candidate, frozen, result

    def _adjudicate(self, outcome: ComparisonOutcome) -> AdjudicatedConclusion:
        hypothesis, baseline, candidate, frozen, result = self._bundle(outcome)
        return AdjudicatedConclusion.create(
            hypothesis=hypothesis,
            baseline_manifest=baseline,
            candidate_manifest=candidate,
            frozen=frozen,
            result=result,
        )

    def test_normal_creation_exposes_no_caller_verdict_summary_or_identity_authority(self) -> None:
        parameters = inspect.signature(AdjudicatedConclusion.create).parameters
        self.assertNotIn("verdict", parameters)
        self.assertNotIn("summary", parameters)
        self.assertNotIn("conclusion_id", parameters)

    def test_all_comparison_outcomes_map_to_exact_policy_scoped_verdicts(self) -> None:
        expected = {
            ComparisonOutcome.SUPPORTED: ConclusionVerdict.SUPPORTED,
            ComparisonOutcome.REFUTED: ConclusionVerdict.REFUTED,
            ComparisonOutcome.INCONCLUSIVE: ConclusionVerdict.INCONCLUSIVE,
        }
        for outcome, verdict in expected.items():
            with self.subTest(outcome=outcome):
                conclusion = self._adjudicate(outcome)
                self.assertEqual(conclusion.scope, ConclusionScope.FROZEN_COMPARISON_POLICY_V1)
                self.assertEqual(conclusion.comparison_outcome, outcome)
                self.assertEqual(conclusion.verdict, verdict)
                self.assertIn("policy", conclusion.summary.lower())

    def test_exact_result_produces_one_deterministic_conclusion(self) -> None:
        hypothesis, baseline, candidate, frozen, result = self._bundle(
            ComparisonOutcome.REFUTED
        )
        first = AdjudicatedConclusion.create(
            hypothesis=hypothesis,
            baseline_manifest=baseline,
            candidate_manifest=candidate,
            frozen=frozen,
            result=result,
        )
        second = AdjudicatedConclusion.create(
            hypothesis=hypothesis,
            baseline_manifest=baseline,
            candidate_manifest=candidate,
            frozen=frozen,
            result=result,
        )

        self.assertEqual(first.to_dict(), second.to_dict())
        self.assertEqual(first.verdict, ConclusionVerdict.REFUTED)
        self.assertIn("limited to that declared policy and evidence", first.summary)
        self.assertEqual(first.policy_digest, frozen.policy.policy_digest)
        self.assertEqual(first.freeze_digest, frozen.freeze_digest)
        self.assertEqual(first.result_digest, result.result_digest)

    def test_baseline_candidate_reversal_cannot_be_rebound_as_same_adjudication(self) -> None:
        hypothesis, baseline, candidate, frozen, result = self._bundle(
            ComparisonOutcome.SUPPORTED
        )
        with self.assertRaises(EvidenceBindingError):
            AdjudicatedConclusion.create(
                hypothesis=hypothesis,
                baseline_manifest=candidate,
                candidate_manifest=baseline,
                frozen=frozen,
                result=result,
            )

    def test_foreign_comparison_result_cannot_be_attached_to_another_policy(self) -> None:
        hypothesis, baseline, candidate, frozen, _result = self._bundle(
            ComparisonOutcome.SUPPORTED
        )
        _h2, _b2, _c2, _f2, foreign_result = self._bundle(
            ComparisonOutcome.REFUTED
        )
        with self.assertRaises(EvidenceBindingError):
            AdjudicatedConclusion.create(
                hypothesis=hypothesis,
                baseline_manifest=baseline,
                candidate_manifest=candidate,
                frozen=frozen,
                result=foreign_result,
            )

    def test_decoder_rejects_caller_authored_summary(self) -> None:
        conclusion = self._adjudicate(ComparisonOutcome.REFUTED)
        payload = conclusion.to_dict()
        payload["summary"] = "The hypothesis is universally false."
        with self.assertRaises(InvalidLabRecordError):
            adjudicated_conclusion_from_dict(payload)

    def test_decoder_rejects_verdict_that_disagrees_with_comparison_outcome(self) -> None:
        conclusion = self._adjudicate(ComparisonOutcome.REFUTED)
        payload = conclusion.to_dict()
        payload["verdict"] = ConclusionVerdict.SUPPORTED.value
        with self.assertRaises(InvalidLabRecordError):
            adjudicated_conclusion_from_dict(payload)

    def test_decoder_rejects_result_rebinding_with_stale_identity(self) -> None:
        conclusion = self._adjudicate(ComparisonOutcome.SUPPORTED)
        payload = conclusion.to_dict()
        payload["result_digest"] = "0" * 64
        with self.assertRaises(InvalidLabRecordError):
            adjudicated_conclusion_from_dict(payload)

    def test_decoder_round_trip_preserves_exact_contract(self) -> None:
        conclusion = self._adjudicate(ComparisonOutcome.INCONCLUSIVE)
        recovered = adjudicated_conclusion_from_dict(conclusion.to_dict())
        self.assertEqual(recovered.to_dict(), conclusion.to_dict())


if __name__ == "__main__":
    unittest.main()
