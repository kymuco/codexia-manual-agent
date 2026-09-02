from __future__ import annotations

import hmac
import ipaddress
import os
import re
import secrets
import shlex
import shutil
import socket
import subprocess
import tempfile
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from codexia_manual_agent.domain.errors import (
    GitMutationPreconditionChangedError,
    GitRepositoryBoundaryError,
    InvalidGitMutationError,
)
from codexia_manual_agent.git_mutation.models import GitExecutableIdentity
from codexia_manual_agent.git_mutation.network_transport import (
    GitNetworkEndpoint,
    GitNetworkTransport,
)
from codexia_manual_agent.git_mutation.repository import (
    RepositorySnapshot,
    base_env,
    resolve_git_helper,
    run_git,
    sha256_bytes,
    validate_remote_name,
)
from codexia_manual_agent.git_mutation.ssh_execution import resolve_git_command_shell


MAX_HTTPS_CA_BUNDLE_BYTES = 16 * 1024 * 1024
MAX_HTTPS_CREDENTIAL_SOURCE_BYTES = 64 * 1024
MAX_HTTPS_CREDENTIAL_SECRET_CHARS = 4096
_SHELL_STARTUP_ENV = {"BASH_ENV", "ENV", "SHELLOPTS", "CDPATH", "IFS"}
_NATIVE_TLS_BACKEND = "bound-git-default"


@dataclass(frozen=True, slots=True)
class HttpsFileIdentity:
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
class HttpsCredentialSource:
    path: str
    size_bytes: int
    username: str
    # The ordinary file digest is intentionally private so a serialized proposal
    # never exposes an offline-testable digest of a low-entropy credential URL.
    secret_sha256: str = field(repr=False)
    commitment_key: bytes = field(repr=False)
    commitment_key_sha256: str
    secret_hmac_sha256: str

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "size_bytes": self.size_bytes,
            "username": self.username,
            "commitment_key_sha256": self.commitment_key_sha256,
            "secret_hmac_sha256": self.secret_hmac_sha256,
        }


@dataclass(frozen=True, slots=True)
class HttpsRouteBinding:
    address: str
    family: str
    curl_resolve_entry: str

    def to_dict(self) -> dict[str, str]:
        return {
            "address": self.address,
            "family": self.family,
            "curl_resolve_entry": self.curl_resolve_entry,
        }


@dataclass(frozen=True, slots=True)
class HttpsTransportBinding:
    endpoint: GitNetworkEndpoint
    route: HttpsRouteBinding
    git_shell: GitExecutableIdentity
    git_remote_https: GitExecutableIdentity
    git_remote_https_resolved_target: str
    credential_source: HttpsCredentialSource
    credential_bundle_root: str
    credential_bundle_path: str
    credential_helper_shell_command: str
    credential_helper_command_sha256: str
    ca_bundle: HttpsFileIdentity
    tls_backend: str = _NATIVE_TLS_BACKEND
    credential_mode: str = "frozen-shell-response.v1"
    route_mode: str = "curlopt-resolve-exact.v1"

    def to_dict(self) -> dict[str, Any]:
        return {
            "endpoint": self.endpoint.to_dict(),
            "review_destination": self.endpoint.review_destination,
            "route": self.route.to_dict(),
            "git_shell": self.git_shell.to_dict(),
            "git_remote_https": self.git_remote_https.to_dict(),
            "git_remote_https_resolved_target": self.git_remote_https_resolved_target,
            "credential_source": self.credential_source.to_public_dict(),
            "credential_helper_command_sha256": self.credential_helper_command_sha256,
            "ca_bundle": self.ca_bundle.to_dict(),
            "tls_backend": self.tls_backend,
            "credential_mode": self.credential_mode,
            "route_mode": self.route_mode,
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
) -> tuple[HttpsFileIdentity, bytes]:
    lexical = Path(value)
    if not lexical.is_absolute():
        raise InvalidGitMutationError(f"{label} must use an explicit absolute path")
    try:
        resolved = lexical.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise InvalidGitMutationError(f"{label} does not resolve") from exc
    if _is_link_like(lexical) or _norm(lexical) != _norm(resolved):
        raise GitRepositoryBoundaryError(
            f"{label} cannot be a symlink, junction, or redirected path"
        )
    try:
        resolved.relative_to(snapshot.workspace_root)
    except ValueError:
        pass
    else:
        raise GitRepositoryBoundaryError(f"{label} cannot be stored inside the workspace")
    if not resolved.is_file():
        raise InvalidGitMutationError(f"{label} must be a regular file")
    try:
        payload = resolved.read_bytes()
    except OSError as exc:
        raise InvalidGitMutationError(f"{label} cannot be read") from exc
    if not 1 <= len(payload) <= max_bytes:
        raise InvalidGitMutationError(f"{label} is empty or exceeds the M2.5.1 budget")
    return HttpsFileIdentity(str(resolved), len(payload), sha256_bytes(payload)), payload


