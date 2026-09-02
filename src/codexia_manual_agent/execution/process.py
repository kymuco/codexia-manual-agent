from __future__ import annotations

import os
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import BinaryIO, Sequence
from uuid import uuid4

from codexia_manual_agent.authority import (
    ActionLifecycle,
    ActionPhase,
    ActionProposal,
    LocalApprovalAuthority,
)
from codexia_manual_agent.domain.capabilities import Capability
from codexia_manual_agent.domain.errors import (
    InvalidActionTransitionError,
    InvalidProcessSpecError,
    ProcessExecutableChangedError,
    ProcessExecutableNotFoundError,
    ProcessExecutionError,
    ProcessWorkspaceBoundaryError,
)
from codexia_manual_agent.execution.models import (
    ProcessExecutionObservation,
    ProcessLimits,
    ProcessTerminationReason,
    StreamObservation,
)


PROCESS_ACTION = "process.execute.v1"
_ENVIRONMENT_PROFILE = "minimal-v1"
_MAX_ARG_COUNT = 256
_MAX_ARG_CHARS = 32_768
_MAX_TOTAL_ARG_CHARS = 131_072
_MAX_EXECUTABLE_BYTES = 268_435_456
_SHELL_INTERPRETERS = {
    "bash",
    "cmd",
    "cmd.exe",
    "csh",
    "dash",
    "fish",
    "ksh",
    "powershell",
    "powershell.exe",
    "pwsh",
    "pwsh.exe",
    "sh",
    "tcsh",
    "wsl",
    "wsl.exe",
    "zsh",
}


@dataclass(frozen=True, slots=True)
class _ValidatedPlan:
    workspace_root: Path
    cwd: Path
    cwd_relative: str
    executable: Path
    argv: tuple[str, ...]
    environment: dict[str, str]
    limits: ProcessLimits


class _StreamCollector:
    def __init__(self, limit: int, exceeded: threading.Event) -> None:
        self.limit = limit
        self.exceeded = exceeded
        self._lock = threading.Lock()
        self._hash = sha256()
        self._stored = bytearray()
        self._byte_count = 0

    def read(self, pipe: BinaryIO) -> None:
        try:
            while True:
                chunk = pipe.read(8192)
                if not chunk:
                    return
                with self._lock:
                    self._hash.update(chunk)
                    self._byte_count += len(chunk)
                    remaining = self.limit - len(self._stored)
                    if remaining > 0:
                        self._stored.extend(chunk[:remaining])
                    if self._byte_count > self.limit:
                        self.exceeded.set()
        finally:
            try:
                pipe.close()
            except OSError:
                pass

    def snapshot(self) -> StreamObservation:
        with self._lock:
            return StreamObservation.from_bytes(
                byte_count=self._byte_count,
                digest=self._hash.hexdigest(),
                stored=bytes(self._stored),
            )


def _validate_argv(argv: Sequence[str]) -> tuple[str, ...]:
    if isinstance(argv, (str, bytes)):
        raise InvalidProcessSpecError("argv must be a sequence of strings")
    values = tuple(argv)
    if not values:
        raise InvalidProcessSpecError("argv must contain an executable")
    if len(values) > _MAX_ARG_COUNT:
        raise InvalidProcessSpecError(f"argv exceeds {_MAX_ARG_COUNT} entries")
    total = 0
    for index, value in enumerate(values):
        if type(value) is not str:
            raise InvalidProcessSpecError(f"argv[{index}] must be a string")
        if not value:
            raise InvalidProcessSpecError(f"argv[{index}] must not be empty")
        if "\x00" in value:
            raise InvalidProcessSpecError(f"argv[{index}] contains a NUL byte")
        if len(value) > _MAX_ARG_CHARS:
            raise InvalidProcessSpecError(f"argv[{index}] is too long")
        total += len(value)
    if total > _MAX_TOTAL_ARG_CHARS:
        raise InvalidProcessSpecError("argv total character budget exceeded")
    return values


def _workspace_root(workspace: str | Path) -> Path:
    try:
        root = Path(workspace).expanduser().resolve(strict=True)
    except OSError as exc:
        raise ProcessWorkspaceBoundaryError(
            f"Workspace cannot be resolved: {workspace}"
        ) from exc
    if not root.is_dir():
        raise ProcessWorkspaceBoundaryError("Workspace root must be a directory")
    return root


