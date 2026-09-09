from __future__ import annotations

import hmac
import os
import sys
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import UUID, uuid5

from codexia_manual_agent.authority import ActionProposal, AuthorizationReceipt
from codexia_manual_agent.domain.capabilities import Capability
from codexia_manual_agent.execution import ProcessLimits
from codexia_manual_agent.execution.process import PROCESS_ACTION
from codexia_manual_agent.lab.automation import SqliteAutomationPlanRegistry
from codexia_manual_agent.lab.comparison import SqliteComparisonRegistry
from codexia_manual_agent.lab.errors import (
    InvalidLabRecordError,
    LabPersistenceIntegrityError,
    LabRegistryStateError,
)
from codexia_manual_agent.lab.execution_registry import (
    RunExecutionPhase,
    RunExecutionRecovery,
    SqliteRunExecutionRegistry,
)
from codexia_manual_agent.lab.governed_python import (
    MAX_RESULT_BYTES,
    GovernedPythonJsonRunner,
    PreparedPythonJsonRun,
    PythonJsonExperimentSpec,
    SqlitePhysicalEvidenceRegistry,
)
from codexia_manual_agent.lab.models import ExperimentManifest, ExperimentRun
from codexia_manual_agent.lab.registry import (
    LabRegistryRecovery,
    RegisteredRunSnapshot,
    SqliteLabRegistry,
)
from codexia_manual_agent.session_events import (
    ActionRecoveryState,
    EventKind,
    SessionEventStateError,
    SessionRecovery,
    SqliteSessionEventStore,
    UnknownSessionError,
)


AUTOMATION_STATE_SCHEMA_VERSION = 1
_AUTOMATION_SESSION_PROMPT_VERSION = "m5.2"
_AUTOMATION_SESSION_MODE = "bounded-automation"
_AUTOMATION_SESSION_PROVIDER = "local-m5.2"
_PREPARE_SUMMARY = "Execute one exact M4.3.2 inline Python JSON experiment."


class AutomationRunArm(StrEnum):
    BASELINE = "baseline"
    CANDIDATE = "candidate"


class AutomationPhase(StrEnum):
    READY = "ready"
    RUN_REGISTERED = "run_registered"
    PAUSED_AUTHORIZATION_REQUIRED = "paused_authorization_required"
    FINALIZATION_REQUIRED = "finalization_required"
    RUN_SET_COMPLETE = "run_set_complete"
    STOPPED_BUDGET = "stopped_budget"
    STOPPED_ERROR = "stopped_error"


@dataclass(frozen=True, slots=True)
class AutomationRunSlot:
    arm: AutomationRunArm
    ordinal: int
    seed: int
    experiment_id: str
    manifest_digest: str
    run_id: str
    m3_session_id: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "arm": self.arm.value,
            "ordinal": self.ordinal,
            "seed": self.seed,
            "experiment_id": self.experiment_id,
            "manifest_digest": self.manifest_digest,
            "run_id": self.run_id,
            "m3_session_id": self.m3_session_id,
        }


