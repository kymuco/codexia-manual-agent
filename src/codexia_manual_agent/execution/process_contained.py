from __future__ import annotations

import json
import os
import select
import shutil
import signal
import subprocess
import sys
import threading
import time
from hashlib import sha256
from pathlib import Path
from typing import Sequence
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
from codexia_manual_agent.execution import process as _base
from codexia_manual_agent.execution.models import (
    ProcessExecutionObservation,
    ProcessLimits,
    ProcessTerminationReason,
    StreamObservation,
)
from codexia_manual_agent.execution.windows_job import WindowsJobObject


PROCESS_ACTION = _base.PROCESS_ACTION
POWERSHELL_PROFILE = "powershell-explicit-noninteractive-v1"
_BWRAP_PATH = Path("/usr/bin/bwrap")
_TREE_SHUTDOWN_GRACE_SECONDS = 5.0
_LINUX_STARTUP_MAX_SECONDS = 5.0
_EXEC_READY = b"READY"
_EXEC_ERROR_PREFIX = b"ERROR "


def _resolve_executable(
    command: str, root: Path, cwd: Path, *,
    powershell_profile: bool = False,
) -> Path:
    """Resolve explicit relative paths from process cwd, like the OS does."""

    explicit = _base._is_explicit_executable(command)
    try:
        if explicit:
            candidate = Path(command).expanduser()
            if not candidate.is_absolute():
                candidate = cwd / candidate
            resolved = candidate.resolve(strict=True)
        else:
            found = shutil.which(command, path=_base._filtered_search_path(root))
            if not found:
                raise ProcessExecutableNotFoundError(
                    f"Executable not found on filtered host PATH: {command}"
                )
            resolved = Path(found).resolve(strict=True)
            _base._reject_workspace_bare_executable(resolved, root, command)
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
    if resolved.name.lower() in _base._SHELL_INTERPRETERS:
        if not (
            powershell_profile
            and os.name == "nt"
            and resolved.name.lower() == "powershell.exe"
        ):
            raise InvalidProcessSpecError(
                f"Shell interpreters are not admitted in M2.1: {resolved.name}"
            )
    return resolved


def _shell_argv(argv: Sequence[str]) -> None:
    if (
        len(argv) != 5
        or argv[0].lower() not in ("powershell.exe", "powershell")
        or list(argv[1:4]) != ["-NoProfile", "-NonInteractive", "-Command"]
        or not argv[4].strip()
        or len(argv[4]) > 12_000
    ):
        raise InvalidProcessSpecError(
            "PowerShell profile requires exact noninteractive -Command"
        )


def _shell_python(root: Path, *, windows: bool | None = None) -> Path:
    """Prefer the checked project venv, never a random Python from PATH.

    This executable is bound by size and SHA-256 in the process proposal.
    """
    if windows is None:
        windows = os.name == "nt"
    if windows and (root / ".venv").exists():
        candidate = root / ".venv" / "Scripts" / "python.exe"
        if candidate.is_symlink() or not candidate.is_file():
            raise InvalidProcessSpecError("project .venv has no regular Windows python.exe")
        resolved = candidate.resolve(strict=True)
        if not resolved.is_relative_to(root.resolve(strict=True)):
            raise InvalidProcessSpecError("project Python escapes the workspace")
        return resolved
    interpreter = Path(sys.executable)
    if not interpreter.is_absolute() or not interpreter.is_file():
        raise InvalidProcessSpecError("Codexia Python executable cannot be resolved")
    if windows and interpreter.name.lower() != "python.exe":
        raise InvalidProcessSpecError("PowerShell requires a python.exe runtime")
    return interpreter.resolve(strict=True)


def _environment(root: Path, shell_profile: str | None) -> dict[str, str]:
    environment = _base._minimal_environment()
    if shell_profile:
        # WinPS initializes .NET and built-in module paths even under -NoProfile.
        # Keep normal M2.1 processes on the original minimal environment.
        # The controller may run from an explicit venv/python.exe path even
        # when its Scripts directory is absent from the host PATH. Bind the
        # opt-in shell to that same interpreter so literal approved commands
        # like "@' ... '@ | python -" work without rewriting their text.
        python_dir = _shell_python(root).parent
        filtered_path = _base._filtered_search_path(root)
        environment["PATH"] = os.pathsep.join(
            part for part in (str(python_dir), filtered_path) if part
        )
        if os.name == "nt":
            # Windows PowerShell resolves a bare `python` using PATHEXT.
            # Keep command discovery narrow; never inherit arbitrary executable
            # extension mappings into this explicit shell capability.
            environment["PATHEXT"] = ".EXE"
        for name in (
            "USERPROFILE", "HOMEDRIVE", "HOMEPATH",
            "APPDATA", "LOCALAPPDATA", "ProgramFiles", "ProgramFiles(x86)",
            "ProgramData", "PSModulePath", "TEMP", "TMP",
        ):
            value = os.environ.get(name)
            if value:
                environment[name] = value
    return dict(sorted(environment.items()))


