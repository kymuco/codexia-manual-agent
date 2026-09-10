from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from codexia_manual_agent.lab.automation import SqliteAutomationPlanRegistry
from codexia_manual_agent.lab.automation_runtime import (
    AutomationPhase,
    GovernedAutomationStateMachine,
)
from codexia_manual_agent.lab.comparison import SqliteComparisonRegistry
from codexia_manual_agent.lab.comparison_result import (
    ComparisonResult,
    SqliteComparisonResultRegistry,
)
from codexia_manual_agent.lab.conclusion_adjudication import AdjudicatedConclusion
from codexia_manual_agent.lab.conclusion_registry import SqliteAdjudicatedConclusionRegistry
from codexia_manual_agent.lab.errors import (
    InvalidLabRecordError,
    LabPersistenceIntegrityError,
    LabRegistryStateError,
)
from codexia_manual_agent.lab.execution_registry import SqliteRunExecutionRegistry
from codexia_manual_agent.lab.governed_python import SqlitePhysicalEvidenceRegistry
from codexia_manual_agent.lab.registry import SqliteLabRegistry
from codexia_manual_agent.session_events import SqliteSessionEventStore


AUTOMATION_CLOSURE_STATE_SCHEMA_VERSION = 1


class AutomationClosurePhase(StrEnum):
    RUN_SET_COMPLETE = "run_set_complete"
    BASELINE_EXPERIMENT_SEALED = "baseline_experiment_sealed"
    EXPERIMENTS_SEALED = "experiments_sealed"
    COMPARISON_COMPLETE = "comparison_complete"
    STOPPED_CONCLUSION = "stopped_conclusion"
    STOPPED_BUDGET = "stopped_budget"


@dataclass(frozen=True, slots=True)
class AutomationClosureState:
    schema_version: int
    automation_id: str
    plan_digest: str
    policy_id: str
    phase: AutomationClosurePhase
    steps_used: int
    run_steps_used: int
    closure_steps_used: int
    max_steps: int
    runs_completed: int
    required_runs: int
    result: ComparisonResult | None
    conclusion: AdjudicatedConclusion | None
    detail: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "automation_id": self.automation_id,
            "plan_digest": self.plan_digest,
            "policy_id": self.policy_id,
            "phase": self.phase.value,
            "steps_used": self.steps_used,
            "run_steps_used": self.run_steps_used,
            "closure_steps_used": self.closure_steps_used,
            "max_steps": self.max_steps,
            "runs_completed": self.runs_completed,
            "required_runs": self.required_runs,
            "result": self.result.to_dict() if self.result else None,
            "conclusion": self.conclusion.to_dict() if self.conclusion else None,
            "detail": self.detail,
        }


