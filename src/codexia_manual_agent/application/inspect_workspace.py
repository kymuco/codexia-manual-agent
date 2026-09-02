from __future__ import annotations

from typing import Any

from codexia_manual_agent.domain.errors import (
    CodexiaError,
    InvalidToolArgumentsError,
    UnsupportedToolError,
)
from codexia_manual_agent.domain.models import ToolName, ToolObservation, ToolRequest
from codexia_manual_agent.ports.workspace_reader import WorkspaceReader


class InspectWorkspaceService:
    """Dispatches the four M1.0 read-only tools."""

    def __init__(self, workspace: WorkspaceReader) -> None:
        self._workspace = workspace

    def execute(self, request: ToolRequest) -> ToolObservation:
        try:
            data = self._execute(request)
            return ToolObservation(
                request_id=request.request_id,
                tool=request.name,
                success=True,
                data=data,
            )
        except (CodexiaError, ValueError, TypeError) as exc:
            return ToolObservation(
                request_id=request.request_id,
                tool=request.name,
                success=False,
                error=str(exc),
            )

    def _execute(self, request: ToolRequest) -> Any:
        arguments = dict(request.arguments)

        if request.name is ToolName.READ_FILE:
            path = self._require_string(arguments, "path")
            return {
                "path": path,
                "text": self._workspace.read_file(
                    path,
                    max_bytes=self._optional_int(arguments, "max_bytes", 131_072),
                ),
            }

        if request.name is ToolName.LIST_FILES:
            entries = self._workspace.list_files(
                self._optional_string(arguments, "path", "."),
                recursive=self._optional_bool(arguments, "recursive", False),
                max_entries=self._optional_int(arguments, "max_entries", 200),
            )
            return {"entries": [entry.to_dict() for entry in entries]}

        if request.name is ToolName.SEARCH_TEXT:
            matches = self._workspace.search_text(
                self._require_string(arguments, "query"),
                self._optional_string(arguments, "path", "."),
                max_matches=self._optional_int(arguments, "max_matches", 100),
            )
            return {"matches": [match.to_dict() for match in matches]}

        if request.name is ToolName.GIT_STATUS:
            if arguments:
                raise InvalidToolArgumentsError(
                    "git_status does not accept arguments"
                )
            return self._workspace.git_status().to_dict()

        raise UnsupportedToolError(f"Unsupported tool: {request.name}")

    @staticmethod
    def _require_string(arguments: dict[str, Any], key: str) -> str:
        value = arguments.get(key)
        if not isinstance(value, str) or not value:
            raise InvalidToolArgumentsError(
                f"Argument {key!r} must be a non-empty string"
            )
        return value

    @staticmethod
    def _optional_string(
        arguments: dict[str, Any],
        key: str,
        default: str,
    ) -> str:
        value = arguments.get(key, default)
        if not isinstance(value, str):
            raise InvalidToolArgumentsError(f"Argument {key!r} must be a string")
        return value

    @staticmethod
    def _optional_int(
        arguments: dict[str, Any],
        key: str,
        default: int,
    ) -> int:
        value = arguments.get(key, default)
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise InvalidToolArgumentsError(
                f"Argument {key!r} must be a positive integer"
            )
        return value

    @staticmethod
    def _optional_bool(
        arguments: dict[str, Any],
        key: str,
        default: bool,
    ) -> bool:
        value = arguments.get(key, default)
        if not isinstance(value, bool):
            raise InvalidToolArgumentsError(f"Argument {key!r} must be boolean")
        return value