def prepare_process_proposal(
    *,
    workspace: str | Path,
    argv: Sequence[str],
    cwd: str | Path = ".",
    limits: ProcessLimits | None = None,
    summary: str | None = None,
    shell_profile: str | None = None,
) -> ActionProposal:
    root = _base._workspace_root(workspace)
    normalized_argv = _base._validate_argv(argv)
    resolved_cwd, cwd_relative = _base._resolve_cwd(root, cwd)
    if shell_profile not in (None, POWERSHELL_PROFILE):
        raise InvalidProcessSpecError("unknown shell execution profile")
    if shell_profile is not None:
        _shell_argv(normalized_argv)
    executable = _resolve_executable(
        normalized_argv[0], root, resolved_cwd,
        **({"powershell_profile": True} if shell_profile else {}),
    )
    executable_size, executable_sha256 = _base._file_identity(executable)
    process_limits = limits or ProcessLimits()
    environment = _environment(root, shell_profile)
    shell_python = _shell_python(root) if shell_profile else None
    shell_python_identity = (
        _base._file_identity(shell_python) if shell_python is not None else None
    )

    return ActionProposal.create(
        capability=Capability.EXECUTE_PROCESS,
        action=PROCESS_ACTION,
        workspace_root=str(root),
        parameters={
            **({"shell_profile": shell_profile} if shell_profile else {}),
            **({
                "shell_python": str(shell_python),
                "shell_python_size": shell_python_identity[0],
                "shell_python_sha256": shell_python_identity[1],
            } if shell_python is not None else {}),
            "argv": list(normalized_argv),
            "resolved_executable": str(executable),
            "executable_size": executable_size,
            "executable_sha256": executable_sha256,
            "cwd": cwd_relative,
            "environment_profile": _base._ENVIRONMENT_PROFILE,
            "environment": environment,
            "limits": process_limits.to_dict(),
        },
        summary=summary or "Execute a local process with structured argv.",
    )


def _validate_process_proposal(proposal: ActionProposal) -> _base._ValidatedPlan:
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
    profile = params.get("shell_profile")
    if profile is not None:
        if profile != POWERSHELL_PROFILE:
            raise InvalidProcessSpecError("unknown explicit PowerShell profile")
        expected_keys.update((
            "shell_profile", "shell_python", "shell_python_size",
            "shell_python_sha256",
        ))
    if set(params) != expected_keys:
        raise InvalidProcessSpecError("Process proposal parameters do not match M2.1 schema")

    root = _base._workspace_root(proposal.workspace_root)
    if str(root) != proposal.workspace_root:
        raise ProcessWorkspaceBoundaryError("Proposal workspace root is not canonical")

    argv = _base._validate_argv(params["argv"])
    if profile is not None:
        _shell_argv(argv)
    cwd, cwd_relative = _base._resolve_cwd(root, params["cwd"])
    if cwd_relative != params["cwd"]:
        raise ProcessWorkspaceBoundaryError("Proposal cwd is not canonical")

    if params["environment_profile"] != _base._ENVIRONMENT_PROFILE:
        raise InvalidProcessSpecError("Unsupported process environment profile")
    environment = params["environment"]
    if not isinstance(environment, dict) or any(
        type(key) is not str or type(value) is not str
        for key, value in environment.items()
    ):
        raise InvalidProcessSpecError("Process environment must be a string mapping")
    if environment != _environment(root, profile):
        raise InvalidProcessSpecError(
            "Process environment differs from the local minimal environment profile"
        )
    if profile is not None:
        expected_python = _shell_python(root)
        if (
            type(params["shell_python"]) is not str
            or type(params["shell_python_size"]) is not int
            or type(params["shell_python_sha256"]) is not str
            or params["shell_python"] != str(expected_python)
        ):
            raise InvalidProcessSpecError("project Python binding differs")
        py_size, py_digest = _base._file_identity(expected_python)
        if (py_size, py_digest) != (
            params["shell_python_size"], params["shell_python_sha256"]
        ):
            raise ProcessExecutableChangedError("project Python bytes changed after approval")

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
    freshly_resolved = _resolve_executable(
        argv[0], root, cwd,
        **({"powershell_profile": True} if profile else {}),
    )
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
    size, digest = _base._file_identity(recorded_executable)
    if size != params["executable_size"] or digest != params["executable_sha256"]:
        raise ProcessExecutableChangedError(
            "Executable bytes changed after proposal approval"
        )

    return _base._ValidatedPlan(
        workspace_root=root,
        cwd=cwd,
        cwd_relative=cwd_relative,
        executable=recorded_executable,
        argv=argv,
        environment=environment,
        limits=limits,
    )


