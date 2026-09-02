from __future__ import annotations

from codexia_manual_agent.session_events.models import SessionEventStateError
from codexia_manual_agent.session_events.sqlite_store import SqliteSessionEventStore


class DurableAuthorizationConsumptionRegistry:
    """Session-bound crash-safe implementation of the M2.x consumption registry API."""

    def __init__(
        self,
        store: SqliteSessionEventStore,
        *,
        session_id: str,
    ) -> None:
        if not isinstance(store, SqliteSessionEventStore):
            raise TypeError("store must be a SqliteSessionEventStore")
        if not isinstance(session_id, str) or not session_id.strip():
            raise ValueError("session_id must be a non-empty string")
        self._store = store
        self._session_id = session_id

    @property
    def session_id(self) -> str:
        return self._session_id

    def consume(
        self,
        receipt_id: str,
        *,
        receipt_digest: str | None = None,
        proposal_id: str | None = None,
        proposal_digest: str | None = None,
    ) -> None:
        if (
            not isinstance(receipt_digest, str)
            or not receipt_digest
            or not isinstance(proposal_id, str)
            or not proposal_id
            or not isinstance(proposal_digest, str)
            or not proposal_digest
        ):
            raise SessionEventStateError(
                "Durable authorization consumption requires exact receipt/proposal binding"
            )
        self._store.consume_recorded_authorization(
            session_id=self._session_id,
            receipt_id=receipt_id,
            receipt_digest=receipt_digest,
            proposal_id=proposal_id,
            proposal_digest=proposal_digest,
        )

    def is_consumed(self, receipt_id: str) -> bool:
        return self._store.is_authorization_consumed(receipt_id)