@dataclass(frozen=True, slots=True)
class AutomationState:
    schema_version: int
    automation_id: str
    plan_digest: str
    phase: AutomationPhase
    steps_used: int
    runs_started: int
    runs_completed: int
    required_runs: int
    active_slot: AutomationRunSlot | None
    proposal: ActionProposal | None
    detail: str | None

    def __post_init__(self) -> None:
        if self.schema_version != AUTOMATION_STATE_SCHEMA_VERSION:
            raise InvalidLabRecordError("Unsupported M5.2 automation state schema")
        try:
            parsed = UUID(self.automation_id)
        except (TypeError, ValueError, AttributeError) as exc:
            raise InvalidLabRecordError("automation_id must be a canonical UUID") from exc
        if str(parsed) != self.automation_id:
            raise InvalidLabRecordError("automation_id must be a canonical UUID")
        if type(self.steps_used) is not int or self.steps_used < 0:
            raise InvalidLabRecordError("steps_used must be non-negative")
        if type(self.runs_started) is not int or self.runs_started < 0:
            raise InvalidLabRecordError("runs_started must be non-negative")
        if type(self.runs_completed) is not int or self.runs_completed < 0:
            raise InvalidLabRecordError("runs_completed must be non-negative")
        if type(self.required_runs) is not int or self.required_runs < 1:
            raise InvalidLabRecordError("required_runs must be positive")
        if not self.runs_completed <= self.runs_started <= self.required_runs:
            raise InvalidLabRecordError("M5.2 run counters are inconsistent")
        phase = AutomationPhase(self.phase)
        object.__setattr__(self, "phase", phase)
        if phase is AutomationPhase.PAUSED_AUTHORIZATION_REQUIRED and self.proposal is None:
            raise InvalidLabRecordError("Authorization pause requires the exact pending proposal")
        if self.proposal is not None and not isinstance(self.proposal, ActionProposal):
            raise InvalidLabRecordError("proposal must be an ActionProposal or null")
        if self.detail is not None and not isinstance(self.detail, str):
            raise InvalidLabRecordError("detail must be text or null")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "automation_id": self.automation_id,
            "plan_digest": self.plan_digest,
            "phase": self.phase.value,
            "steps_used": self.steps_used,
            "runs_started": self.runs_started,
            "runs_completed": self.runs_completed,
            "required_runs": self.required_runs,
            "active_slot": self.active_slot.to_dict() if self.active_slot else None,
            "proposal": self.proposal.to_dict() if self.proposal else None,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class _SlotRecovery:
    slot: AutomationRunSlot
    manifest: ExperimentManifest
    run: RegisteredRunSnapshot | None
    execution: RunExecutionRecovery | None
    session: SessionRecovery | None
    stage: int
    terminal_error: str | None
    physical_verified: bool


