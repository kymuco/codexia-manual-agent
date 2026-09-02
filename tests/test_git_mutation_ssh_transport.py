from __future__ import annotations

import base64
import hashlib
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from codexia_manual_agent.domain.errors import GitRepositoryBoundaryError, InvalidGitMutationError
from codexia_manual_agent.git_mutation.network_transport import parse_network_git_endpoint
from codexia_manual_agent.git_mutation.repository import snapshot_repository
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


def _ed25519_blob(seed: int = 17) -> bytes:
    return _ssh_string(b"ssh-ed25519") + _ssh_string(bytes([seed]) * 32)


def _host_key_line(host_token: str, *, blob: bytes | None = None, key_type: str = "ssh-ed25519") -> bytes:
    payload = _ed25519_blob() if blob is None else blob
    encoded = base64.b64encode(payload).decode("ascii")
    return f"{host_token} {key_type} {encoded}\n".encode("utf-8")


@unittest.skipUnless(shutil.which("git") and shutil.which("ssh"), "Git and OpenSSH executables are required")
class GitSshTransportBindingTests(unittest.TestCase):
    def _fixture(self):
        return tempfile.TemporaryDirectory(), tempfile.TemporaryDirectory()

    def test_binds_exact_identity_ssh_executable_and_single_host_key_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as raw, tempfile.TemporaryDirectory() as trust_raw:
            root, trust = Path(raw), Path(trust_raw)
            _init_repo(root)
            identity = trust / "id_ed25519"
            identity.write_bytes(b"synthetic-private-key-material\n")
            host_key = trust / "known_host"
            blob = _ed25519_blob(23)
            host_key.write_bytes(_host_key_line("example.com", blob=blob))

            binding = bind_ssh_transport(
                snapshot_repository(root),
                parse_network_git_endpoint("git@example.com:team/repo.git"),
                identity_file=identity.resolve(),
                host_key_file=host_key.resolve(),
            )

            expected_fp = base64.b64encode(hashlib.sha256(blob).digest()).decode("ascii").rstrip("=")
            self.assertEqual(binding.identity_file.path, str(identity.resolve()))
            self.assertEqual(binding.identity_file.sha256, hashlib.sha256(identity.read_bytes()).hexdigest())
            self.assertTrue(Path(binding.ssh_executable.path).is_absolute())
            self.assertEqual(binding.host_key_pin.host_token, "example.com")
            self.assertEqual(binding.host_key_pin.key_type, "ssh-ed25519")
            self.assertEqual(binding.host_key_pin.fingerprint_sha256, f"SHA256:{expected_fp}")
            self.assertEqual(binding.credential_mode, "explicit-identity-file.v1")
            self.assertEqual(binding.host_key_mode, "single-exact-known-host.v1")

    def test_nondefault_port_requires_exact_bracketed_known_hosts_token(self) -> None:
        with tempfile.TemporaryDirectory() as raw, tempfile.TemporaryDirectory() as trust_raw:
            root, trust = Path(raw), Path(trust_raw)
            _init_repo(root)
            identity = trust / "id"
            identity.write_bytes(b"identity\n")
            endpoint = parse_network_git_endpoint("ssh://git@example.com:2222/team/repo.git")

            wrong = trust / "wrong_host"
            wrong.write_bytes(_host_key_line("example.com"))
            with self.assertRaisesRegex(InvalidGitMutationError, "exact endpoint"):
                bind_ssh_transport(
                    snapshot_repository(root),
                    endpoint,
                    identity_file=identity.resolve(),
                    host_key_file=wrong.resolve(),
                )

            exact = trust / "exact_host"
            exact.write_bytes(_host_key_line("[example.com]:2222"))
            binding = bind_ssh_transport(
                snapshot_repository(root),
                endpoint,
                identity_file=identity.resolve(),
                host_key_file=exact.resolve(),
            )
            self.assertEqual(binding.host_key_pin.host_token, "[example.com]:2222")

    def test_host_key_file_rejects_wildcards_hashes_markers_and_multiple_active_keys(self) -> None:
        bad_payloads = (
            _host_key_line("*.example.com"),
            _host_key_line("|1|hashed|host"),
            b"@cert-authority " + _host_key_line("example.com"),
            _host_key_line("example.com") + _host_key_line("example.com", blob=_ed25519_blob(31)),
        )
        for payload in bad_payloads:
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as raw, tempfile.TemporaryDirectory() as trust_raw:
                root, trust = Path(raw), Path(trust_raw)
                _init_repo(root)
                identity = trust / "id"
                identity.write_bytes(b"identity\n")
                host_key = trust / "host"
                host_key.write_bytes(payload)
                with self.assertRaises(InvalidGitMutationError):
                    bind_ssh_transport(
                        snapshot_repository(root),
                        parse_network_git_endpoint("git@example.com:team/repo.git"),
                        identity_file=identity.resolve(),
                        host_key_file=host_key.resolve(),
                    )

    def test_declared_host_key_type_must_match_embedded_blob_type(self) -> None:
        with tempfile.TemporaryDirectory() as raw, tempfile.TemporaryDirectory() as trust_raw:
            root, trust = Path(raw), Path(trust_raw)
            _init_repo(root)
            identity = trust / "id"
            identity.write_bytes(b"identity\n")
            host_key = trust / "host"
            host_key.write_bytes(_host_key_line("example.com", key_type="ssh-rsa"))
            with self.assertRaisesRegex(InvalidGitMutationError, "does not match"):
                bind_ssh_transport(
                    snapshot_repository(root),
                    parse_network_git_endpoint("git@example.com:team/repo.git"),
                    identity_file=identity.resolve(),
                    host_key_file=host_key.resolve(),
                )

    def test_implicit_identity_certificate_sibling_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw, tempfile.TemporaryDirectory() as trust_raw:
            root, trust = Path(raw), Path(trust_raw)
            _init_repo(root)
            identity = trust / "id_ed25519"
            identity.write_bytes(b"identity\n")
            Path(f"{identity}-cert.pub").write_bytes(b"unbound-certificate\n")
            host_key = trust / "host"
            host_key.write_bytes(_host_key_line("example.com"))
            with self.assertRaisesRegex(InvalidGitMutationError, "cert.pub"):
                bind_ssh_transport(
                    snapshot_repository(root),
                    parse_network_git_endpoint("git@example.com:team/repo.git"),
                    identity_file=identity.resolve(),
                    host_key_file=host_key.resolve(),
                )

    def test_identity_and_host_key_paths_must_be_absolute_and_outside_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as raw, tempfile.TemporaryDirectory() as trust_raw:
            root, trust = Path(raw), Path(trust_raw)
            _init_repo(root)
            endpoint = parse_network_git_endpoint("git@example.com:team/repo.git")
            external_identity = trust / "id"
            external_identity.write_bytes(b"identity\n")
            external_host = trust / "host"
            external_host.write_bytes(_host_key_line("example.com"))

            with self.assertRaisesRegex(InvalidGitMutationError, "absolute path"):
                bind_ssh_transport(
                    snapshot_repository(root),
                    endpoint,
                    identity_file=Path("relative-id"),
                    host_key_file=external_host.resolve(),
                )

            internal_identity = root / "identity"
            internal_identity.write_bytes(b"identity\n")
            with self.assertRaises(GitRepositoryBoundaryError):
                bind_ssh_transport(
                    snapshot_repository(root),
                    endpoint,
                    identity_file=internal_identity.resolve(),
                    host_key_file=external_host.resolve(),
                )

            internal_host = root / "host-key"
            internal_host.write_bytes(_host_key_line("example.com"))
            with self.assertRaises(GitRepositoryBoundaryError):
                bind_ssh_transport(
                    snapshot_repository(root),
                    endpoint,
                    identity_file=external_identity.resolve(),
                    host_key_file=internal_host.resolve(),
                )


if __name__ == "__main__":
    unittest.main()
