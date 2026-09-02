from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from codexia_manual_agent.authority import ActionLifecycle, ActionProposal, LocalApprovalAuthority
from codexia_manual_agent.domain.capabilities import Capability
from codexia_manual_agent.domain.errors import (
    GitMutationExecutionError,
    GitMutationPreconditionChangedError,
    GitRepositoryBoundaryError,
    InvalidGitMutationError,
)
from codexia_manual_agent.git_mutation.models import (
    GIT_MUTATION_SCHEMA_VERSION,
    GIT_PUSH_ACTION,
    GitMutationOutcome,
    GitPushApprovalPreview,
    GitPushObservation,
    GitPushPreparation,
)
from codexia_manual_agent.git_mutation.repository import (
    GIT_PUSH_TIMEOUT_SECONDS,
    MAX_GIT_CONFIG_BYTES,
    decode_line,
    read_local_config_identity,
    require_sha256,
    revalidate_git_executable,
    run_git,
    sha256_bytes,
    snapshot_repository,
    validate_head_ref,
    validate_oid,
    validate_remote_name,
    validate_remote_url,
)
from codexia_manual_agent.git_mutation.windows_namespace import WindowsGitNamespacePin


GIT_FILE_PUSH_BACKEND = "file-pack-update-ref.v1"
MAX_PUSH_PACK_BYTES = 64 * 1024 * 1024


