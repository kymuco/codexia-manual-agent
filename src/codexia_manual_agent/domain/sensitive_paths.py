from __future__ import annotations

from pathlib import PurePosixPath


_SENSITIVE_DIRECTORIES = frozenset(
    {
        ".ssh",
        ".aws",
        ".azure",
        ".gnupg",
        ".kube",
    }
)
_SENSITIVE_FILENAMES = frozenset(
    {
        ".env",
        ".git-credentials",
        ".netrc",
        "auth_data.json",
        "cookies.json",
        "credentials.json",
        "id_ed25519",
        "id_rsa",
        "secrets.json",
        "service-account.json",
        "service_account.json",
    }
)
_SENSITIVE_SUFFIXES = (
    ".jks",
    ".key",
    ".p12",
    ".pem",
    ".pfx",
)
_TEMPLATE_MARKERS = (
    ".dist",
    ".example",
    ".sample",
    ".template",
)


def _parts(path: str) -> tuple[str, ...]:
    normalized = path.replace("\\", "/")
    return tuple(part.casefold() for part in PurePosixPath(normalized).parts)


def is_sensitive_name(name: str) -> bool:
    normalized = name.casefold()
    if any(marker in normalized for marker in _TEMPLATE_MARKERS):
        return False
    if normalized in _SENSITIVE_DIRECTORIES:
        return True
    if normalized in _SENSITIVE_FILENAMES:
        return True
    if normalized.startswith(".env."):
        return True
    return normalized.endswith(_SENSITIVE_SUFFIXES)


def is_sensitive_relative_path(path: str) -> bool:
    parts = _parts(path)
    if any(part in _SENSITIVE_DIRECTORIES for part in parts):
        return True
    return bool(parts) and is_sensitive_name(parts[-1])
