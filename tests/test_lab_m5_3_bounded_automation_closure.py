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
    AutomationClosurePhase,
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
    GovernedAutomationClosure,
    GovernedAutomationStateMachine,
    Hypothesis,
    PYTHON_JSON_PROFILE,
    SqliteAutomationPlanRegistry,
    SqliteComparisonRegistry,
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

    from codexia_manual_agent.lab import GovernedAutomationClosure, SqliteLabRegistry
    from codexia_manual_agent.session_events import SqliteSessionEventStore

    db = Path(sys.argv[1]).resolve()
    automation_id = sys.argv[2]

    state = GovernedAutomationClosure(
        SqliteLabRegistry(db),
        SqliteSessionEventStore(db),
    ).recover(automation_id)
    if state.result is None or state.conclusion is None:
        raise RuntimeError("M5.3 terminal recovery lacks result/conclusion")

    print(json.dumps({
        "automation_id": state.automation_id,
        "plan_digest": state.plan_digest,
        "policy_id": state.policy_id,
        "phase": state.phase.value,
        "steps_used": state.steps_used,
        "run_steps_used": state.run_steps_used,
        "closure_steps_used": state.closure_steps_used,
        "max_steps": state.max_steps,
        "runs_completed": state.runs_completed,
        "required_runs": state.required_runs,
        "result_id": state.result.result_id,
        "result_digest": state.result.result_digest,
        "outcome": state.result.outcome.value,
        "effect": state.result.effect,
        "conclusion_id": state.conclusion.conclusion_id,
        "conclusion_digest": state.conclusion.conclusion_digest,
        "scope": state.conclusion.scope.value,
        "verdict": state.conclusion.verdict.value,
        "summary": state.conclusion.summary,
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

    def _register_plan(self, *, seeds: tuple[int, ...], max_steps: int):
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
            budget=AutomationBudget.create(
                max_steps=max_steps,
                max_runs=len(seeds) * 2,
            ),
        )
        frozen_plan = self.plans.register_plan(plan)
        return frozen_plan, frozen_policy, baseline, candidate

    def _drive_run_set(
        self,
        plan: AutomationPlan,
        *,
        seeds: tuple[int, ...],
    ) -> tuple[GovernedAutomationStateMachine, list[str]]:
        machine = GovernedAutomationStateMachine(self.lab, self.m3)
        expected_slots = tuple(
            (arm, ordinal, seed)
            for arm in (AutomationRunArm.BASELINE, AutomationRunArm.CANDIDATE)
            for ordinal, seed in enumerate(seeds)
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

            # Repeated orchestration is inert until authority arrives externally.
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
        self.assertEqual(complete.steps_used, len(seeds) * 2 * 3)
        self.assertEqual(complete.runs_started, len(seeds) * 2)
        self.assertEqual(complete.runs_completed, len(seeds) * 2)
        self.assertEqual(complete.required_runs, len(seeds) * 2)
        self.assertIsNone(complete.proposal)
        return machine, run_ids

    def test_real_refuted_m4_loop_runs_through_bounded_automation_and_recovers_without_replay(self) -> None:
        seeds = (101, 202)
        frozen_plan, frozen_policy, _baseline, _candidate = self._register_plan(
            seeds=seeds,
            max_steps=16,
        )
        plan = frozen_plan.plan
        machine, run_ids = self._drive_run_set(plan, seeds=seeds)

        physical_before: dict[str, tuple[Path, bytes, int]] = {}
        for run_id in run_ids:
            evidence = self.physical.recover(run_id)
            path = self.workspace / evidence.artifact.logical_path
            physical_before[run_id] = (
                path,
                path.read_bytes(),
                path.stat().st_mtime_ns,
            )

        closure = GovernedAutomationClosure(self.lab, self.m3)
        initial = closure.recover(plan.automation_id)
        self.assertEqual(initial.phase, AutomationClosurePhase.RUN_SET_COMPLETE)
        self.assertEqual(initial.steps_used, 12)
        self.assertEqual(initial.run_steps_used, 12)
        self.assertEqual(initial.closure_steps_used, 0)
        self.assertEqual(initial.max_steps, 16)
        self.assertIsNone(initial.result)
        self.assertIsNone(initial.conclusion)

        baseline_sealed = closure.advance(plan.automation_id)
        self.assertEqual(
            baseline_sealed.phase,
            AutomationClosurePhase.BASELINE_EXPERIMENT_SEALED,
        )
        self.assertEqual(baseline_sealed.steps_used, 13)
        self.assertEqual(baseline_sealed.closure_steps_used, 1)

        # A fresh coordinator after the first irreversible arm seal must derive
        # the same stage and continue rather than requiring atomic two-arm sealing.
        fresh_after_first_seal = GovernedAutomationClosure(
            SqliteLabRegistry(self.db),
            SqliteSessionEventStore(self.db),
        ).recover(plan.automation_id)
        self.assertEqual(fresh_after_first_seal.to_dict(), baseline_sealed.to_dict())

        experiments_sealed = closure.advance(plan.automation_id)
        self.assertEqual(
            experiments_sealed.phase,
            AutomationClosurePhase.EXPERIMENTS_SEALED,
        )
        self.assertEqual(experiments_sealed.steps_used, 14)
        self.assertEqual(experiments_sealed.closure_steps_used, 2)
        self.assertIsNone(experiments_sealed.result)

        compared = closure.advance(plan.automation_id)
        self.assertEqual(compared.phase, AutomationClosurePhase.COMPARISON_COMPLETE)
        self.assertEqual(compared.steps_used, 15)
        self.assertEqual(compared.closure_steps_used, 3)
        self.assertIsNotNone(compared.result)
        assert compared.result is not None
        self.assertEqual(compared.result.outcome, ComparisonOutcome.REFUTED)
        self.assertEqual(compared.result.baseline_mean, "184")
        self.assertEqual(compared.result.candidate_mean, "8")
        self.assertEqual(compared.result.effect, "176")
        self.assertIsNone(compared.conclusion)

        terminal = closure.advance(plan.automation_id)
        self.assertEqual(terminal.phase, AutomationClosurePhase.STOPPED_CONCLUSION)
        self.assertEqual(terminal.steps_used, 16)
        self.assertEqual(terminal.run_steps_used, 12)
        self.assertEqual(terminal.closure_steps_used, 4)
        self.assertEqual(terminal.runs_completed, 4)
        self.assertEqual(terminal.required_runs, 4)
        self.assertIsNotNone(terminal.result)
        self.assertIsNotNone(terminal.conclusion)
        assert terminal.result is not None
        assert terminal.conclusion is not None
        self.assertEqual(terminal.result.result_id, compared.result.result_id)
        self.assertEqual(terminal.conclusion.scope, ConclusionScope.FROZEN_COMPARISON_POLICY_V1)
        self.assertEqual(terminal.conclusion.verdict, ConclusionVerdict.REFUTED)
        self.assertEqual(terminal.conclusion.summary, REFUTED_SUMMARY)
        self.assertEqual(terminal.conclusion.result_id, terminal.result.result_id)
        self.assertEqual(terminal.policy_id, frozen_policy.policy.policy_id)

        # stop_on_conclusion is terminal: another advance cannot create more work.
        self.assertEqual(
            closure.advance(plan.automation_id).to_dict(),
            terminal.to_dict(),
        )
        self.assertEqual(
            machine.recover(plan.automation_id).phase,
            AutomationPhase.RUN_SET_COMPLETE,
        )

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
        self.assertEqual(
            recovered["phase"],
            AutomationClosurePhase.STOPPED_CONCLUSION.value,
        )
        self.assertEqual(recovered["steps_used"], 16)
        self.assertEqual(recovered["run_steps_used"], 12)
        self.assertEqual(recovered["closure_steps_used"], 4)
        self.assertEqual(recovered["max_steps"], 16)
        self.assertEqual(recovered["runs_completed"], 4)
        self.assertEqual(recovered["required_runs"], 4)
        self.assertEqual(recovered["result_id"], terminal.result.result_id)
        self.assertEqual(recovered["result_digest"], terminal.result.result_digest)
        self.assertEqual(recovered["outcome"], ComparisonOutcome.REFUTED.value)
        self.assertEqual(recovered["effect"], "176")
        self.assertEqual(recovered["conclusion_id"], terminal.conclusion.conclusion_id)
        self.assertEqual(
            recovered["conclusion_digest"],
            terminal.conclusion.conclusion_digest,
        )
        self.assertEqual(
            recovered["scope"],
            ConclusionScope.FROZEN_COMPARISON_POLICY_V1.value,
        )
        self.assertEqual(recovered["verdict"], ConclusionVerdict.REFUTED.value)
        self.assertEqual(recovered["summary"], REFUTED_SUMMARY)

        for path, before_bytes, before_mtime in physical_before.values():
            self.assertEqual(path.read_bytes(), before_bytes)
            self.assertEqual(path.stat().st_mtime_ns, before_mtime)

    def test_frozen_budget_can_stop_after_comparison_before_conclusion(self) -> None:
        seeds = (101,)
        frozen_plan, _frozen_policy, _baseline, _candidate = self._register_plan(
            seeds=seeds,
            max_steps=9,
        )
        plan = frozen_plan.plan
        self._drive_run_set(plan, seeds=seeds)

        closure = GovernedAutomationClosure(self.lab, self.m3)
        self.assertEqual(closure.advance(plan.automation_id).steps_used, 7)
        self.assertEqual(closure.advance(plan.automation_id).steps_used, 8)
        stopped = closure.advance(plan.automation_id)

        self.assertEqual(stopped.phase, AutomationClosurePhase.STOPPED_BUDGET)
        self.assertEqual(stopped.steps_used, 9)
        self.assertEqual(stopped.run_steps_used, 6)
        self.assertEqual(stopped.closure_steps_used, 3)
        self.assertIsNotNone(stopped.result)
        self.assertIsNone(stopped.conclusion)
        self.assertEqual(
            closure.advance(plan.automation_id).to_dict(),
            stopped.to_dict(),
        )


if __name__ == "__main__":
    unittest.main()
