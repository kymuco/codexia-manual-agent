from __future__ import annotations

import hashlib
import hmac
import os
import tempfile
import unittest
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from codexia_manual_agent.git_mutation.https_push import (
    _critical_transport_files,
    _transport_manifest,
)
from codexia_manual_agent.git_mutation.https_transport import (
    HttpsCredentialSource,
    HttpsFileIdentity,
    HttpsRouteBinding,
    bind_https_transport,
    close_https_transport,
    revalidate_https_transport,
)
from codexia_manual_agent.git_mutation.models import GitExecutableIdentity
from codexia_manual_agent.git_mutation.network_transport import parse_network_git_endpoint
from codexia_manual_agent.git_mutation.repository import RepositorySnapshot, resolve_git_helper
from codexia_manual_agent.git_mutation.ssh_execution import SshRouteBinding, _ssh_option_argv
from codexia_manual_agent.git_mutation.ssh_transport import (
    SshFileIdentity,
    SshHostKeyPin,
    SshTransportBinding,
)
from codexia_manual_agent.domain.errors import GitMutationPreconditionChangedError


def _identity(path: Path) -> GitExecutableIdentity:
    payload = path.read_bytes()
    return GitExecutableIdentity(
        path=str(path),
        size_bytes=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
    )


class PublicReadinessTransportRepairTests(unittest.TestCase):
    @unittest.skipIf(
        os.name == "nt",
        "symlink helper regression is exercised on POSIX Git packaging",
    )
    def test_git_helper_binding_preserves_requested_helper_alias_path(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            workspace = root / "workspace"
            exec_dir = root / "exec"
            workspace.mkdir()
            exec_dir.mkdir()
            git_executable = root / "git"
            git_executable.write_bytes(b"git")
            git_executable.chmod(0o755)
            target = exec_dir / "git-remote-http"
            target.write_bytes(b"helper")
            target.chmod(0o755)
            alias = exec_dir / "git-remote-https"
            alias.symlink_to(target.name)

            snapshot = RepositorySnapshot(
                workspace_root=workspace.resolve(),
                git_dir=(workspace / ".git"),
                git=_identity(git_executable.resolve()),
                object_format="sha1",
                oid_length=40,
                head_ref="refs/heads/main",
                head_oid="a" * 40,
            )
            fake_result = SimpleNamespace(
                stdout=(str(exec_dir) + "\n").encode(),
                returncode=0,
            )

            with mock.patch(
                "codexia_manual_agent.git_mutation.repository.run_git",
                return_value=fake_result,
            ):
                identity = resolve_git_helper(snapshot, "git-remote-https")

            self.assertEqual(Path(identity.path), alias.absolute())
            self.assertEqual(Path(identity.path).name, "git-remote-https")

    @unittest.skipIf(
        os.name == "nt",
        "symlink retarget regression is exercised on POSIX Git packaging",
    )
    def test_https_revalidation_rejects_same_bytes_helper_symlink_retarget(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            workspace = root / "workspace"
            exec_dir = root / "exec"
            trust = root / "trust"
            workspace.mkdir()
            exec_dir.mkdir()
            trust.mkdir()

            initial_target = exec_dir / "git-remote-http"
            initial_target.write_bytes(b"same-helper-bytes")
            initial_target.chmod(0o755)
            workspace_target = workspace / "workspace-helper"
            workspace_target.write_bytes(b"same-helper-bytes")
            workspace_target.chmod(0o755)
            alias = exec_dir / "git-remote-https"
            alias.symlink_to(initial_target)

            git_executable = root / "git"
            git_executable.write_bytes(b"git")
            git_executable.chmod(0o755)
            shell = root / "sh"
            shell.write_bytes(b"shell")
            shell.chmod(0o755)
            credential = trust / "credential"
            credential_payload = b"https://user:secret@example.com/team/repo.git\n"
            credential.write_bytes(credential_payload)
            ca = trust / "ca.pem"
            ca_payload = b"synthetic-ca\n"
            ca.write_bytes(ca_payload)

            key = b"k" * 32
            source = HttpsCredentialSource(
                path=str(credential.resolve()),
                size_bytes=len(credential_payload),
                username="user",
                secret_sha256=hashlib.sha256(credential_payload).hexdigest(),
                commitment_key=key,
                commitment_key_sha256=hashlib.sha256(key).hexdigest(),
                secret_hmac_sha256=hmac.new(key, credential_payload, sha256).hexdigest(),
            )
            ca_identity = HttpsFileIdentity(
                path=str(ca.resolve()),
                size_bytes=len(ca_payload),
                sha256=hashlib.sha256(ca_payload).hexdigest(),
            )
            snapshot = RepositorySnapshot(
                workspace_root=workspace.resolve(),
                git_dir=workspace / ".git",
                git=_identity(git_executable.resolve()),
                object_format="sha1",
                oid_length=40,
                head_ref="refs/heads/main",
                head_oid="a" * 40,
            )
            endpoint = parse_network_git_endpoint("https://example.com/team/repo.git")
            route = HttpsRouteBinding(
                address="203.0.113.41",
                family="ipv4",
                curl_resolve_entry="example.com:443:203.0.113.41",
            )

            with (
                mock.patch(
                    "codexia_manual_agent.git_mutation.https_transport._reject_local_http_credential_influence"
                ),
                mock.patch(
                    "codexia_manual_agent.git_mutation.https_transport._credential_source",
                    return_value=source,
                ),
                mock.patch(
                    "codexia_manual_agent.git_mutation.https_transport._bind_file",
                    return_value=(ca_identity, ca_payload),
                ),
                mock.patch(
                    "codexia_manual_agent.git_mutation.https_transport.resolve_git_command_shell",
                    return_value=_identity(shell.resolve()),
                ),
                mock.patch(
                    "codexia_manual_agent.git_mutation.https_transport._require_credential_shell_builtins"
                ),
                mock.patch(
                    "codexia_manual_agent.git_mutation.https_transport._resolve_route",
                    return_value=route,
                ),
                mock.patch(
                    "codexia_manual_agent.git_mutation.https_transport.resolve_git_helper",
                    return_value=_identity(alias.absolute()),
                ),
            ):
                binding = bind_https_transport(
                    snapshot,
                    endpoint,
                    remote_name="origin",
                    credential_file=credential,
                    ca_bundle_file=ca,
                )
            self.addCleanup(close_https_transport, binding)

            alias.unlink()
            alias.symlink_to(workspace_target)

            with self.assertRaisesRegex(
                GitMutationPreconditionChangedError,
                "git-remote-https.*target|target.*git-remote-https",
            ):
                revalidate_https_transport(binding, require_materialized=False)

    def test_https_transport_manifest_binds_resolved_helper_target(self) -> None:
        resolved_target = "/trusted/git-core/git-remote-http"
        binding = SimpleNamespace(
            endpoint=SimpleNamespace(to_dict=lambda: {"transport": "https"}),
            route=SimpleNamespace(to_dict=lambda: {"address": "203.0.113.41"}),
            git_shell=SimpleNamespace(to_dict=lambda: {"path": "/bin/sh"}),
            git_remote_https=SimpleNamespace(
                to_dict=lambda: {
                    "path": "/trusted/git-core/git-remote-https",
                    "size_bytes": 1,
                    "sha256": "0" * 64,
                }
            ),
            git_remote_https_resolved_target=resolved_target,
            credential_source=SimpleNamespace(
                to_public_dict=lambda: {"path": "/trusted/credential"}
            ),
            credential_helper_command_sha256="1" * 64,
            ca_bundle=SimpleNamespace(to_dict=lambda: {"path": "/trusted/ca.pem"}),
            tls_backend="openssl",
            credential_mode="frozen-shell-response.v1",
            route_mode="curlopt-resolve-exact.v1",
        )

        manifest = _transport_manifest(binding)

        self.assertEqual(
            manifest["git_remote_https_resolved_target"],
            resolved_target,
        )

    def test_https_execution_pins_lexical_and_resolved_helper_paths(self) -> None:
        lexical = Path("/trusted/git-core/git-remote-https")
        resolved = Path("/trusted/git-core/git-remote-http")
        git = Path("/trusted/bin/git")
        shell = Path("/trusted/bin/sh")
        credential = Path("/trusted/credential")
        ca = Path("/trusted/ca.pem")
        snapshot = SimpleNamespace(git=SimpleNamespace(path=str(git)))
        binding = SimpleNamespace(
            git_shell=SimpleNamespace(path=str(shell)),
            git_remote_https=SimpleNamespace(path=str(lexical)),
            git_remote_https_resolved_target=str(resolved),
            credential_source=SimpleNamespace(path=str(credential)),
            ca_bundle=SimpleNamespace(path=str(ca)),
        )

        critical = _critical_transport_files(snapshot, binding)

        self.assertIn(lexical, critical)
        self.assertIn(resolved, critical)

    def test_ssh_nondefault_port_uses_exact_bound_known_host_token_as_alias(self) -> None:
        endpoint = parse_network_git_endpoint("ssh://git@example.com:2222/team/repo.git")
        executable = GitExecutableIdentity(
            path="/usr/bin/ssh",
            size_bytes=1,
            sha256="0" * 64,
        )
        source = SshFileIdentity(
            path="/tmp/known_hosts",
            size_bytes=1,
            sha256="1" * 64,
        )
        binding = SshTransportBinding(
            endpoint=endpoint,
            ssh_executable=executable,
            identity_file=SshFileIdentity(
                path="/tmp/id",
                size_bytes=1,
                sha256="2" * 64,
            ),
            host_key_pin=SshHostKeyPin(
                host_token="[example.com]:2222",
                key_type="ssh-ed25519",
                fingerprint_sha256="SHA256:test",
                source_file=source,
            ),
        )

        argv = _ssh_option_argv(
            binding,
            SshRouteBinding(address="203.0.113.7", family="ipv4"),
            Path("/tmp/identity"),
            Path("/tmp/known_hosts"),
            Path("/tmp/cert-block"),
        )

        self.assertIn("HostKeyAlias=[example.com]:2222", argv)
        self.assertNotIn("HostKeyAlias=example.com", argv)


if __name__ == "__main__":
    unittest.main()
