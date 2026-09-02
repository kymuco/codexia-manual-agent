from __future__ import annotations

import ipaddress
import os
import shlex
import shutil
import socket
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from codexia_manual_agent.domain.errors import (
    GitMutationPreconditionChangedError,
    GitRepositoryBoundaryError,
    InvalidGitMutationError,
)
from codexia_manual_agent.git_mutation.models import GitExecutableIdentity
from codexia_manual_agent.git_mutation.repository import (
    RepositorySnapshot,
    base_env,
    sha256_bytes,
)
from codexia_manual_agent.git_mutation.ssh_transport import (
    SshFileIdentity,
    SshTransportBinding,
)


_SHELL_STARTUP_ENV = {
    "BASH_ENV",
    "ENV",
    "SHELLOPTS",
    "CDPATH",
    "IFS",
}
_SSH_PATH_EXPANSION_CHARS = "%$~\r\n\x00"


@dataclass(frozen=True, slots=True)
class SshRouteBinding:
    address: str
    family: str

    def to_dict(self) -> dict[str, str]:
        return {"address": self.address, "family": self.family}


@dataclass(frozen=True, slots=True)
class SshExecutionPlan:
    binding: SshTransportBinding
    route: SshRouteBinding
    git_shell: GitExecutableIdentity
    bundle_root: str
    bundle_identity_path: str
    bundle_known_hosts_path: str
    certificate_block_path: str
    ssh_command: str
    ssh_command_sha256: str
    backend: str = "network-ssh-direct.v1"

    def to_dict(self) -> dict[str, Any]:
        return {
            "binding": self.binding.to_dict(),
            "route": self.route.to_dict(),
            "git_shell": self.git_shell.to_dict(),
            "bundle_identity_path": self.bundle_identity_path,
            "bundle_known_hosts_path": self.bundle_known_hosts_path,
            "ssh_command_sha256": self.ssh_command_sha256,
            "backend": self.backend,
        }


def _hash_executable(path: Path, *, label: str) -> GitExecutableIdentity:
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise InvalidGitMutationError(f"{label} does not resolve") from exc
    if not resolved.is_file():
        raise InvalidGitMutationError(f"{label} must be a regular file")
    try:
        payload = resolved.read_bytes()
    except OSError as exc:
        raise InvalidGitMutationError(f"{label} cannot be read") from exc
    return GitExecutableIdentity(
        path=str(resolved),
        size_bytes=len(payload),
        sha256=sha256_bytes(payload),
    )


def _git_shell_path(snapshot: RepositorySnapshot) -> Path:
    if os.name != "nt":
        fallback = Path("/bin/sh")
        if fallback.exists():
            return fallback
        raise InvalidGitMutationError("Git POSIX command shell cannot be resolved")

    # Current Git for Windows implements git_shell_path() with locate_in_PATH("sh").
    # The execution environment later exposes only this installation-local shell.
    git_path = Path(snapshot.git.path)
    roots: list[Path] = []
    for parent in (git_path.parent.parent, git_path.parent.parent.parent):
        if parent not in roots:
            roots.append(parent)
    candidates: list[Path] = []
    for root in roots:
        candidates.extend((root / "usr" / "bin" / "sh.exe", root / "bin" / "sh.exe"))
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise InvalidGitMutationError(
        "M2.5.1 requires Git for Windows with an installation-local sh.exe"
    )


def resolve_git_command_shell(snapshot: RepositorySnapshot) -> GitExecutableIdentity:
    shell = _git_shell_path(snapshot)
    try:
        shell.resolve(strict=True).relative_to(snapshot.workspace_root)
    except ValueError:
        pass
    else:
        raise GitRepositoryBoundaryError("Git command shell cannot resolve inside the workspace")
    return _hash_executable(shell, label="Git command shell")


