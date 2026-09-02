from __future__ import annotations

from typing import Any, Mapping

from codexia_manual_agent.domain.errors import InvalidToolArgumentsError
from codexia_manual_agent.domain.models import ToolName, ToolRequest
from codexia_manual_agent.domain.sensitive_paths import is_sensitive_relative_path


class ReadOnlyAgentToolPolicy:
    """Rejects model-controlled arguments outside M1.1 budgets."""

    def validate(self, request: ToolRequest) -> ToolRequest:
        arguments = dict(request.arguments)
        if request.name is ToolName.READ_FILE:
            self._require_allowed(arguments, {"path", "max_bytes"})
            self._require_nonempty_string(arguments, "path")
            self._reject_sensitive_path(arguments["path"])
            max_bytes = self._positive_int(arguments.get("max_bytes", 65_536), "max_bytes")
            if max_bytes > 65_536:
                raise InvalidToolArgumentsError("read_file.max_bytes exceeds 65536")
            arguments["max_bytes"] = max_bytes
        elif request.name is ToolName.LIST_FILES:
            self._require_allowed(arguments, {"path", "recursive", "max_entries"})
            path = arguments.get("path", ".")
            if not isinstance(path, str):
                raise InvalidToolArgumentsError("list_files.path must be a string")
            self._reject_sensitive_path(path)
            recursive = arguments.get("recursive", False)
            if not isinstance(recursive, bool):
                raise InvalidToolArgumentsError("list_files.recursive must be boolean")
            max_entries = self._positive_int(arguments.get("max_entries", 200), "max_entries")
            if max_entries > 200:
                raise InvalidToolArgumentsError("list_files.max_entries exceeds 200")
            arguments.update(path=path, recursive=recursive, max_entries=max_entries)
        elif request.name is ToolName.SEARCH_TEXT:
            self._require_allowed(arguments, {"query", "path", "max_matches"})
            self._require_nonempty_string(arguments, "query")
            path = arguments.get("path", ".")
            if not isinstance(path, str):
                raise InvalidToolArgumentsError("search_text.path must be a string")
            self._reject_sensitive_path(path)
            max_matches = self._positive_int(arguments.get("max_matches", 100), "max_matches")
            if max_matches > 100:
                raise InvalidToolArgumentsError("search_text.max_matches exceeds 100")
            arguments.update(path=path, max_matches=max_matches)
        elif request.name is ToolName.GIT_STATUS:
            if arguments:
                raise InvalidToolArgumentsError("git_status does not accept arguments")
        return ToolRequest(request.request_id, request.name, arguments)

    @staticmethod
    def _reject_sensitive_path(path: str) -> None:
        if is_sensitive_relative_path(path):
            raise InvalidToolArgumentsError(
                f"Sensitive path is not available to the model: {path}"
            )

    @staticmethod
    def _require_allowed(arguments: Mapping[str, Any], allowed: set[str]) -> None:
        extra = sorted(set(arguments) - allowed)
        if extra:
            raise InvalidToolArgumentsError(f"Unsupported tool arguments: {extra}")

    @staticmethod
    def _require_nonempty_string(arguments: Mapping[str, Any], key: str) -> None:
        value = arguments.get(key)
        if not isinstance(value, str) or not value:
            raise InvalidToolArgumentsError(f"{key} must be a non-empty string")

    @staticmethod
    def _positive_int(value: Any, key: str) -> int:
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise InvalidToolArgumentsError(f"{key} must be a positive integer")
        return value
