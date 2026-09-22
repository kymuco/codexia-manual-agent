from __future__ import annotations

import hmac

from codexia_manual_agent.role_core.admission import RoleAdmission
from codexia_manual_agent.role_core.models import (
    CognitionOutcome,
    CognitionRequest,
    RoleRunSnapshot,
    RoleRunState,
)
from codexia_manual_agent.role_core.ports import CognitionPort
from codexia_manual_agent.role_core.projection import project_role_run
from codexia_manual_agent.role_core.transport_models import (
    CognitionHandoff,
    CognitionPortRequest,
)
from codexia_manual_agent.role_core.transport_projection import (
    project_cognition_handoffs,
)
from codexia_manual_agent.work_core import WorkStore
from codexia_manual_agent.workflow_core import (
    WorkflowRunState,
    project_workflow_run,
)


class CognitionTransportBridgeError(RuntimeError):
    """Base failure for the G2.13 cognition handoff boundary."""


class CognitionTransportBindingError(CognitionTransportBridgeError):
    """Transport dispatch changed exact request/Role/Work binding."""


class CognitionTransportRoutingConflictError(CognitionTransportBridgeError):
    """One cognition request was already handed to another port."""


class CognitionTransportPortError(CognitionTransportBridgeError):
    """Selected cognition port failed after durable handoff admission."""


