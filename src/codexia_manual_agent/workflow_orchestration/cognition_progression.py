from __future__ import annotations

from codexia_manual_agent.role_core import (
    CognitionPort,
    CognitionTransportBridge,
    RoleAdmission,
    RoleRunSnapshot,
    RoleRunState,
    project_role_run,
)
from codexia_manual_agent.work_core import WorkStore
from codexia_manual_agent.workflow_orchestration.role_cognition import (
    ContextProjectionMaterialPort,
    RoleCognitionMaterializationService,
    RoleInstructionsMaterialPort,
)


class RoleCognitionProgressionService:
    """Advance one RoleRun through at most one bounded cognition dispatch.

    The service composes existing Gen2 boundaries without introducing new
    durable truth or policy:

      ACTIVE
        -> materialize one exact CognitionRequest
        -> admit that request
        -> durable CognitionHandoff
        -> selected CognitionPort

      REQUESTED
        -> rematerialize the exact admitted CognitionRequest
        -> existing G2.13 dispatch_once semantics

      terminal
        -> return the recovered RoleRun unchanged

    It owns no scheduler loop, provider selection, retry policy, workspace,
    capability host, tool surface, or authority.
    """

    def __init__(
        self,
        *,
        store: WorkStore,
        instructions: RoleInstructionsMaterialPort,
        context: ContextProjectionMaterialPort,
    ) -> None:
        self._store = store
        self._materializer = RoleCognitionMaterializationService(
            store=store,
            instructions=instructions,
            context=context,
        )
        self._admission = RoleAdmission(store)
        self._transport = CognitionTransportBridge(store)

    def progress_once(
        self,
        *,
        work_id: str,
        role_run_id: str,
        port: CognitionPort,
    ) -> RoleRunSnapshot:
        role = project_role_run(self._store.events(work_id), role_run_id)

        if role.state in {
            RoleRunState.COMPLETED,
            RoleRunState.FAILED,
            RoleRunState.OUTCOME_UNKNOWN,
        }:
            return role

        if role.state is RoleRunState.ACTIVE:
            materialized = self._materializer.prepare_request(
                work_id=work_id,
                role_run_id=role_run_id,
            )
            self._admission.admit_request(materialized.request)
        else:
            materialized = self._materializer.rematerialize_request(
                work_id=work_id,
                role_run_id=role_run_id,
            )

        return self._transport.dispatch_once(materialized.request, port)
