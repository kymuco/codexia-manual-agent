from __future__ import annotations

import base64
import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from codexia_manual_agent.domain.errors import GitRepositoryBoundaryError, InvalidGitMutationError
from codexia_manual_agent.git_mutation.models import GitExecutableIdentity
from codexia_manual_agent.git_mutation.network_transport import GitNetworkEndpoint, GitNetworkTransport
from codexia_manual_agent.git_mutation.repository import RepositorySnapshot, resolve_ssh_executable, sha256_bytes


MAX_SSH_IDENTITY_BYTES = 1024 * 1024
MAX_SSH_HOST_KEY_FILE_BYTES = 256 * 1024
_ALLOWED_HOST_KEY_TYPES = {
    "ssh-ed25519",
    "ecdsa-sha2-nistp256",
    "ssh-rsa",
}


@dataclass(frozen=True, slots=True)
class SshFileIdentity:
    path: str
    size_bytes: int
    sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
        }


@dataclass(frozen=True, slots=True)
class SshHostKeyPin:
    host_token: str
    key_type: str
    fingerprint_sha256: str
    source_file: SshFileIdentity

    def to_dict(self) -> dict[str, Any]:
        return {
            "host_token": self.host_token,
            "key_type": self.key_type,
            "fingerprint_sha256": self.fingerprint_sha256,
            "source_file": self.source_file.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class SshTransportBinding:
    endpoint: GitNetworkEndpoint
    ssh_executable: GitExecutableIdentity
    identity_file: SshFileIdentity
    host_key_pin: SshHostKeyPin
    credential_mode: str = "explicit-identity-file.v1"
    host_key_mode: str = "single-exact-known-host.v1"

    def to_dict(self) -> dict[str, Any]:
        return {
            "endpoint": self.endpoint.to_dict(),
            "review_destination": self.endpoint.review_destination,
            "ssh_executable": self.ssh_executable.to_dict(),
            "identity_file": self.identity_file.to_dict(),
            "host_key_pin": self.host_key_pin.to_dict(),
            "credential_mode": self.credential_mode,
            "host_key_mode": self.host_key_mode,
        }


def _norm(path: Path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def _is_link_like(path: Path) -> bool:
    if path.is_symlink():
        return True
    junction = getattr(path, "is_junction", None)
    return bool(junction is not None and junction())


def _bind_file(
    snapshot: RepositorySnapshot,
    value: str | Path,
    *,
    label: str,
    max_bytes: int,
) -> tuple[SshFileIdentity, bytes]:
    lexical = Path(value)
    if not lexical.is_absolute():
        raise InvalidGitMutationError(f"{label} must use an explicit absolute path")
    try:
        resolved = lexical.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise InvalidGitMutationError(f"{label} does not resolve") from exc
    if _is_link_like(lexical) or _norm(lexical) != _norm(resolved):
        raise GitRepositoryBoundaryError(f"{label} cannot be a symlink, junction, or redirected path")
    try:
        resolved.relative_to(snapshot.workspace_root)
    except ValueError:
        pass
    else:
        raise GitRepositoryBoundaryError(f"{label} cannot be stored inside the governed workspace")
    if not resolved.is_file():
        raise InvalidGitMutationError(f"{label} must be a regular file")
    try:
        payload = resolved.read_bytes()
    except OSError as exc:
        raise InvalidGitMutationError(f"{label} cannot be read") from exc
    if not 1 <= len(payload) <= max_bytes:
        raise InvalidGitMutationError(f"{label} is empty or exceeds the M2.5.1 budget")
    return (
        SshFileIdentity(
            path=str(resolved),
            size_bytes=len(payload),
            sha256=sha256_bytes(payload),
        ),
        payload,
    )


def _expected_known_host_token(endpoint: GitNetworkEndpoint) -> str:
    if endpoint.transport is not GitNetworkTransport.SSH:
        raise InvalidGitMutationError("SSH host-key binding requires an SSH endpoint")
    if endpoint.port == 22:
        return endpoint.host
    return f"[{endpoint.host}]:{endpoint.port}"


def _ssh_string(payload: bytes, offset: int = 0) -> tuple[bytes, int]:
    if len(payload) - offset < 4:
        raise InvalidGitMutationError("SSH host key blob is truncated")
    size = int.from_bytes(payload[offset : offset + 4], "big")
    start = offset + 4
    end = start + size
    if size <= 0 or end > len(payload):
        raise InvalidGitMutationError("SSH host key blob has an invalid string length")
    return payload[start:end], end


def _parse_host_key_line(endpoint: GitNetworkEndpoint, payload: bytes) -> tuple[str, str, str]:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise InvalidGitMutationError("SSH host-key file must be UTF-8 text") from exc
    active: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        active.append(line)
    if len(active) != 1:
        raise InvalidGitMutationError("M2.5.1 SSH host-key file must contain exactly one active key")
    line = active[0]
    if line.startswith("@"):
        raise InvalidGitMutationError("M2.5.1 SSH host-key markers are not admitted")
    fields = line.split()
    if len(fields) < 3:
        raise InvalidGitMutationError("SSH host-key line has an invalid form")
    host_token, key_type, encoded = fields[:3]
    expected_token = _expected_known_host_token(endpoint)
    if (
        host_token != expected_token
        or "," in host_token
        or "*" in host_token
        or "?" in host_token
        or host_token.startswith("|")
    ):
        raise InvalidGitMutationError("SSH host-key entry does not bind the exact endpoint")
    if key_type not in _ALLOWED_HOST_KEY_TYPES:
        raise InvalidGitMutationError("SSH host-key algorithm is not admitted by M2.5.1")
    try:
        key_blob = base64.b64decode(encoded.encode("ascii"), validate=True)
    except (UnicodeEncodeError, ValueError) as exc:
        raise InvalidGitMutationError("SSH host-key payload is not valid base64") from exc
    declared_type, _ = _ssh_string(key_blob)
    try:
        embedded_type = declared_type.decode("ascii")
    except UnicodeDecodeError as exc:
        raise InvalidGitMutationError("SSH host-key blob type is not ASCII") from exc
    if embedded_type != key_type:
        raise InvalidGitMutationError("SSH host-key line type does not match the key blob")
    fingerprint = base64.b64encode(hashlib.sha256(key_blob).digest()).decode("ascii").rstrip("=")
    return host_token, key_type, f"SHA256:{fingerprint}"


def bind_ssh_transport(
    snapshot: RepositorySnapshot,
    endpoint: GitNetworkEndpoint,
    *,
    identity_file: str | Path,
    host_key_file: str | Path,
) -> SshTransportBinding:
    if not isinstance(snapshot, RepositorySnapshot):
        raise TypeError("snapshot must be RepositorySnapshot")
    if not isinstance(endpoint, GitNetworkEndpoint) or endpoint.transport is not GitNetworkTransport.SSH:
        raise InvalidGitMutationError("SSH transport binding requires an admitted SSH endpoint")
    identity, _ = _bind_file(
        snapshot,
        identity_file,
        label="SSH identity file",
        max_bytes=MAX_SSH_IDENTITY_BYTES,
    )
    implicit_cert = Path(f"{identity.path}-cert.pub")
    if implicit_cert.exists() or implicit_cert.is_symlink():
        raise InvalidGitMutationError(
            "M2.5.1 rejects implicit <IdentityFile>-cert.pub credential discovery"
        )
    host_file, host_payload = _bind_file(
        snapshot,
        host_key_file,
        label="SSH host-key file",
        max_bytes=MAX_SSH_HOST_KEY_FILE_BYTES,
    )
    host_token, key_type, fingerprint = _parse_host_key_line(endpoint, host_payload)
    return SshTransportBinding(
        endpoint=endpoint,
        ssh_executable=resolve_ssh_executable(snapshot.workspace_root),
        identity_file=identity,
        host_key_pin=SshHostKeyPin(
            host_token=host_token,
            key_type=key_type,
            fingerprint_sha256=fingerprint,
            source_file=host_file,
        ),
    )
