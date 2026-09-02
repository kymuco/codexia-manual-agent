from __future__ import annotations

import base64
import os
import shutil
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codexia_manual_agent.domain.errors import GitMutationPreconditionChangedError
from codexia_manual_agent.git_mutation.network_transport import parse_network_git_endpoint
from codexia_manual_agent.git_mutation.repository import snapshot_repository
from codexia_manual_agent.git_mutation.ssh_execution import (
    build_isolated_ssh_execution_plan,
    close_ssh_execution_plan,
    materialize_ssh_execution_plan,
    probe_ssh_effective_config,
    revalidate_ssh_execution_plan,
    ssh_git_environment,
)
from codexia_manual_agent.git_mutation.ssh_transport import bind_ssh_transport


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        check=False,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr.decode("utf-8", errors="replace"))
    return result


def _init_repo(root: Path) -> None:
    _git(root, "init")
    _git(root, "symbolic-ref", "HEAD", "refs/heads/main")
    _git(root, "config", "user.name", "Codexia Test")
    _git(root, "config", "user.email", "codexia@example.invalid")
    (root / "tracked.txt").write_bytes(b"base\n")
    _git(root, "add", "tracked.txt")
    _git(root, "commit", "--no-gpg-sign", "-m", "base")


def _ssh_string(payload: bytes) -> bytes:
    return len(payload).to_bytes(4, "big") + payload


def _host_key_line(host: str) -> bytes:
    blob = _ssh_string(b"ssh-ed25519") + _ssh_string(b"x" * 32)
    return f"{host} ssh-ed25519 {base64.b64encode(blob).decode('ascii')}\n".encode("ascii")


def _binding(root: Path, trust: Path):
    identity = trust / "source_identity"
    identity.write_bytes(b"synthetic-private-key-material\n")
    known_hosts = trust / "source_known_hosts"
    known_hosts.write_bytes(_host_key_line("example.com"))
    snapshot = snapshot_repository(root)
    binding = bind_ssh_transport(
        snapshot,
        parse_network_git_endpoint("git@example.com:team/repo.git"),
        identity_file=identity.resolve(),
        host_key_file=known_hosts.resolve(),
    )
    return snapshot, binding, identity, known_hosts


def _plan(snapshot, binding):
    records = [
        (
            socket.AF_INET,
            socket.SOCK_STREAM,
            socket.IPPROTO_TCP,
            "",
            ("203.0.113.17", 22),
        )
    ]
    with mock.patch(
        "codexia_manual_agent.git_mutation.ssh_execution.socket.getaddrinfo",
        return_value=records,
    ):
        return build_isolated_ssh_execution_plan(snapshot, binding)


