from __future__ import annotations

import os
import shutil
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codexia_manual_agent.domain.errors import (
    GitMutationPreconditionChangedError,
    InvalidGitMutationError,
)
from codexia_manual_agent.git_mutation.https_transport import (
    bind_https_transport,
    close_https_transport,
    https_git_config_args,
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


def _git_input(
    root: Path,
    args: list[str],
    payload: bytes,
    *,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        input=payload,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        check=False,
        env=env or {**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    if check and result.returncode != 0:
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
    _git(root, "config", "remote.origin.url", "https://example.com/team/repo.git")


def _binding(root: Path, trust: Path):
    ca = trust / "ca-bundle.pem"
    ca.write_text(
        "-----BEGIN CERTIFICATE-----\nsynthetic\n-----END CERTIFICATE-----\n",
        encoding="ascii",
    )
    credential = trust / "credentials"
    credential.write_text(
        "https://codexia-user:exact-secret@example.com/team/repo.git\n",
        encoding="utf-8",
    )
    snapshot = snapshot_repository(root)
    records = [
        (
            socket.AF_INET,
            socket.SOCK_STREAM,
            socket.IPPROTO_TCP,
            "",
            ("203.0.113.41", 443),
        )
    ]
    with mock.patch(
        "codexia_manual_agent.git_mutation.https_transport.socket.getaddrinfo",
        return_value=records,
    ):
        binding = bind_https_transport(
            snapshot,
            parse_network_git_endpoint("https://example.com/team/repo.git"),
            remote_name="origin",
            credential_file=credential.resolve(),
            ca_bundle_file=ca.resolve(),
        )
    return snapshot, binding, ca, credential


@unittest.skipUnless(shutil.which("git"), "Git is required")
class GitHttpsTransportBindingTests(unittest.TestCase):
    def test_binding_binds_route_shell_response_credential_source_and_ca_without_secret(self) -> None:
        with tempfile.TemporaryDirectory() as raw, tempfile.TemporaryDirectory() as trust_raw:
            root, trust = Path(raw), Path(trust_raw)
            _init_repo(root)
            _snapshot, binding, ca, credential = _binding(root, trust)
            self.addCleanup(close_https_transport, binding)

            self.assertEqual(binding.route.address, "203.0.113.41")
            self.assertEqual(binding.route.family, "ipv4")
            self.assertEqual(
                binding.route.curl_resolve_entry,
                "example.com:443:203.0.113.41",
            )
            self.assertEqual(
                binding.endpoint.review_destination,
                "https://example.com:443/team/repo.git",
            )
            self.assertEqual(Path(binding.credential_source.path), credential.resolve())
            self.assertEqual(binding.credential_source.username, "codexia-user")
            self.assertEqual(Path(binding.ca_bundle.path), ca.resolve())
            self.assertIn("git-remote-https", Path(binding.git_remote_https.path).name)
            self.assertEqual(binding.credential_mode, "frozen-shell-response.v1")
            self.assertTrue(binding.git_shell.sha256)
            self.assertEqual(list(Path(binding.credential_bundle_root).iterdir()), [])
            revalidate_https_transport(binding, require_materialized=False)

            public = binding.to_dict()
            rendered_public = repr(public)
            self.assertNotIn("exact-secret", rendered_public)
            self.assertNotIn(binding.credential_source.secret_sha256, rendered_public)
            self.assertNotIn("credential_store_helper", public)

    def test_materialized_config_uses_exact_ca_route_and_shell_only_read_only_helper(self) -> None:
        with tempfile.TemporaryDirectory() as raw, tempfile.TemporaryDirectory() as trust_raw:
            root, trust = Path(raw), Path(trust_raw)
            _init_repo(root)
            _snapshot, binding, _ca, _credential = _binding(root, trust)
            self.addCleanup(close_https_transport, binding)
            materialize_https_credentials(binding)
            revalidate_https_transport(binding, require_materialized=True)

            self.assertEqual(
                Path(binding.credential_bundle_path).read_bytes(),
                b"username=codexia-user\npassword=exact-secret\n",
            )
            command = binding.credential_helper_shell_command
            self.assertIn("get)", command)
            self.assertIn("store|erase) exit 0", command)
            self.assertIn("read -r line", command)
            self.assertIn("printf", command)
            self.assertNotIn("credential-store", command)
            self.assertNotIn("git.exe", command.casefold())

            args = https_git_config_args(binding)
            rendered = "\n".join(args)
            self.assertFalse(any(arg.startswith("http.sslBackend=") for arg in args))
            self.assertNotIn("http.pinnedPubkey=", args)
            for required in (
                "http.proxy=",
                "http.sslVerify=true",
                f"http.sslCAInfo={binding.ca_bundle.path}",
                "http.sslCAPath=",
                "http.schannelUseSSLCAInfo=true",
                "http.followRedirects=false",
                "http.extraHeader=",
                "http.cookieFile=",
                "http.saveCookies=false",
                "http.curloptResolve=",
                "http.curloptResolve=example.com:443:203.0.113.41",
                "http.proactiveAuth=none",
                "http.emptyAuth=false",
                "http.delegation=none",
                "credential.helper=",
                "credential.useHttpPath=true",
                "credential.interactive=false",
                "credential.guiPrompt=false",
            ):
                with self.subTest(required=required):
                    self.assertIn(required, rendered)

    def test_real_git_credential_protocol_reads_exact_secret_but_store_and_erase_are_noops(self) -> None:
        with tempfile.TemporaryDirectory() as raw, tempfile.TemporaryDirectory() as trust_raw:
            root, trust = Path(raw), Path(trust_raw)
            _init_repo(root)
            snapshot, binding, _ca, _credential = _binding(root, trust)
            self.addCleanup(close_https_transport, binding)
            materialize_https_credentials(binding)
            before = Path(binding.credential_bundle_path).read_bytes()
            config = [
                "-c",
                "credential.helper=",
                "-c",
                f"credential.helper={binding.credential_helper_shell_command}",
                "-c",
                "credential.useHttpPath=true",
                "-c",
                "credential.interactive=false",
            ]
            query = b"protocol=https\nhost=example.com\npath=team/repo.git\n\n"
            filled = _git_input(
                root,
                [*config, "credential", "fill"],
                query,
                env=https_git_environment(snapshot, binding),
            )
            output = filled.stdout.decode("utf-8")
            self.assertIn("username=codexia-user", output)
            self.assertIn("password=exact-secret", output)

            replacement = (
                b"protocol=https\nhost=example.com\npath=team/repo.git\n"
                b"username=codexia-user\npassword=replacement-secret\n\n"
            )
            _git_input(
                root,
                [*config, "credential", "approve"],
                replacement,
                env=https_git_environment(snapshot, binding),
            )
            self.assertEqual(Path(binding.credential_bundle_path).read_bytes(), before)
            _git_input(
                root,
                [*config, "credential", "reject"],
                replacement,
                env=https_git_environment(snapshot, binding),
            )
            self.assertEqual(Path(binding.credential_bundle_path).read_bytes(), before)

    def test_local_http_or_credential_config_is_rejected_before_transport_binding(self) -> None:
        with tempfile.TemporaryDirectory() as raw, tempfile.TemporaryDirectory() as trust_raw:
            root, trust = Path(raw), Path(trust_raw)
            _init_repo(root)
            ca = trust / "ca.pem"
            ca.write_bytes(b"ca\n")
            credential = trust / "credentials"
            credential.write_text(
                "https://user:secret@example.com/team/repo.git\n",
                encoding="utf-8",
            )
            endpoint = parse_network_git_endpoint("https://example.com/team/repo.git")
            for key, value in (
                ("http.sslVerify", "false"),
                ("http.proxy", "http://proxy.invalid"),
                ("http.sslCert", "/tmp/client.pem"),
                ("http.pinnedPubkey", "sha256//attacker"),
                ("credential.helper", "!echo attacker"),
                ("credential.useHttpPath", "false"),
                ("remote.origin.proxy", "http://proxy.invalid"),
            ):
                with self.subTest(key=key):
                    _git(root, "config", key, value)
                    with self.assertRaises(InvalidGitMutationError):
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
                            bind_https_transport(
                                snapshot_repository(root),
                                endpoint,
                                remote_name="origin",
                                credential_file=credential.resolve(),
                                ca_bundle_file=ca.resolve(),
                            )
                    _git(root, "config", "--unset-all", key)

    def test_worktree_http_config_cannot_bypass_transport_binding_policy(self) -> None:
        with tempfile.TemporaryDirectory() as raw, tempfile.TemporaryDirectory() as trust_raw:
            root, trust = Path(raw), Path(trust_raw)
            _init_repo(root)
            _git(root, "config", "extensions.worktreeConfig", "true")
            _git(root, "config", "--worktree", "http.sslVerify", "false")

            with self.assertRaisesRegex(InvalidGitMutationError, "worktree"):
                _binding(root, trust)

    def test_local_include_config_cannot_bypass_transport_binding_policy(self) -> None:
        with tempfile.TemporaryDirectory() as raw, tempfile.TemporaryDirectory() as trust_raw:
            root, trust = Path(raw), Path(trust_raw)
            _init_repo(root)
            included = trust / "included.gitconfig"
            included.write_text("[http]\n\tsslVerify = false\n", encoding="utf-8")
            _git(root, "config", "include.path", str(included))
            self.assertEqual(
                _git(root, "config", "--includes", "--get", "http.sslVerify").stdout.strip(),
                b"false",
            )

            with self.assertRaisesRegex(InvalidGitMutationError, "include"):
                _binding(root, trust)

    def test_local_includeif_config_semantics_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw, tempfile.TemporaryDirectory() as trust_raw:
            root, trust = Path(raw), Path(trust_raw)
            _init_repo(root)
            included = trust / "conditional.gitconfig"
            included.write_text("[http]\n\tsslBackend = attacker\n", encoding="utf-8")
            _git(root, "config", "includeIf.gitdir:/never-match/.path", str(included))

            with self.assertRaisesRegex(InvalidGitMutationError, "include"):
                _binding(root, trust)

    def test_credential_source_must_bind_exact_host_port_and_repository_path(self) -> None:
        with tempfile.TemporaryDirectory() as raw, tempfile.TemporaryDirectory() as trust_raw:
            root, trust = Path(raw), Path(trust_raw)
            _init_repo(root)
            ca = trust / "ca.pem"
            ca.write_bytes(b"ca\n")
            endpoint = parse_network_git_endpoint("https://example.com/team/repo.git")
            for line in (
                "https://user:secret@other.example/team/repo.git\n",
                "https://user:secret@example.com/other/repo.git\n",
                "https://user:secret@example.com:444/team/repo.git\n",
                "https://example.com/team/repo.git\n",
            ):
                with self.subTest(line=line):
                    credential = trust / "credential"
                    credential.write_text(line, encoding="utf-8")
                    with self.assertRaises(InvalidGitMutationError):
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
                            bind_https_transport(
                                snapshot_repository(root),
                                endpoint,
                                remote_name="origin",
                                credential_file=credential.resolve(),
                                ca_bundle_file=ca.resolve(),
                            )

    def test_environment_removes_proxy_tls_curl_openssl_http_trace_and_gcm_inheritance(self) -> None:
        with tempfile.TemporaryDirectory() as raw, tempfile.TemporaryDirectory() as trust_raw:
            root, trust = Path(raw), Path(trust_raw)
            _init_repo(root)
            snapshot, binding, _ca, _credential = _binding(root, trust)
            self.addCleanup(close_https_transport, binding)
            materialize_https_credentials(binding)
            poison = {
                "HTTP_PROXY": "http://attacker.invalid",
                "https_proxy": "http://attacker.invalid",
                "ALL_PROXY": "socks5://attacker.invalid",
                "NO_PROXY": "example.com",
                "CURL_CA_BUNDLE": "/tmp/attacker-ca",
                "CURL_HOME": "/tmp/attacker-curl-home",
                "CURL_SSL_BACKEND": "attacker",
                "OPENSSL_CONF": "/tmp/attacker-openssl.cnf",
                "OPENSSL_MODULES": "/tmp/attacker-modules",
                "SSL_CERT_FILE": "/tmp/attacker-cert",
                "SSL_CERT_DIR": "/tmp/attacker-dir",
                "SSLKEYLOGFILE": "/tmp/tls-secrets.log",
                "GIT_SSL_NO_VERIFY": "1",
                "GIT_SSL_CAINFO": "/tmp/attacker-ca",
                "GIT_PROXY_SSL_CERT": "/tmp/attacker-cert",
                "GIT_HTTP_PROXY_AUTHMETHOD": "ntlm",
                "GIT_CURL_VERBOSE": "1",
                "GCM_INTERACTIVE": "1",
                "GCM_GUI_PROMPT": "1",
                "GCM_TRACE": "/tmp/secrets.log",
                "GCM_TRACE_SECRETS": "1",
                "BASH_ENV": "/tmp/attacker-shell",
            }
            with mock.patch.dict(os.environ, poison, clear=False):
                env = https_git_environment(snapshot, binding)

            for key in poison:
                with self.subTest(key=key):
                    self.assertNotIn(key, env)
                    self.assertNotIn(key.upper(), env)
            self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")

    def test_ca_or_credential_source_drift_invalidates_binding(self) -> None:
        with tempfile.TemporaryDirectory() as raw, tempfile.TemporaryDirectory() as trust_raw:
            root, trust = Path(raw), Path(trust_raw)
            _init_repo(root)
            _snapshot, binding, ca, credential = _binding(root, trust)
            self.addCleanup(close_https_transport, binding)
            ca.write_bytes(ca.read_bytes() + b"tamper")
            with self.assertRaisesRegex(GitMutationPreconditionChangedError, "CA bundle"):
                revalidate_https_transport(binding)

            close_https_transport(binding)
            _snapshot, binding, _ca, credential = _binding(root, trust)
            self.addCleanup(close_https_transport, binding)
            credential.write_bytes(credential.read_bytes() + b"tamper")
            with self.assertRaisesRegex(GitMutationPreconditionChangedError, "credential source"):
                revalidate_https_transport(binding)


if __name__ == "__main__":
    unittest.main()