def _linux_bwrap() -> Path:
    try:
        resolved = _BWRAP_PATH.resolve(strict=True)
    except OSError as exc:
        raise ProcessExecutionError(
            "Linux process-tree containment requires /usr/bin/bwrap (bubblewrap)"
        ) from exc
    if not resolved.is_file():
        raise ProcessExecutionError(
            "Linux process-tree containment requires /usr/bin/bwrap (bubblewrap)"
        )
    return resolved


def _linux_sandbox_argv(
    plan: _base._ValidatedPlan,
    bwrap: Path,
    *,
    json_status_fd: int,
    exec_status_fd: int,
) -> tuple[str, ...]:
    trampoline = Path(__file__).with_name("exec_trampoline.py").resolve(strict=True)
    return (
        str(bwrap),
        "--unshare-pid",
        "--die-with-parent",
        "--bind",
        "/",
        "/",
        "--proc",
        "/proc",
        "--chdir",
        str(plan.cwd),
        "--json-status-fd",
        str(json_status_fd),
        "--",
        sys.executable,
        str(trampoline),
        str(exec_status_fd),
        str(plan.executable),
        *plan.argv[1:],
    )


def _empty_stream_observation() -> StreamObservation:
    return StreamObservation.from_bytes(
        byte_count=0,
        digest=sha256(b"").hexdigest(),
        stored=b"",
    )


def _wait_for_process_shutdown(process: subprocess.Popen[bytes]) -> None:
    try:
        process.wait(timeout=_TREE_SHUTDOWN_GRACE_SECONDS)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        process.kill()
    except OSError:
        pass
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass


