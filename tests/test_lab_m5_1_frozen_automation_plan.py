from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from codexia_manual_agent.lab import (
    AutomationBudget,
    AutomationPlan,
    AutomationStopPolicy,
    ComparisonDirection,
    ComparisonMissingPolicy,
    ComparisonPolicy,
    ExperimentManifest,
    ExperimentRun,
    Hypothesis,
    InvalidLabRecordError,
    LabIdentityConflictError,
    LabPersistenceIntegrityError,
    LabRegistryStateError,
    PYTHON_JSON_PROFILE,
    SqliteAutomationPlanRegistry,
    SqliteComparisonRegistry,
    SqliteLabRegistry,
)


class M51FrozenAutomationPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name).resolve()
        self.db = self.root / "m5-1.sqlite3"
        self.lab = SqliteLabRegistry(self.db)
        self.comparisons = SqliteComparisonRegistry(self.lab)
        self.automation = SqliteAutomationPlanRegistry(
            self.comparisons,
            self.lab,
        )

    @staticmethod
    def _source() -> str:
        return "import sys\nsys.stdout.write('{}')"

    def _manifest(
        self,
        hypothesis: Hypothesis,
        *,
        arm: str,
        profile: str = PYTHON_JSON_PROFILE,
    ) -> ExperimentManifest:
        return ExperimentManifest.create(
            hypothesis=hypothesis,
            procedure="Emit one deterministic numeric result for M5.1 planning.",
            parameters={
                "profile": profile,
                "source": self._source(),
                "input": {"arm": arm},
                "metric": {"name": "score", "unit": "unit"},
            },
        )

    def _frozen_policy(
        self,
        *,
        baseline_profile: str = PYTHON_JSON_PROFILE,
        candidate_profile: str = PYTHON_JSON_PROFILE,
    ):
        hypothesis = Hypothesis.create(
            statement="Candidate score improves on baseline by at least one unit.",
            falsification_criterion=(
                "The frozen higher-is-better comparison effect is less than one."
            ),
        )
        baseline = self._manifest(
            hypothesis,
            arm="baseline",
            profile=baseline_profile,
        )
        candidate = self._manifest(
            hypothesis,
            arm="candidate",
            profile=candidate_profile,
        )
        self.lab.register_experiment(hypothesis, baseline)
        self.lab.register_experiment(hypothesis, candidate)
        policy = ComparisonPolicy.create(
            hypothesis=hypothesis,
            baseline_manifest=baseline,
            candidate_manifest=candidate,
            metric_name="score",
            metric_unit="unit",
            direction=ComparisonDirection.HIGHER_IS_BETTER,
            minimum_effect=1,
            seeds=(101, 202),
            missing_run_policy=ComparisonMissingPolicy.ERROR,
        )
        return (
            self.comparisons.register_policy(policy),
            baseline,
            candidate,
        )

    @staticmethod
    def _plan(frozen, *, max_steps: int = 16, max_runs: int = 4):
        return AutomationPlan.create(
            frozen_policy=frozen,
            budget=AutomationBudget.create(
                max_steps=max_steps,
                max_runs=max_runs,
            ),
        )

    def test_plan_freezes_before_runs_and_recovers_after_later_run(self) -> None:
        frozen_policy, baseline, _candidate = self._frozen_policy()
        plan = self._plan(frozen_policy)
        frozen = self.automation.register_plan(plan)

        self.assertEqual(frozen.plan, plan)
        self.assertEqual(frozen.plan.required_runs, 4)
        self.assertEqual(frozen.baseline_event_sequence, 0)
        self.assertEqual(frozen.candidate_event_sequence, 0)
        self.assertTrue(
            frozen.plan.stop_policy.pause_on_authorization_required
        )

        run = ExperimentRun.create(
            manifest=baseline,
            ordinal=0,
            seed=101,
        )
        self.lab.register_run(run)

        recovered = self.automation.recover(plan.automation_id)
        self.assertEqual(recovered, frozen)

    def test_late_plan_after_first_run_is_rejected(self) -> None:
        frozen_policy, baseline, _candidate = self._frozen_policy()
        run = ExperimentRun.create(
            manifest=baseline,
            ordinal=0,
            seed=101,
        )
        self.lab.register_run(run)

        with self.assertRaises(LabRegistryStateError):
            self.automation.register_plan(self._plan(frozen_policy))

    def test_same_policy_cannot_shop_for_another_budget(self) -> None:
        frozen_policy, _baseline, _candidate = self._frozen_policy()
        first = self._plan(frozen_policy, max_steps=16, max_runs=4)
        self.automation.register_plan(first)

        alternate = self._plan(
            frozen_policy,
            max_steps=8,
            max_runs=2,
        )
        with self.assertRaises(LabIdentityConflictError):
            self.automation.register_plan(alternate)

    def test_run_budget_cannot_exceed_declared_comparison_set(self) -> None:
        frozen_policy, _baseline, _candidate = self._frozen_policy()
        with self.assertRaises(InvalidLabRecordError):
            self._plan(frozen_policy, max_steps=16, max_runs=5)

    def test_v1_stop_policy_cannot_disable_authorization_pause(self) -> None:
        with self.assertRaises(InvalidLabRecordError):
            AutomationStopPolicy(
                schema_version=1,
                pause_on_authorization_required=False,
                stop_on_error=True,
                stop_on_budget_exhaustion=True,
                stop_on_conclusion=True,
            )

    def test_non_m4_3_python_profile_is_not_automation_admitted(self) -> None:
        frozen_policy, _baseline, _candidate = self._frozen_policy(
            candidate_profile="unsupported-profile.v1"
        )
        plan = self._plan(frozen_policy)
        with self.assertRaises(InvalidLabRecordError):
            self.automation.register_plan(plan)

    def test_persisted_plan_tamper_fails_recovery(self) -> None:
        frozen_policy, _baseline, _candidate = self._frozen_policy()
        plan = self._plan(frozen_policy)
        self.automation.register_plan(plan)

        with sqlite3.connect(self.db) as connection:
            row = connection.execute(
                """
                SELECT payload_json
                FROM lab_automation_plan_freezes
                WHERE automation_id = ?
                """,
                (plan.automation_id,),
            ).fetchone()
            assert row is not None
            raw = row[0]
            tampered = raw.replace('"max_steps":16', '"max_steps":15', 1)
            self.assertNotEqual(tampered, raw)
            connection.execute(
                """
                UPDATE lab_automation_plan_freezes
                SET payload_json = ?
                WHERE automation_id = ?
                """,
                (tampered, plan.automation_id),
            )
            connection.commit()

        with self.assertRaises(LabPersistenceIntegrityError):
            self.automation.recover(plan.automation_id)


if __name__ == "__main__":
    unittest.main()
