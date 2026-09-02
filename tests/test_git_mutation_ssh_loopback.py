from __future__ import annotations

import getpass
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from codexia_manual_agent.authority import (
    ActionLifecycle,
    ApprovalMode,
    LocalApprovalAuthority,
)
from codexia_manual_agent.git_mutation.models import GitMutationOutcome
from codexia_manual_agent.git_mutation.network_push import (
    execute_network_git_push,
    prepare_network_git_push_proposal,
)


_USER_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


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
    _git(root, "config", "user.name", "SSH Loopback Test")
    _git(root, "config", "user.email", "ssh-loopback@example.invalid")
    (root / "tracked.txt").write_bytes(b"base\n")
    _git(root, "add", "tracked.txt")
    _git(root, "commit", "--no-gpg-sign", "-m", "base")


def _direct_commit(root: Path) -> str:
    (root / "tracked.txt").write_bytes(b"ssh-network-ahead\n")
    _git(root, "add", "tracked.txt")
    _git(root, "commit", "--no-gpg-sign", "-m", "ssh network ahead")
    return _oid(root)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_listener(process: subprocess.Popen[bytes], port: int) -> tuple[bool, str]:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stderr = process.stderr.read().decode("utf-8", errors="replace") if process.stderr else ""
            return False, stderr
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return True, ""
        except OSError:
            time.sleep(0.05)
    return False, "sshd did not open the loopback listener"


class _NoopPin:
    def close(self):
        return None


@unittest.skipIf(os.name == "nt", "POSIX sshd fixture uses an absolute POSIX repository path")
@unittest.skipUnless(
    shutil.which("git") and shutil.which("ssh") and shutil.which("sshd") and shutil.which("ssh-keygen"),
    "Git, OpenSSH client/server and ssh-keygen are required",
)
class SshNetworkGitPushLoopbackTests(unittest.TestCase):
    def test_real_sshd_public_key_push_uses_bound_host_key_route_identity_and_lease(self) -> None:
        user = getpass.getuser()
        if _USER_RE.fullmatch(user) is None:
            self.skipTest("Current OS username is outside the admitted SSH user grammar")

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            local = root / "local"
            bare = root / "remote.git"
            trust = root / "trust"
            local.mkdir()
            bare.mkdir()
            trust.mkdir()
            _init_repo(local)
            _git(bare, "init", "--bare")
            _git(local, "remote", "add", "seed", bare.as_uri())
            _git(local, "push", "seed", "main")
            base = _oid(local)

            ssh_keygen = shutil.which("ssh-keygen")
            sshd = shutil.which("sshd")
            assert ssh_keygen is not None and sshd is not None
            host_key = trust / "host_ed25519"
            user_key = trust / "user_ed25519"
            for target in (host_key, user_key):
                result = subprocess.run(
                    [ssh_keygen, "-q", "-t", "ed25519", "-N", "", "-f", str(target)],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    shell=False,
                    check=False,
                )
                if result.returncode != 0:
                    self.skipTest(
                        "ssh-keygen could not create loopback keys: "
                        + result.stderr.decode("utf-8", errors="replace")
                    )
                target.chmod(0o600)

            authorized_keys = trust / "authorized_keys"
            authorized_keys.write_bytes(Path(f"{user_key}.pub").read_bytes())
            authorized_keys.chmod(0o600)

            port = _free_port()
            config = trust / "sshd_config"
            config.write_text(
                "\n".join(
                    (
                        f"Port {port}",
                        "ListenAddress 127.0.0.1",
                        f"HostKey {host_key}",
                        f"PidFile {trust / 'sshd.pid'}",
                        f"AuthorizedKeysFile {authorized_keys}",
                        "PubkeyAuthentication yes",
                        "PasswordAuthentication no",
                        "KbdInteractiveAuthentication no",
                        "PermitEmptyPasswords no",
                        "StrictModes no",
                        "AllowTcpForwarding no",
                        "X11Forwarding no",
                        "PermitTunnel no",
                        "GatewayPorts no",
                        "LogLevel ERROR",
                    )
                )
                + "\n",
                encoding="utf-8",
            )
            process = subprocess.Popen(
                [str(Path(sshd).resolve()), "-D", "-e", "-f", str(config)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                shell=False,
                env=os.environ.copy(),
            )
            self.addCleanup(process.kill)
            self.addCleanup(process.wait, 5)
            self.addCleanup(process.terminate)
            listening, startup_error = _wait_for_listener(process, port)
            if not listening:
                self.skipTest(f"Local sshd could not start: {startup_error[:1000]}")

            public = Path(f"{host_key}.pub").read_text(encoding="utf-8").split()
            known_hosts = trust / "known_hosts"
            known_hosts.write_text(
                f"[127.0.0.1]:{port} {public[0]} {public[1]}\n",
                encoding="utf-8",
            )
            remote_url = f"ssh://{user}@127.0.0.1:{port}{bare.as_posix()}"
            _git(local, "remote", "set-url", "seed", remote_url)
            _git(local, "remote", "rename", "seed", "origin")
            _git(local, "update-ref", "refs/remotes/origin/main", base)
            local_oid = _direct_commit(local)
            identity_before = user_key.read_bytes()

            prepared = prepare_network_git_push_proposal(
                workspace=local,
                remote="origin",
                destination_ref="refs/heads/main",
                identity_file=user_key.resolve(),
                host_key_file=known_hosts.resolve(),
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
                "codexia_manual_agent.git_mutation.network_push.WindowsGitNamespacePin.acquire",
                return_value=_NoopPin(),
            ):
                observation = execute_network_git_push(
                    prepared,
                    lifecycle=lifecycle,
                    authority=authority,
                )

            if observation.outcome is not GitMutationOutcome.APPLIED:
                stderr = ""
                if process.stderr is not None and process.poll() is not None:
                    stderr = process.stderr.read().decode("utf-8", errors="replace")
                self.fail(f"SSH loopback outcome={observation.to_dict()} sshd={stderr[:1000]}")
            self.assertEqual(observation.observed_remote_oid, local_oid)
            self.assertEqual(_oid(bare, "refs/heads/main"), local_oid)
            self.assertEqual(user_key.read_bytes(), identity_before)
            self.assertTrue(authority.is_consumed(receipt))


if __name__ == "__main__":
    unittest.main()