def _resolve_cwd(root: Path, cwd: str | Path) -> tuple[Path, str]:
    relative = Path(cwd)
    if relative.is_absolute():
        raise ProcessWorkspaceBoundaryError("Process cwd must be workspace-relative")
    try:
        resolved = (root / relative).resolve(strict=True)
    except OSError as exc:
        raise ProcessWorkspaceBoundaryError(f"Process cwd does not exist: {cwd}") from exc
    try:
        rel = resolved.relative_to(root)
    except ValueError as exc:
        raise ProcessWorkspaceBoundaryError("Process cwd escapes workspace boundary") from exc
    if not resolved.is_dir():
        raise ProcessWorkspaceBoundaryError("Process cwd must resolve to a directory")
    rendered = rel.as_posix()
    return resolved, rendered if rendered else "."


def _filtered_search_path(root: Path) -> str:
    safe: list[str] = []
    for raw in os.environ.get("PATH", "").split(os.pathsep):
        if not raw:
            continue
        try:
            candidate = Path(raw).expanduser().resolve(strict=True)
        except OSError:
            continue
        try:
            candidate.relative_to(root)
        except ValueError:
            safe.append(str(candidate))
    return os.pathsep.join(safe)


def _is_explicit_executable(value: str) -> bool:
    return (
        Path(value).is_absolute()
        or os.sep in value
        or bool(os.altsep and os.altsep in value)
    )


def _reject_workspace_bare_executable(
    resolved: Path,
    root: Path,
    command: str,
) -> None:
    """Fail closed when an implicitly resolved host command lands in workspace."""

    try:
        resolved.relative_to(root)
    except ValueError:
        return
    raise ProcessExecutableNotFoundError(
        f"Bare executable resolved inside workspace boundary: {command}"
    )


def _resolve_executable(command: str, root: Path) -> Path:
    explicit = _is_explicit_executable(command)
    try:
        if explicit:
            candidate = Path(command).expanduser()
            if not candidate.is_absolute():
                candidate = root / candidate
            resolved = candidate.resolve(strict=True)
        else:
            found = shutil.which(command, path=_filtered_search_path(root))
            if not found:
                raise ProcessExecutableNotFoundError(
                    f"Executable not found on filtered host PATH: {command}"
                )
            resolved = Path(found).resolve(strict=True)
            _reject_workspace_bare_executable(resolved, root, command)
    except FileNotFoundError as exc:
        raise ProcessExecutableNotFoundError(
            f"Executable does not exist: {command}"
        ) from exc
    except OSError as exc:
        raise ProcessExecutableNotFoundError(
            f"Executable cannot be resolved: {command}"
        ) from exc

    if not resolved.is_file():
        raise ProcessExecutableNotFoundError(
            f"Executable is not a regular file: {resolved}"
        )
    if resolved.name.lower() in _SHELL_INTERPRETERS:
        raise InvalidProcessSpecError(
            f"Shell interpreters are not admitted in M2.1: {resolved.name}"
        )
    return resolved


def _file_identity(path: Path) -> tuple[int, str]:
    try:
        before = path.stat()
    except OSError as exc:
        raise ProcessExecutableChangedError(
            f"Executable is no longer available: {path}"
        ) from exc
    if before.st_size > _MAX_EXECUTABLE_BYTES:
        raise InvalidProcessSpecError("Executable exceeds the M2.1 hashing budget")

    digest = sha256()
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
        after = path.stat()
    except OSError as exc:
        raise ProcessExecutableChangedError(
            f"Executable changed while being inspected: {path}"
        ) from exc
    if (
        before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
    ):
        raise ProcessExecutableChangedError(
            f"Executable changed while being inspected: {path}"
        )
    return after.st_size, digest.hexdigest()


def _minimal_environment() -> dict[str, str]:
    environment = {
        "CODEXIA_EXECUTION": "1",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
    }
    for key in ("SystemRoot", "WINDIR", "LANG", "LC_ALL"):
        value = os.environ.get(key)
        if value:
            environment[key] = value
    return dict(sorted(environment.items()))


def prepare_process_proposal(
    *,
    workspace: str | Path,
    argv: Sequence[str],
    cwd: str | Path = ".",
    limits: ProcessLimits | None = None,
    summary: str | None = None,
) -> ActionProposal:
    root = _workspace_root(workspace)
    normalized_argv = _validate_argv(argv)
    resolved_cwd, cwd_relative = _resolve_cwd(root, cwd)
    executable = _resolve_executable(normalized_argv[0], root)
    executable_size, executable_sha256 = _file_identity(executable)
    process_limits = limits or ProcessLimits()
    environment = _minimal_environment()

    return ActionProposal.create(
        capability=Capability.EXECUTE_PROCESS,
        action=PROCESS_ACTION,
        workspace_root=str(root),
        parameters={
            "argv": list(normalized_argv),
            "resolved_executable": str(executable),
            "executable_size": executable_size,
            "executable_sha256": executable_sha256,
            "cwd": cwd_relative,
            "environment_profile": _ENVIRONMENT_PROFILE,
            "environment": environment,
            "limits": process_limits.to_dict(),
        },
        summary=summary or "Execute a local process with structured argv.",
    )


