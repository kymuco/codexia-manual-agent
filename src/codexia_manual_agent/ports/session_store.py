from __future__ import annotations

from typing import Protocol

from codexia_manual_agent.domain.models import SessionManifest


class SessionStore(Protocol):
    def save(self, manifest: SessionManifest) -> None: ...

    def load(self, session_id: str) -> SessionManifest: ...

    def list(self) -> tuple[SessionManifest, ...]: ...
