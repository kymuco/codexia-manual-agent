from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Iterator

from codexia_manual_agent.domain.errors import (
    BinaryFileError,
    FileTooLargeError,
    GitStatusError,
    WorkspaceBoundaryError,
    WorkspaceNotFoundError,
    WorkspacePathNotFoundError,
    WorkspacePathTypeError,
)
from codexia_manual_agent.domain.models import GitStatus, SearchMatch, WorkspaceEntry
from codexia_manual_agent.domain.sensitive_paths import (
    is_sensitive_name,
    is_sensitive_relative_path,
)


_DEFAULT_IGNORED_DIRS = frozenset(
    {
        ".git",
        ".codexia",
        ".venv",
        "venv",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "node_modules",
    }
)


class FilesystemWorkspace:
    """Workspace-bounded, read-only repository inspection.

    The only child process is a fixed ``git status`` argv. Arbitrary command
    execution is not exposed by this adapter.
    """

    def __init__(self, root: str | Path) -> None:
        candidate = Path(root).expanduser()
        if not candidate.exists() or not candidate.is_dir():
            raise WorkspaceNotFoundError(f"Workspace directory not found: {candidate}")
        self._root = candidate.resolve()

    @property
    def root(self) -> str:
        return str(self._root)

    def _resolve(self, relative_path: str, *, must_exist: bool = True) -> Path:
        supplied = Path(relative_path)
        if supplied.is_absolute():
            raise WorkspaceBoundaryError(
                f"Absolute paths are not allowed inside a workspace request: {relative_path}"
            )

        candidate = self._root / supplied

        # Reject lexical parent traversal before existence checks so an absent
        # outside target is still classified as a boundary violation.
        lexical = candidate.resolve(strict=False)
        if not lexical.is_relative_to(self._root):
            raise WorkspaceBoundaryError(
                f"Path escapes workspace boundary: {relative_path}"
            )

        try:
            resolved = candidate.resolve(strict=must_exist)
        except FileNotFoundError as exc:
            raise WorkspacePathNotFoundError(
                f"Workspace path not found: {relative_path}"
            ) from exc

        # Existing symlinks may redirect after lexical normalization.
        if not resolved.is_relative_to(self._root):
            raise WorkspaceBoundaryError(
                f"Path escapes workspace boundary: {relative_path}"
            )
        return resolved

    def _relative(self, path: Path) -> str:
        relative = path.relative_to(self._root)
        return "." if not relative.parts else relative.as_posix()

    def read_file(self, path: str, *, max_bytes: int = 131_072) -> str:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")

        resolved = self._resolve(path)
        if not resolved.is_file():
            raise WorkspacePathTypeError(f"Not a file: {path}")

        size = resolved.stat().st_size
        if size > max_bytes:
            raise FileTooLargeError(
                f"File exceeds read limit ({size} > {max_bytes} bytes): {path}"
            )

        payload = resolved.read_bytes()
        if b"\x00" in payload:
            raise BinaryFileError(f"Binary file is not readable as text: {path}")

        try:
            return payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise BinaryFileError(
                f"File is not valid UTF-8 text: {path}"
            ) from exc

    def list_files(
        self,
        path: str = ".",
        *,
        recursive: bool = False,
        max_entries: int = 200,
    ) -> tuple[WorkspaceEntry, ...]:
        if max_entries <= 0:
            raise ValueError("max_entries must be positive")

        if is_sensitive_relative_path(path):
            raise WorkspaceBoundaryError(f"Sensitive path is excluded from listing: {path}")

        resolved = self._resolve(path)
        if not resolved.is_dir():
            raise WorkspacePathTypeError(f"Not a directory: {path}")

        paths: Iterator[Path]
        if recursive:
            paths = self._iter_tree(resolved)
        else:
            paths = iter(sorted(resolved.iterdir(), key=lambda item: item.name.casefold()))

        entries: list[WorkspaceEntry] = []
        for item in paths:
            if item.is_dir() and (
                item.name in _DEFAULT_IGNORED_DIRS or is_sensitive_name(item.name)
            ):
                continue
            if item.is_file() and is_sensitive_name(item.name):
                continue
            if item.is_symlink():
                kind = "symlink"
                size = None
            elif item.is_dir():
                kind = "directory"
                size = None
            elif item.is_file():
                kind = "file"
                size = item.stat().st_size
            else:
                kind = "other"
                size = None

            entries.append(
                WorkspaceEntry(path=self._relative(item), kind=kind, size_bytes=size)
            )
            if len(entries) >= max_entries:
                break

        return tuple(entries)

    def _iter_tree(self, root: Path) -> Iterator[Path]:
        for current_root, dir_names, file_names in os.walk(root, followlinks=False):
            dir_names[:] = sorted(
                [
                    name
                    for name in dir_names
                    if name not in _DEFAULT_IGNORED_DIRS
                    and not is_sensitive_name(name)
                ],
                key=str.casefold,
            )
            current = Path(current_root)
            for name in dir_names:
                yield current / name
            for name in sorted(file_names, key=str.casefold):
                if is_sensitive_name(name):
                    continue
                yield current / name

    def search_text(
        self,
        query: str,
        path: str = ".",
        *,
        max_matches: int = 100,
        max_files: int = 2_000,
        max_file_bytes: int = 1_048_576,
    ) -> tuple[SearchMatch, ...]:
        if not query:
            raise ValueError("query must not be empty")
        if min(max_matches, max_files, max_file_bytes) <= 0:
            raise ValueError("search limits must be positive")
        if is_sensitive_relative_path(path):
            raise WorkspaceBoundaryError(f"Sensitive path is excluded from search: {path}")

        resolved = self._resolve(path)
        candidates: Iterator[Path]
        if resolved.is_file():
            candidates = iter((resolved,))
        elif resolved.is_dir():
            candidates = (
                item for item in self._iter_tree(resolved) if item.is_file()
            )
        else:
            raise WorkspacePathTypeError(f"Not a file or directory: {path}")

        matches: list[SearchMatch] = []
        inspected_files = 0
        query_folded = query.casefold()

        for candidate in candidates:
            if inspected_files >= max_files:
                break
            inspected_files += 1

            try:
                if candidate.is_symlink():
                    continue
                resolved_candidate = candidate.resolve(strict=True)
                if not resolved_candidate.is_relative_to(self._root):
                    continue
                if not resolved_candidate.is_file():
                    continue
                if resolved_candidate.stat().st_size > max_file_bytes:
                    continue
                payload = resolved_candidate.read_bytes()
            except OSError:
                continue

            if b"\x00" in payload:
                continue

            try:
                text = payload.decode("utf-8")
            except UnicodeDecodeError:
                continue

            for line_number, line in enumerate(text.splitlines(), start=1):
                if query_folded in line.casefold():
                    matches.append(
                        SearchMatch(
                            path=self._relative(candidate),
                            line_number=line_number,
                            line=line,
                        )
                    )
                    if len(matches) >= max_matches:
                        return tuple(matches)

        return tuple(matches)

    def git_status(self) -> GitStatus:
        try:
            completed = subprocess.run(
                [
                    "git",
                    "-C",
                    str(self._root),
                    "status",
                    "--short",
                    "--branch",
                    "--untracked-files=normal",
                ],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                shell=False,
            )
        except FileNotFoundError as exc:
            raise GitStatusError("Git executable was not found in PATH.") from exc
        except subprocess.TimeoutExpired as exc:
            raise GitStatusError("git status exceeded the 10 second limit.") from exc

        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise GitStatusError(detail or "git status failed.")

        lines = tuple(line for line in completed.stdout.splitlines() if line)
        branch_line = lines[0] if lines and lines[0].startswith("##") else None
        entries = lines[1:] if branch_line is not None else lines
        return GitStatus(
            branch_line=branch_line,
            entries=tuple(entries),
            clean=not entries,
        )