def _norm(path: Path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def _is_link_like(path: Path) -> bool:
    if path.is_symlink():
        return True
    junction = getattr(path, "is_junction", None)
    return bool(junction is not None and junction())


def _require_real(path: Path, *, label: str, directory: bool) -> Path:
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise GitRepositoryBoundaryError(f"{label} does not resolve") from exc
    if _is_link_like(path) or _norm(resolved) != _norm(path):
        raise GitRepositoryBoundaryError(
            f"{label} cannot be a symlink, junction, or redirected path"
        )
    if directory and not path.is_dir():
        raise GitRepositoryBoundaryError(f"{label} must be a directory")
    if not directory and not path.is_file():
        raise GitRepositoryBoundaryError(f"{label} must be a regular file")
    return resolved


def _path_identity(path: Path) -> dict[str, int]:
    try:
        stat = path.stat()
    except OSError as exc:
        raise GitRepositoryBoundaryError(f"Git path identity cannot be read: {path}") from exc
    if not stat.st_ino:
        raise GitRepositoryBoundaryError("Git path identity requires a stable file id")
    return {"st_dev": int(stat.st_dev), "st_ino": int(stat.st_ino)}


def _validate_path_identity(value: Any, label: str) -> dict[str, int]:
    if not isinstance(value, dict) or set(value) != {"st_dev", "st_ino"}:
        raise InvalidGitMutationError(f"{label} identity schema mismatch")
    if any(type(value[key]) is not int or value[key] < 0 for key in value):
        raise InvalidGitMutationError(f"{label} identity is invalid")
    if value["st_ino"] == 0:
        raise InvalidGitMutationError(f"{label} identity lacks a stable file id")
    return value


def _file_remote_path(remote_url: str) -> Path:
    validate_remote_url(remote_url)
    parsed = urlparse(remote_url)
    if parsed.scheme != "file":
        raise InvalidGitMutationError(
            "M2.5 v1 executes only local bare file:// pushes; network push is M2.5.1"
        )
    raw_path = unquote(parsed.path)
    if os.name == "nt" and re.fullmatch(r"/[A-Za-z]:/.*", raw_path):
        raw_path = raw_path[1:]
    lexical = Path(raw_path)
    return _require_real(lexical, label="Git file remote", directory=True)


def _ref_parent(git_dir: Path, ref: str) -> Path:
    relative = ref.removeprefix("refs/")
    return git_dir.joinpath("refs", *relative.split("/")[:-1])


def _log_ref_parent(git_dir: Path, ref: str) -> Path | None:
    relative = ref.removeprefix("refs/")
    candidate = git_dir.joinpath("logs", "refs", *relative.split("/")[:-1])
    return candidate if candidate.is_dir() else None


def _remote_ref_storage(remote_path: Path, destination_ref: str) -> None:
    relative = destination_ref.removeprefix("refs/")
    parts = relative.split("/")
    current = remote_path / "refs"
    _require_real(current, label="Git file remote refs directory", directory=True)
    for part in parts[:-1]:
        current = current / part
        _require_real(current, label="Git file remote ref parent", directory=True)
    leaf = remote_path.joinpath("refs", *parts)
    if leaf.exists() or leaf.is_symlink():
        _require_real(leaf, label="Git file remote loose ref", directory=False)


def _read_file(remote_path: Path, relative: str, *, max_bytes: int, label: str) -> tuple[int, str]:
    path = _require_real(remote_path / relative, label=label, directory=False)
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise GitRepositoryBoundaryError(f"{label} cannot be stat'ed") from exc
    if not 1 <= size <= max_bytes:
        raise InvalidGitMutationError(f"{label} is empty or exceeds the M2.5 budget")
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise GitRepositoryBoundaryError(f"{label} cannot be read") from exc
    if len(payload) != size:
        raise GitMutationPreconditionChangedError(f"{label} changed while being read")
    return size, sha256_bytes(payload)


def _run_remote(
    snapshot,
    remote_path: Path,
    args: list[str],
    *,
    input_bytes: bytes | None = None,
    check: bool = True,
):
    return run_git(
        snapshot.git,
        remote_path,
        [f"--git-dir={remote_path}", *args],
        input_bytes=input_bytes,
        timeout=GIT_PUSH_TIMEOUT_SECONDS,
        check=check,
    )


def _zero_oid(snapshot) -> str:
    return "0" * snapshot.oid_length


def _remote_ref_oid(snapshot, remote_path: Path, destination_ref: str) -> str:
    result = _run_remote(
        snapshot,
        remote_path,
        ["rev-parse", "--verify", destination_ref],
        check=False,
    )
    if result.returncode != 0:
        return _zero_oid(snapshot)
    return validate_oid(
        decode_line(result.stdout, "Git file remote object"),
        snapshot.oid_length,
        "Git file remote object",
    )


def _snapshot_file_remote(snapshot, remote_url: str, destination_ref: str) -> dict[str, Any]:
    remote_path = _file_remote_path(remote_url)
    for path, label, directory in (
        (remote_path / "objects", "Git file remote objects directory", True),
        (remote_path / "objects" / "pack", "Git file remote pack directory", True),
        (remote_path / "refs", "Git file remote refs directory", True),
        (remote_path / "HEAD", "Git file remote HEAD", False),
        (remote_path / "config", "Git file remote config", False),
    ):
        _require_real(path, label=label, directory=directory)
    packed_refs = remote_path / "packed-refs"
    if packed_refs.exists() or packed_refs.is_symlink():
        _require_real(packed_refs, label="Git file remote packed-refs", directory=False)
    _remote_ref_storage(remote_path, destination_ref)

    bare = decode_line(
        _run_remote(snapshot, remote_path, ["rev-parse", "--is-bare-repository"]).stdout,
        "Git file remote bare state",
    )
    if bare != "true":
        raise GitRepositoryBoundaryError("M2.5 file push requires a bare destination repository")
    object_format = decode_line(
        _run_remote(snapshot, remote_path, ["rev-parse", "--show-object-format"]).stdout,
        "Git file remote object format",
    )
    if object_format != snapshot.object_format:
        raise InvalidGitMutationError("Local and file-remote Git object formats differ")
    config_size, config_sha256 = _read_file(
        remote_path,
        "config",
        max_bytes=MAX_GIT_CONFIG_BYTES,
        label="Git file remote config",
    )
    expected_oid = _remote_ref_oid(snapshot, remote_path, destination_ref)
    if expected_oid == _zero_oid(snapshot):
        raise InvalidGitMutationError(
            "M2.5 v1 updates only an existing destination branch; branch creation is deferred"
        )
    return {
        "remote_path": str(remote_path),
        "remote_repository_identity": _path_identity(remote_path),
        "remote_object_format": object_format,
        "remote_config_size_bytes": config_size,
        "remote_config_sha256": config_sha256,
        "expected_remote_oid": expected_oid,
    }


def _require_fast_forward(snapshot, expected_oid: str, local_oid: str) -> None:
    if expected_oid == local_oid:
        raise InvalidGitMutationError("Git push proposal would be a no-op")
    present = run_git(
        snapshot.git,
        snapshot.workspace_root,
        ["cat-file", "-e", f"{expected_oid}^{{commit}}"],
        check=False,
    )
    if present.returncode != 0:
        raise InvalidGitMutationError(
            "The current file-remote commit is not present locally; synchronize before push"
        )
    result = run_git(
        snapshot.git,
        snapshot.workspace_root,
        ["merge-base", "--is-ancestor", expected_oid, local_oid],
        check=False,
    )
    if result.returncode == 1:
        raise InvalidGitMutationError("M2.5 v1 refuses a non-fast-forward push proposal")
    if result.returncode != 0:
        raise InvalidGitMutationError("Git could not verify fast-forward ancestry")


def _build_exact_pack(snapshot, local_oid: str, expected_oid: str) -> bytes:
    result = run_git(
        snapshot.git,
        snapshot.workspace_root,
        ["pack-objects", "--stdout", "--revs"],
        input_bytes=f"{local_oid}\n^{expected_oid}\n".encode("ascii"),
        timeout=GIT_PUSH_TIMEOUT_SECONDS,
    )
    payload = result.stdout
    if not 1 <= len(payload) <= MAX_PUSH_PACK_BYTES:
        raise InvalidGitMutationError(
            f"Git push pack must be between 1 and {MAX_PUSH_PACK_BYTES} bytes"
        )
    return payload


def _remote_push_url(snapshot, remote: str) -> str:
    result = run_git(
        snapshot.git,
        snapshot.workspace_root,
        ["remote", "get-url", "--push", remote],
    )
    return validate_remote_url(decode_line(result.stdout, "Git push URL"))


def _preview(
    snapshot,
    remote: str,
    remote_url: str,
    remote_state: dict[str, Any],
    destination_ref: str,
    pack_bytes: bytes,
) -> GitPushApprovalPreview:
    return GitPushApprovalPreview(
        local_oid=snapshot.head_oid,
        local_ref=snapshot.head_ref,
        remote_name=remote,
        remote_url=remote_url,
        remote_path=remote_state["remote_path"],
        destination_ref=destination_ref,
        expected_remote_oid=remote_state["expected_remote_oid"],
        backend=GIT_FILE_PUSH_BACKEND,
        pack_size_bytes=len(pack_bytes),
        pack_sha256=sha256_bytes(pack_bytes),
    )


def prepare_git_push_proposal(
    *,
    workspace: str | Path,
    remote: str,
    destination_ref: str,
) -> GitPushPreparation:
    remote = validate_remote_name(remote)
    destination_ref = validate_head_ref(destination_ref)
    snapshot = snapshot_repository(workspace)
    remote_url = _remote_push_url(snapshot, remote)
    remote_state = _snapshot_file_remote(snapshot, remote_url, destination_ref)
    _require_fast_forward(snapshot, remote_state["expected_remote_oid"], snapshot.head_oid)
    local_config_size, local_config_sha256 = read_local_config_identity(snapshot)
    pack_bytes = _build_exact_pack(
        snapshot,
        snapshot.head_oid,
        remote_state["expected_remote_oid"],
    )

    params = {
        "schema_version": GIT_MUTATION_SCHEMA_VERSION,
        "local_oid": snapshot.head_oid,
        "local_ref": snapshot.head_ref,
        "object_format": snapshot.object_format,
        "remote_name": remote,
        "remote_url": remote_url,
        "remote_path": remote_state["remote_path"],
        "remote_repository_identity": remote_state["remote_repository_identity"],
        "remote_object_format": remote_state["remote_object_format"],
        "remote_config_size_bytes": remote_state["remote_config_size_bytes"],
        "remote_config_sha256": remote_state["remote_config_sha256"],
        "destination_ref": destination_ref,
        "expected_remote_oid": remote_state["expected_remote_oid"],
        "local_config_size_bytes": local_config_size,
        "local_config_sha256": local_config_sha256,
        "backend": GIT_FILE_PUSH_BACKEND,
        "pack_size_bytes": len(pack_bytes),
        "pack_sha256": sha256_bytes(pack_bytes),
        "git_executable": snapshot.git.to_dict(),
    }
    proposal = ActionProposal.create(
        capability=Capability.GIT_PUSH,
        action=GIT_PUSH_ACTION,
        workspace_root=str(snapshot.workspace_root),
        parameters=params,
        summary=(
            f"Install exact pack {params['pack_sha256']} and CAS {destination_ref} from "
            f"{params['expected_remote_oid']} to {snapshot.head_oid} in {remote_url}."
        ),
    )
    return GitPushPreparation(
        proposal=proposal,
        approval_preview=_preview(
            snapshot,
            remote,
            remote_url,
            remote_state,
            destination_ref,
            pack_bytes,
        ),
        pack_bytes=pack_bytes,
    )


def _params(proposal: ActionProposal) -> dict[str, Any]:
    if proposal.capability is not Capability.GIT_PUSH or proposal.action != GIT_PUSH_ACTION:
        raise InvalidGitMutationError("Action proposal is not an M2.5 Git push")
    params = proposal.to_dict()["parameters"]
    expected = {
        "schema_version",
        "local_oid",
        "local_ref",
        "object_format",
        "remote_name",
        "remote_url",
        "remote_path",
        "remote_repository_identity",
        "remote_object_format",
        "remote_config_size_bytes",
        "remote_config_sha256",
        "destination_ref",
        "expected_remote_oid",
        "local_config_size_bytes",
        "local_config_sha256",
        "backend",
        "pack_size_bytes",
        "pack_sha256",
        "git_executable",
    }
    if set(params) != expected or params["schema_version"] != GIT_MUTATION_SCHEMA_VERSION:
        raise InvalidGitMutationError("Git push proposal schema mismatch")
    oid_len = 40 if params["object_format"] == "sha1" else 64 if params["object_format"] == "sha256" else 0
    if not oid_len or params["remote_object_format"] != params["object_format"]:
        raise InvalidGitMutationError("Unsupported or mismatched Git object format")
    validate_oid(params["local_oid"], oid_len, "Git push local object")
    validate_head_ref(params["local_ref"])
    validate_remote_name(params["remote_name"])
    validate_remote_url(params["remote_url"])
    validate_head_ref(params["destination_ref"])
    validate_oid(params["expected_remote_oid"], oid_len, "Git push expected remote object")
    _validate_path_identity(params["remote_repository_identity"], "Remote Git repository")
    if params["backend"] != GIT_FILE_PUSH_BACKEND:
        raise InvalidGitMutationError("Unsupported M2.5 Git push backend")
    for field in ("local_config_size_bytes", "remote_config_size_bytes"):
        if type(params[field]) is not int or not 1 <= params[field] <= MAX_GIT_CONFIG_BYTES:
            raise InvalidGitMutationError(f"{field} is invalid")
    require_sha256(params["local_config_sha256"], "Local Git config digest")
    require_sha256(params["remote_config_sha256"], "Remote Git config digest")
    if type(params["pack_size_bytes"]) is not int or not 1 <= params["pack_size_bytes"] <= MAX_PUSH_PACK_BYTES:
        raise InvalidGitMutationError("Git push pack size is invalid")
    require_sha256(params["pack_sha256"], "Git push pack digest")
    git_identity = params["git_executable"]
    if not isinstance(git_identity, dict) or set(git_identity) != {"path", "size_bytes", "sha256"}:
        raise InvalidGitMutationError("Git executable identity schema mismatch")
    from codexia_manual_agent.git_mutation.repository import parse_git_identity

    params["git_executable"] = parse_git_identity(git_identity)
    return params


def _revalidate(preparation: GitPushPreparation, params: dict[str, Any]):
    snapshot = snapshot_repository(
        preparation.proposal.workspace_root,
        git=params["git_executable"],
    )
    if str(snapshot.workspace_root) != preparation.proposal.workspace_root:
        raise GitRepositoryBoundaryError("Git workspace canonical identity changed")
    revalidate_git_executable(params["git_executable"])
    if (
        snapshot.object_format != params["object_format"]
        or snapshot.head_oid != params["local_oid"]
        or snapshot.head_ref != params["local_ref"]
    ):
        raise GitMutationPreconditionChangedError(
            "Git local ref/object changed before push authorization consumption"
        )
    local_config_size, local_config_sha256 = read_local_config_identity(snapshot)
    if (
        local_config_size != params["local_config_size_bytes"]
        or local_config_sha256 != params["local_config_sha256"]
    ):
        raise GitMutationPreconditionChangedError(
            "Local Git config changed before push authorization consumption"
        )
    remote_url = _remote_push_url(snapshot, params["remote_name"])
    if remote_url != params["remote_url"]:
        raise GitMutationPreconditionChangedError(
            "Git push remote URL changed before authorization consumption"
        )
    remote_state = _snapshot_file_remote(snapshot, remote_url, params["destination_ref"])
    for key in (
        "remote_path",
        "remote_repository_identity",
        "remote_object_format",
        "remote_config_size_bytes",
        "remote_config_sha256",
        "expected_remote_oid",
    ):
        if remote_state[key] != params[key]:
            raise GitMutationPreconditionChangedError(
                f"Git file remote {key} changed before authorization consumption"
            )
    _require_fast_forward(snapshot, params["expected_remote_oid"], params["local_oid"])
    if (
        len(preparation.pack_bytes) != params["pack_size_bytes"]
        or sha256_bytes(preparation.pack_bytes) != params["pack_sha256"]
    ):
        raise InvalidGitMutationError("Held Git push pack does not match the proposal")
    canonical_preview = _preview(
        snapshot,
        params["remote_name"],
        remote_url,
        remote_state,
        params["destination_ref"],
        preparation.pack_bytes,
    )
    if canonical_preview.to_dict() != preparation.approval_preview.to_dict():
        raise InvalidGitMutationError(
            "Displayed Git push preview does not match the exact proposal state"
        )
    return snapshot, Path(params["remote_path"])


def _pin_directories(remote_path: Path, destination_ref: str) -> tuple[Path, ...]:
    paths = [
        remote_path,
        remote_path / "objects",
        remote_path / "objects" / "pack",
        _ref_parent(remote_path, destination_ref),
    ]
    log_parent = _log_ref_parent(remote_path, destination_ref)
    if log_parent is not None:
        paths.append(log_parent)
    return tuple(paths)


def _observe_remote(snapshot, remote_path: Path, destination_ref: str) -> str | None:
    try:
        return _remote_ref_oid(snapshot, remote_path, destination_ref)
    except Exception:
        return None


def _append_error(existing: str | None, extra: str | None) -> str | None:
    if not extra:
        return existing
    return extra if not existing else f"{existing}; {extra}"


def execute_git_push(
    preparation: GitPushPreparation,
    *,
    lifecycle: ActionLifecycle,
    authority: LocalApprovalAuthority,
) -> GitPushObservation:
    if not isinstance(preparation, GitPushPreparation):
        raise TypeError("preparation must be GitPushPreparation")
    if lifecycle.proposal != preparation.proposal:
        raise InvalidGitMutationError("Lifecycle is not bound to the Git push proposal")
    params = _params(preparation.proposal)

    pre_snapshot, pre_remote_path = _revalidate(preparation, params)
    pin = WindowsGitNamespacePin.acquire(
        _pin_directories(pre_remote_path, params["destination_ref"]),
        locked_files=(
            pre_remote_path / "config",
            Path(params["git_executable"].path),
        ),
    )
    execution_id: str | None = None
    error: str | None = None
    outcome = GitMutationOutcome.INCOMPLETE
    observed: str | None = None
    try:
        snapshot, remote_path = _revalidate(preparation, params)
        lifecycle.consume_authorization(authority=authority)
        execution_id = lifecycle.record_executed()
        try:
            _run_remote(
                snapshot,
                remote_path,
                ["index-pack", "--stdin"],
                input_bytes=preparation.pack_bytes,
            )
            object_probe = _run_remote(
                snapshot,
                remote_path,
                ["cat-file", "-e", f"{params['local_oid']}^{{commit}}"],
                check=False,
            )
            if object_probe.returncode != 0:
                raise GitMutationExecutionError(
                    "Installed Git push pack does not expose the exact approved commit"
                )
            update = _run_remote(
                snapshot,
                remote_path,
                [
                    "update-ref",
                    params["destination_ref"],
                    params["local_oid"],
                    params["expected_remote_oid"],
                ],
                check=False,
            )
            observed = _observe_remote(snapshot, remote_path, params["destination_ref"])
            if observed == params["local_oid"]:
                outcome = GitMutationOutcome.APPLIED
            elif update.returncode != 0 and observed is not None:
                outcome = GitMutationOutcome.REJECTED
                error = update.stderr.decode("utf-8", errors="replace")[:4096]
            elif update.returncode == 0:
                outcome = GitMutationOutcome.MISMATCH
            else:
                outcome = GitMutationOutcome.INCOMPLETE
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            observed = _observe_remote(snapshot, remote_path, params["destination_ref"])
            if observed == params["local_oid"]:
                outcome = GitMutationOutcome.APPLIED
            else:
                outcome = GitMutationOutcome.INCOMPLETE
    finally:
        cleanup_error = pin.close()
        error = _append_error(error, cleanup_error)

    if execution_id is None:
        raise GitMutationExecutionError(
            error or "Git push execution did not reach authorization consumption"
        )

    observation = GitPushObservation(
        execution_id=execution_id,
        proposal_id=preparation.proposal.proposal_id,
        proposal_digest=preparation.proposal.proposal_digest,
        outcome=outcome,
        local_oid=params["local_oid"],
        remote_url=params["remote_url"],
        remote_path=params["remote_path"],
        destination_ref=params["destination_ref"],
        expected_remote_oid=params["expected_remote_oid"],
        observed_remote_oid=observed,
        backend=params["backend"],
        pack_size_bytes=params["pack_size_bytes"],
        pack_sha256=params["pack_sha256"],
        error=error,
    )
    lifecycle.record_observed()
    return observation