@unittest.skipUnless(shutil.which("git") and shutil.which("ssh"), "Git and OpenSSH are required")
class GitSshExecutionPlanTests(unittest.TestCase):
    def test_preview_plan_has_no_secret_copy_then_materializes_exact_private_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as raw, tempfile.TemporaryDirectory() as trust_raw:
            root, trust = Path(raw), Path(trust_raw)
            _init_repo(root)
            snapshot, binding, source_identity, source_known_hosts = _binding(root, trust)
            plan = _plan(snapshot, binding)
            self.addCleanup(close_ssh_execution_plan, plan)

            bundle_root = Path(plan.bundle_root)
            public_block = Path(f"{plan.bundle_identity_path}.pub")
            self.assertNotEqual(bundle_root, trust)
            self.assertTrue(public_block.is_dir())
            self.assertTrue(Path(plan.certificate_block_path).is_dir())
            self.assertFalse(Path(plan.bundle_identity_path).exists())
            self.assertFalse(Path(plan.bundle_known_hosts_path).exists())
            self.assertEqual(
                {entry.name for entry in bundle_root.iterdir()},
                {"identity.pub", "identity-cert.pub"},
            )
            self.assertNotIn(str(source_identity), plan.ssh_command)
            self.assertNotIn(str(source_known_hosts), plan.ssh_command)
            self.assertEqual(plan.route.address, "203.0.113.17")
            self.assertEqual(plan.route.family, "ipv4")
            revalidate_ssh_execution_plan(plan, require_materialized=False)

            materialize_ssh_execution_plan(plan)
            self.assertEqual(Path(plan.bundle_identity_path).read_bytes(), source_identity.read_bytes())
            self.assertEqual(Path(plan.bundle_known_hosts_path).read_bytes(), source_known_hosts.read_bytes())
            self.assertEqual(
                {entry.name for entry in bundle_root.iterdir()},
                {"identity", "known_hosts", "identity.pub", "identity-cert.pub"},
            )
            revalidate_ssh_execution_plan(plan, require_materialized=True)

    def test_generated_command_disables_ambient_config_agent_proxy_prompt_and_multiplexing(self) -> None:
        with tempfile.TemporaryDirectory() as raw, tempfile.TemporaryDirectory() as trust_raw:
            root, trust = Path(raw), Path(trust_raw)
            _init_repo(root)
            snapshot, binding, _, _ = _binding(root, trust)
            plan = _plan(snapshot, binding)
            self.addCleanup(close_ssh_execution_plan, plan)

            command = plan.ssh_command
            required = (
                "-F none",
                "-l git",
                "-p 22",
                "BatchMode=yes",
                "PasswordAuthentication=no",
                "KbdInteractiveAuthentication=no",
                "PreferredAuthentications=publickey",
                "PubkeyAcceptedAlgorithms=-sk-*,-webauthn-sk-*",
                "IdentitiesOnly=yes",
                "IdentityAgent=none",
                f"CertificateFile={plan.certificate_block_path}",
                "HostName=203.0.113.17",
                "HostKeyAlias=example.com",
                "StrictHostKeyChecking=yes",
                "UpdateHostKeys=no",
                "VerifyHostKeyDNS=no",
                "CanonicalizeHostname=no",
                "ProxyCommand=none",
                "ProxyJump=none",
                "ControlMaster=no",
                "ControlPath=none",
                "ControlPersist=no",
                "ForwardAgent=no",
                "ForwardX11=no",
                "ClearAllForwardings=yes",
                "PermitLocalCommand=no",
            )
            for value in required:
                with self.subTest(value=value):
                    self.assertIn(value, command)
            known_hosts = str(Path(plan.bundle_known_hosts_path))
            self.assertIn(f"UserKnownHostsFile={known_hosts}", command)
            self.assertIn(f"GlobalKnownHostsFile={known_hosts}", command)
            self.assertNotIn("SecurityKeyProvider=", command)
            self.assertNotIn("PKCS11Provider=", command)

    def test_real_ssh_g_reports_bound_effective_route_auth_and_trust_policy_before_secret_copy(self) -> None:
        with tempfile.TemporaryDirectory() as raw, tempfile.TemporaryDirectory() as trust_raw:
            root, trust = Path(raw), Path(trust_raw)
            _init_repo(root)
            snapshot, binding, _, _ = _binding(root, trust)
            plan = _plan(snapshot, binding)
            self.addCleanup(close_ssh_execution_plan, plan)

            self.assertFalse(Path(plan.bundle_identity_path).exists())
            config = probe_ssh_effective_config(plan)
            self.assertEqual(config["hostname"], ("203.0.113.17",))
            self.assertEqual(config["user"], ("git",))
            self.assertEqual(config["port"], ("22",))
            self.assertEqual(config["hostkeyalias"], ("example.com",))
            self.assertEqual(config["identityagent"], ("none",))
            self.assertEqual(config["identitiesonly"], ("yes",))
            self.assertEqual(config["stricthostkeychecking"], ("true",))
            self.assertEqual(config["preferredauthentications"], ("publickey",))
            self.assertEqual(len(config["identityfile"]), 1)
            self.assertEqual(len(config["certificatefile"]), 1)
            self.assertEqual(len(config["userknownhostsfile"]), 1)
            self.assertEqual(len(config["globalknownhostsfile"]), 1)

    def test_environment_requires_materialization_and_removes_shell_startup_and_all_ssh_prefixes(self) -> None:
        with tempfile.TemporaryDirectory() as raw, tempfile.TemporaryDirectory() as trust_raw:
            root, trust = Path(raw), Path(trust_raw)
            _init_repo(root)
            snapshot, binding, _, _ = _binding(root, trust)
            plan = _plan(snapshot, binding)
            self.addCleanup(close_ssh_execution_plan, plan)

            with self.assertRaises(GitMutationPreconditionChangedError):
                ssh_git_environment(plan)
            materialize_ssh_execution_plan(plan)
            poison = {
                "BASH_ENV": "/tmp/attacker-bash-env",
                "ENV": "/tmp/attacker-env",
                "SHELLOPTS": "xtrace",
                "CDPATH": "/tmp/attacker-cdpath",
                "IFS": "x",
                "SSH_AUTH_SOCK": "/tmp/attacker-agent",
                "SSH_AGENT_PID": "123",
                "SSH_ASKPASS": "/tmp/attacker-askpass",
                "SSH_ASKPASS_REQUIRE": "force",
                "SSH_SK_PROVIDER": "/tmp/attacker-provider",
                "SSH_SOMETHING_FUTURE": "attacker",
            }
            with mock.patch.dict(os.environ, poison, clear=False):
                env = ssh_git_environment(plan)

            for key in poison:
                with self.subTest(key=key):
                    self.assertNotIn(key, env)
            self.assertEqual(env["GIT_SSH_COMMAND"], plan.ssh_command)
            self.assertEqual(env["GIT_SSH_VARIANT"], "ssh")
            self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")
            if os.name == "nt":
                self.assertEqual(env["PATH"], str(Path(plan.git_shell.path).parent))

    def test_bundle_tamper_or_new_entry_invalidates_materialized_plan(self) -> None:
        with tempfile.TemporaryDirectory() as raw, tempfile.TemporaryDirectory() as trust_raw:
            root, trust = Path(raw), Path(trust_raw)
            _init_repo(root)
            snapshot, binding, _, _ = _binding(root, trust)
            plan = _plan(snapshot, binding)
            self.addCleanup(close_ssh_execution_plan, plan)
            materialize_ssh_execution_plan(plan)

            identity = Path(plan.bundle_identity_path)
            identity.write_bytes(identity.read_bytes() + b"tamper")
            with self.assertRaisesRegex(GitMutationPreconditionChangedError, "bundle identity"):
                revalidate_ssh_execution_plan(plan, require_materialized=True)

            close_ssh_execution_plan(plan)
            plan = _plan(snapshot, binding)
            self.addCleanup(close_ssh_execution_plan, plan)
            (Path(plan.bundle_root) / "unexpected").write_bytes(b"x")
            with self.assertRaisesRegex(GitMutationPreconditionChangedError, "unbound entries"):
                revalidate_ssh_execution_plan(plan, require_materialized=False)

    def test_public_and_certificate_blockers_must_remain_directories(self) -> None:
        with tempfile.TemporaryDirectory() as raw, tempfile.TemporaryDirectory() as trust_raw:
            root, trust = Path(raw), Path(trust_raw)
            _init_repo(root)
            snapshot, binding, _, _ = _binding(root, trust)

            for suffix, payload in (
                (".pub", b"public-key-race"),
                ("-cert.pub", b"certificate-race"),
            ):
                with self.subTest(suffix=suffix):
                    plan = _plan(snapshot, binding)
                    self.addCleanup(close_ssh_execution_plan, plan)
                    blocker = Path(f"{plan.bundle_identity_path}{suffix}")
                    blocker.rmdir()
                    blocker.write_bytes(payload)
                    with self.assertRaisesRegex(
                        GitMutationPreconditionChangedError,
                        "namespace changed",
                    ):
                        revalidate_ssh_execution_plan(plan, require_materialized=False)

    def test_literal_ip_endpoint_needs_no_dns_and_is_bound_as_route(self) -> None:
        with tempfile.TemporaryDirectory() as raw, tempfile.TemporaryDirectory() as trust_raw:
            root, trust = Path(raw), Path(trust_raw)
            _init_repo(root)
            identity = trust / "identity"
            identity.write_bytes(b"identity\n")
            known_hosts = trust / "known_hosts"
            known_hosts.write_bytes(_host_key_line("203.0.113.99"))
            snapshot = snapshot_repository(root)
            binding = bind_ssh_transport(
                snapshot,
                parse_network_git_endpoint("git@203.0.113.99:team/repo.git"),
                identity_file=identity.resolve(),
                host_key_file=known_hosts.resolve(),
            )
            with mock.patch(
                "codexia_manual_agent.git_mutation.ssh_execution.socket.getaddrinfo"
            ) as resolver:
                plan = build_isolated_ssh_execution_plan(snapshot, binding)
            self.addCleanup(close_ssh_execution_plan, plan)
            resolver.assert_not_called()
            self.assertEqual(plan.route.address, "203.0.113.99")
            self.assertEqual(plan.route.family, "ipv4")


if __name__ == "__main__":
    unittest.main()
