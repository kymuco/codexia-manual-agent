from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from codexia_manual_agent.authority import ApprovalMode, LocalApprovalAuthority
from codexia_manual_agent.lab import (
    AutomationBudget,
    AutomationPhase,
    AutomationPlan,
    ComparisonDirection,
    ComparisonMissingPolicy,
    ComparisonPolicy,
    ExperimentManifest,
    ExperimentRun,
    GovernedAutomationStateMachine,
    Hypothesis,
    LabPersistenceIntegrityError,
    LabRegistryStateError,
    PYTHON_JSON_PROFILE,
    RunExecutionPhase,
    SqliteAutomationPlanRegistry,
    SqliteComparisonRegistry,
    SqliteLabRegistry,
    SqliteRunExecutionRegistry,
)
from codexia_manual_agent.session_events import SqliteSessionEventStore


SOURCE = r'''
from pathlib import Path
import json
import sys

run_id, output_path, input_json = sys.argv[1:4]
payload = json.loads(input_json)
result = {
    "schema": "codexia.python-json-result.v1",
    "run_id": run_id,
    "metric": {
        "name": "score",
        "value": payload["value"],
        "unit": "points",
    },
}
encoded = json.dumps(result, sort_keys=True, separators=(",", ":"))
target = Path(output_path)
target.parent.mkdir(parents=True, exist_ok=True)
target.write_text(encoded, encoding="utf-8")
sys.stdout.write(encoded)
'''.strip()


