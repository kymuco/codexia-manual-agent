from __future__ import annotations

import hmac

from codexia_manual_agent.delegation_core.models import Delegation
from codexia_manual_agent.delegation_core.projection import project_delegation
from codexia_manual_agent.work_core import WorkStore


class DelegationAdmissionError(RuntimeError):
    """Base failure for Gen2 durable child-Work admission."""


class DelegationBindingError(DelegationAdmissionError):
    """Delegation changed exact parent/child semantic binding."""


class DelegationAdmission:
    """Atomically admit one parent-owned durable child Work."""

    def __init__(self, store: WorkStore) -> None:
        self._store = store

    def admit(self, delegation: Delegation) -> Delegation:
        if not isinstance(delegation, Delegation):
            raise TypeError("delegation must be Delegation")

        parent = self._store.snapshot(delegation.parent_work_id)
        if not hmac.compare_digest(
            parent.work.work_digest,
            delegation.parent_work_digest,
        ):
            raise DelegationBindingError("Delegation changed parent Work binding")

        self._store.append_with_child_create(
            delegation.parent_work_id,
            expected_revision=delegation.start_revision,
            event=delegation.to_parent_event(),
            child_work=delegation.child_work,
        )
        recovered = project_delegation(
            self._store.events(delegation.parent_work_id),
            delegation.delegation_id,
        )
        if recovered != delegation:
            raise DelegationBindingError(
                "delegation identity reused for different exact ownership"
            )
        child = self._store.snapshot(delegation.child_work.work_id)
        if child.work.to_dict() != delegation.child_work.to_dict():
            raise DelegationBindingError(
                "durable child Work differs from Delegation binding"
            )
        return recovered
