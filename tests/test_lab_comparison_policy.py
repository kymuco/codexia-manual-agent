from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from uuid import uuid4

from codexia_manual_agent.lab import (
    ComparisonAggregation,
    ComparisonDirection,
    ComparisonMissingPolicy,
    ComparisonPolicy,
    EvidenceBindingError,
    ExperimentManifest,
    ExperimentRun,
    Hypothesis,
    InvalidLabRecordError,
    LabIdentityConflictError,
    LabPersistenceIntegrityError,
    LabRegistryStateError,
    SqliteComparisonRegistry,
    SqliteLabRegistry,
)


class FrozenComparisonPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.db = Path(self.tempdir.name) / "comparison.sqlite3"
        self.lab = SqliteLabRegistry(self.db)
        self.comparisons = SqliteComparisonRegistry(self.lab)
        self.hypothesis = Hypothesis.create(
            statement="The candidate reduces absolute error by at least one unit.",
            falsification_criterion=(
                "The frozen comparison yields candidate improvement below one unit."
            ),
        )
        self.baseline = ExperimentManifest.create(
            hypothesis=self.hypothesis,
            procedure="Run the exact baseline evaluator.",
            parameters={"arm": "baseline"},
        )
        self.candidate = ExperimentManifest.create(
            hypothesis=self.hypothesis,
            procedure="Run the exact candidate evaluator.",
            parameters={"arm": "candidate"},
        )
        self.lab.register_experiment(self.hypothesis, self.baseline)
        self.lab.register_experiment(self.hypothesis, self.candidate)

    def _policy(self, **overrides) -> ComparisonPolicy:
        values = {
            "hypothesis": self.hypothesis,
            "baseline_manifest": self.baseline,
            "candidate_manifest": self.candidate,
            "metric_name": "absolute_error",
            "metric_unit": "integer",
            "direction": ComparisonDirection.LOWER_IS_BETTER,
            "minimum_effect": 1,
            "seeds": (11, 22, 33),
            "aggregation": ComparisonAggregation.MEAN,
            "missing_run_policy": ComparisonMissingPolicy.INCONCLUSIVE,
        }
        values.update(overrides)
        return ComparisonPolicy.create(**values)

    def test_policy_freezes_exact_two_arm_pre_run_state_and_survives_restart(self) -> None:
        policy = self._policy()
        frozen = self.comparisons.register_policy(policy)

        baseline = self.lab.recover_experiment(self.baseline.experiment_id)
        candidate = self.lab.recover_experiment(self.candidate.experiment_id)
        self.assertEqual(frozen.policy, policy)
        self.assertEqual(frozen.baseline_event_id, baseline.events[0].event_id)
        self.assertEqual(frozen.baseline_event_digest, baseline.events[0].event_digest)
        self.assertEqual(frozen.candidate_event_id, candidate.events[0].event_id)
        self.assertEqual(frozen.candidate_event_digest, candidate.events[0].event_digest)

        recovered = SqliteComparisonRegistry(SqliteLabRegistry(self.db)).recover_policy(
            policy.policy_id
        )
        self.assertEqual(recovered, frozen)

    def test_policy_can_be_recovered_after_runs_are_registered_later(self) -> None:
        frozen = self.comparisons.register_policy(self._policy())
        baseline_run = ExperimentRun.create(
            manifest=self.baseline,
            ordinal=0,
            seed=11,
        )
        candidate_run = ExperimentRun.create(
            manifest=self.candidate,
            ordinal=0,
            seed=11,
        )
        self.lab.register_run(baseline_run)
        self.lab.register_run(candidate_run)

        recovered = SqliteComparisonRegistry(SqliteLabRegistry(self.db)).recover_policy(
            frozen.policy.policy_id
        )
        self.assertEqual(recovered.freeze_digest, frozen.freeze_digest)
        self.assertEqual(
            recovered.baseline_event_digest,
            frozen.baseline_event_digest,
        )
        self.assertEqual(
            recovered.candidate_event_digest,
            frozen.candidate_event_digest,
        )

    def test_policy_cannot_freeze_after_either_arm_has_registered_run(self) -> None:
        for arm in ("baseline", "candidate"):
            with self.subTest(arm=arm):
                with tempfile.TemporaryDirectory() as tempdir:
                    db = Path(tempdir) / "late.sqlite3"
                    lab = SqliteLabRegistry(db)
                    comparisons = SqliteComparisonRegistry(lab)
                    hypothesis = Hypothesis.create(
                        statement="Candidate is better than baseline.",
                        falsification_criterion="Frozen comparison does not meet threshold.",
                    )
                    baseline = ExperimentManifest.create(
                        hypothesis=hypothesis,
                        procedure="baseline",
                    )
                    candidate = ExperimentManifest.create(
                        hypothesis=hypothesis,
                        procedure="candidate",
                    )
                    lab.register_experiment(hypothesis, baseline)
                    lab.register_experiment(hypothesis, candidate)
                    chosen = baseline if arm == "baseline" else candidate
                    lab.register_run(
                        ExperimentRun.create(manifest=chosen, ordinal=0, seed=11)
                    )
                    policy = ComparisonPolicy.create(
                        hypothesis=hypothesis,
                        baseline_manifest=baseline,
                        candidate_manifest=candidate,
                        metric_name="score",
                        metric_unit=None,
                        direction=ComparisonDirection.HIGHER_IS_BETTER,
                        minimum_effect=1,
                        seeds=(11,),
                    )

                    with self.assertRaises(LabRegistryStateError):
                        comparisons.register_policy(policy)
                    with self.assertRaises(InvalidLabRecordError):
                        comparisons.recover_policy(policy.policy_id)

    def test_policy_rejects_cross_hypothesis_candidate(self) -> None:
        other_hypothesis = Hypothesis.create(
            statement="Another claim.",
            falsification_criterion="Another criterion.",
        )
        other_candidate = ExperimentManifest.create(
            hypothesis=other_hypothesis,
            procedure="other candidate",
        )

        with self.assertRaises(EvidenceBindingError):
            ComparisonPolicy.create(
                hypothesis=self.hypothesis,
                baseline_manifest=self.baseline,
                candidate_manifest=other_candidate,
                metric_name="score",
                metric_unit=None,
                direction=ComparisonDirection.HIGHER_IS_BETTER,
                minimum_effect=1,
                seeds=(1,),
            )

    def test_policy_rejects_same_experiment_as_both_arms(self) -> None:
        with self.assertRaises(EvidenceBindingError):
            ComparisonPolicy.create(
                hypothesis=self.hypothesis,
                baseline_manifest=self.baseline,
                candidate_manifest=self.baseline,
                metric_name="score",
                metric_unit=None,
                direction=ComparisonDirection.HIGHER_IS_BETTER,
                minimum_effect=1,
                seeds=(1,),
            )

    def test_policy_requires_positive_effect_and_unique_nonempty_seeds(self) -> None:
        for effect in (0, -1, float("inf"), float("nan"), True):
            with self.subTest(effect=effect):
                with self.assertRaises(InvalidLabRecordError):
                    self._policy(minimum_effect=effect)
        for seeds in ((), (1, 1), (True,)):
            with self.subTest(seeds=seeds):
                with self.assertRaises(InvalidLabRecordError):
                    self._policy(seeds=seeds)

    def test_policy_id_cannot_rebind_to_different_exact_policy(self) -> None:
        policy = self._policy()
        self.comparisons.register_policy(policy)
        rebound = self._policy(
            policy_id=policy.policy_id,
            minimum_effect=2,
        )

        with self.assertRaises(LabIdentityConflictError):
            self.comparisons.register_policy(rebound)

    def test_payload_tamper_is_detected_after_restart(self) -> None:
        frozen = self.comparisons.register_policy(self._policy())
        with closing(sqlite3.connect(self.db)) as connection:
            row = connection.execute(
                "SELECT payload_json FROM lab_comparison_policy_freezes WHERE policy_id = ?",
                (frozen.policy.policy_id,),
            ).fetchone()
            assert row is not None
            payload = json.loads(row[0])
            payload["policy"]["minimum_effect"] = 999
            connection.execute(
                "UPDATE lab_comparison_policy_freezes SET payload_json = ? WHERE policy_id = ?",
                (
                    json.dumps(payload, sort_keys=True, separators=(",", ":")),
                    frozen.policy.policy_id,
                ),
            )
            connection.commit()

        with self.assertRaises(LabPersistenceIntegrityError):
            SqliteComparisonRegistry(SqliteLabRegistry(self.db)).recover_policy(
                frozen.policy.policy_id
            )

    def test_freeze_anchor_tamper_is_detected_after_restart(self) -> None:
        frozen = self.comparisons.register_policy(self._policy())
        with closing(sqlite3.connect(self.db)) as connection:
            row = connection.execute(
                "SELECT payload_json FROM lab_comparison_policy_freezes WHERE policy_id = ?",
                (frozen.policy.policy_id,),
            ).fetchone()
            assert row is not None
            payload = json.loads(row[0])
            payload["baseline_event_digest"] = "0" * 64
            connection.execute(
                "UPDATE lab_comparison_policy_freezes SET payload_json = ? WHERE policy_id = ?",
                (
                    json.dumps(payload, sort_keys=True, separators=(",", ":")),
                    frozen.policy.policy_id,
                ),
            )
            connection.commit()

        with self.assertRaises(LabPersistenceIntegrityError):
            SqliteComparisonRegistry(SqliteLabRegistry(self.db)).recover_policy(
                frozen.policy.policy_id
            )

    def test_unknown_policy_is_rejected(self) -> None:
        with self.assertRaises(InvalidLabRecordError):
            self.comparisons.recover_policy(str(uuid4()))


if __name__ == "__main__":
    unittest.main()