def _credential_values(
    endpoint: GitNetworkEndpoint,
    payload: bytes,
) -> tuple[str, str]:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise InvalidGitMutationError("HTTPS credential source must be UTF-8") from exc
    active = [line.strip() for line in text.splitlines() if line.strip()]
    if len(active) != 1:
        raise InvalidGitMutationError(
            "M2.5.1 HTTPS credential source must contain exactly one credential URL"
        )
    parsed = urlparse(active[0])
    if parsed.scheme != "https" or parsed.query or parsed.fragment:
        raise InvalidGitMutationError("HTTPS credential source must contain one exact https:// URL")
    if not parsed.hostname or parsed.username is None or parsed.password is None:
        raise InvalidGitMutationError("HTTPS credential source requires explicit username and secret")
    try:
        port = 443 if parsed.port is None else parsed.port
    except ValueError as exc:
        raise InvalidGitMutationError("HTTPS credential source port is invalid") from exc
    path = parsed.path[1:] if parsed.path.startswith("/") else parsed.path
    if (
        parsed.hostname.casefold() != endpoint.host.casefold()
        or port != endpoint.port
        or path != endpoint.repository_path
    ):
        raise InvalidGitMutationError(
            "HTTPS credential source does not bind the exact reviewed repository endpoint"
        )
    username = unquote(parsed.username)
    secret = unquote(parsed.password)
    if (
        not username
        or len(username) > 256
        or any(char in username for char in "\r\n\x00")
        or not secret
        or len(secret) > MAX_HTTPS_CREDENTIAL_SECRET_CHARS
        or any(char in secret for char in "\r\n\x00")
    ):
        raise InvalidGitMutationError("HTTPS credential source username/secret is invalid")
    return username, secret


def _credential_source(
    snapshot: RepositorySnapshot,
    endpoint: GitNetworkEndpoint,
    value: str | Path,
) -> HttpsCredentialSource:
    identity, payload = _bind_file(
        snapshot,
        value,
        label="HTTPS credential source",
        max_bytes=MAX_HTTPS_CREDENTIAL_SOURCE_BYTES,
    )
    username, _secret = _credential_values(endpoint, payload)
    commitment_key = secrets.token_bytes(32)
    return HttpsCredentialSource(
        path=identity.path,
        size_bytes=identity.size_bytes,
        username=username,
        secret_sha256=identity.sha256,
        commitment_key=commitment_key,
        commitment_key_sha256=sha256_bytes(commitment_key),
        secret_hmac_sha256=hmac.new(commitment_key, payload, sha256).hexdigest(),
    )


