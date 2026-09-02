from __future__ import annotations

import hmac
import os
import shutil
import socket
import subprocess
import tempfile
import unittest
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from unittest import mock

from codexia_manual_agent.domain.errors import GitMutationPreconditionChangedError
from codexia_manual_agent.git_mutation.https_transport import (
    bind_https_transport,
    close_https_transport,
    https_git_environment,
    materialize_https_credentials,
    revalidate_https_transport,
)
from codexia_manual_agent.git_mutation.network_transport import parse_network_git_endpoint
from codexia_manual_agent.git_mutation.repository import snapshot_repository


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
    _git(root, "config", "user.name", "Commitment Test")
    _git(root, "config", "user.email", "commitment@example.invalid")
    (root / "tracked.txt").write_bytes(b"base\n")
    _git(root, "add", "tracked.txt")
    _git(root, "commit", "--no-gpg-sign", "-m", "base")
    _git(root, "config", "remote.origin.url", "https://example.com/team/repo.git")


def _binding(root: Path, trust: Path):
    credential = trust / "credentials"
    credential.write_text(
        "https://codexia-user:exact-secret@example.com/team/repo.git\n",
        encoding="utf-8",
    )
    ca = trust / "ca.pem"
    ca.write_bytes(b"synthetic-ca\n")
    snapshot = snapshot_repository(root)
    with mock.patch(
        "codexia_manual_agent.git_mutation.https_transport.socket.getaddrinfo",
        return_value=[
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("203.0.113.41", 443),
            )
        ],
    ):
        binding = bind_https_transport(
            snapshot,
            parse_network_git_endpoint("https://example.com/team/repo.git"),
            remote_name="origin",
            credential_file=credential.resolve(),
            ca_bundle_file=ca.resolve(),
        )
    return snapshot, binding, credential


@unittest.skipUnless(shutil.which("git"), "Git is required")
class HttpsCredentialCommitmentTests(unittest.TestCase):
    def test_public_manifest_binds_secret_with_keyed_commitment_without_serializing_key_or_plain_digest(self) -> None:
        with tempfile.TemporaryDirectory() as raw, tempfile.TemporaryDirectory() as trust_raw:
            root, trust = Path(raw), Path(trust_raw)
            _init_repo(root)
            _snapshot, binding, credential = _binding(root, trust)
            self.addCleanup(close_https_transport, binding)

            source = binding.credential_source
            payload = credential.read_bytes()
            public = source.to_public_dict()
            rendered = repr(binding.to_dict())

            self.assertEqual(len(source.commitment_key), 32)
            self.assertEqual(public["commitment_key_sha256"], sha256(source.commitment_key).hexdigest())
            self.assertEqual(
                public["secret_hmac_sha256"],
                hmac.new(source.commitment_key, payload, sha256).hexdigest(),
            )
            self.assertNotIn("exact-secret", rendered)
            self.assertNotIn(source.secret_sha256, rendered)
            self.assertNotIn(source.commitment_key.hex(), rendered)
            self.assertNotIn("commitment_key'", rendered)

    def test_forged_private_commitment_key_or_secret_hmac_invalidates_preparation(self) -> None:
        with tempfile.TemporaryDirectory() as raw, tempfile.TemporaryDirectory() as trust_raw:
            root, trust = Path(raw), Path(trust_raw)
            _init_repo(root)
            _snapshot, binding, _credential = _binding(root, trust)
            self.addCleanup(close_https_transport, binding)

            forged_key = replace(
                binding.credential_source,
                commitment_key=b"x" * 32,
            )
            with self.assertRaisesRegex(
                GitMutationPreconditionChangedError,
                "commitment key",
            ):
                revalidate_https_transport(replace(binding, credential_source=forged_key))

            forged_hmac = replace(
                binding.credential_source,
                secret_hmac_sha256="0" * 64,
            )
            with self.assertRaisesRegex(
                GitMutationPreconditionChangedError,
                "commitment changed",
            ):
                revalidate_https_transport(replace(binding, credential_source=forged_hmac))

    def test_execution_environment_forces_exact_bound_git_exec_path(self) -> None:
        with tempfile.TemporaryDirectory() as raw, tempfile.TemporaryDirectory() as trust_raw:
            root, trust = Path(raw), Path(trust_raw)
            _init_repo(root)
            snapshot, binding, _credential = _binding(root, trust)
            self.addCleanup(close_https_transport, binding)
            materialize_https_credentials(binding)

            env = https_git_environment(snapshot, binding)
            self.assertEqual(
                env["GIT_EXEC_PATH"],
                str(Path(binding.git_remote_https.path).parent),
            )
            self.assertEqual(
                Path(env["GIT_EXEC_PATH"]).resolve(),
                Path(binding.git_remote_https.path).parent.resolve(),
            )


if __name__ == "__main__":
    unittest.main()