def _terminate_linux_sandbox(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
        return
    except (ProcessLookupError, PermissionError):
        pass
    try:
        process.kill()
    except OSError:
        pass


def _terminate_unowned_windows_tree(process: subprocess.Popen[bytes]) -> None:
    system_root = os.environ.get("SystemRoot") or os.environ.get("WINDIR")
    taskkill = Path(system_root) / "System32" / "taskkill.exe" if system_root else None
    if taskkill is not None and taskkill.is_file():
        try:
            completed = subprocess.run(
                [str(taskkill), "/PID", str(process.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=5,
            )
            if completed.returncode == 0:
                return
        except (OSError, subprocess.TimeoutExpired):
            pass
    try:
        process.kill()
    except OSError:
        pass


def _terminate_windows_tree(
    process: subprocess.Popen[bytes],
    windows_job: WindowsJobObject | None,
) -> None:
    if windows_job is not None:
        try:
            windows_job.terminate()
            return
        except OSError:
            pass
    _terminate_unowned_windows_tree(process)


def _decode_exec_error(line: bytes) -> str:
    payload = line[len(_EXEC_ERROR_PREFIX) :]
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "Approved target exec failed"
    if isinstance(value, dict) and value.get("error") is not None:
        return f"Approved target exec failed: {value['error']}"
    return "Approved target exec failed"


def _await_linux_target_exec(
    process: subprocess.Popen[bytes],
    *,
    json_status_read: int,
    exec_status_read: int,
    deadline: float,
    exceeded: threading.Event,
) -> tuple[int | None, str | None, ProcessTerminationReason | None]:
    """Prove target exec or return the exact startup termination reason."""

    child_pid: int | None = None
    trampoline_ready = False
    exec_eof = False
    json_buffer = bytearray()
    exec_buffer = bytearray()
    open_fds = {json_status_read, exec_status_read}

    while True:
        if child_pid is not None and trampoline_ready and exec_eof:
            return child_pid, None, None
        if exceeded.is_set():
            return None, None, ProcessTerminationReason.OUTPUT_LIMIT
        now = time.monotonic()
        if now >= deadline:
            return None, None, ProcessTerminationReason.TIMEOUT
        if not open_fds:
            return (
                None,
                "Bubblewrap exited before approved target exec was confirmed",
                ProcessTerminationReason.SPAWN_ERROR,
            )

        timeout = min(0.05, max(0.0, deadline - now))
        try:
            readable, _, _ = select.select(list(open_fds), [], [], timeout)
        except (OSError, ValueError) as exc:
            return (
                None,
                f"Approved target exec handshake failed: {exc}",
                ProcessTerminationReason.SPAWN_ERROR,
            )

        if not readable:
            continue

        for fd in readable:
            try:
                data = os.read(fd, 4096)
            except OSError as exc:
                return (
                    None,
                    f"Approved target exec handshake read failed: {exc}",
                    ProcessTerminationReason.SPAWN_ERROR,
                )

            if not data:
                open_fds.discard(fd)
                if fd == exec_status_read:
                    exec_eof = True
                continue

            if fd == json_status_read:
                json_buffer.extend(data)
                while b"\n" in json_buffer:
                    raw_line, _, remainder = json_buffer.partition(b"\n")
                    json_buffer = bytearray(remainder)
                    try:
                        value = json.loads(raw_line.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        continue
                    if isinstance(value, dict) and type(value.get("child-pid")) is int:
                        child_pid = int(value["child-pid"])
            else:
                exec_buffer.extend(data)
                while b"\n" in exec_buffer:
                    raw_line, _, remainder = exec_buffer.partition(b"\n")
                    exec_buffer = bytearray(remainder)
                    if raw_line == _EXEC_READY:
                        trampoline_ready = True
                    elif raw_line.startswith(_EXEC_ERROR_PREFIX):
                        return (
                            None,
                            _decode_exec_error(raw_line),
                            ProcessTerminationReason.SPAWN_ERROR,
                        )


def _record_failure(
    *,
    lifecycle: ActionLifecycle,
    receipt,
    execution_id: str,
    started_at: float,
    plan: _base._ValidatedPlan,
    actual_argv: tuple[str, ...],
    reason: ProcessTerminationReason,
    started: bool,
    pid: int | None,
    exit_code: int | None,
    error: str | None,
    stdout: StreamObservation | None = None,
    stderr: StreamObservation | None = None,
) -> ProcessExecutionObservation:
    lifecycle.record_executed(execution_id)
    empty = _empty_stream_observation()
    observation = ProcessExecutionObservation.create(
        proposal_id=lifecycle.proposal.proposal_id,
        proposal_digest=lifecycle.proposal.proposal_digest,
        receipt_id=receipt.receipt_id,
        receipt_digest=receipt.receipt_digest,
        execution_id=execution_id,
        started=started,
        pid=pid,
        cwd=plan.cwd_relative,
        resolved_executable=str(plan.executable),
        argv=actual_argv,
        exit_code=exit_code,
        termination_reason=reason,
        duration_ms=max(0, int((time.monotonic() - started_at) * 1000)),
        stdout=stdout or empty,
        stderr=stderr or empty,
        error=error,
    )
    lifecycle.record_observed(observation.observation_id)
    return observation


class ProcessExecutor:
    """Human-authorized process executor with Linux/Windows PID-tree containment."""

    def execute(
        self,
        lifecycle: ActionLifecycle,
        *,
        authority: LocalApprovalAuthority,
        stdin_bytes: bytes | None = None,
    ) -> ProcessExecutionObservation:
        if lifecycle.phase is not ActionPhase.AUTHORIZED:
            raise InvalidActionTransitionError(
                "Process execution requires an AUTHORIZED lifecycle"
            )
        if lifecycle.authorization is None:
            raise InvalidActionTransitionError("Authorized process has no receipt")

        plan = _validate_process_proposal(lifecycle.proposal)
        if stdin_bytes is not None:
            profile = lifecycle.proposal.to_dict()["parameters"].get("shell_profile")
            if (
                os.name != "nt"
                or profile != POWERSHELL_PROFILE
                or type(stdin_bytes) is not bytes
                or not 0 < len(stdin_bytes) <= 8192
                or not stdin_bytes.endswith(b"\n")
            ):
                raise InvalidProcessSpecError(
                    "bounded stdin is allowed only for the explicit Windows PowerShell profile"
                )
        if os.name != "nt" and not sys.platform.startswith("linux"):
            raise ProcessExecutionError(
                "M2.1 descendant-tree containment is supported only on Linux and Windows"
            )

        bwrap: Path | None = None
        if sys.platform.startswith("linux"):
            bwrap = _linux_bwrap()

        receipt = lifecycle.authorization
        execution_id = str(uuid4())
        started_at = time.monotonic()
        execution_deadline = started_at + plan.limits.timeout_seconds
        actual_argv = (str(plan.executable), *plan.argv[1:])
        windows_job: WindowsJobObject | None = None
        windows_assigned = False
        target_started = False
        process: subprocess.Popen[bytes] | None = None
        observed_pid: int | None = None
        json_status_read: int | None = None
        json_status_write: int | None = None
        exec_status_read: int | None = None
        exec_status_write: int | None = None

        try:
            if os.name == "nt":
                try:
                    windows_job = WindowsJobObject()
                except OSError as exc:
                    raise ProcessExecutionError(
                        f"Windows process-tree ownership could not be established: {exc}"
                    ) from exc

            lifecycle.consume_authorization(authority=authority)

            if os.name == "nt":
                creationflags = (
                    getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                    | getattr(subprocess, "CREATE_SUSPENDED", 0x00000004)
                )
                try:
                    process = subprocess.Popen(
                        list(actual_argv),
                        cwd=str(plan.cwd),
                        env=plan.environment,
                        stdin=subprocess.PIPE if stdin_bytes is not None else subprocess.DEVNULL,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        shell=False,
                        creationflags=creationflags,
                    )
                    if windows_job is None:  # pragma: no cover
                        raise ProcessExecutionError("Windows job was not created")
                    windows_job.assign(int(getattr(process, "_handle")))
                    windows_assigned = True
                    windows_job.resume(process.pid)
                    target_started = True
                    observed_pid = process.pid
                    if stdin_bytes is not None:
                        if process.stdin is None:  # pragma: no cover
                            raise ProcessExecutionError("PowerShell input pipe is missing")
                        try:
                            process.stdin.write(stdin_bytes)
                            process.stdin.flush()
                        except BrokenPipeError:
                            # The child may already have exited. Preserve its
                            # exit status instead of replaying the input.
                            pass
                        finally:
                            process.stdin.close()
                except OSError as exc:
                    if process is not None:
                        if windows_assigned:
                            _terminate_windows_tree(process, windows_job)
                        else:
                            _terminate_unowned_windows_tree(process)
                        _wait_for_process_shutdown(process)
                    return _record_failure(
                        lifecycle=lifecycle,
                        receipt=receipt,
                        execution_id=execution_id,
                        started_at=started_at,
                        plan=plan,
                        actual_argv=actual_argv,
                        reason=ProcessTerminationReason.SPAWN_ERROR,
                        started=target_started,
                        pid=process.pid if process is not None else None,
                        exit_code=process.returncode if process is not None else None,
                        error=f"{type(exc).__name__}: {exc}",
                    )
            else:
                assert bwrap is not None
                try:
                    json_status_read, json_status_write = os.pipe()
                    exec_status_read, exec_status_write = os.pipe()
                except OSError as exc:
                    return _record_failure(
                        lifecycle=lifecycle,
                        receipt=receipt,
                        execution_id=execution_id,
                        started_at=started_at,
                        plan=plan,
                        actual_argv=actual_argv,
                        reason=ProcessTerminationReason.SPAWN_ERROR,
                        started=False,
                        pid=None,
                        exit_code=None,
                        error=f"{type(exc).__name__}: {exc}",
                    )

                sandbox_argv = _linux_sandbox_argv(
                    plan,
                    bwrap,
                    json_status_fd=json_status_write,
                    exec_status_fd=exec_status_write,
                )
                try:
                    process = subprocess.Popen(
                        list(sandbox_argv),
                        cwd=str(plan.cwd),
                        env=plan.environment,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        shell=False,
                        start_new_session=True,
                        close_fds=True,
                        pass_fds=(json_status_write, exec_status_write),
                    )
                except OSError as exc:
                    return _record_failure(
                        lifecycle=lifecycle,
                        receipt=receipt,
                        execution_id=execution_id,
                        started_at=started_at,
                        plan=plan,
                        actual_argv=actual_argv,
                        reason=ProcessTerminationReason.SPAWN_ERROR,
                        started=False,
                        pid=None,
                        exit_code=None,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                finally:
                    for fd_name in ("json_status_write", "exec_status_write"):
                        fd = locals()[fd_name]
                        if fd is not None:
                            try:
                                os.close(fd)
                            except OSError:
                                pass
                            if fd_name == "json_status_write":
                                json_status_write = None
                            else:
                                exec_status_write = None

            if process is None:  # pragma: no cover
                raise ProcessExecutionError("Process creation did not return a process")
            if process.stdout is None or process.stderr is None:  # pragma: no cover
                if os.name == "nt":
                    if windows_assigned:
                        _terminate_windows_tree(process, windows_job)
                    else:
                        _terminate_unowned_windows_tree(process)
                else:
                    _terminate_linux_sandbox(process)
                raise ProcessExecutionError("Process pipes were not created")

            lifecycle.record_executed(execution_id)
            exceeded = threading.Event()
            stdout = _base._StreamCollector(plan.limits.max_stdout_bytes, exceeded)
            stderr = _base._StreamCollector(plan.limits.max_stderr_bytes, exceeded)
            threads = [
                threading.Thread(target=stdout.read, args=(process.stdout,), daemon=True),
                threading.Thread(target=stderr.read, args=(process.stderr,), daemon=True),
            ]
            for thread in threads:
                thread.start()

            if os.name != "nt":
                if json_status_read is None or exec_status_read is None:  # pragma: no cover
                    raise ProcessExecutionError("Linux exec-status pipes were not created")
                startup_deadline = min(
                    execution_deadline,
                    time.monotonic() + _LINUX_STARTUP_MAX_SECONDS,
                )
                observed_pid, startup_error, startup_reason = _await_linux_target_exec(
                    process,
                    json_status_read=json_status_read,
                    exec_status_read=exec_status_read,
                    deadline=startup_deadline,
                    exceeded=exceeded,
                )
                if startup_reason is not None:
                    _terminate_linux_sandbox(process)
                    _wait_for_process_shutdown(process)
                    for thread in threads:
                        thread.join(timeout=2)
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
                        termination_reason=startup_reason,
                        duration_ms=max(0, int((time.monotonic() - started_at) * 1000)),
                        stdout=stdout.snapshot(),
                        stderr=stderr.snapshot(),
                        error=startup_error,
                    )
                    lifecycle.record_observed(observation.observation_id)
                    return observation
                target_started = True

            reason = ProcessTerminationReason.EXITED
            while True:
                process.poll()
                if os.name == "nt":
                    tree_alive = (
                        windows_job.active_processes() > 0
                        if windows_job is not None
                        else process.poll() is None
                    )
                else:
                    tree_alive = process.poll() is None
                if not tree_alive:
                    break
                if exceeded.is_set():
                    reason = ProcessTerminationReason.OUTPUT_LIMIT
                    if os.name == "nt":
                        _terminate_windows_tree(process, windows_job)
                    else:
                        _terminate_linux_sandbox(process)
                    break
                if time.monotonic() >= execution_deadline:
                    reason = ProcessTerminationReason.TIMEOUT
                    if os.name == "nt":
                        _terminate_windows_tree(process, windows_job)
                    else:
                        _terminate_linux_sandbox(process)
                    break
                time.sleep(0.01)

            _wait_for_process_shutdown(process)
            for thread in threads:
                thread.join(timeout=2)
            if exceeded.is_set() and reason is ProcessTerminationReason.EXITED:
                reason = ProcessTerminationReason.OUTPUT_LIMIT

            observation = ProcessExecutionObservation.create(
                proposal_id=lifecycle.proposal.proposal_id,
                proposal_digest=lifecycle.proposal.proposal_digest,
                receipt_id=receipt.receipt_id,
                receipt_digest=receipt.receipt_digest,
                execution_id=execution_id,
                started=target_started,
                pid=observed_pid,
                cwd=plan.cwd_relative,
                resolved_executable=str(plan.executable),
                argv=actual_argv,
                exit_code=process.returncode,
                termination_reason=reason,
                duration_ms=max(0, int((time.monotonic() - started_at) * 1000)),
                stdout=stdout.snapshot(),
                stderr=stderr.snapshot(),
            )
            lifecycle.record_observed(observation.observation_id)
            return observation
        finally:
            if (
                sys.platform.startswith("linux")
                and process is not None
                and process.poll() is None
            ):
                _terminate_linux_sandbox(process)
                _wait_for_process_shutdown(process)
            for fd in (
                json_status_read,
                json_status_write,
                exec_status_read,
                exec_status_write,
            ):
                if fd is not None:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
            if process is not None:
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None:
                        try:
                            stream.close()
                        except OSError:
                            pass
            if windows_job is not None:
                windows_job.close()