class GovernedAutomationStateMachine:
    """M5.2 coordinator derived from the existing durable M3/M4 authority spine.

    `advance()` performs at most one non-authority transition. Process execution is
    available only through `continue_authorized()` with an externally supplied
    receipt for the exact pending M4.3 proposal.
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
            raise ValueError("M5.2 requires one M3/M4 SQLite trust domain")
        self._lab = lab_registry
        self._m3 = session_store
        self._comparisons = SqliteComparisonRegistry(lab_registry)
        self._plans = SqliteAutomationPlanRegistry(self._comparisons, lab_registry)
        self._executions = SqliteRunExecutionRegistry(lab_registry, session_store)
        self._physical = SqlitePhysicalEvidenceRegistry(lab_registry, self._executions)
        self._runner = GovernedPythonJsonRunner(
            lab_registry,
            session_store,
            self._executions,
            self._physical,
        )

    @property
    def database_path(self) -> Path:
        return self._lab.database_path

    @staticmethod
    def _slot_id(automation_id: str, arm: AutomationRunArm, ordinal: int, seed: int) -> str:
        return str(
            uuid5(
                UUID(automation_id),
                f"codexia:m5.2:run:{arm.value}:{ordinal}:{seed}",
            )
        )

    @staticmethod
    def _session_id(automation_id: str, arm: AutomationRunArm, ordinal: int, seed: int) -> str:
        return str(
            uuid5(
                UUID(automation_id),
                f"codexia:m5.2:m3-session:{arm.value}:{ordinal}:{seed}",
            )
        )

    @staticmethod
    def _session_payload(workspace_root: str) -> dict[str, Any]:
        return {
            "workspace": workspace_root,
            "prompt_version": _AUTOMATION_SESSION_PROMPT_VERSION,
            "mode": _AUTOMATION_SESSION_MODE,
            "capabilities": ("execute_process",),
            "provider": _AUTOMATION_SESSION_PROVIDER,
            "title": None,
            "model": None,
            "reasoning_effort": None,
        }

    def _slots(self, automation_id: str):
        frozen_plan = self._plans.recover(automation_id)
        frozen_policy = self._comparisons.recover_policy(frozen_plan.plan.policy_id)
        plan = frozen_plan.plan
        policy = frozen_policy.policy
        if plan.policy_id != policy.policy_id:
            raise LabPersistenceIntegrityError("M5 plan no longer binds the exact M4.4 policy")
        if plan.required_runs != len(policy.seeds) * 2:
            raise LabPersistenceIntegrityError("M5 plan required run count drifted from M4.4 policy")
        arms = (
            (
                AutomationRunArm.BASELINE,
                policy.baseline_experiment_id,
                policy.baseline_manifest_digest,
            ),
            (
                AutomationRunArm.CANDIDATE,
                policy.candidate_experiment_id,
                policy.candidate_manifest_digest,
            ),
        )
        slots: list[AutomationRunSlot] = []
        for arm, experiment_id, manifest_digest in arms:
            for ordinal, seed in enumerate(policy.seeds):
                slots.append(
                    AutomationRunSlot(
                        arm=arm,
                        ordinal=ordinal,
                        seed=seed,
                        experiment_id=experiment_id,
                        manifest_digest=manifest_digest,
                        run_id=self._slot_id(automation_id, arm, ordinal, seed),
                        m3_session_id=self._session_id(automation_id, arm, ordinal, seed),
                    )
                )
        return frozen_plan, frozen_policy, tuple(slots)

    @staticmethod
    def _manifest_for_slot(
        slot: AutomationRunSlot,
        baseline: LabRegistryRecovery,
        candidate: LabRegistryRecovery,
    ) -> tuple[LabRegistryRecovery, ExperimentManifest]:
        recovery = baseline if slot.arm is AutomationRunArm.BASELINE else candidate
        if recovery.experiment_id != slot.experiment_id:
            raise LabPersistenceIntegrityError("M5.2 arm recovery references another experiment")
        if not hmac.compare_digest(recovery.manifest.manifest_digest, slot.manifest_digest):
            raise LabPersistenceIntegrityError("M5.2 arm manifest drifted from frozen policy")
        return recovery, recovery.manifest

    @staticmethod
    def _validate_arm_runs(
        automation_id: str,
        arm: AutomationRunArm,
        seeds: tuple[int, ...],
        recovery: LabRegistryRecovery,
    ) -> None:
        for snapshot in recovery.runs.values():
            run = snapshot.run
            if run.ordinal < 0 or run.ordinal >= len(seeds):
                raise LabPersistenceIntegrityError(
                    "M5.2 arm contains a run ordinal outside the frozen comparison set"
                )
            expected_seed = seeds[run.ordinal]
            expected_id = GovernedAutomationStateMachine._slot_id(
                automation_id,
                arm,
                run.ordinal,
                expected_seed,
            )
            if run.seed != expected_seed or run.run_id != expected_id:
                raise LabPersistenceIntegrityError(
                    "M5.2 arm contains a foreign run identity/seed in a declared slot"
                )

    def _recover_session(self, slot: AutomationRunSlot, workspace_root: str) -> SessionRecovery | None:
        try:
            recovery = self._m3.recover(slot.m3_session_id)
        except UnknownSessionError:
            return None
        if not recovery.events or recovery.events[0].kind is not EventKind.SESSION_STARTED:
            raise LabPersistenceIntegrityError("M5.2 M3 session lacks its exact start event")
        if dict(recovery.events[0].payload) != self._session_payload(workspace_root):
            raise LabPersistenceIntegrityError(
                "M5.2 deterministic M3 session does not bind the frozen workspace/profile"
            )
        return recovery

    @staticmethod
    def _expected_output(slot: AutomationRunSlot) -> str:
        return f".codexia/m4-runs/{slot.run_id}/result.json"

    @staticmethod
    def _expected_executable() -> str:
        try:
            executable = Path(sys.executable).resolve(strict=True)
        except OSError as exc:  # pragma: no cover - running interpreter invariant
            raise LabPersistenceIntegrityError("Current Python executable cannot be resolved") from exc
        return str(executable)

    def _validate_bound_proposal(
        self,
        *,
        slot: AutomationRunSlot,
        manifest: ExperimentManifest,
        workspace_root: str,
        execution: RunExecutionRecovery,
        session: SessionRecovery,
    ) -> ActionProposal:
        proposal = execution.binding.proposal
        spec = PythonJsonExperimentSpec.from_manifest(manifest)
        executable = self._expected_executable()
        parameters = proposal.to_dict()["parameters"]
        expected_argv = [
            executable,
            "-I",
            "-c",
            spec.source,
            slot.run_id,
            self._expected_output(slot),
            spec.input_json,
        ]
        expected_limits = ProcessLimits(
            timeout_seconds=30.0,
            max_stdout_bytes=MAX_RESULT_BYTES,
            max_stderr_bytes=65_536,
        ).to_dict()
        if (
            proposal.capability is not Capability.EXECUTE_PROCESS
            or proposal.action != PROCESS_ACTION
            or proposal.workspace_root != workspace_root
            or proposal.summary != _PREPARE_SUMMARY
            or parameters.get("argv") != expected_argv
            or parameters.get("resolved_executable") != executable
            or parameters.get("cwd") != "."
            or parameters.get("environment_profile") != "minimal-v1"
            or parameters.get("limits") != expected_limits
        ):
            raise LabPersistenceIntegrityError(
                "M5.2 execution binding is not the exact M4.3 Python proposal for this slot"
            )
        if len(session.actions) != 1:
            raise LabPersistenceIntegrityError(
                "M5.2 deterministic run session must contain exactly one action proposal"
            )
        action = session.actions[0]
        if (
            action.proposal.proposal_id != proposal.proposal_id
            or action.proposal.to_dict() != proposal.to_dict()
        ):
            raise LabPersistenceIntegrityError(
                "M5.2 pending proposal disagrees with authoritative M3 action"
            )
        return proposal

    def _recover_slot(
        self,
        *,
        slot: AutomationRunSlot,
        experiment: LabRegistryRecovery,
        manifest: ExperimentManifest,
        workspace_root: str,
    ) -> _SlotRecovery:
        snapshot = experiment.runs.get(slot.run_id)
        session = self._recover_session(slot, workspace_root)
        if snapshot is None:
            if session is not None:
                raise LabPersistenceIntegrityError(
                    "M5.2 deterministic M3 session exists before its exact durable run"
                )
            return _SlotRecovery(slot, manifest, None, None, None, 0, None, False)

        run = snapshot.run
        if (
            run.run_id != slot.run_id
            or run.experiment_id != slot.experiment_id
            or run.ordinal != slot.ordinal
            or run.seed != slot.seed
            or not hmac.compare_digest(run.manifest_digest, slot.manifest_digest)
        ):
            raise LabPersistenceIntegrityError("M5.2 durable run does not match its exact slot")

        try:
            execution = self._executions.recover(slot.run_id)
        except InvalidLabRecordError:
            execution = None
        if execution is None:
            if snapshot.evidence_sealed:
                raise LabPersistenceIntegrityError(
                    "M5.2 run is sealed without governed execution provenance"
                )
            if session is not None and session.actions:
                return _SlotRecovery(
                    slot,
                    manifest,
                    snapshot,
                    None,
                    session,
                    2,
                    "A durable M3 proposal exists without its M4 execution binding; "
                    "M5.2 will not mint a replacement approval target",
                    False,
                )
            return _SlotRecovery(slot, manifest, snapshot, None, session, 1, None, False)

        if session is None:
            raise LabPersistenceIntegrityError("M4 execution binding references a missing M5.2 M3 session")
        proposal = self._validate_bound_proposal(
            slot=slot,
            manifest=manifest,
            workspace_root=workspace_root,
            execution=execution,
            session=session,
        )
        action = session.actions[0]
        output = Path(workspace_root) / PurePosixPath(self._expected_output(slot))

        if execution.phase is RunExecutionPhase.BOUND:
            if action.state is not ActionRecoveryState.PROPOSED:
                return _SlotRecovery(
                    slot,
                    manifest,
                    snapshot,
                    execution,
                    session,
                    3,
                    "M3 authority advanced beyond PROPOSED without matching M4 execution "
                    "chronology; replay is forbidden",
                    False,
                )
            if os.path.lexists(output):
                return _SlotRecovery(
                    slot,
                    manifest,
                    snapshot,
                    execution,
                    session,
                    2,
                    "Physical output exists before governed execution evidence; M5.2 "
                    "will not overwrite or replay",
                    False,
                )
            if proposal.to_dict() != execution.binding.proposal.to_dict():  # pragma: no cover
                raise LabPersistenceIntegrityError("M5.2 proposal changed during recovery")
            return _SlotRecovery(slot, manifest, snapshot, execution, session, 2, None, False)

        if execution.phase is RunExecutionPhase.AUTHORIZED:
            return _SlotRecovery(
                slot,
                manifest,
                snapshot,
                execution,
                session,
                3,
                "Authorization is durable but no terminal execution evidence exists; "
                "M5.2 fails closed instead of replaying the process",
                False,
            )

        if not execution.execution_succeeded:
            return _SlotRecovery(
                slot,
                manifest,
                snapshot,
                execution,
                session,
                3,
                "Governed process execution produced a terminal failure",
                False,
            )
        try:
            self._physical.recover(slot.run_id)
        except InvalidLabRecordError:
            return _SlotRecovery(
                slot,
                manifest,
                snapshot,
                execution,
                session,
                3,
                "Successful governed execution lacks verified physical evidence; "
                "M5.2 will not rerun it",
                False,
            )
        return _SlotRecovery(slot, manifest, snapshot, execution, session, 3, None, True)

    def _derived(self, automation_id: str):
        frozen_plan, frozen_policy, slots = self._slots(automation_id)
        plan = frozen_plan.plan
        policy = frozen_policy.policy
        baseline = self._lab.recover_experiment(policy.baseline_experiment_id)
        candidate = self._lab.recover_experiment(policy.candidate_experiment_id)
        self._validate_arm_runs(
            automation_id,
            AutomationRunArm.BASELINE,
            policy.seeds,
            baseline,
        )
        self._validate_arm_runs(
            automation_id,
            AutomationRunArm.CANDIDATE,
            policy.seeds,
            candidate,
        )

        recovered: list[_SlotRecovery] = []
        seen_incomplete = False
        for slot in slots:
            experiment, manifest = self._manifest_for_slot(slot, baseline, candidate)
            item = self._recover_slot(
                slot=slot,
                experiment=experiment,
                manifest=manifest,
                workspace_root=plan.workspace_root,
            )
            if seen_incomplete and item.stage != 0:
                raise LabPersistenceIntegrityError(
                    "M5.2 durable run progression is not a contiguous deterministic prefix"
                )
            if item.stage < 3 or item.run is None or not item.run.evidence_sealed:
                seen_incomplete = True
            recovered.append(item)

        for arm, experiment in (
            (AutomationRunArm.BASELINE, baseline),
            (AutomationRunArm.CANDIDATE, candidate),
        ):
            if not experiment.experiment_sealed:
                continue
            arm_items = [item for item in recovered if item.slot.arm is arm]
            if any(
                item.stage != 3
                or item.terminal_error is not None
                or not item.physical_verified
                or item.run is None
                or not item.run.evidence_sealed
                for item in arm_items
            ):
                raise LabPersistenceIntegrityError(
                    "M5.2 experiment was sealed before its exact automated run set completed"
                )
        return frozen_plan, frozen_policy, baseline, candidate, tuple(recovered)

    def recover(self, automation_id: str) -> AutomationState:
        frozen_plan, _frozen_policy, baseline, candidate, recovered = self._derived(automation_id)
        plan = frozen_plan.plan
        steps_used = sum(item.stage for item in recovered)
        runs_started = sum(1 for item in recovered if item.stage >= 1)
        runs_completed = sum(
            1
            for item in recovered
            if item.stage == 3
            and item.terminal_error is None
            and item.physical_verified
            and item.run is not None
            and item.run.evidence_sealed
        )
        if steps_used > plan.budget.max_steps or runs_started > plan.budget.max_runs:
            raise LabPersistenceIntegrityError(
                "Durable M5.2 work exceeds the previously frozen automation budget"
            )

        active: _SlotRecovery | None = None
        for item in recovered:
            if item.terminal_error is not None:
                return self._state(
                    plan,
                    AutomationPhase.STOPPED_ERROR,
                    steps_used,
                    runs_started,
                    runs_completed,
                    item,
                    item.terminal_error,
                )
            if item.stage == 3 and item.physical_verified:
                if item.run is not None and item.run.evidence_sealed:
                    continue
                return self._state(
                    plan,
                    AutomationPhase.FINALIZATION_REQUIRED,
                    steps_used,
                    runs_started,
                    runs_completed,
                    item,
                    "Verified physical evidence is durable; only the M4 run seal remains",
                )
            active = item
            break

        if active is None:
            return self._state(
                plan,
                AutomationPhase.RUN_SET_COMPLETE,
                steps_used,
                runs_started,
                runs_completed,
                None,
                "Exact frozen run set is complete; experiment sealing, comparison, and "
                "conclusion remain M5.3 work",
            )

        if active.stage == 0:
            if steps_used >= plan.budget.max_steps or runs_started >= plan.budget.max_runs:
                phase = AutomationPhase.STOPPED_BUDGET
                detail = "Frozen automation budget does not admit another run registration"
            else:
                phase = AutomationPhase.READY
                detail = None
        elif active.stage == 1:
            if steps_used >= plan.budget.max_steps:
                phase = AutomationPhase.STOPPED_BUDGET
                detail = "Frozen step budget does not admit preparation of the registered run"
            else:
                phase = AutomationPhase.RUN_REGISTERED
                detail = None
        elif active.stage == 2:
            if steps_used >= plan.budget.max_steps:
                phase = AutomationPhase.STOPPED_BUDGET
                detail = "Frozen step budget does not admit execution of the prepared run"
            else:
                phase = AutomationPhase.PAUSED_AUTHORIZATION_REQUIRED
                detail = "External authorization is required for the exact durable process proposal"
        else:  # pragma: no cover - stage-3 cases return above
            raise LabPersistenceIntegrityError("M5.2 could not derive a valid active phase")
        return self._state(
            plan,
            phase,
            steps_used,
            runs_started,
            runs_completed,
            active,
            detail,
        )

    @staticmethod
    def _state(
        plan,
        phase: AutomationPhase,
        steps_used: int,
        runs_started: int,
        runs_completed: int,
        active: _SlotRecovery | None,
        detail: str | None,
    ) -> AutomationState:
        proposal = active.execution.binding.proposal if active and active.execution else None
        return AutomationState(
            schema_version=AUTOMATION_STATE_SCHEMA_VERSION,
            automation_id=plan.automation_id,
            plan_digest=plan.plan_digest,
            phase=phase,
            steps_used=steps_used,
            runs_started=runs_started,
            runs_completed=runs_completed,
            required_runs=plan.required_runs,
            active_slot=active.slot if active else None,
            proposal=proposal,
            detail=detail,
        )

    def _ensure_session(self, slot: AutomationRunSlot, workspace_root: str) -> SessionRecovery:
        recovery = self._recover_session(slot, workspace_root)
        if recovery is None:
            try:
                self._m3.start_session(
                    session_id=slot.m3_session_id,
                    payload=self._session_payload(workspace_root),
                )
            except SessionEventStateError:
                recovery = self._recover_session(slot, workspace_root)
                if recovery is None:
                    raise
            else:
                recovery = self._recover_session(slot, workspace_root)
        assert recovery is not None
        return recovery

    def advance(self, automation_id: str) -> AutomationState:
        state = self.recover(automation_id)
        if state.phase is AutomationPhase.READY:
            assert state.active_slot is not None
            slot = state.active_slot
            manifest = self._lab.recover_experiment(slot.experiment_id).manifest
            self._lab.register_run(
                ExperimentRun.create(
                    manifest=manifest,
                    ordinal=slot.ordinal,
                    seed=slot.seed,
                    run_id=slot.run_id,
                )
            )
            return self.recover(automation_id)

        if state.phase is AutomationPhase.RUN_REGISTERED:
            assert state.active_slot is not None
            slot = state.active_slot
            frozen_plan = self._plans.recover(automation_id)
            session = self._ensure_session(slot, frozen_plan.plan.workspace_root)
            if session.actions:
                raise LabRegistryStateError(
                    "M5.2 deterministic M3 session already contains an unbound proposal; "
                    "recovery must fail closed rather than create another"
                )
            self._runner.prepare(
                run_id=slot.run_id,
                m3_session_id=slot.m3_session_id,
                workspace=frozen_plan.plan.workspace_root,
            )
            return self.recover(automation_id)

        if state.phase is AutomationPhase.FINALIZATION_REQUIRED:
            assert state.active_slot is not None
            run = self._lab.recover_for_run(state.active_slot.run_id).run(
                state.active_slot.run_id
            ).run
            self._lab.seal_run(run.run_id, run.run_digest)
            return self.recover(automation_id)

        return state

    def _prepared(self, automation_id: str, state: AutomationState) -> PreparedPythonJsonRun:
        if state.phase is not AutomationPhase.PAUSED_AUTHORIZATION_REQUIRED:
            raise LabRegistryStateError(
                "M5.2 can consume external authorization only at the exact paused boundary"
            )
        assert state.active_slot is not None
        slot = state.active_slot
        frozen_plan, _frozen_policy, _baseline, _candidate, recovered = self._derived(
            automation_id
        )
        active = next(item for item in recovered if item.slot.run_id == slot.run_id)
        if active.execution is None or active.run is None or active.session is None:
            raise LabPersistenceIntegrityError("Paused M5.2 state lacks exact prepared provenance")
        proposal = self._validate_bound_proposal(
            slot=slot,
            manifest=active.manifest,
            workspace_root=frozen_plan.plan.workspace_root,
            execution=active.execution,
            session=active.session,
        )
        if active.execution.phase is not RunExecutionPhase.BOUND:
            raise LabRegistryStateError("Pending M5.2 run is no longer awaiting authorization")
        if proposal.to_dict() != active.execution.binding.proposal.to_dict():  # pragma: no cover
            raise LabPersistenceIntegrityError("Pending M5.2 proposal changed during recovery")
        return PreparedPythonJsonRun(
            run=active.run.run,
            manifest_digest=active.manifest.manifest_digest,
            m3_session_id=slot.m3_session_id,
            output_logical_path=self._expected_output(slot),
            spec=PythonJsonExperimentSpec.from_manifest(active.manifest),
            binding=active.execution.binding,
        )

    def continue_authorized(
        self,
        automation_id: str,
        *,
        receipt: AuthorizationReceipt,
    ) -> AutomationState:
        if not isinstance(receipt, AuthorizationReceipt):
            raise TypeError("receipt must be an AuthorizationReceipt")
        state = self.recover(automation_id)
        prepared = self._prepared(automation_id, state)
        result = self._runner.execute_authorized(prepared, receipt=receipt)
        if result.execution.phase is not RunExecutionPhase.OBSERVED:
            raise LabRegistryStateError("Governed M5.2 execution did not reach terminal observation")
        if result.physical is not None:
            self._lab.seal_run(prepared.run.run_id, prepared.run.run_digest)
        return self.recover(automation_id)
