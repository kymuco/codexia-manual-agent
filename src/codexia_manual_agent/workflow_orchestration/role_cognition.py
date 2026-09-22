from __future__ import annotations

import hmac
from dataclasses import dataclass
from typing import Protocol

from codexia_manual_agent.pack_core import (
    PackWorkflowBinding,
    project_workflow_pack_binding,
)
from codexia_manual_agent.role_core import (
    COGNITION_REQUESTED_EVENT,
    CognitionRequest,
    ContextProjection,
    InvalidRoleRecord,
    RoleBinding,
    RoleRunSnapshot,
    RoleRunState,
    project_role_run,
)
from codexia_manual_agent.work_core import WorkEvent, WorkSnapshot
from codexia_manual_agent.workflow_core import (
    WorkflowRunState,
    project_workflow_run,
)
from codexia_manual_agent.workflow_orchestration.step import (
    WorkflowReadStorePort,
)


class RoleCognitionMaterializationError(RuntimeError):
    """Base failure for G2.12 exact cognition request materialization."""


class RoleCognitionReadConflictError(RoleCognitionMaterializationError):
    """Work changed while one exact cognition read view was recovered."""


class RoleCognitionPackRequiredError(RoleCognitionMaterializationError):
    """Modern cognition materialization requires an exact durable Pack pin."""


class RoleCognitionStateError(RoleCognitionMaterializationError):
    """Role/Workflow lifecycle cannot support the requested materialization."""


class RoleCognitionMaterialBindingError(RoleCognitionMaterializationError):
    """Resolved plaintext does not match durable semantic/content digests."""


class RoleInstructionsMaterialPort(Protocol):
    """Resolve exact instruction bytes for one RoleBinding."""

    def resolve(self, binding: RoleBinding) -> str: ...


class ContextProjectionMaterialPort(Protocol):
    """Resolve exact context bytes for one ContextProjection."""

    def resolve(self, projection: ContextProjection) -> str: ...


@dataclass(frozen=True, slots=True)
class CognitionRequestMaterialization:
    """Ephemeral exact CognitionRequest plus bounded recovery provenance."""

    work_id: str
    workflow_run_id: str
    role_run_id: str
    work_revision: int
    work_event_digest: str | None
    pack_binding_digest: str
    role_binding_digest: str
    context_projection_digest: str
    request: CognitionRequest
    admitted: bool

    def __post_init__(self) -> None:
        for field_name, value in (
            ("work_id", self.work_id),
            ("workflow_run_id", self.workflow_run_id),
            ("role_run_id", self.role_run_id),
        ):
            if not isinstance(value, str) or not value:
                raise TypeError(f"{field_name} must be non-empty text")
        if type(self.work_revision) is not int or self.work_revision < 0:
            raise TypeError("work_revision must be a non-negative integer")
        if self.work_event_digest is not None and (
            not isinstance(self.work_event_digest, str)
            or len(self.work_event_digest) != 64
        ):
            raise TypeError("work_event_digest must be SHA-256 text or None")
        for field_name, value in (
            ("pack_binding_digest", self.pack_binding_digest),
            ("role_binding_digest", self.role_binding_digest),
            ("context_projection_digest", self.context_projection_digest),
        ):
            if not isinstance(value, str) or len(value) != 64:
                raise TypeError(f"{field_name} must be SHA-256 text")
        if not isinstance(self.request, CognitionRequest):
            raise TypeError("request must be CognitionRequest")
        if type(self.admitted) is not bool:
            raise TypeError("admitted must be bool")
        if self.request.work_id != self.work_id:
            raise RoleCognitionMaterialBindingError(
                "CognitionRequest crossed Work identity"
            )
        if self.request.workflow_run_id != self.workflow_run_id:
            raise RoleCognitionMaterialBindingError(
                "CognitionRequest crossed WorkflowRun identity"
            )
        if self.request.role_run_id != self.role_run_id:
            raise RoleCognitionMaterialBindingError(
                "CognitionRequest crossed RoleRun identity"
            )