class M52GovernedAutomationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name).resolve()
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.db = self.root / "m5-2.sqlite3"
        self.lab = SqliteLabRegistry(self.db)
        self.m3 = SqliteSessionEventStore(self.db)
        self.comparisons = SqliteComparisonRegistry(self.lab)
        self.plans = SqliteAutomationPlanRegistry(self.comparisons, self.lab)

    def _manifest(self, hypothesis: Hypothesis, *, arm: str, value: int) -> ExperimentManifest:
        return ExperimentManifest.create(
            hypothesis=hypothesis,
            procedure="Emit one deterministic score for bounded automation.",
            parameters={
                "profile": PYTHON_JSON_PROFILE,
                "source": SOURCE,
                "input": {"arm": arm, "value": value},
                "metric": {"name": "score", "unit": "points"},
            },
        )

    def _plan(self, *, max_steps: int = 6, max_runs: int = 2):
        hypothesis = Hypothesis.create(
            statement="Candidate score is at least five points above baseline.",
            falsification_criterion="The frozen higher-is-better effect is below five.",
        )
        baseline = self._manifest(hypothesis, arm="baseline", value=10)
        candidate = self._manifest(hypothesis, arm="candidate", value=20)
        self.lab.register_experiment(hypothesis, baseline)
        self.lab.register_experiment(hypothesis, candidate)
        policy = ComparisonPolicy.create(
            hypothesis=hypothesis,
            baseline_manifest=baseline,
            candidate_manifest=candidate,
            metric_name="score",
            metric_unit="points",
            direction=ComparisonDirection.HIGHER_IS_BETTER,
            minimum_effect=5,
            seeds=(101,),
            missing_run_policy=ComparisonMissingPolicy.ERROR,
        )
        frozen_policy = self.comparisons.register_policy(policy)
        plan = AutomationPlan.create(
            frozen_policy=frozen_policy,
            workspace_root=self.workspace,
            budget=AutomationBudget.create(
                max_steps=max_steps,
                max_runs=max_runs,
            ),
        )
        self.plans.register_plan(plan)
        return plan, baseline, candidate

    def _machine(self) -> GovernedAutomationStateMachine:
        return GovernedAutomationStateMachine(
            SqliteLabRegistry(self.db),
            SqliteSessionEventStore(self.db),
        )

    @staticmethod
    def _approve(state):
        assert state.proposal is not None
        return LocalApprovalAuthority().decide(
            state.proposal,
            mode=ApprovalMode.RISKY,
            approved=True,
            actor="m5.2-test-human",
        )

    def test_pauses_before_process_and_fresh_recovery_keeps_exact_proposal(self) -> None:
        plan, _baseline, _candidate = self._plan()
        machine = self._machine()

        initial = machine.recover(plan.automation_id)
        self.assertEqual(initial.phase, AutomationPhase.READY)
        self.assertEqual(initial.steps_used, 0)

        registered = machine.advance(plan.automation_id)
        self.assertEqual(registered.phase, AutomationPhase.RUN_REGISTERED)
        self.assertEqual(registered.steps_used, 1)
        self.assertEqual(registered.runs_started, 1)
        self.assertIsNone(registered.proposal)

        paused = machine.advance(plan.automation_id)
        self.assertEqual(paused.phase, AutomationPhase.PAUSED_AUTHORIZATION_REQUIRED)
        self.assertEqual(paused.steps_used, 2)
        self.assertIsNotNone(paused.proposal)
        assert paused.active_slot is not None
        assert paused.proposal is not None
        output = self.workspace / ".codexia" / "m4-runs" / paused.active_slot.run_id / "result.json"
        self.assertFalse(output.exists())

        fresh = self._machine()
        recovered = fresh.recover(plan.automation_id)
        self.assertEqual(recovered.to_dict(), paused.to_dict())
        self.assertFalse(output.exists())

        unchanged = fresh.advance(plan.automation_id)
        self.assertEqual(unchanged.to_dict(), paused.to_dict())
        self.assertFalse(output.exists())

        executions = SqliteRunExecutionRegistry(
            SqliteLabRegistry(self.db),
            SqliteSessionEventStore(self.db),
        )
        lineage = executions.recover(paused.active_slot.run_id)
        self.assertEqual(lineage.phase, RunExecutionPhase.BOUND)
        self.assertIsNone(lineage.authorization)
        self.assertIsNone(lineage.evidence)

    def test_external_receipts_drive_exact_run_set_and_restart_does_not_rerun(self) -> None:
        plan, _baseline, _candidate = self._plan()
        machine = self._machine()
        output_stats: dict[str, tuple[bytes, int]] = {}

        for expected_completed in (1, 2):
            registered = machine.advance(plan.automation_id)
            self.assertEqual(registered.phase, AutomationPhase.RUN_REGISTERED)
            paused = machine.advance(plan.automation_id)
            self.assertEqual(paused.phase, AutomationPhase.PAUSED_AUTHORIZATION_REQUIRED)
            assert paused.active_slot is not None
            authorized = machine.continue_authorized(
                plan.automation_id,
                receipt=self._approve(paused),
            )
            self.assertEqual(authorized.runs_completed, expected_completed)
            path = (
                self.workspace
                / ".codexia"
                / "m4-runs"
                / paused.active_slot.run_id
                / "result.json"
            )
            output_stats[paused.active_slot.run_id] = (
                path.read_bytes(),
                path.stat().st_mtime_ns,
            )

        complete = machine.recover(plan.automation_id)
        self.assertEqual(complete.phase, AutomationPhase.RUN_SET_COMPLETE)
        self.assertEqual(complete.steps_used, 6)
        self.assertEqual(complete.runs_started, 2)
        self.assertEqual(complete.runs_completed, 2)
        self.assertIsNone(complete.proposal)

        fresh = self._machine()
        self.assertEqual(fresh.recover(plan.automation_id).to_dict(), complete.to_dict())
        self.assertEqual(fresh.advance(plan.automation_id).to_dict(), complete.to_dict())
        for run_id, (before_bytes, before_mtime) in output_stats.items():
            path = self.workspace / ".codexia" / "m4-runs" / run_id / "result.json"
            self.assertEqual(path.read_bytes(), before_bytes)
            self.assertEqual(path.stat().st_mtime_ns, before_mtime)

    def test_step_budget_can_stop_at_prepared_authorization_boundary(self) -> None:
        plan, _baseline, _candidate = self._plan(max_steps=2, max_runs=2)
        machine = self._machine()
        machine.advance(plan.automation_id)
        stopped = machine.advance(plan.automation_id)

        self.assertEqual(stopped.phase, AutomationPhase.STOPPED_BUDGET)
        self.assertEqual(stopped.steps_used, 2)
        self.assertIsNotNone(stopped.proposal)
        assert stopped.active_slot is not None
        assert stopped.proposal is not None
        receipt = LocalApprovalAuthority().decide(
            stopped.proposal,
            mode=ApprovalMode.RISKY,
            approved=True,
            actor="m5.2-test-human",
        )
        with self.assertRaises(LabRegistryStateError):
            machine.continue_authorized(plan.automation_id, receipt=receipt)
        output = (
            self.workspace
            / ".codexia"
            / "m4-runs"
            / stopped.active_slot.run_id
            / "result.json"
        )
        self.assertFalse(output.exists())

    def test_run_budget_stops_before_registering_second_arm(self) -> None:
        plan, _baseline, candidate = self._plan(max_steps=6, max_runs=1)
        machine = self._machine()
        machine.advance(plan.automation_id)
        paused = machine.advance(plan.automation_id)
        after_first = machine.continue_authorized(
            plan.automation_id,
            receipt=self._approve(paused),
        )

        self.assertEqual(after_first.phase, AutomationPhase.STOPPED_BUDGET)
        self.assertEqual(after_first.steps_used, 3)
        self.assertEqual(after_first.runs_started, 1)
        self.assertEqual(after_first.runs_completed, 1)
        self.assertEqual(self.lab.recover_experiment(candidate.experiment_id).runs, {})
        self.assertEqual(machine.advance(plan.automation_id).to_dict(), after_first.to_dict())

    def test_foreign_run_in_declared_slot_is_rejected_instead_of_adopted(self) -> None:
        plan, baseline, _candidate = self._plan()
        foreign = ExperimentRun.create(
            manifest=baseline,
            ordinal=0,
            seed=101,
        )
        self.lab.register_run(foreign)

        with self.assertRaises(LabPersistenceIntegrityError):
            self._machine().recover(plan.automation_id)

    def test_unbound_durable_proposal_fails_closed_without_minting_replacement(self) -> None:
        plan, _baseline, _candidate = self._plan()
        machine = self._machine()
        registered = machine.advance(plan.automation_id)
        assert registered.active_slot is not None
        slot = registered.active_slot
        self.m3.start_session(
            session_id=slot.m3_session_id,
            payload={
                "workspace": str(self.workspace),
                "prompt_version": "m5.2",
                "mode": "bounded-automation",
                "capabilities": ["execute_process"],
                "provider": "local-m5.2",
                "title": None,
                "model": None,
                "reasoning_effort": None,
            },
        )
        # A proposal durable in M3 but not bound into M4 represents a partial
        # preparation crash. M5.2 must stop instead of producing another approval target.
        from codexia_manual_agent.execution import prepare_process_proposal

        proposal = prepare_process_proposal(
            workspace=self.workspace,
            argv=[__import__("sys").executable, "-c", "print('never execute')"],
        )
        self.m3.record_proposal(slot.m3_session_id, proposal)

        stopped = self._machine().recover(plan.automation_id)
        self.assertEqual(stopped.phase, AutomationPhase.STOPPED_ERROR)
        self.assertEqual(stopped.steps_used, 2)
        self.assertIsNone(stopped.proposal)
        before = len(self.m3.recover(slot.m3_session_id).actions)
        self.assertEqual(self._machine().advance(plan.automation_id).phase, AutomationPhase.STOPPED_ERROR)
        self.assertEqual(len(self.m3.recover(slot.m3_session_id).actions), before)


if __name__ == "__main__":
    unittest.main()
