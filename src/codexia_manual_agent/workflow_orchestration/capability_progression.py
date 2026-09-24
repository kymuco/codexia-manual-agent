from __future__ import annotations

from codexia_manual_agent.capability_core import (
    CapabilityHostBridge,
    CapabilityHostPort,
    CapabilityNeedSnapshot,
    CapabilityNeedState,
    project_capability_need,
)
from codexia_manual_agent.work_core import WorkStore


class CapabilityProgressionService:
    """Advance one admitted CapabilityNeed through at most one host dispatch.

    The service owns no host selection, authority, executor, retry policy,
    scheduler loop, Workflow progression, or cognition dispatch.

    Terminal CapabilityNeeds are exact no-ops. Pending Needs delegate all
    durable handoff, no-redispatch, host routing, and outcome-admission
    semantics to the existing G2.5 CapabilityHostBridge.
    """

    def __init__(self, store: WorkStore) -> None:
        self._store = store
        self._bridge = CapabilityHostBridge(store)

    def progress_once(
        self,
        *,
        work_id: str,
        need_id: str,
        port: CapabilityHostPort,
    ) -> CapabilityNeedSnapshot:
        need = project_capability_need(
            self._store.events(work_id),
            need_id,
        )

        if need.state in {
            CapabilityNeedState.SUCCEEDED,
            CapabilityNeedState.FAILED,
            CapabilityNeedState.OUTCOME_UNKNOWN,
        }:
            return need

        return self._bridge.dispatch_once(need, port)
