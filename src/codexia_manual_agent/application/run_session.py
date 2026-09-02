from __future__ import annotations

from pathlib import Path

from codexia_manual_agent.domain.errors import WorkspaceNotFoundError
from codexia_manual_agent.domain.models import SessionManifest
from codexia_manual_agent.ports.session_store import SessionStore
from codexia_manual_agent.prompts.loader import load_prompt


class RunSessionService:
    """Creates a truthful read-only session manifest."""

    def __init__(self, store: SessionStore) -> None:
        self._store = store

    def prepare(
        self,
        *,
        workspace: str | Path,
        prompt_version: str = "v0.3",
        title: str | None = None,
        provider: str = "unconfigured",
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> SessionManifest:
        """Validate and construct a manifest without publishing it.

        M3 uses this seam so its authoritative event ledger can be committed before
        the discovery/summary manifest becomes externally visible. The legacy
        `start()` API preserves its previous create-and-save behavior.
        """

        resolved = Path(workspace).expanduser()
        if not resolved.exists() or not resolved.is_dir():
            raise WorkspaceNotFoundError(f"Workspace directory not found: {resolved}")

        load_prompt(prompt_version)

        return SessionManifest.create(
            workspace=resolved.resolve(),
            prompt_version=prompt_version,
            title=title,
            provider=provider,
            model=model,
            reasoning_effort=reasoning_effort,
        )

    def start(
        self,
        *,
        workspace: str | Path,
        prompt_version: str = "v0.3",
        title: str | None = None,
        provider: str = "unconfigured",
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> SessionManifest:
        manifest = self.prepare(
            workspace=workspace,
            prompt_version=prompt_version,
            title=title,
            provider=provider,
            model=model,
            reasoning_effort=reasoning_effort,
        )
        self._store.save(manifest)
        return manifest
