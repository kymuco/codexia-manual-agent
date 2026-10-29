"""Scoped, opt-in ordinary Read-Host values. Not a secret manager.

Allowed provenance: deliberately named CODEXIA_INPUT_<TARGET> environment variable,
or a value explicitly saved by the local human for this exact project/target/prompt.
No model text is automatically treated as an input value or as consent.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path

from codexia_manual_agent.execution.powershell_read_host import stdin_for_answer

_RESTRICTED = re.compile(
    r"password|passwd|token|secret|private.?key|api.?key|credential|cookie|"
    r"парол|токен|секрет|ключ.доступ", re.IGNORECASE,
)
_PROJECT = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}\Z")
_TARGET = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


def sensitive_input(target: str, prompt: str) -> bool:
    return bool(_RESTRICTED.search(target + " " + prompt))


class ProjectInputStore:
    """Local opt-in cache, never sent to Controller; not for credentials.

    The persisted values are plaintext. Only non-secret inputs can be stored.
    The file lives under the current user's home, never inside a repository.
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else Path.home() / ".codexia" / "inputs-v1.json"

    @staticmethod
    def _key(project: str, target: str, prompt: str) -> str:
        if not _PROJECT.fullmatch(project) or not _TARGET.fullmatch(target):
            raise ValueError("invalid project or input target")
        if not isinstance(prompt, str) or not prompt or len(prompt) > 128:
            raise ValueError("invalid input prompt")
        return json.dumps([project, target.upper(), prompt], ensure_ascii=False, separators=(",", ":"))

    def _load(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        if self.path.is_symlink() or not self.path.is_file():
            raise ValueError("input store path is not a regular file")
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or set(data) != {"schema_version", "values"} or data["schema_version"] != 1 or not isinstance(data["values"], dict):
            raise ValueError("invalid local input store schema")
        if any(type(k) is not str or type(v) is not str for k, v in data["values"].items()):
            raise ValueError("invalid local input entries")
        return data["values"]

    def lookup(self, project: str, target: str, prompt: str) -> tuple[str, str] | None:
        key = self._key(project, target, prompt)
        # Never auto-resolve credentials or other obviously sensitive values.
        if sensitive_input(target, prompt):
            return None
        # This namespace means the local operator explicitly supplied a binding.
        explicit = os.environ.get("CODEXIA_INPUT_" + target.upper())
        if explicit is not None:
            stdin_for_answer(explicit)
            return explicit, "operator-env"
        saved = self._load().get(key)
        if saved is not None:
            stdin_for_answer(saved)
            return saved, "human-saved"
        return None

    def remember(self, project: str, target: str, prompt: str, value: str) -> None:
        key = self._key(project, target, prompt)
        if sensitive_input(target, prompt):
            raise ValueError("credential-like values cannot be persisted by this store")
        stdin_for_answer(value)
        values = self._load()
        values[key] = value
        if self.path.parent.is_symlink():
            raise ValueError("input store directory cannot be a symlink")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"schema_version": 1, "values": values}, ensure_ascii=False, indent=2) + "\n"
        fd, temp = tempfile.mkstemp(prefix=".input-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as out:
                out.write(payload)
                out.flush()
                os.fsync(out.fileno())
            os.chmod(temp, 0o600)
            os.replace(temp, self.path)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)