def _validate_process_proposal(proposal: ActionProposal) -> _ValidatedPlan:
    if proposal.capability is not Capability.EXECUTE_PROCESS:
        raise InvalidProcessSpecError("Process executor requires execute_process capability")
    if proposal.action != PROCESS_ACTION:
        raise InvalidProcessSpecError("Unsupported process action")

    params = proposal.to_dict()["parameters"]
    expected_keys = {
        "argv",
        "resolved_executable",
        "executable_size",
        "executable_sha256",
        "cwd",
        "environment_profile",
        "environment",
        "limits",
    }
    if set(params) != expected_keys:
        raise InvalidProcessSpecError("Process proposal parameters do not match M2.1 schema")

    root = _workspace_root(proposal.workspace_root)
    if str(root) != proposal.workspace_root:
        raise ProcessWorkspaceBoundaryError("Proposal workspace root is not canonical")

    argv = _validate_argv(params["argv"])
    cwd, cwd_relative = _resolve_cwd(root, params["cwd"])
    if cwd_relative != params["cwd"]:
        raise ProcessWorkspaceBoundaryError("Proposal cwd is not canonical")

    if params["environment_profile"] != _ENVIRONMENT_PROFILE:
        raise InvalidProcessSpecError("Unsupported process environment profile")
    environment = params["environment"]
    if not isinstance(environment, dict) or any(
        type(key) is not str or type(value) is not str
        for key, value in environment.items()
    ):
        raise InvalidProcessSpecError("Process environment must be a string mapping")
    expected_environment = _minimal_environment()
    if environment != expected_environment:
        raise InvalidProcessSpecError(
            "Process environment differs from the local minimal environment profile"
        )

    limits_data = params["limits"]
    if not isinstance(limits_data, dict):
        raise InvalidProcessSpecError("Process limits must be an object")
    try:
        limits = ProcessLimits(**limits_data)
    except (TypeError, ValueError) as exc:
        raise InvalidProcessSpecError("Invalid process limits") from exc
    if limits.to_dict() != limits_data:
        raise InvalidProcessSpecError("Process limits are not canonical")

    if type(params["resolved_executable"]) is not str:
        raise InvalidProcessSpecError("resolved_executable must be a string")
    freshly_resolved = _resolve_executable(argv[0], root)
    try:
        recorded_executable = Path(params["resolved_executable"]).resolve(strict=True)
    except OSError as exc:
        raise ProcessExecutableChangedError("Recorded executable no longer exists") from exc
    if freshly_resolved != recorded_executable:
        raise ProcessExecutableChangedError(
            "Executable resolution changed after proposal approval"
        )

    if type(params["executable_size"]) is not int:
        raise InvalidProcessSpecError("executable_size must be an integer")
    if type(params["executable_sha256"]) is not str:
        raise InvalidProcessSpecError("executable_sha256 must be a string")
    size, digest = _file_identity(recorded_executable)
    if size != params["executable_size"] or digest != params["executable_sha256"]:
        raise ProcessExecutableChangedError(
            "Executable bytes changed after proposal approval"
        )

    return _ValidatedPlan(
        workspace_root=root,
        cwd=cwd,
        cwd_relative=cwd_relative,
        executable=recorded_executable,
        argv=argv,
        environment=environment,
        limits=limits,
    )


