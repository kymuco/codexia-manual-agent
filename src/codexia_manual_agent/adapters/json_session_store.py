from __future__ import annotations

import json
import os
import re
from pathlib import Path

from codexia_manual_agent.domain.errors import (
    InvalidSessionIdError,
    SessionNotFoundError,
)
from codexia_manual_agent.domain.models import SessionManifest


_SESSION_ID_PATTERN = re.compile(r"^[0-9a-fA-F-]{36}$")


class JsonSessionStore:
    """Atomic local manifest storage. It stores metadata, not model transcripts."""

    def __init__(self, directory: str | Path) -> None:
        self._directory = Path(directory).expanduser().resolve()

    @property
    def directory(self) -> Path:
        return self._directory

    def _validate_session_id(self, session_id: str) -> None:
        if not _SESSION_ID_PATTERN.fullmatch(session_id):
            raise InvalidSessionIdError(f"Invalid session id: {session_id}")

    def _path_for(self, session_id: str) -> Path:
        self._validate_session_id(session_id)
        return self._directory / f"{session_id}.json"

    def save(self, manifest: SessionManifest) -> None:
        target = self._path_for(manifest.session_id)
        self._directory.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(f".json.tmp-{os.getpid()}")
        payload = json.dumps(
            manifest.to_dict(),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ) + "\n"
        temporary.write_text(payload, encoding="utf-8")
        os.replace(temporary, target)

    def load(self, session_id: str) -> SessionManifest:
        target = self._path_for(session_id)
        if not target.is_file():
            raise SessionNotFoundError(f"Session not found: {session_id}")
        data = json.loads(target.read_text(encoding="utf-8"))
        return SessionManifest.from_dict(data)

    def list(self) -> tuple[SessionManifest, ...]:
        if not self._directory.exists():
            return ()
        manifests: list[SessionManifest] = []
        for path in sorted(self._directory.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                manifests.append(SessionManifest.from_dict(data))
            except (OSError, ValueError, KeyError, TypeError):
                continue
        manifests.sort(key=lambda item: item.created_at, reverse=True)
        return tuple(manifests)