def _resolve_route(endpoint: GitNetworkEndpoint) -> HttpsRouteBinding:
    try:
        literal = ipaddress.ip_address(endpoint.host)
    except ValueError:
        try:
            records = socket.getaddrinfo(
                endpoint.host,
                endpoint.port,
                family=socket.AF_UNSPEC,
                type=socket.SOCK_STREAM,
                proto=socket.IPPROTO_TCP,
            )
        except OSError as exc:
            raise InvalidGitMutationError("HTTPS endpoint route cannot be resolved") from exc
        chosen: tuple[int, ipaddress.IPv4Address | ipaddress.IPv6Address] | None = None
        seen: set[tuple[int, str]] = set()
        for family, _socktype, _proto, _canonname, sockaddr in records:
            if family not in {socket.AF_INET, socket.AF_INET6}:
                continue
            if family == socket.AF_INET6 and len(sockaddr) >= 4 and sockaddr[3]:
                continue
            try:
                address = ipaddress.ip_address(sockaddr[0])
            except ValueError:
                continue
            marker = (family, address.compressed.lower())
            if marker in seen:
                continue
            seen.add(marker)
            if chosen is None:
                chosen = (family, address)
        if chosen is None:
            raise InvalidGitMutationError("HTTPS endpoint has no admitted IPv4/IPv6 route")
        family, address = chosen
    else:
        family = socket.AF_INET6 if literal.version == 6 else socket.AF_INET
        address = literal
    rendered = address.compressed.lower()
    curl_address = f"[{rendered}]" if address.version == 6 else rendered
    return HttpsRouteBinding(
        address=rendered,
        family="ipv6" if family == socket.AF_INET6 else "ipv4",
        curl_resolve_entry=f"{endpoint.host}:{endpoint.port}:{curl_address}",
    )


def _reject_local_http_credential_influence(
    snapshot: RepositorySnapshot,
    *,
    remote_name: str,
) -> None:
    worktree_config = run_git(
        snapshot.git,
        snapshot.workspace_root,
        [
            "config",
            "--local",
            "--no-includes",
            "--type=bool",
            "--get",
            "extensions.worktreeConfig",
        ],
        check=False,
    )
    if worktree_config.returncode == 0:
        if worktree_config.stdout.strip() != b"false":
            raise InvalidGitMutationError(
                "M2.5.1 HTTPS rejects per-worktree Git config semantics"
            )
    elif worktree_config.returncode != 1:
        raise InvalidGitMutationError(
            "M2.5.1 HTTPS worktree config policy could not be checked"
        )

    include_config = run_git(
        snapshot.git,
        snapshot.workspace_root,
        [
            "config",
            "--local",
            "--no-includes",
            "--get-regexp",
            r"^include(\.path|[iI]f\..*\.path)$",
        ],
        check=False,
    )
    if include_config.returncode == 0:
        raise InvalidGitMutationError(
            "M2.5.1 HTTPS rejects local Git config include semantics"
        )
    if include_config.returncode != 1:
        raise InvalidGitMutationError(
            "M2.5.1 HTTPS local Git config include policy could not be checked"
        )

    for pattern, label in (
        (r"^(http|credential)\.", "HTTP/TLS/credential behavior"),
        (
            rf"^remote\.{re.escape(remote_name)}\.(proxy|proxyauthmethod)$",
            "remote proxy behavior",
        ),
    ):
        result = run_git(
            snapshot.git,
            snapshot.workspace_root,
            ["config", "--local", "--no-includes", "--get-regexp", pattern],
            check=False,
        )
        if result.returncode == 0:
            raise InvalidGitMutationError(
                f"M2.5.1 HTTPS rejects local Git config that can alter {label}"
            )
        if result.returncode != 1:
            raise InvalidGitMutationError(f"Local Git config could not be checked for {label}")


def _git_shell_path_text(path: str | Path) -> str:
    rendered = str(path).replace("\\", "/")
    if any(char in rendered for char in "\r\n\x00"):
        raise InvalidGitMutationError("HTTPS credential response path contains control characters")
    return rendered


def _shell_probe_environment() -> dict[str, str]:
    env = base_env()
    for key in tuple(env):
        if key.upper() in _SHELL_STARTUP_ENV:
            env.pop(key, None)
    return env


