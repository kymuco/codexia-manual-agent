from __future__ import annotations

import hmac

from codexia_manual_agent.capability_core.admission import CapabilityAdmission
from codexia_manual_agent.capability_core.host_models import (
    CapabilityHandoff,
    CapabilityHostRequest,
)
from codexia_manual_agent.capability_core.host_port import CapabilityHostPort
from codexia_manual_agent.capability_core.host_projection import (
    project_capability_handoffs,
)
from codexia_manual_agent.capability_core.models import (
    CapabilityNeedSnapshot,
    CapabilityNeedState,
    CapabilityOutcome,
)
from codexia_manual_agent.capability_core.projection import project_capability_need
from codexia_manual_agent.work_core import WorkStore


class CapabilityHostBridgeError(RuntimeError):
    """Base failure for the G2.5 host-neutral dispatch boundary."""


class CapabilityHostBindingError(CapabilityHostBridgeError):
    """Host dispatch changed exact Need/Work binding."""


class CapabilityHostRoutingConflictError(CapabilityHostBridgeError):
    """One CapabilityNeed was already handed to another host."""


class CapabilityHostPortError(CapabilityHostBridgeError):
    """Selected host port failed after durable handoff admission."""


class CapabilityHostBridge:
    """Conservative one-handoff bridge from Codexia Core to a selected host.

    The caller selects the port. Core never chooses authority policy or executor.

    Order is deliberate:
      1. recover exact admitted CapabilityNeed;
      2. durably admit one CapabilityHandoff;
      3. only then call the host port;
      4. admit a returned CapabilityOutcome, if one is available.

    Once a handoff exists, dispatch_once never calls a host again for that Need.
    This remains true after restart and after a host-port exception.
    """

    def __init__(self, store: WorkStore) -> None:
        self._store = store
        self._capability_admission = CapabilityAdmission(store)

    def dispatch_once(
        self,
        need: CapabilityNeedSnapshot,
        port: CapabilityHostPort,
    ) -> CapabilityNeedSnapshot:
        if not isinstance(need, CapabilityNeedSnapshot):
            raise TypeError("need must be CapabilityNeedSnapshot")

        source = need.need
        events = self._store.events(source.work_id)
        recovered = project_capability_need(events, source.need_id)
        if recovered.need != source:
            raise CapabilityHostBindingError(
                "Caller CapabilityNeed differs from canonical admitted Need"
            )

        existing = next(
            (
                handoff
                for handoff in project_capability_handoffs(events)
                if handoff.need_id == source.need_id
            ),
            None,
        )
        if existing is not None:
            if existing.host_id != port.host_id:
                raise CapabilityHostRoutingConflictError(
                    "CapabilityNeed is already bound to another host"
                )
            # Handoff presence is deliberately enough to suppress redispatch.
            # It does not claim that the host received or attempted the request.
            return recovered

        if recovered.state is not CapabilityNeedState.PENDING:
            raise CapabilityHostBridgeError(
                f"CapabilityNeed is {recovered.state.value}; it cannot be dispatched"
            )

        current = self._store.snapshot(source.work_id)
        handoff = CapabilityHandoff.create(
            need=recovered,
            snapshot=current,
            host_id=port.host_id,
        )
        self._store.append(
            source.work_id,
            expected_revision=current.revision,
            event=handoff.to_event(),
        )

        request = CapabilityHostRequest(handoff=handoff, need=recovered)
        try:
            outcome = port.submit(request)
        except Exception as exc:
            raise CapabilityHostPortError(
                "Host port failed after durable CapabilityHandoff admission"
            ) from exc

        if outcome is None:
            return project_capability_need(
                self._store.events(source.work_id),
                source.need_id,
            )
        if not isinstance(outcome, CapabilityOutcome):
            raise CapabilityHostPortError(
                "Host port returned neither CapabilityOutcome nor None"
            )
        if outcome.need_id != source.need_id:
            raise CapabilityHostBindingError(
                "Host response changed CapabilityNeed identity"
            )
        if not hmac.compare_digest(outcome.need_digest, source.need_digest):
            raise CapabilityHostBindingError(
                "Host response changed CapabilityNeed binding"
            )
        if outcome.work_id != source.work_id:
            raise CapabilityHostBindingError(
                "Host response crossed Work identity"
            )
        if not hmac.compare_digest(outcome.work_digest, source.work_digest):
            raise CapabilityHostBindingError(
                "Host response changed Work binding"
            )
        return self.record_outcome(
            outcome,
            host_id=handoff.host_id,
        )

    def record_outcome(
        self,
        outcome: CapabilityOutcome,
        *,
        host_id: str,
    ) -> CapabilityNeedSnapshot:
        """Record an asynchronous or synchronous host observation.

        This method performs no dispatch and therefore cannot create an external
        attempt. CapabilityAdmission validates the exact Need binding.
        """

        if not isinstance(outcome, CapabilityOutcome):
            raise TypeError("outcome must be CapabilityOutcome")

        events = self._store.events(outcome.work_id)
        handoff = next(
            (
                item
                for item in project_capability_handoffs(events)
                if item.need_id == outcome.need_id
            ),
            None,
        )
        if handoff is None:
            raise CapabilityHostBindingError(
                "CapabilityOutcome has no durable host handoff"
            )
        if handoff.host_id != host_id:
            raise CapabilityHostRoutingConflictError(
                "CapabilityOutcome callback came from another host"
            )
        if not hmac.compare_digest(handoff.need_digest, outcome.need_digest):
            raise CapabilityHostBindingError(
                "CapabilityOutcome callback changed Need binding"
            )
        return self._capability_admission.admit_outcome(outcome)