class CognitionTransportBridge:
    """Conservative one-handoff bridge to a selected CognitionPort.

    Order is deliberate:
      1. recover exact durable REQUESTED RoleRun;
      2. verify caller's materialized CognitionRequest;
      3. durably admit one CognitionHandoff;
      4. only then call the selected CognitionPort;
      5. bind any returned CognitionOutcome to current Work chronology;
      6. admit it through the existing RoleAdmission boundary.

    Existing handoff presence suppresses redispatch, including after restart or
    after a port exception. Handoff itself is not attempt or response proof.
    """

    def __init__(self, store: WorkStore) -> None:
        self._store = store
        self._role_admission = RoleAdmission(store)

    def dispatch_once(
        self,
        request: CognitionRequest,
        port: CognitionPort,
    ) -> RoleRunSnapshot:
        if not isinstance(request, CognitionRequest):
            raise TypeError("request must be CognitionRequest")

        events = self._store.events(request.work_id)
        recovered = project_role_run(events, request.role_run_id)
        self._validate_request(request=request, role=recovered)

        existing = next(
            (
                handoff
                for handoff in project_cognition_handoffs(events)
                if handoff.request_id == request.request_id
            ),
            None,
        )
        if existing is not None:
            if existing.port_id != port.port_id:
                raise CognitionTransportRoutingConflictError(
                    "CognitionRequest is already bound to another port"
                )
            return recovered

        if recovered.state is not RoleRunState.REQUESTED:
            raise CognitionTransportBridgeError(
                f"RoleRun is {recovered.state.value}; cognition cannot be dispatched"
            )

        workflow = project_workflow_run(
            events,
            request.workflow_run_id,
        )
        if workflow.state is not WorkflowRunState.ACTIVE:
            raise CognitionTransportBridgeError(
                "New CognitionHandoff requires active requesting WorkflowRun"
            )
        if not hmac.compare_digest(
            workflow.run.run_digest,
            request.workflow_run_digest,
        ):
            raise CognitionTransportBindingError(
                "CognitionRequest changed WorkflowRun binding"
            )

        current = self._store.snapshot(request.work_id)
        handoff = CognitionHandoff.create(
            role=recovered,
            snapshot=current,
            port_id=port.port_id,
        )
        self._store.append(
            request.work_id,
            expected_revision=current.revision,
            event=handoff.to_event(),
        )

        port_request = CognitionPortRequest(
            handoff=handoff,
            request=request,
        )
        try:
            outcome = port.complete(port_request)
        except Exception as exc:
            raise CognitionTransportPortError(
                "CognitionPort failed after durable CognitionHandoff admission"
            ) from exc

        if outcome is None:
            return project_role_run(
                self._store.events(request.work_id),
                request.role_run_id,
            )
        if not isinstance(outcome, CognitionOutcome):
            raise CognitionTransportPortError(
                "CognitionPort returned neither CognitionOutcome nor None"
            )

        self._validate_outcome(outcome=outcome, request=request)
        return self.record_outcome(
            outcome,
            port_id=handoff.port_id,
        )

    def record_outcome(
        self,
        outcome: CognitionOutcome,
        *,
        port_id: str,
    ) -> RoleRunSnapshot:
        """Record an asynchronous/synchronous port result without redispatch."""

        if not isinstance(outcome, CognitionOutcome):
            raise TypeError("outcome must be CognitionOutcome")

        events = self._store.events(outcome.work_id)
        handoff = next(
            (
                item
                for item in project_cognition_handoffs(events)
                if item.request_id == outcome.request_id
            ),
            None,
        )
        if handoff is None:
            raise CognitionTransportBindingError(
                "CognitionOutcome has no durable cognition handoff"
            )
        if handoff.port_id != port_id:
            raise CognitionTransportRoutingConflictError(
                "CognitionOutcome callback came from another port"
            )
        if not hmac.compare_digest(
            handoff.request_digest,
            outcome.request_digest,
        ):
            raise CognitionTransportBindingError(
                "CognitionOutcome callback changed request binding"
            )
        if handoff.role_run_id != outcome.role_run_id:
            raise CognitionTransportBindingError(
                "CognitionOutcome callback changed RoleRun identity"
            )
        if not hmac.compare_digest(
            handoff.role_run_digest,
            outcome.role_run_digest,
        ):
            raise CognitionTransportBindingError(
                "CognitionOutcome callback changed RoleRun binding"
            )
        if handoff.work_id != outcome.work_id:
            raise CognitionTransportBindingError(
                "CognitionOutcome callback crossed Work identity"
            )
        if not hmac.compare_digest(
            handoff.work_digest,
            outcome.work_digest,
        ):
            raise CognitionTransportBindingError(
                "CognitionOutcome callback changed Work binding"
            )

        current = self._store.snapshot(outcome.work_id)
        rebound = outcome.bind_to_snapshot(current)
        return self._role_admission.admit_outcome(rebound)

    @staticmethod
    def _validate_request(
        *,
        request: CognitionRequest,
        role: RoleRunSnapshot,
    ) -> None:
        if role.request_id != request.request_id:
            raise CognitionTransportBindingError(
                "Caller CognitionRequest differs from durable request identity"
            )
        if role.request_digest is None or not hmac.compare_digest(
            role.request_digest,
            request.request_digest,
        ):
            raise CognitionTransportBindingError(
                "Caller CognitionRequest differs from durable request binding"
            )
        run = role.run
        if request.role_run_id != run.role_run_id:
            raise CognitionTransportBindingError(
                "CognitionRequest changed RoleRun identity"
            )
        if not hmac.compare_digest(
            request.role_run_digest,
            run.run_digest,
        ):
            raise CognitionTransportBindingError(
                "CognitionRequest changed RoleRun binding"
            )
        if request.work_id != run.work_id:
            raise CognitionTransportBindingError(
                "CognitionRequest crossed Work identity"
            )
        if not hmac.compare_digest(
            request.work_digest,
            run.work_digest,
        ):
            raise CognitionTransportBindingError(
                "CognitionRequest changed Work binding"
            )
        if request.workflow_run_id != run.workflow_run_id:
            raise CognitionTransportBindingError(
                "CognitionRequest changed WorkflowRun identity"
            )
        if not hmac.compare_digest(
            request.workflow_run_digest,
            run.workflow_run_digest,
        ):
            raise CognitionTransportBindingError(
                "CognitionRequest changed WorkflowRun binding"
            )

    @staticmethod
    def _validate_outcome(
        *,
        outcome: CognitionOutcome,
        request: CognitionRequest,
    ) -> None:
        if outcome.request_id != request.request_id:
            raise CognitionTransportBindingError(
                "CognitionPort response changed request identity"
            )
        if not hmac.compare_digest(
            outcome.request_digest,
            request.request_digest,
        ):
            raise CognitionTransportBindingError(
                "CognitionPort response changed request binding"
            )
        if outcome.role_run_id != request.role_run_id:
            raise CognitionTransportBindingError(
                "CognitionPort response changed RoleRun identity"
            )
        if not hmac.compare_digest(
            outcome.role_run_digest,
            request.role_run_digest,
        ):
            raise CognitionTransportBindingError(
                "CognitionPort response changed RoleRun binding"
            )
        if outcome.work_id != request.work_id:
            raise CognitionTransportBindingError(
                "CognitionPort response crossed Work identity"
            )
        if not hmac.compare_digest(
            outcome.work_digest,
            request.work_digest,
        ):
            raise CognitionTransportBindingError(
                "CognitionPort response changed Work binding"
            )
        if outcome.workflow_run_id != request.workflow_run_id:
            raise CognitionTransportBindingError(
                "CognitionPort response changed WorkflowRun identity"
            )
        if not hmac.compare_digest(
            outcome.workflow_run_digest,
            request.workflow_run_digest,
        ):
            raise CognitionTransportBindingError(
                "CognitionPort response changed WorkflowRun binding"
            )
