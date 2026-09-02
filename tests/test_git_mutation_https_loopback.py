from __future__ import annotations

import base64
import os
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from unittest import mock

from codexia_manual_agent.authority import (
    ActionLifecycle,
    ApprovalMode,
    LocalApprovalAuthority,
)
from codexia_manual_agent.git_mutation.https_push import (
    execute_https_network_git_push,
    prepare_https_network_git_push_proposal,
)
from codexia_manual_agent.git_mutation.models import GitMutationOutcome


_EXPECTED_AUTH = "Basic " + base64.b64encode(b"codexia-user:exact-secret").decode("ascii")


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        check=False,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    if check and result.returncode != 0:
        raise AssertionError(result.stderr.decode("utf-8", errors="replace"))
    return result


def _oid(root: Path, ref: str = "HEAD") -> str:
    return _git(root, "rev-parse", "--verify", ref).stdout.decode().strip()


def _init_repo(root: Path) -> None:
    _git(root, "init")
    _git(root, "symbolic-ref", "HEAD", "refs/heads/main")
    _git(root, "config", "user.name", "Loopback Test")
    _git(root, "config", "user.email", "loopback@example.invalid")
    (root / "tracked.txt").write_bytes(b"base\n")
    _git(root, "add", "tracked.txt")
    _git(root, "commit", "--no-gpg-sign", "-m", "base")


def _direct_commit(root: Path) -> str:
    (root / "tracked.txt").write_bytes(b"network-ahead\n")
    _git(root, "add", "tracked.txt")
    _git(root, "commit", "--no-gpg-sign", "-m", "network ahead")
    return _oid(root)


def _generate_tls_identity(openssl: str, trust: Path) -> tuple[Path, Path]:
    cert = trust / "cert.pem"
    key = trust / "key.pem"
    result = subprocess.run(
        [
            openssl,
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-days",
            "1",
            "-subj",
            "/CN=example.test",
            "-addext",
            "subjectAltName=DNS:example.test",
            "-addext",
            "basicConstraints=critical,CA:TRUE",
            "-addext",
            "keyUsage=critical,digitalSignature,keyEncipherment,keyCertSign",
            "-addext",
            "extendedKeyUsage=serverAuth",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        check=False,
    )
    if result.returncode != 0:
        raise unittest.SkipTest(
            "OpenSSL could not generate the loopback TLS certificate: "
            + result.stderr.decode("utf-8", errors="replace")[:1000]
        )
    if os.name != "nt":
        key.chmod(0o600)
    return cert, key


class _NoopPin:
    def close(self):
        return None


class _GitHttpServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, handler, *, git: str, project_root: Path):
        super().__init__(address, handler)
        self.git = git
        self.project_root = project_root
        self.requests_seen: list[tuple[str, str, bool]] = []


class _GitHttpHandler(BaseHTTPRequestHandler):
    server: _GitHttpServer

    def log_message(self, _format: str, *args) -> None:
        return

    def do_GET(self) -> None:
        self._serve_git()

    def do_POST(self) -> None:
        self._serve_git()

    def _serve_git(self) -> None:
        authorized = self.headers.get("Authorization") == _EXPECTED_AUTH
        self.server.requests_seen.append((self.command, self.path, authorized))
        if not authorized:
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="codexia-loopback"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        parsed = urlsplit(self.path)
        length = int(self.headers.get("Content-Length", "0") or "0")
        body = self.rfile.read(length) if length else b""
        env = {
            **os.environ,
            "GIT_PROJECT_ROOT": str(self.server.project_root),
            "GIT_HTTP_EXPORT_ALL": "1",
            "PATH_INFO": parsed.path,
            "QUERY_STRING": parsed.query,
            "REQUEST_METHOD": self.command,
            "CONTENT_TYPE": self.headers.get("Content-Type", ""),
            "CONTENT_LENGTH": str(len(body)),
            "REMOTE_USER": "codexia-user",
            "REMOTE_ADDR": "127.0.0.1",
            "SERVER_NAME": "example.test",
            "SERVER_PORT": str(self.server.server_address[1]),
            "SERVER_PROTOCOL": self.request_version,
        }
        result = subprocess.run(
            [self.server.git, "http-backend"],
            input=body,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            check=False,
            env=env,
        )
        if result.returncode != 0:
            self.send_response(500)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(result.stderr)))
            self.end_headers()
            self.wfile.write(result.stderr)
            return

        header_blob, separator, response_body = result.stdout.partition(b"\r\n\r\n")
        if not separator:
            header_blob, separator, response_body = result.stdout.partition(b"\n\n")
        if not separator:
            self.send_response(500)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        status = 200
        headers: list[tuple[str, str]] = []
        for raw in header_blob.replace(b"\r\n", b"\n").split(b"\n"):
            if not raw:
                continue
            name_raw, sep, value_raw = raw.partition(b":")
            if not sep:
                continue
            name = name_raw.decode("latin-1")
            value = value_raw.lstrip().decode("latin-1")
            if name.casefold() == "status":
                status = int(value.split(" ", 1)[0])
            else:
                headers.append((name, value))

        self.send_response(status)
        for name, value in headers:
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(response_body)))
        self.end_headers()
        self.wfile.write(response_body)