def _resolve_route(binding: SshTransportBinding) -> SshRouteBinding:
    host = binding.endpoint.host
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        try:
            records = socket.getaddrinfo(
                host,
                binding.endpoint.port,
                family=socket.AF_UNSPEC,
                type=socket.SOCK_STREAM,
                proto=socket.IPPROTO_TCP,
            )
        except OSError as exc:
            raise InvalidGitMutationError("SSH endpoint route cannot be resolved") from exc
        chosen: tuple[int, str] | None = None
        seen: set[tuple[int, str]] = set()
        for family, _socktype, _proto, _canonname, sockaddr in records:
            if family not in {socket.AF_INET, socket.AF_INET6}:
                continue
            raw = sockaddr[0]
            if family == socket.AF_INET6 and len(sockaddr) >= 4 and sockaddr[3]:
                continue
            try:
                canonical = ipaddress.ip_address(raw).compressed.lower()
            except ValueError:
                continue
            item = (family, canonical)
            if item in seen:
                continue
            seen.add(item)
            if chosen is None:
                chosen = item
        if chosen is None:
            raise InvalidGitMutationError("SSH endpoint has no admitted IPv4/IPv6 route")
        family, raw_address = chosen
        return SshRouteBinding(
            address=raw_address,
            family="ipv6" if family == socket.AF_INET6 else "ipv4",
        )
    return SshRouteBinding(
        address=address.compressed.lower(),
        family="ipv6" if address.version == 6 else "ipv4",
    )


def _validate_ssh_literal_path(path: Path, *, label: str) -> None:
    rendered = str(path)
    if any(char in rendered for char in _SSH_PATH_EXPANSION_CHARS):
        raise GitRepositoryBoundaryError(
            f"{label} contains OpenSSH token/environment expansion syntax"
        )


def _source_bytes(identity: SshFileIdentity, *, label: str) -> bytes:
    path = Path(identity.path)
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise GitMutationPreconditionChangedError(f"{label} no longer resolves") from exc
    if len(payload) != identity.size_bytes or sha256_bytes(payload) != identity.sha256:
        raise GitMutationPreconditionChangedError(f"{label} identity changed")
    return payload


def _write_exact_new(
    path: Path,
    payload: bytes,
    *,
    expected_size: int,
    expected_sha256: str,
    label: str,
) -> None:
    if len(payload) != expected_size or sha256_bytes(payload) != expected_sha256:
        raise GitMutationPreconditionChangedError(f"{label} source identity changed")
    try:
        with path.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if os.name != "nt":
            path.chmod(0o600)
        observed = path.read_bytes()
    except FileExistsError as exc:
        raise GitMutationPreconditionChangedError(f"{label} target already exists") from exc
    except OSError as exc:
        raise InvalidGitMutationError(f"{label} cannot be materialized") from exc
    if len(observed) != expected_size or sha256_bytes(observed) != expected_sha256:
        raise GitMutationPreconditionChangedError(f"{label} changed while being materialized")


def _ssh_option_argv(
    binding: SshTransportBinding,
    route: SshRouteBinding,
    identity: Path,
    known_hosts: Path,
    certificate_block: Path,
) -> tuple[str, ...]:
    user = binding.endpoint.ssh_user
    if user is None:
        raise InvalidGitMutationError("SSH execution requires an explicit user")
    return (
        "-F",
        "none",
        "-T",
        "-l",
        user,
        "-p",
        str(binding.endpoint.port),
        "-o",
        "BatchMode=yes",
        "-o",
        "PasswordAuthentication=no",
        "-o",
        "KbdInteractiveAuthentication=no",
        "-o",
        "PreferredAuthentications=publickey",
        "-o",
        "PubkeyAuthentication=yes",
        "-o",
        "PubkeyAcceptedAlgorithms=-sk-*,-webauthn-sk-*",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "IdentityAgent=none",
        "-o",
        f"IdentityFile={identity}",
        "-o",
        f"CertificateFile={certificate_block}",
        "-o",
        f"HostName={route.address}",
        "-o",
        f"HostKeyAlias={binding.host_key_pin.host_token}",
        "-o",
        f"UserKnownHostsFile={known_hosts}",
        "-o",
        f"GlobalKnownHostsFile={known_hosts}",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "UpdateHostKeys=no",
        "-o",
        "VerifyHostKeyDNS=no",
        "-o",
        "CanonicalizeHostname=no",
        "-o",
        "CheckHostIP=no",
        "-o",
        "ProxyCommand=none",
        "-o",
        "ProxyJump=none",
        "-o",
        "ControlMaster=no",
        "-o",
        "ControlPath=none",
        "-o",
        "ControlPersist=no",
        "-o",
        "ForwardAgent=no",
        "-o",
        "ForwardX11=no",
        "-o",
        "ClearAllForwardings=yes",
        "-o",
        "PermitLocalCommand=no",
        "-o",
        "RequestTTY=no",
    )


