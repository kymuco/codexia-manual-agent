from __future__ import annotations

from codexia_manual_agent.domain.models import SessionManifest
from codexia_manual_agent.ports.session_store import SessionStore


class SessionQueryService:
    def __init__(self, store: SessionStore) -> None:
        self._store = store

    def resume(self, session_id: str) -> SessionManifest:
        return self._store.load(session_id)

    def list(self) -> tuple[SessionManifest, ...]:
        return self._store.list()
