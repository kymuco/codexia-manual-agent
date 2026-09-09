from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from codexia_manual_agent.authority import ApprovalMode, LocalApprovalAuthority
from codexia_manual_agent.lab import (
    AutomationBudget,
    AutomationPhase,
    AutomationPlan,
    AutomationRunArm,
    ComparisonDirection,
    ComparisonMissingPolicy,
    ComparisonOutcome,
    ComparisonPolicy,
    ConclusionScope,
    ConclusionVerdict,
    ExperimentManifest,
    GovernedAutomationStateMachine,
    Hypothesis,
    PYTHON_JSON_PROFILE,
    SqliteAdjudicatedConclusionRegistry,
    SqliteAutomationPlanRegistry,
    SqliteComparisonRegistry,
    SqliteComparisonResultRegistry,
    SqliteLabRegistry,
    SqlitePhysicalEvidenceRegistry,
    SqliteRunExecutionRegistry,
)
from codexia_manual_agent.session_events import SqliteSessionEventStore


RECOVERY_PROGRAM = textwrap.dedent(
    r"""
    import json
    import sys
    from pathlib import Path

    from codexia_manual_agent.lab import (
        GovernedAutomationStateMachine,
        SqliteAdjudicatedConclusionRegistry,
        SqliteAutomationPlanRegistry,
        SqliteComparisonRegistry,
        SqliteComparisonResultRegistry,
        SqliteLabRegistry,
        SqlitePhysicalEvidenceRegistry,
        SqliteRunExecutionRegistry,
    )
    from codexia_manual_agent.session_events import SqliteSessionEventStore

    db = Path(sys.argv[1]).resolve()
    automation_id = sys.argv[2]

    lab = SqliteLabRegistry(db)
    m3 = SqliteSessionEventStore(db)
    comparisons = SqliteComparisonRegistry(lab)
    plans = SqliteAutomationPlanRegistry(comparisons, lab)
    frozen_plan = plans.recover(automation_id)
    policy_id = frozen_plan.plan.policy_id

    automation = GovernedAutomationStateMachine(lab, m3)
    state = automation.recover(automation_id)

    executions = SqliteRunExecutionRegistry(lab, m3)
    physical = SqlitePhysicalEvidenceRegistry(lab, executions)
    results = SqliteComparisonResultRegistry(comparisons, lab, physical)
    conclusions = SqliteAdjudicatedConclusionRegistry(comparisons, results, lab)
    result = results.recover_result(policy_id)
    conclusion = conclusions.recover(policy_id)

    print(json.dumps({
        "automation_id": frozen_plan.plan.automation_id,
        "plan_digest": frozen_plan.plan.plan_digest,
        "policy_id": policy_id,
        "phase": state.phase.value,
        "steps_used": state.steps_used,
        "runs_started": state.runs_started,
        "runs_completed": state.runs_completed,
        "required_runs": state.required_runs,
        "result_id": result.result_id,
        "result_digest": result.result_digest,
        "outcome": result.outcome.value,
        "effect": result.effect,
        "conclusion_id": conclusion.conclusion_id,
        "conclusion_digest": conclusion.conclusion_digest,
        "scope": conclusion.scope.value,
        "verdict": conclusion.verdict.value,
        "summary": conclusion.summary,
    }, sort_keys=True, separators=(",", ":")))
    """
).strip()


REFUTED_SUMMARY = (
    "The verified evidence does not satisfy the exact frozen comparison policy "
    "for this hypothesis; refutation is limited to that declared policy and evidence."
)


class M53FirstRealBoundedAutomationClosureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name).resolve()
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.db = self.root / "m5-3.sqlite3"

        self.lab = SqliteLabRegistry(self.db)
        self.m3 = SqliteSessionEventStore(self.db)
        self.comparisons = SqliteComparisonRegistry(self.lab)
        self.plans = SqliteAutomationPlanRegistry(self.comparisons, self.lab)
        self.executions = SqliteRunExecutionRegistry(self.lab, self.m3)
        self.physical = SqlitePhysicalEvidenceRegistry(self.lab, self.executions)
        self.results = SqliteComparisonResultRegistry(
            self.comparisons,
            self.lab,
            self.physical,
        )
        self.conclusions = SqliteAdjudicatedConclusionRegistry(
            self.comparisons,
            self.results,
            self.lab,
        )

    @staticmethod
    def _fixture_source() -> str:
        path = (
            Path(__file__).with_name("fixtures")
            / "m4_4_3_integration_error_experiment.py"
        )
        return path.read_text(encoding="utf-8").strip()

    def _manifest(
        self,
        hypothesis: Hypothesis,
        *,
        method: str,
    ) -> ExperimentManifest:
        return ExperimentManifest.create(
            hypothesis=hypothesis,
            procedure=(
                "Approximate integral_0^1 x^2 dx with n=8 and emit the absolute "
                "error numerator over the common denominator 6*n^3."
            ),
            parameters={
                "profile": PYTHON_JSON_PROFILE,
                "source": self._fixture_source(),
                "input": {"method": method, "n": 8},
                "metric": {
                    "name": "integration_error_numerator",
                    "unit": "over_6n_cubed",
                },
            },
        )

    @staticmethod
    def _approve(state):
        assert state.proposal is not None
        return LocalApprovalAuthority().decide(
            state.proposal,
            mode=ApprovalMode.RISKY,
            approved=True,
            actor="m5.3-test-human",
        )

    def test_real_refuted_m4_loop_runs_through_bounded_automation_and_recovers_without_replay(self) -> None:
        hypothesis = Hypothesis.create(
            statement=(
                "For n=8, trapezoid integration reduces the exact common-denominator "
                "error numerator by at least 180 relative to left Riemann."
            ),
            falsification_criterion=(
                "The frozen lower-is-better comparison effect is less than 180."
            ),
        )
        baseline = self._manifest(hypothesis, method="left")
        candidate = self._manifest(hypothesis, method="trapezoid")
        self.lab.register_experiment(hypothesis, baseline)
        self.lab.register_experiment(hypothesis, candidate)

        seeds = (101, 202)
        policy = ComparisonPolicy.create(
            hypothesis=hypothesis,
            baseline_manifest=baseline,
            candidate_manifest=candidate,
            metric_name="integration_error_numerator",
            metric_unit="over_6n_cubed",
            direction=ComparisonDirection.LOWER_IS_BETTER,
            minimum_effect=180,
            seeds=seeds,
            missing_run_policy=ComparisonMissingPolicy.ERROR,
        )
        frozen_policy = self.comparisons.register_policy(policy)
        plan = AutomationPlan.create(
            frozen_policy=frozen_policy,
            workspace_root=self.workspace,
            budget=AutomationBudget.create(max_steps=12, max_runs=4),
        )
        frozen_plan = self.plans.register_plan(plan)

        machine = GovernedAutomationStateMachine(self.lab, self.m3)
        expected_slots = (
            (AutomationRunArm.BASELINE, 0, 101),
            (AutomationRunArm.BASELINE, 1, 202),
            (AutomationRunArm.CANDIDATE, 0, 101),
            (AutomationRunArm.CANDIDATE, 1, 202),
        )
        run_ids: list[str] = []

        for completed_count, (arm, ordinal, seed) in enumerate(expected_slots, start=1):
            ready = machine.recover(plan.automation_id)
            self.assertEqual(ready.phase, AutomationPhase.READY)

            registered = machine.advance(plan.automation_id)
            self.assertEqual(registered.phase, AutomationPhase.RUN_REGISTERED)
            assert registered.active_slot is not None
            self.assertEqual(registered.active_slot.arm, arm)
            self.assertEqual(registered.active_slot.ordinal, ordinal)
            self.assertEqual(registered.active_slot.seed, seed)

            paused = machine.advance(plan.automation_id)
            self.assertEqual(paused.phase, AutomationPhase.PAUSED_AUTHORIZATION_REQUIRED)
            assert paused.active_slot is not None
            self.assertEqual(paused.active_slot.run_id, registered.active_slot.run_id)
            self.assertIsNotNone(paused.proposal)
            output = (
                self.workspace
                / ".codexia"
                / "m4-runs"
                / paused.active_slot.run_id
                / "result.json"
            )
            self.assertFalse(output.exists())

            # The coordinator remains inert until authority is supplied externally.
            self.assertEqual(
                machine.advance(plan.automation_id).to_dict(),
                paused.to_dict(),
            )
            self.assertFalse(output.exists())

            continued = machine.continue_authorized(
                plan.automation_id,
                receipt=self._approve(paused),
            )
            self.assertEqual(continued.runs_completed, completed_count)
            run_ids.append(paused.active_slot.run_id)

        complete = machine.recover(plan.automation_id)
        self.assertEqual(complete.phase, AutomationPhase.RUN_SET_COMPLETE)
        self.assertEqual(complete.steps_used, 12)
        self.assertEqual(complete.runs_started, 4)
        self.assertEqual(complete.runs_completed, 4)
        self.assertEqual(complete.required_runs, 4)
        self.assertIsNone(complete.proposal)

        physical_before: dict[str, tuple[Path, bytes, int]] = {}
        for run_id in run_ids:
            evidence = self.physical.recover(run_id)
            path = self.workspace / evidence.artifact.logical_path
            physical_before[run_id] = (
                path,
                path.read_bytes(),
                path.stat().st_mtime_ns,
            )

        # M5.3 owns the post-run scientific closure and may crash between the
        # two irreversible experiment seals. M5.2 recovery must remain stable.
        self.lab.seal_experiment(baseline.experiment_id, baseline.manifest_digest)
        one_arm_sealed = GovernedAutomationStateMachine(
            SqliteLabRegistry(self.db),
            SqliteSessionEventStore(self.db),
        ).recover(plan.automation_id)
        self.assertEqual(one_arm_sealed.phase, AutomationPhase.RUN_SET_COMPLETE)
        self.assertEqual(one_arm_sealed.steps_used, 12)

        self.lab.seal_experiment(candidate.experiment_id, candidate.manifest_digest)
        both_arms_sealed = machine.recover(plan.automation_id)
        self.assertEqual(both_arms_sealed.phase, AutomationPhase.RUN_SET_COMPLETE)

        result = self.results.evaluate(frozen_policy.policy.policy_id)
        self.assertEqual(result.outcome, ComparisonOutcome.REFUTED)
        self.assertEqual(result.baseline_mean, "184")
        self.assertEqual(result.candidate_mean, "8")
        self.assertEqual(result.effect, "176")

        conclusion = self.conclusions.publish(frozen_policy.policy.policy_id)
        self.assertEqual(conclusion.scope, ConclusionScope.FROZEN_COMPARISON_POLICY_V1)
        self.assertEqual(conclusion.verdict, ConclusionVerdict.REFUTED)
        self.assertEqual(conclusion.summary, REFUTED_SUMMARY)
        self.assertEqual(conclusion.result_id, result.result_id)
        self.assertEqual(conclusion.policy_id, frozen_policy.policy.policy_id)

        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                RECOVERY_PROGRAM,
                str(self.db),
                plan.automation_id,
            ],
            cwd=self.workspace,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
        )
        self.assertEqual(completed.returncode, 0, msg=completed.stderr)
        recovered = json.loads(completed.stdout)

        self.assertEqual(recovered["automation_id"], frozen_plan.plan.automation_id)
        self.assertEqual(recovered["plan_digest"], frozen_plan.plan.plan_digest)
        self.assertEqual(recovered["policy_id"], frozen_policy.policy.policy_id)
        self.assertEqual(recovered["phase"], AutomationPhase.RUN_SET_COMPLETE.value)
        self.assertEqual(recovered["steps_used"], 12)
        self.assertEqual(recovered["runs_started"], 4)
        self.assertEqual(recovered["runs_completed"], 4)
        self.assertEqual(recovered["required_runs"], 4)
        self.assertEqual(recovered["result_id"], result.result_id)
        self.assertEqual(recovered["result_digest"], result.result_digest)
        self.assertEqual(recovered["outcome"], ComparisonOutcome.REFUTED.value)
        self.assertEqual(recovered["effect"], "176")
        self.assertEqual(recovered["conclusion_id"], conclusion.conclusion_id)
        self.assertEqual(recovered["conclusion_digest"], conclusion.conclusion_digest)
        self.assertEqual(
            recovered["scope"],
            ConclusionScope.FROZEN_COMPARISON_POLICY_V1.value,
        )
        self.assertEqual(recovered["verdict"], ConclusionVerdict.REFUTED.value)
        self.assertEqual(recovered["summary"], REFUTED_SUMMARY)

        for path, before_bytes, before_mtime in physical_before.values():
            self.assertEqual(path.read_bytes(), before_bytes)
            self.assertEqual(path.stat().st_mtime_ns, before_mtime)


if __name__ == "__main__":
    unittest.main()