@unittest.skipUnless(shutil.which("git"), "Git is required")
class HttpsNetworkGitPushLoopbackTests(unittest.TestCase):
    def test_real_tls_smart_http_push_uses_bound_route_ca_credentials_and_lease(self) -> None:
        openssl = shutil.which("openssl")
        if not openssl:
            self.skipTest("OpenSSL CLI is required to generate the loopback TLS identity")

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            local = root / "local"
            project_root = root / "http-root"
            bare = project_root / "team" / "repo.git"
            trust = root / "trust"
            local.mkdir()
            bare.mkdir(parents=True)
            trust.mkdir()
            _init_repo(local)
            _git(bare, "init", "--bare")
            _git(bare, "config", "http.receivepack", "true")
            _git(local, "remote", "add", "seed", bare.as_uri())
            _git(local, "push", "seed", "main")
            base = _oid(local)

            cert, key = _generate_tls_identity(openssl, trust)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(certfile=str(cert), keyfile=str(key))

            git = shutil.which("git")
            assert git is not None
            server = _GitHttpServer(
                ("127.0.0.1", 0),
                _GitHttpHandler,
                git=git,
                project_root=project_root,
            )
            server.socket = context.wrap_socket(server.socket, server_side=True)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            # addCleanup is LIFO: shutdown first, then close socket, then join.
            self.addCleanup(thread.join, 5)
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)

            port = server.server_address[1]
            remote_url = f"https://example.test:{port}/team/repo.git"
            _git(local, "remote", "set-url", "seed", remote_url)
            _git(local, "remote", "rename", "seed", "origin")
            _git(local, "update-ref", "refs/remotes/origin/main", base)
            local_oid = _direct_commit(local)

            credential = trust / "credentials"
            credential.write_text(
                f"https://codexia-user:exact-secret@example.test:{port}/team/repo.git\n",
                encoding="utf-8",
            )
            credential_before = credential.read_bytes()
            records = [
                (
                    socket.AF_INET,
                    socket.SOCK_STREAM,
                    socket.IPPROTO_TCP,
                    "",
                    ("127.0.0.1", port),
                )
            ]
            with mock.patch(
                "codexia_manual_agent.git_mutation.https_transport.socket.getaddrinfo",
                return_value=records,
            ):
                prepared = prepare_https_network_git_push_proposal(
                    workspace=local,
                    remote="origin",
                    destination_ref="refs/heads/main",
                    credential_file=credential.resolve(),
                    ca_bundle_file=cert.resolve(),
                )

            authority = LocalApprovalAuthority()
            lifecycle = ActionLifecycle(prepared.proposal, ApprovalMode.ALWAYS)
            receipt = authority.decide(
                prepared.proposal,
                mode=ApprovalMode.ALWAYS,
                approved=True,
                actor="loopback-human",
            )
            lifecycle.apply_receipt(receipt, authority=authority)
            with mock.patch(
                "codexia_manual_agent.git_mutation.https_push.WindowsGitNamespacePin.acquire",
                return_value=_NoopPin(),
            ):
                observation = execute_https_network_git_push(
                    prepared,
                    lifecycle=lifecycle,
                    authority=authority,
                )

            self.assertIs(observation.outcome, GitMutationOutcome.APPLIED)
            self.assertEqual(observation.observed_remote_oid, local_oid)
            self.assertEqual(_oid(bare, "refs/heads/main"), local_oid)
            self.assertEqual(credential.read_bytes(), credential_before)
            self.assertTrue(authority.is_consumed(receipt))

            authorized_posts = [
                (method, path)
                for method, path, authorized in server.requests_seen
                if authorized and method == "POST"
            ]
            self.assertTrue(
                any(
                    path.startswith("/team/repo.git/git-receive-pack")
                    for _, path in authorized_posts
                ),
                server.requests_seen,
            )
            self.assertTrue(
                all(path.startswith("/team/repo.git/") for _, path, _ in server.requests_seen),
                server.requests_seen,
            )


if __name__ == "__main__":
    unittest.main()