def _quote_command(argv: tuple[str, ...]) -> str:
    return " ".join(shlex.quote(value) for value in argv)


def _ssh_base_environment() -> dict[str, str]:
    env = base_env()
    for key in tuple(env):
        upper = key.upper()
        if upper in _SHELL_STARTUP_ENV or upper.startswith("SSH_"):
            env.pop(key, None)
    return env


def _bundle_entries(plan: SshExecutionPlan) -> set[str]:
    root = Path(plan.bundle_root)
    try:
        return {entry.name for entry in root.iterdir()}
    except OSError as exc:
        raise GitMutationPreconditionChangedError("SSH execution bundle cannot be enumerated") from exc


def _revalidate_source(identity: SshFileIdentity, *, label: str) -> None:
    _source_bytes(identity, label=label)


def revalidate_ssh_execution_plan(
    plan: SshExecutionPlan,
    *,
    require_materialized: bool = False,
) -> None:
    if not isinstance(plan, SshExecutionPlan):
        raise TypeError("plan must be SshExecutionPlan")
    current_shell = _hash_executable(Path(plan.git_shell.path), label="Bound Git command shell")
    if current_shell != plan.git_shell:
        raise GitMutationPreconditionChangedError("Bound Git command shell identity changed")
    current_ssh = _hash_executable(
        Path(plan.binding.ssh_executable.path),
        label="Bound SSH executable",
    )
    if current_ssh != plan.binding.ssh_executable:
        raise GitMutationPreconditionChangedError("Bound SSH executable identity changed")
    _revalidate_source(plan.binding.identity_file, label="SSH identity source")
    _revalidate_source(plan.binding.host_key_pin.source_file, label="SSH host-key source")

    root = Path(plan.bundle_root)
    cert_block = Path(plan.certificate_block_path)
    public_block = Path(f"{plan.bundle_identity_path}.pub")
    if not root.is_dir() or not cert_block.is_dir() or not public_block.is_dir():
        raise GitMutationPreconditionChangedError("SSH execution bundle namespace changed")
    _validate_ssh_literal_path(root, label="SSH execution bundle")
    blockers = {"identity.pub", "identity-cert.pub"}
    expected = blockers | ({"identity", "known_hosts"} if require_materialized else set())
    if _bundle_entries(plan) != expected:
        raise GitMutationPreconditionChangedError("SSH execution bundle contains unbound entries")
    if require_materialized:
        identity_path = Path(plan.bundle_identity_path)
        known_hosts_path = Path(plan.bundle_known_hosts_path)
        try:
            identity_payload = identity_path.read_bytes()
            known_hosts_payload = known_hosts_path.read_bytes()
        except OSError as exc:
            raise GitMutationPreconditionChangedError("SSH execution bundle files are unavailable") from exc
        if (
            len(identity_payload) != plan.binding.identity_file.size_bytes
            or sha256_bytes(identity_payload) != plan.binding.identity_file.sha256
        ):
            raise GitMutationPreconditionChangedError("SSH bundle identity changed")
        source_host = plan.binding.host_key_pin.source_file
        if (
            len(known_hosts_payload) != source_host.size_bytes
            or sha256_bytes(known_hosts_payload) != source_host.sha256
        ):
            raise GitMutationPreconditionChangedError("SSH bundle known-hosts changed")
    if sha256_bytes(plan.ssh_command.encode("utf-8")) != plan.ssh_command_sha256:
        raise GitMutationPreconditionChangedError("SSH command identity changed")