def _require_credential_shell_builtins(shell: GitExecutableIdentity) -> None:
    try:
        result = subprocess.run(
            [shell.path, "-c", "type read; type printf"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            check=False,
            timeout=5,
            env=_shell_probe_environment(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise InvalidGitMutationError("Git command shell credential builtin probe failed") from exc
    if result.returncode != 0:
        raise InvalidGitMutationError("Git command shell lacks required credential builtins")
    try:
        lines = [line.casefold() for line in result.stdout.decode("utf-8").splitlines() if line]
    except UnicodeDecodeError as exc:
        raise InvalidGitMutationError("Git command shell builtin probe was not UTF-8") from exc
    if len(lines) != 2 or any("builtin" not in line for line in lines):
        raise InvalidGitMutationError(
            "M2.5.1 HTTPS requires shell-builtin read/printf for credential delivery"
        )


def _read_only_helper_command(bundle_file: Path) -> str:
    response_file = shlex.quote(_git_shell_path_text(bundle_file))
    return (
        "!f() { case \"$1\" in "
        f"get) while IFS= read -r line; do printf '%s\\n' \"$line\"; done < {response_file} ;; "
        "store|erase) exit 0 ;; *) exit 0 ;; esac; }; f"
    )


def _bind_https_git_helper(
    snapshot: RepositorySnapshot,
) -> tuple[GitExecutableIdentity, str]:
    identity = resolve_git_helper(snapshot, "git-remote-https")
    helper_path = Path(identity.path)
    try:
        resolved_target = helper_path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise InvalidGitMutationError("Bound git-remote-https target does not resolve") from exc
    try:
        resolved_target.relative_to(snapshot.workspace_root)
    except ValueError:
        pass
    else:
        raise GitRepositoryBoundaryError(
            "git-remote-https resolved target cannot live inside the governed workspace"
        )
    if not resolved_target.is_file():
        raise InvalidGitMutationError("Bound git-remote-https target must be a regular file")
    return identity, str(resolved_target)


def bind_https_transport(
    snapshot: RepositorySnapshot,
    endpoint: GitNetworkEndpoint,
    *,
    remote_name: str,
    credential_file: str | Path,
    ca_bundle_file: str | Path,
) -> HttpsTransportBinding:
    if not isinstance(snapshot, RepositorySnapshot):
        raise TypeError("snapshot must be RepositorySnapshot")
    if not isinstance(endpoint, GitNetworkEndpoint) or endpoint.transport is not GitNetworkTransport.HTTPS:
        raise InvalidGitMutationError("HTTPS transport binding requires an admitted HTTPS endpoint")
    remote_name = validate_remote_name(remote_name)
    _reject_local_http_credential_influence(snapshot, remote_name=remote_name)
    source = _credential_source(snapshot, endpoint, credential_file)
    ca_bundle, _ = _bind_file(
        snapshot,
        ca_bundle_file,
        label="HTTPS CA bundle",
        max_bytes=MAX_HTTPS_CA_BUNDLE_BYTES,
    )
    git_shell = resolve_git_command_shell(snapshot)
    _require_credential_shell_builtins(git_shell)
    git_remote_https, git_remote_https_resolved_target = _bind_https_git_helper(snapshot)
    root = Path(tempfile.mkdtemp(prefix="codexia-m251-https-"))
    try:
        resolved_root = root.resolve(strict=True)
        try:
            resolved_root.relative_to(snapshot.workspace_root)
        except ValueError:
            pass
        else:
            raise GitRepositoryBoundaryError("HTTPS credential bundle cannot live inside workspace")
        bundle_path = resolved_root / "credentials"
        command = _read_only_helper_command(bundle_path)
        return HttpsTransportBinding(
            endpoint=endpoint,
            route=_resolve_route(endpoint),
            git_shell=git_shell,
            git_remote_https=git_remote_https,
            git_remote_https_resolved_target=git_remote_https_resolved_target,
            credential_source=source,
            credential_bundle_root=str(resolved_root),
            credential_bundle_path=str(bundle_path),
            credential_helper_shell_command=command,
            credential_helper_command_sha256=sha256_bytes(command.encode("utf-8")),
            ca_bundle=ca_bundle,
        )
    except BaseException:
        shutil.rmtree(root, ignore_errors=True)
        raise


def _credential_source_bytes(binding: HttpsTransportBinding) -> bytes:
    source = binding.credential_source
    if sha256_bytes(source.commitment_key) != source.commitment_key_sha256:
        raise GitMutationPreconditionChangedError("HTTPS credential commitment key changed")
    path = Path(source.path)
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise GitMutationPreconditionChangedError("Bound HTTPS credential source no longer resolves") from exc
    if len(payload) != source.size_bytes or sha256_bytes(payload) != source.secret_sha256:
        raise GitMutationPreconditionChangedError("Bound HTTPS credential source identity changed")
    observed_hmac = hmac.new(source.commitment_key, payload, sha256).hexdigest()
    if not hmac.compare_digest(observed_hmac, source.secret_hmac_sha256):
        raise GitMutationPreconditionChangedError("Bound HTTPS credential commitment changed")
    return payload


def _credential_response_bytes(binding: HttpsTransportBinding, payload: bytes) -> bytes:
    try:
        username, secret = _credential_values(binding.endpoint, payload)
    except InvalidGitMutationError as exc:
        raise GitMutationPreconditionChangedError(
            "Bound HTTPS credential source semantics changed"
        ) from exc
    if username != binding.credential_source.username:
        raise GitMutationPreconditionChangedError("Bound HTTPS credential username changed")
    return f"username={username}\npassword={secret}\n".encode("utf-8")


def _revalidate_identity(identity: GitExecutableIdentity, *, label: str) -> None:
    path = Path(identity.path)
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise GitMutationPreconditionChangedError(f"Bound {label} no longer resolves") from exc
    if len(payload) != identity.size_bytes or sha256_bytes(payload) != identity.sha256:
        raise GitMutationPreconditionChangedError(f"Bound {label} identity changed")


def _revalidate_https_git_helper(binding: HttpsTransportBinding) -> None:
    helper_path = Path(binding.git_remote_https.path)
    bound_target = Path(binding.git_remote_https_resolved_target)
    if not bound_target.is_absolute():
        raise GitMutationPreconditionChangedError(
            "Bound git-remote-https target identity is not absolute"
        )
    try:
        current_target = helper_path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise GitMutationPreconditionChangedError(
            "Bound git-remote-https target no longer resolves"
        ) from exc
    if _norm(current_target) != _norm(bound_target):
        raise GitMutationPreconditionChangedError("Bound git-remote-https target changed")
    if not current_target.is_file():
        raise GitMutationPreconditionChangedError(
            "Bound git-remote-https target is no longer a regular file"
        )
    _revalidate_identity(binding.git_remote_https, label="git-remote-https")


def revalidate_https_transport(
    binding: HttpsTransportBinding,
    *,
    require_materialized: bool = False,
) -> None:
    if not isinstance(binding, HttpsTransportBinding):
        raise TypeError("binding must be HttpsTransportBinding")
    if binding.tls_backend != _NATIVE_TLS_BACKEND:
        raise GitMutationPreconditionChangedError("HTTPS TLS backend policy changed")
    _revalidate_identity(binding.git_shell, label="Git command shell")
    _revalidate_https_git_helper(binding)

    ca_path = Path(binding.ca_bundle.path)
    try:
        ca_payload = ca_path.read_bytes()
    except OSError as exc:
        raise GitMutationPreconditionChangedError("Bound HTTPS CA bundle no longer resolves") from exc
    if (
        len(ca_payload) != binding.ca_bundle.size_bytes
        or sha256_bytes(ca_payload) != binding.ca_bundle.sha256
    ):
        raise GitMutationPreconditionChangedError("Bound HTTPS CA bundle identity changed")
    source_payload = _credential_source_bytes(binding)

    root = Path(binding.credential_bundle_root)
    if not root.is_dir():
        raise GitMutationPreconditionChangedError("HTTPS credential bundle namespace changed")
    try:
        entries = {entry.name for entry in root.iterdir()}
    except OSError as exc:
        raise GitMutationPreconditionChangedError("HTTPS credential bundle cannot be enumerated") from exc
    expected = {"credentials"} if require_materialized else set()
    if entries != expected:
        raise GitMutationPreconditionChangedError("HTTPS credential bundle contains unbound entries")
    if require_materialized:
        target = Path(binding.credential_bundle_path)
        try:
            payload = target.read_bytes()
        except OSError as exc:
            raise GitMutationPreconditionChangedError("HTTPS credential bundle file is unavailable") from exc
        expected_response = _credential_response_bytes(binding, source_payload)
        if not hmac.compare_digest(payload, expected_response):
            raise GitMutationPreconditionChangedError("HTTPS credential response bytes changed")
    if (
        sha256_bytes(binding.credential_helper_shell_command.encode("utf-8"))
        != binding.credential_helper_command_sha256
    ):
        raise GitMutationPreconditionChangedError("HTTPS credential helper command changed")


def materialize_https_credentials(binding: HttpsTransportBinding) -> None:
    revalidate_https_transport(binding, require_materialized=False)
    source_payload = _credential_source_bytes(binding)
    payload = _credential_response_bytes(binding, source_payload)
    target = Path(binding.credential_bundle_path)
    try:
        with target.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if os.name != "nt":
            target.chmod(0o600)
    except FileExistsError as exc:
        raise GitMutationPreconditionChangedError("HTTPS credential bundle already exists") from exc
    except OSError as exc:
        raise InvalidGitMutationError("HTTPS credential bundle cannot be materialized") from exc
    revalidate_https_transport(binding, require_materialized=True)


def close_https_transport(binding: HttpsTransportBinding) -> None:
    if not isinstance(binding, HttpsTransportBinding):
        raise TypeError("binding must be HttpsTransportBinding")
    shutil.rmtree(binding.credential_bundle_root, ignore_errors=True)


def https_git_config_args(binding: HttpsTransportBinding) -> list[str]:
    revalidate_https_transport(binding, require_materialized=True)
    return [
        "-c",
        "http.proxy=",
        "-c",
        "http.sslVerify=true",
        "-c",
        f"http.sslCAInfo={binding.ca_bundle.path}",
        "-c",
        "http.sslCAPath=",
        "-c",
        "http.schannelUseSSLCAInfo=true",
        "-c",
        "http.followRedirects=false",
        "-c",
        "http.extraHeader=",
        "-c",
        "http.cookieFile=",
        "-c",
        "http.saveCookies=false",
        "-c",
        "http.curloptResolve=",
        "-c",
        f"http.curloptResolve={binding.route.curl_resolve_entry}",
        "-c",
        "http.proactiveAuth=none",
        "-c",
        "http.emptyAuth=false",
        "-c",
        "http.delegation=none",
        "-c",
        "credential.helper=",
        "-c",
        f"credential.helper={binding.credential_helper_shell_command}",
        "-c",
        "credential.useHttpPath=true",
        "-c",
        "credential.interactive=false",
        "-c",
        "credential.guiPrompt=false",
    ]


def https_git_environment(
    snapshot: RepositorySnapshot,
    binding: HttpsTransportBinding,
) -> dict[str, str]:
    revalidate_https_transport(binding, require_materialized=True)
    env = base_env()
    for key in tuple(env):
        upper = key.upper()
        if (
            upper in _SHELL_STARTUP_ENV
            or upper in {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "SSLKEYLOGFILE"}
            or upper.startswith("CURL_")
            or upper.startswith("OPENSSL_")
            or upper.startswith("SSL_CERT_")
            or upper.startswith("GIT_SSL_")
            or upper.startswith("GIT_PROXY_")
            or upper.startswith("GIT_HTTP_")
            or upper == "GIT_CURL_VERBOSE"
            or upper.startswith("GCM_")
        ):
            env.pop(key, None)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_EXEC_PATH"] = str(Path(binding.git_remote_https.path).parent)
    if os.name == "nt":
        directories = [
            str(Path(binding.git_shell.path).parent),
            str(Path(snapshot.git.path).parent),
        ]
        env["PATH"] = os.pathsep.join(dict.fromkeys(directories))
    return env