def _terminate_process_tree(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        system_root = os.environ.get("SystemRoot") or os.environ.get("WINDIR")
        taskkill = (
            Path(system_root) / "System32" / "taskkill.exe"
            if system_root
            else None
        )
        if taskkill is not None and taskkill.is_file():
            try:
                subprocess.run(
                    [str(taskkill), "/PID", str(process.pid), "/T", "/F"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    timeout=5,
                )
                return
            except (OSError, subprocess.TimeoutExpired):
                pass
        try:
            process.kill()
        except OSError:
            pass
        return

    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            process.kill()
        except OSError:
            pass


class ProcessExecutor:
    """Human-authorized bounded subprocess executor.

    M2.1 deliberately does not expose this surface to the remote model. It is a
    local execution primitive, not an OS sandbox: an approved executable may
    itself have capabilities beyond process creation.
    """

    def execute(
        self,
        lifecycle: ActionLifecycle,
        *,
        authority: LocalApprovalAuthority,
    ) -> ProcessExecutionObservation:
        if lifecycle.phase is not ActionPhase.AUTHORIZED:
            raise InvalidActionTransitionError(
                "Process execution requires an AUTHORIZED lifecycle"
            )
        if lifecycle.authorization is None:
            raise InvalidActionTransitionError("Authorized process has no receipt")

        # Validate every execution-relevant field before consuming the one-shot
        # receipt. A changed executable/cwd/environment therefore cannot burn an
        # approval and then silently execute a different action.
        plan = _validate_process_proposal(lifecycle.proposal)
        receipt = lifecycle.authorization
        execution_id = str(uuid4())
        started_at = time.monotonic()
        lifecycle.consume_authorization(authority=authority)

        actual_argv = (str(plan.executable), *plan.argv[1:])
        creationflags = 0
        popen_kwargs: dict[str, object] = {}
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            popen_kwargs["start_new_session"] = True

        try:
            process = subprocess.Popen(
                list(actual_argv),
                cwd=str(plan.cwd),
                env=plan.environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                creationflags=creationflags,
                **popen_kwargs,
            )
        except OSError as exc:
            lifecycle.record_executed(execution_id)
            empty = StreamObservation.from_bytes(
                byte_count=0,
                digest=sha256(b"").hexdigest(),
                stored=b"",
            )
            observation = ProcessExecutionObservation.create(
                proposal_id=lifecycle.proposal.proposal_id,
                proposal_digest=lifecycle.proposal.proposal_digest,
                receipt_id=receipt.receipt_id,
                receipt_digest=receipt.receipt_digest,
                execution_id=execution_id,
                started=False,
                pid=None,
                cwd=plan.cwd_relative,
                resolved_executable=str(plan.executable),
                argv=actual_argv,
                exit_code=None,
                termination_reason=ProcessTerminationReason.SPAWN_ERROR,
                duration_ms=max(0, int((time.monotonic() - started_at) * 1000)),
                stdout=empty,
                stderr=empty,
                error=f"{type(exc).__name__}: {exc}",
            )
            lifecycle.record_observed(observation.observation_id)
            return observation

        lifecycle.record_executed(execution_id)
        if process.stdout is None or process.stderr is None:  # pragma: no cover
            _terminate_process_tree(process)
            raise ProcessExecutionError("Process pipes were not created")

        exceeded = threading.Event()
        stdout = _StreamCollector(plan.limits.max_stdout_bytes, exceeded)
        stderr = _StreamCollector(plan.limits.max_stderr_bytes, exceeded)
        threads = [
            threading.Thread(target=stdout.read, args=(process.stdout,), daemon=True),
            threading.Thread(target=stderr.read, args=(process.stderr,), daemon=True),
        ]
        for thread in threads:
            thread.start()

        reason = ProcessTerminationReason.EXITED
        deadline = started_at + plan.limits.timeout_seconds
        while process.poll() is None:
            if exceeded.is_set():
                reason = ProcessTerminationReason.OUTPUT_LIMIT
                _terminate_process_tree(process)
                break
            if time.monotonic() >= deadline:
                reason = ProcessTerminationReason.TIMEOUT
                _terminate_process_tree(process)
                break
            time.sleep(0.01)

        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _terminate_process_tree(process)
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:  # pragma: no cover
                pass

        for thread in threads:
            thread.join(timeout=2)
        if exceeded.is_set() and reason is ProcessTerminationReason.EXITED:
            reason = ProcessTerminationReason.OUTPUT_LIMIT

        stdout_observation = stdout.snapshot()
        stderr_observation = stderr.snapshot()
        observation = ProcessExecutionObservation.create(
            proposal_id=lifecycle.proposal.proposal_id,
            proposal_digest=lifecycle.proposal.proposal_digest,
            receipt_id=receipt.receipt_id,
            receipt_digest=receipt.receipt_digest,
            execution_id=execution_id,
            started=True,
            pid=process.pid,
            cwd=plan.cwd_relative,
            resolved_executable=str(plan.executable),
            argv=actual_argv,
            exit_code=process.returncode,
            termination_reason=reason,
            duration_ms=max(0, int((time.monotonic() - started_at) * 1000)),
            stdout=stdout_observation,
            stderr=stderr_observation,
        )
        lifecycle.record_observed(observation.observation_id)
        return observation