class GovernedAutomationClosure:
    """M5.3 non-authority coordinator for scientific closure after M5.2.

    The coordinator derives every transition from the frozen automation/policy and
    existing M4 durable state. It does not execute a process or create authority.
    """

    def __init__(
        self,
        lab_registry: SqliteLabRegistry,
        session_store: SqliteSessionEventStore,
    ) -> None:
        if not isinstance(lab_registry, SqliteLabRegistry):
            raise TypeError("lab_registry must be a SqliteLabRegistry")
        if not isinstance(session_store, SqliteSessionEventStore):
            raise TypeError("session_store must be a SqliteSessionEventStore")
        if lab_registry.database_path.resolve() != session_store.path.resolve():
            raise ValueError("M5.3 requires one M3/M4/M5 SQLite trust domain")

        self._lab = lab_registry
        self._m3 = session_store
        self._comparisons = SqliteComparisonRegistry(lab_registry)
        self._plans = SqliteAutomationPlanRegistry(self._comparisons, lab_registry)
        self._automation = GovernedAutomationStateMachine(lab_registry, session_store)
        self._executions = SqliteRunExecutionRegistry(lab_registry, session_store)
        self._physical = SqlitePhysicalEvidenceRegistry(lab_registry, self._executions)
        self._results = SqliteComparisonResultRegistry(
            self._comparisons,
            lab_registry,
            self._physical,
        )
        self._conclusions = SqliteAdjudicatedConclusionRegistry(
            self._comparisons,
            self._results,
            lab_registry,
        )

    @staticmethod
    def _recover_result_if_present(
        registry: SqliteComparisonResultRegistry,
        policy_id: str,
    ) -> ComparisonResult | None:
        try:
            return registry.recover_result(policy_id)
        except InvalidLabRecordError as exc:
            if str(exc) == "Unknown durable comparison result":
                return None
            raise

    @staticmethod
    def _recover_conclusion_if_present(
        registry: SqliteAdjudicatedConclusionRegistry,
        policy_id: str,
    ) -> AdjudicatedConclusion | None:
        try:
            return registry.recover(policy_id)
        except InvalidLabRecordError as exc:
            if str(exc) == "Unknown durable adjudicated conclusion":
                return None
            raise

    def recover(self, automation_id: str) -> AutomationClosureState:
        frozen_plan = self._plans.recover(automation_id)
        plan = frozen_plan.plan
        run_state = self._automation.recover(automation_id)
        if run_state.phase is not AutomationPhase.RUN_SET_COMPLETE:
            raise LabRegistryStateError(
                "M5.3 scientific closure requires M5.2 RUN_SET_COMPLETE"
            )
        if run_state.runs_completed != run_state.required_runs:
            raise LabPersistenceIntegrityError(
                "M5.3 RUN_SET_COMPLETE does not contain the exact completed run set"
            )
        if run_state.steps_used > plan.budget.max_steps:
            raise LabPersistenceIntegrityError(
                "M5.3 recovered run work already exceeds the frozen step budget"
            )

        frozen_policy = self._comparisons.recover_policy(plan.policy_id)
        policy = frozen_policy.policy
        if policy.policy_id != plan.policy_id:
            raise LabPersistenceIntegrityError(
                "M5.3 frozen automation plan no longer binds its exact comparison policy"
            )

        baseline = self._lab.recover_experiment(policy.baseline_experiment_id)
        candidate = self._lab.recover_experiment(policy.candidate_experiment_id)
        if candidate.experiment_sealed and not baseline.experiment_sealed:
            raise LabPersistenceIntegrityError(
                "M5.3 candidate experiment was sealed before deterministic baseline closure"
            )

        result: ComparisonResult | None = None
        conclusion: AdjudicatedConclusion | None = None
        if not baseline.experiment_sealed:
            closure_steps = 0
            base_phase = AutomationClosurePhase.RUN_SET_COMPLETE
            detail = "Exact automated run set is complete; baseline experiment seal is next"
        elif not candidate.experiment_sealed:
            closure_steps = 1
            base_phase = AutomationClosurePhase.BASELINE_EXPERIMENT_SEALED
            detail = "Baseline experiment is sealed; candidate experiment seal is next"
        else:
            result = self._recover_result_if_present(self._results, plan.policy_id)
            if result is None:
                closure_steps = 2
                base_phase = AutomationClosurePhase.EXPERIMENTS_SEALED
                detail = "Both experiment arms are sealed; exact frozen comparison is next"
            else:
                conclusion = self._recover_conclusion_if_present(
                    self._conclusions,
                    plan.policy_id,
                )
                if conclusion is None:
                    closure_steps = 3
                    base_phase = AutomationClosurePhase.COMPARISON_COMPLETE
                    detail = "Frozen comparison is durable; bounded conclusion publication is next"
                else:
                    closure_steps = 4
                    base_phase = AutomationClosurePhase.STOPPED_CONCLUSION
                    detail = "Frozen stop-on-conclusion rule reached after bounded adjudication"

        steps_used = run_state.steps_used + closure_steps
        if steps_used > plan.budget.max_steps:
            raise LabPersistenceIntegrityError(
                "Durable M5.3 closure work exceeds the previously frozen step budget"
            )
        if (
            base_phase is not AutomationClosurePhase.STOPPED_CONCLUSION
            and steps_used >= plan.budget.max_steps
        ):
            phase = AutomationClosurePhase.STOPPED_BUDGET
            detail = "Frozen step budget does not admit the next scientific closure transition"
        else:
            phase = base_phase

        return AutomationClosureState(
            schema_version=AUTOMATION_CLOSURE_STATE_SCHEMA_VERSION,
            automation_id=plan.automation_id,
            plan_digest=plan.plan_digest,
            policy_id=plan.policy_id,
            phase=phase,
            steps_used=steps_used,
            run_steps_used=run_state.steps_used,
            closure_steps_used=closure_steps,
            max_steps=plan.budget.max_steps,
            runs_completed=run_state.runs_completed,
            required_runs=run_state.required_runs,
            result=result,
            conclusion=conclusion,
            detail=detail,
        )

    def advance(self, automation_id: str) -> AutomationClosureState:
        state = self.recover(automation_id)
        if state.phase in (
            AutomationClosurePhase.STOPPED_BUDGET,
            AutomationClosurePhase.STOPPED_CONCLUSION,
        ):
            return state

        frozen_plan = self._plans.recover(automation_id)
        frozen_policy = self._comparisons.recover_policy(frozen_plan.plan.policy_id)
        policy = frozen_policy.policy

        if state.phase is AutomationClosurePhase.RUN_SET_COMPLETE:
            self._lab.seal_experiment(
                policy.baseline_experiment_id,
                policy.baseline_manifest_digest,
            )
        elif state.phase is AutomationClosurePhase.BASELINE_EXPERIMENT_SEALED:
            self._lab.seal_experiment(
                policy.candidate_experiment_id,
                policy.candidate_manifest_digest,
            )
        elif state.phase is AutomationClosurePhase.EXPERIMENTS_SEALED:
            self._results.evaluate(policy.policy_id)
        elif state.phase is AutomationClosurePhase.COMPARISON_COMPLETE:
            self._conclusions.publish(policy.policy_id)
        else:  # pragma: no cover - exhaustive enum guard
            raise LabRegistryStateError("M5.3 cannot advance the recovered closure phase")
        return self.recover(automation_id)