class RoleCognitionMaterializationService:
    """Prepare or restart-rematerialize one exact cognition request.

    The service resolves plaintext only through explicit material ports. It
    performs no RoleAdmission, no CognitionPort call, no model invocation, no
    outcome construction/admission, no retry and no scheduling.
    """

    def __init__(
        self,
        *,
        store: WorkflowReadStorePort,
        instructions: RoleInstructionsMaterialPort,
        context: ContextProjectionMaterialPort,
    ) -> None:
        self._store = store
        self._instructions = instructions
        self._context = context

    def prepare_request(
        self,
        *,
        work_id: str,
        role_run_id: str,
    ) -> CognitionRequestMaterialization:
        events, snapshot, role, pack_binding = self._recover(
            work_id=work_id,
            role_run_id=role_run_id,
        )
        del events

        if role.state is not RoleRunState.ACTIVE:
            raise RoleCognitionStateError(
                f"RoleRun is {role.state.value}; a new cognition request "
                "cannot be prepared"
            )

        instructions, context = self._resolve_materials(role)
        try:
            request = CognitionRequest.create(
                role=role,
                snapshot=snapshot,
                instructions=instructions,
                context=context,
            )
        except InvalidRoleRecord as exc:
            raise RoleCognitionMaterialBindingError(
                "Resolved cognition material does not match RoleRun bindings"
            ) from exc

        return self._result(
            snapshot=snapshot,
            role=role,
            pack_binding=pack_binding,
            request=request,
            admitted=False,
        )

    def rematerialize_request(
        self,
        *,
        work_id: str,
        role_run_id: str,
    ) -> CognitionRequestMaterialization:
        events, snapshot, role, pack_binding = self._recover(
            work_id=work_id,
            role_run_id=role_run_id,
        )

        if role.state is not RoleRunState.REQUESTED:
            raise RoleCognitionStateError(
                f"RoleRun is {role.state.value}; no admitted cognition request "
                "is pending"
            )
        if role.request_id is None or role.request_digest is None:
            raise RoleCognitionStateError(
                "REQUESTED RoleRun lacks durable request identity"
            )

        record = self._durable_request_record(
            events=events,
            request_id=role.request_id,
        )
        instructions, context = self._resolve_materials(role)
        try:
            request = CognitionRequest.from_durable_dict(
                record,
                instructions=instructions,
                context=context,
            )
        except (InvalidRoleRecord, KeyError, TypeError, ValueError) as exc:
            raise RoleCognitionMaterialBindingError(
                "Durable cognition request cannot be rematerialized "
                "from supplied exact material"
            ) from exc

        if request.request_id != role.request_id:
            raise RoleCognitionMaterialBindingError(
                "Rematerialized request changed request identity"
            )
        if not hmac.compare_digest(
            request.request_digest,
            role.request_digest,
        ):
            raise RoleCognitionMaterialBindingError(
                "Rematerialized request changed durable request binding"
            )

        return self._result(
            snapshot=snapshot,
            role=role,
            pack_binding=pack_binding,
            request=request,
            admitted=True,
        )

    def _recover(
        self,
        *,
        work_id: str,
        role_run_id: str,
    ) -> tuple[
        tuple[WorkEvent, ...],
        WorkSnapshot,
        RoleRunSnapshot,
        PackWorkflowBinding,
    ]:
        events = self._store.events(work_id)
        snapshot = self._store.snapshot(work_id)
        self._validate_read_view(
            expected_work_id=work_id,
            events=events,
            snapshot=snapshot,
        )

        role = project_role_run(events, role_run_id)
        if role.run.work_id != work_id:
            raise RoleCognitionMaterialBindingError(
                "Recovered RoleRun belongs to another Work"
            )

        workflow = project_workflow_run(
            events,
            role.run.workflow_run_id,
        )
        if workflow.state is not WorkflowRunState.ACTIVE:
            raise RoleCognitionStateError(
                "Cognition cannot be materialized under terminal WorkflowRun"
            )
        if not hmac.compare_digest(
            workflow.run.run_digest,
            role.run.workflow_run_digest,
        ):
            raise RoleCognitionMaterialBindingError(
                "RoleRun changed WorkflowRun binding"
            )

        pack_binding = project_workflow_pack_binding(
            events,
            role.run.workflow_run_id,
        )
        if pack_binding is None:
            raise RoleCognitionPackRequiredError(
                "RoleRun Workflow has no durable PackWorkflowBinding"
            )

        return events, snapshot, role, pack_binding

    def _resolve_materials(
        self,
        role: RoleRunSnapshot,
    ) -> tuple[str, str]:
        instructions = self._instructions.resolve(role.run.binding)
        context = self._context.resolve(role.run.context)
        return instructions, context

    @staticmethod
    def _durable_request_record(
        *,
        events: tuple[WorkEvent, ...],
        request_id: str,
    ) -> dict[str, object]:
        matches = [event for event in events if event.event_id == request_id]
        if len(matches) != 1:
            raise RoleCognitionMaterialBindingError(
                "Durable cognition request event identity is not unique"
            )
        event = matches[0]
        if event.kind != COGNITION_REQUESTED_EVENT:
            raise RoleCognitionMaterialBindingError(
                "Durable request identity is bound to another event kind"
            )
        payload = event.to_dict()["payload"]
        if not isinstance(payload, dict) or set(payload) != {
            "_workflow",
            "payload",
        }:
            raise RoleCognitionMaterialBindingError(
                "Durable cognition request wrapper is not exact"
            )
        body = payload["payload"]
        if (
            not isinstance(body, dict)
            or set(body) != {"cognition_request"}
            or not isinstance(body["cognition_request"], dict)
        ):
            raise RoleCognitionMaterialBindingError(
                "Durable cognition request body is not exact"
            )
        return body["cognition_request"]

    @staticmethod
    def _validate_read_view(
        *,
        expected_work_id: str,
        events: tuple[WorkEvent, ...],
        snapshot: WorkSnapshot,
    ) -> None:
        if snapshot.work.work_id != expected_work_id:
            raise RoleCognitionReadConflictError(
                "Recovered snapshot crossed requested Work identity"
            )
        if snapshot.revision != len(events):
            raise RoleCognitionReadConflictError(
                "Work changed while recovering cognition materialization state"
            )
        expected_digest = None if not events else events[-1].event_digest
        if snapshot.last_event_digest != expected_digest:
            raise RoleCognitionReadConflictError(
                "Recovered snapshot does not match exact event chronology"
            )
        if events and any(
            event.work_id != snapshot.work.work_id
            for event in events
        ):
            raise RoleCognitionReadConflictError(
                "Recovered chronology crossed Work identity"
            )

    @staticmethod
    def _result(
        *,
        snapshot: WorkSnapshot,
        role: RoleRunSnapshot,
        pack_binding: PackWorkflowBinding,
        request: CognitionRequest,
        admitted: bool,
    ) -> CognitionRequestMaterialization:
        return CognitionRequestMaterialization(
            work_id=role.run.work_id,
            workflow_run_id=role.run.workflow_run_id,
            role_run_id=role.run.role_run_id,
            work_revision=snapshot.revision,
            work_event_digest=snapshot.last_event_digest,
            pack_binding_digest=pack_binding.pack.binding_digest,
            role_binding_digest=role.run.binding.binding_digest,
            context_projection_digest=role.run.context.projection_digest,
            request=request,
            admitted=admitted,
        )
