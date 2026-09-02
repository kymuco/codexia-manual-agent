from __future__ import annotations

from typing import Protocol

from codexia_manual_agent.domain.models import GitStatus, SearchMatch, WorkspaceEntry


class WorkspaceReader(Protocol):
    @property
    def root(self) -> str: ...

    def read_file(self, path: str, *, max_bytes: int = 131_072) -> str: ...

    def list_files(
        self,
        path: str = ".",
        *,
        recursive: bool = False,
        max_entries: int = 200,
    ) -> tuple[WorkspaceEntry, ...]: ...

    def search_text(
        self,
        query: str,
        path: str = ".",
        *,
        max_matches: int = 100,
        max_files: int = 2_000,
        max_file_bytes: int = 1_048_576,
    ) -> tuple[SearchMatch, ...]: ...

    def git_status(self) -> GitStatus: ...
