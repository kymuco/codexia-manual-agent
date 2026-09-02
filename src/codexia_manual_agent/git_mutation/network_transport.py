from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from urllib.parse import urlparse

from codexia_manual_agent.domain.errors import InvalidGitMutationError


MAX_NETWORK_REMOTE_URL_CHARS = 4096

_USER_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_DNS_LABEL_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
_REPOSITORY_PATH_RE = re.compile(r"^[A-Za-z0-9._/-]{1,2048}$")
_SCP_REMOTE_RE = re.compile(
    r"^(?P<user>[A-Za-z0-9._-]{1,128})@(?P<host>[^:/\\\s]+):(?P<path>.+)$"
)


class GitNetworkTransport(StrEnum):
    SSH = "ssh"
    HTTPS = "https"


class GitSshUrlForm(StrEnum):
    URI = "uri"
    SCP = "scp"


@dataclass(frozen=True, slots=True)
class GitNetworkEndpoint:
    transport: GitNetworkTransport
    original_url: str
    host: str
    port: int
    repository_path: str
    ssh_user: str | None = None
    ssh_url_form: GitSshUrlForm | None = None
    ssh_path_is_absolute: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "transport": self.transport.value,
            "original_url": self.original_url,
            "host": self.host,
            "port": self.port,
            "repository_path": self.repository_path,
            "ssh_user": self.ssh_user,
            "ssh_url_form": self.ssh_url_form.value if self.ssh_url_form else None,
            "ssh_path_is_absolute": self.ssh_path_is_absolute,
        }

    @property
    def review_destination(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        if self.transport is GitNetworkTransport.HTTPS:
            return f"https://{host}:{self.port}/{self.repository_path}"
        path_marker = "/" if self.ssh_path_is_absolute else "/~/"
        return f"ssh://{self.ssh_user}@{host}:{self.port}{path_marker}{self.repository_path}"


def _validate_url_text(url: str) -> str:
    if not isinstance(url, str) or not url or len(url) > MAX_NETWORK_REMOTE_URL_CHARS:
        raise InvalidGitMutationError("Network Git remote URL is missing or too long")
    if url.startswith("-") or any(char in url for char in "\r\n\x00"):
        raise InvalidGitMutationError("Network Git remote URL contains a forbidden form")
    if "::" in url:
        raise InvalidGitMutationError("Git remote-helper syntax is not admitted for network push")
    try:
        url.encode("ascii")
    except UnicodeEncodeError as exc:
        raise InvalidGitMutationError(
            "Network Git remote URL must use explicit ASCII host/path spelling"
        ) from exc
    return url


def _canonical_host(host: str | None) -> str:
    if not host:
        raise InvalidGitMutationError("Network Git endpoint host is required")
    if host.endswith(".") or "%" in host or any(char.isspace() for char in host):
        raise InvalidGitMutationError("Network Git endpoint host has an ambiguous form")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if all(char.isdigit() or char == "." for char in host):
            raise InvalidGitMutationError("Network Git numeric host has an ambiguous form")
        labels = host.split(".")
        if any(not label or _DNS_LABEL_RE.fullmatch(label) is None for label in labels):
            raise InvalidGitMutationError("Network Git endpoint host is not canonical DNS text")
        return host.lower()
    return address.compressed.lower()


def _port(parsed, default: int) -> int:
    try:
        port = parsed.port
    except ValueError as exc:
        raise InvalidGitMutationError("Network Git endpoint port is invalid") from exc
    value = default if port is None else port
    if not 1 <= value <= 65535:
        raise InvalidGitMutationError("Network Git endpoint port is out of range")
    return value


def _repository_path(raw: str, *, allow_leading_slash: bool) -> tuple[str, bool]:
    if not raw:
        raise InvalidGitMutationError("Network Git repository path is required")
    absolute = raw.startswith("/")
    if absolute and not allow_leading_slash:
        raise InvalidGitMutationError("Network Git repository path has an unsupported form")
    normalized = raw[1:] if absolute else raw
    if (
        not normalized
        or "\\" in normalized
        or "%" in normalized
        or _REPOSITORY_PATH_RE.fullmatch(normalized) is None
    ):
        raise InvalidGitMutationError("Network Git repository path has an unsupported form")
    parts = normalized.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise InvalidGitMutationError("Network Git repository path contains ambiguous segments")
    return normalized, absolute


def _ssh_uri(url: str) -> GitNetworkEndpoint:
    parsed = urlparse(url)
    if parsed.scheme != "ssh":
        raise InvalidGitMutationError("Network Git SSH URI must use ssh://")
    if parsed.password is not None or parsed.query or parsed.fragment:
        raise InvalidGitMutationError("SSH Git remote contains unsupported URL components")
    if "%" in parsed.netloc:
        raise InvalidGitMutationError("SSH Git authority must not use percent-encoded aliases")
    user = parsed.username
    if user is None or _USER_RE.fullmatch(user) is None:
        raise InvalidGitMutationError("SSH Git endpoint requires an explicit canonical user")
    host = _canonical_host(parsed.hostname)
    path, absolute = _repository_path(parsed.path, allow_leading_slash=True)
    if not absolute:
        raise InvalidGitMutationError("ssh:// Git repository path must be absolute")
    return GitNetworkEndpoint(
        transport=GitNetworkTransport.SSH,
        original_url=url,
        host=host,
        port=_port(parsed, 22),
        repository_path=path,
        ssh_user=user,
        ssh_url_form=GitSshUrlForm.URI,
        ssh_path_is_absolute=True,
    )


def _scp_like(url: str) -> GitNetworkEndpoint:
    match = _SCP_REMOTE_RE.fullmatch(url)
    if match is None:
        raise InvalidGitMutationError("SCP-like Git remote has an unsupported form")
    user = match.group("user")
    host = _canonical_host(match.group("host"))
    path, absolute = _repository_path(match.group("path"), allow_leading_slash=True)
    return GitNetworkEndpoint(
        transport=GitNetworkTransport.SSH,
        original_url=url,
        host=host,
        port=22,
        repository_path=path,
        ssh_user=user,
        ssh_url_form=GitSshUrlForm.SCP,
        ssh_path_is_absolute=absolute,
    )


def _https(url: str) -> GitNetworkEndpoint:
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise InvalidGitMutationError("Network Git HTTPS endpoint must use https://")
    if parsed.username is not None or parsed.password is not None:
        raise InvalidGitMutationError("HTTPS Git credentials cannot be embedded in the remote URL")
    if parsed.query or parsed.fragment or "%" in parsed.netloc:
        raise InvalidGitMutationError("HTTPS Git remote contains unsupported URL components")
    host = _canonical_host(parsed.hostname)
    path, absolute = _repository_path(parsed.path, allow_leading_slash=True)
    if not absolute:
        raise InvalidGitMutationError("HTTPS Git repository path must be absolute")
    return GitNetworkEndpoint(
        transport=GitNetworkTransport.HTTPS,
        original_url=url,
        host=host,
        port=_port(parsed, 443),
        repository_path=path,
    )


def parse_network_git_endpoint(url: str) -> GitNetworkEndpoint:
    url = _validate_url_text(url)
    parsed = urlparse(url)
    if parsed.scheme == "ssh":
        return _ssh_uri(url)
    if parsed.scheme == "https":
        return _https(url)
    if parsed.scheme:
        raise InvalidGitMutationError("M2.5.1 admits only ssh://, SCP-like SSH, or https://")
    return _scp_like(url)