def probe_ssh_effective_config(plan: SshExecutionPlan) -> dict[str, tuple[str, ...]]:
    """Ask the bound OpenSSH binary to parse the exact option set without networking."""
    revalidate_ssh_execution_plan(plan, require_materialized=False)
    options = _ssh_option_argv(
        plan.binding,
        plan.route,
        Path(plan.bundle_identity_path),
        Path(plan.bundle_known_hosts_path),
        Path(plan.certificate_block_path),
    )
    try:
        result = subprocess.run(
            [plan.binding.ssh_executable.path, "-G", *options, plan.binding.endpoint.host],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            env=_ssh_base_environment(),
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise InvalidGitMutationError("Bound OpenSSH effective-config probe could not start") from exc
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace")[:4096]
        raise InvalidGitMutationError(
            f"Bound OpenSSH rejected the M2.5.1 option set: {detail}"
        )
    try:
        text = result.stdout.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise InvalidGitMutationError("OpenSSH effective config is not UTF-8") from exc
    parsed: dict[str, list[str]] = {}
    for raw in text.splitlines():
        if not raw.strip():
            continue
        key, sep, value = raw.partition(" ")
        if not sep:
            raise InvalidGitMutationError("OpenSSH effective config has an invalid line")
        parsed.setdefault(key.casefold(), []).append(value.strip())
    return {key: tuple(values) for key, values in parsed.items()}


def _require_effective_config(plan: SshExecutionPlan) -> None:
    config = probe_ssh_effective_config(plan)

    def one(key: str) -> str:
        values = config.get(key.casefold(), ())
        if len(values) != 1:
            raise InvalidGitMutationError(f"OpenSSH effective config {key} is not singular")
        return values[0]

    expected_scalar = {
        "hostname": plan.route.address,
        "user": plan.binding.endpoint.ssh_user or "",
        "port": str(plan.binding.endpoint.port),
        "hostkeyalias": plan.binding.host_key_pin.host_token,
        "batchmode": "yes",
        "passwordauthentication": "no",
        "kbdinteractiveauthentication": "no",
        "identitiesonly": "yes",
        "identityagent": "none",
        "stricthostkeychecking": "true",
        "verifyhostkeydns": "false",
        "canonicalizehostname": "false",
        "checkhostip": "no",
        "controlmaster": "false",
        "forwardagent": "no",
        "forwardx11": "no",
        "clearallforwardings": "yes",
        "permitlocalcommand": "no",
        "requesttty": "false",
        "preferredauthentications": "publickey",
    }
    for key, expected in expected_scalar.items():
        if one(key).casefold() != expected.casefold():
            raise InvalidGitMutationError(
                f"OpenSSH effective config {key} does not match the bound transport"
            )
    if len(config.get("identityfile", ())) != 1:
        raise InvalidGitMutationError("OpenSSH effective identity set is not singular")
    if len(config.get("certificatefile", ())) != 1:
        raise InvalidGitMutationError("OpenSSH effective certificate set is not singular")
    if len(config.get("userknownhostsfile", ())) != 1:
        raise InvalidGitMutationError("OpenSSH effective user host-key database is not singular")
    if len(config.get("globalknownhostsfile", ())) != 1:
        raise InvalidGitMutationError("OpenSSH effective global host-key database is not singular")
    for key in ("proxycommand", "proxyjump"):
        values = config.get(key, ())
        if values and (len(values) != 1 or values[0].casefold() != "none"):
            raise InvalidGitMutationError("OpenSSH effective routing inherited a proxy")


def build_isolated_ssh_execution_plan(
    snapshot: RepositorySnapshot,
    binding: SshTransportBinding,
) -> SshExecutionPlan:
    if not isinstance(snapshot, RepositorySnapshot):
        raise TypeError("snapshot must be RepositorySnapshot")
    if not isinstance(binding, SshTransportBinding):
        raise TypeError("binding must be SshTransportBinding")

    # Revalidate source bytes now, but do not copy secret material for preview-only
    # preparation. The copies are created only by materialize_ssh_execution_plan().
    _revalidate_source(binding.identity_file, label="SSH identity source")
    _revalidate_source(binding.host_key_pin.source_file, label="SSH host-key source")
    shell = resolve_git_command_shell(snapshot)
    route = _resolve_route(binding)

    root = Path(tempfile.mkdtemp(prefix="codexia-m251-ssh-"))
    try:
        resolved_root = root.resolve(strict=True)
        try:
            resolved_root.relative_to(snapshot.workspace_root)
        except ValueError:
            pass
        else:
            raise GitRepositoryBoundaryError("SSH execution bundle cannot live inside workspace")
        _validate_ssh_literal_path(resolved_root, label="SSH execution bundle")

        identity_path = resolved_root / "identity"
        known_hosts_path = resolved_root / "known_hosts"
        public_block = resolved_root / "identity.pub"
        cert_block = resolved_root / "identity-cert.pub"
        public_block.mkdir()
        cert_block.mkdir()
        options = _ssh_option_argv(
            binding,
            route,
            identity_path,
            known_hosts_path,
            cert_block.resolve(strict=True),
        )
        command = _quote_command((binding.ssh_executable.path, *options))
        plan = SshExecutionPlan(
            binding=binding,
            route=route,
            git_shell=shell,
            bundle_root=str(resolved_root),
            bundle_identity_path=str(identity_path),
            bundle_known_hosts_path=str(known_hosts_path),
            certificate_block_path=str(cert_block.resolve(strict=True)),
            ssh_command=command,
            ssh_command_sha256=sha256_bytes(command.encode("utf-8")),
        )
        _require_effective_config(plan)
        return plan
    except BaseException:
        shutil.rmtree(root, ignore_errors=True)
        raise


def materialize_ssh_execution_plan(plan: SshExecutionPlan) -> None:
    revalidate_ssh_execution_plan(plan, require_materialized=False)
    identity_payload = _source_bytes(plan.binding.identity_file, label="SSH identity source")
    host_source = plan.binding.host_key_pin.source_file
    known_hosts_payload = _source_bytes(host_source, label="SSH host-key source")
    _write_exact_new(
        Path(plan.bundle_identity_path),
        identity_payload,
        expected_size=plan.binding.identity_file.size_bytes,
        expected_sha256=plan.binding.identity_file.sha256,
        label="SSH bundle identity",
    )
    try:
        _write_exact_new(
            Path(plan.bundle_known_hosts_path),
            known_hosts_payload,
            expected_size=host_source.size_bytes,
            expected_sha256=host_source.sha256,
            label="SSH bundle known-hosts",
        )
    except BaseException:
        try:
            Path(plan.bundle_identity_path).unlink()
        except OSError:
            pass
        raise
    revalidate_ssh_execution_plan(plan, require_materialized=True)


def ssh_git_environment(plan: SshExecutionPlan) -> dict[str, str]:
    revalidate_ssh_execution_plan(plan, require_materialized=True)
    env = _ssh_base_environment()
    env["GIT_SSH_COMMAND"] = plan.ssh_command
    env["GIT_SSH_VARIANT"] = "ssh"
    env["GIT_TERMINAL_PROMPT"] = "0"
    if os.name == "nt":
        env["PATH"] = str(Path(plan.git_shell.path).parent)
    return env


def close_ssh_execution_plan(plan: SshExecutionPlan) -> None:
    if not isinstance(plan, SshExecutionPlan):
        raise TypeError("plan must be SshExecutionPlan")
    shutil.rmtree(plan.bundle_root, ignore_errors=True)
