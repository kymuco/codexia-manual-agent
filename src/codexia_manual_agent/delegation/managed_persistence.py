from __future__ import annotations

from typing import Any

from codexia_manual_agent.delegation.errors import DelegationPersistenceIntegrityError
from codexia_manual_agent.delegation.persistence import (
    DelegationEventKind,
    SqliteDelegationEventStore as _BaseSqliteDelegationEventStore,
    validate_delegation_event_payload,
)


class SqliteDelegationEventStore(_BaseSqliteDelegationEventStore):
    """Public M3.2 store with one fail-closed corruption error surface.

    The base store deliberately keeps transition errors intact for valid requested
    mutations. Raw conversion errors are not part of that orchestration contract;
    when they escape while loading persisted rows they mean the durable bytes are
    malformed and are normalized here.
    """

    @staticmethod
    def _normalize_persisted_failure(exc: Exception) -> None:
        raise DelegationPersistenceIntegrityError(
            "Persisted delegation chronology contains malformed typed data"
        ) from exc

    @staticmethod
    def _guard_prepare(prepare: Any, state: Any, marker: list[bool]):
        """Keep caller-side conversion failures distinct from persisted corruption."""

        try:
            kind, payload = prepare(state)
            normalized_kind = DelegationEventKind(kind)
            validate_delegation_event_payload(normalized_kind, payload)
            return normalized_kind, payload
        except (TypeError, ValueError):
            marker[0] = True
            raise

    def recover(self, root_delegation_id: str):
        try:
            return super().recover(root_delegation_id)
        except (TypeError, ValueError) as exc:
            self._normalize_persisted_failure(exc)

    def recover_for_delegation(self, delegation_id: str):
        try:
            return super().recover_for_delegation(delegation_id)
        except (TypeError, ValueError) as exc:
            self._normalize_persisted_failure(exc)

    def recover_for_continuation(self, continuation_id: str):
        try:
            return super().recover_for_continuation(continuation_id)
        except (TypeError, ValueError) as exc:
            self._normalize_persisted_failure(exc)

    def mutate_delegation(self, delegation_id: str, prepare: Any):
        prepare_conversion_failed = [False]

        def guarded_prepare(state: Any):
            return self._guard_prepare(prepare, state, prepare_conversion_failed)

        try:
            return super().mutate_delegation(delegation_id, guarded_prepare)
        except (TypeError, ValueError) as exc:
            if prepare_conversion_failed[0]:
                raise
            self._normalize_persisted_failure(exc)

    def mutate_escalation(self, escalation_id: str, prepare: Any):
        prepare_conversion_failed = [False]

        def guarded_prepare(state: Any):
            return self._guard_prepare(prepare, state, prepare_conversion_failed)

        try:
            return super().mutate_escalation(escalation_id, guarded_prepare)
        except (TypeError, ValueError) as exc:
            if prepare_conversion_failed[0]:
                raise
            self._normalize_persisted_failure(exc)
