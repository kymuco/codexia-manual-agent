from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from codexia_manual_agent.authority import ActionLifecycle, ActionProposal, LocalApprovalAuthority
from codexia_manual_agent.domain.capabilities import Capability
from codexia_manual_agent.domain.errors import (
    GitMutationExecutionError,
    GitMutationPreconditionChangedError,
    GitRepositoryBoundaryError,
    InvalidGitMutationError,
)
from codexia_manual_agent.git_mutation.models import (
    GIT_COMMIT_ACTION,
    GIT_MUTATION_SCHEMA_VERSION,
    GitCommitApprovalPreview,
    GitCommitObservation,
    GitCommitPreparation,
    GitMutationOutcome,
)
from codexia_manual_agent.git_mutation.repository import (
    MAX_INDEX_BYTES,
    base_env,
    decode_line,
    git_config_value,
    parse_git_identity,
    read_index,
    require_sha256,
    revalidate_git_executable,
    run_git,
    sha256_bytes,
    snapshot_repository,
    staged_diff,
    validate_commit_message,
    validate_head_ref,
    validate_oid,
)
from codexia_manual_agent.git_mutation.windows_namespace import WindowsGitNamespacePin


GIT_COMMIT_BACKEND = "frozen-pack-update-ref.v1"
MAX_COMMIT_PACK_BYTES = 64 * 1024 * 1024


def _timestamp_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _validate_timestamp(value: Any) -> str:
    if not isinstance(value, str):
        raise InvalidGitMutationError("Git commit timestamp must be ISO-8601 text")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise InvalidGitMutationError("Git commit timestamp must be ISO-8601 text") from exc
    if parsed.tzinfo is None:
        raise InvalidGitMutationError("Git commit timestamp must include a timezone")
    return value


def _validate_identity(name: Any, email: Any) -> tuple[str, str]:
    if not isinstance(name, str) or not name.strip() or any(char in name for char in "<>\r\n\x00"):
        raise InvalidGitMutationError("Git author_name is invalid")
    if (
        not isinstance(email, str)
        or not email.strip()
        or any(char in email for char in "<> \t\r\n\x00")
    ):
        raise InvalidGitMutationError("Git author_email is invalid")
    return name, email


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


def _ref_parent(git_dir: Path, ref: str) -> Path:
    relative = ref.removeprefix("refs/")
    return git_dir.joinpath("refs", *relative.split("/")[:-1])


def _log_ref_parent(git_dir: Path, ref: str) -> Path | None:
    relative = ref.removeprefix("refs/")
    candidate = git_dir.joinpath("logs", "refs", *relative.split("/")[:-1])
    return candidate if candidate.is_dir() else None


def _pin_directories(snapshot) -> tuple[Path, ...]:
    paths = [
        snapshot.git_dir,
        snapshot.git_dir / "objects",
        snapshot.git_dir / "objects" / "pack",
        _ref_parent(snapshot.git_dir, snapshot.head_ref),
    ]
    log_parent = _log_ref_parent(snapshot.git_dir, snapshot.head_ref)
    if log_parent is not None:
        paths.append(log_parent)
    return tuple(paths)


def _preview(
    *,
    snapshot,
    index_payload: bytes,
    entries,
    manifest_digest: str,
    diff: str,
    diff_digest: str,
    message: str,
    author_name: str,
    author_email: str,
    commit_timestamp: str,
    tree_oid: str,
    commit_oid: str,
    pack_bytes: bytes,
) -> GitCommitApprovalPreview:
    return GitCommitApprovalPreview(
        head_ref=snapshot.head_ref,
        head_oid=snapshot.head_oid,
        expected_tree_oid=tree_oid,
        expected_commit_oid=commit_oid,
        index_sha256=sha256_bytes(index_payload),
        index_manifest_digest=manifest_digest,
        staged_diff_sha256=diff_digest,
        staged_diff=diff,
        staged_entries=entries,
        message=message,
        author_name=author_name,
        author_email=author_email,
        commit_timestamp=commit_timestamp,
        backend=GIT_COMMIT_BACKEND,
        pack_size_bytes=len(pack_bytes),
        pack_sha256=sha256_bytes(pack_bytes),
    )


def _commit_env(
    params: dict[str, Any],
    *,
    index_file: Path,
    object_directory: Path,
    alternate_objects: Path,
) -> dict[str, str]:
    env = base_env()
    env["GIT_INDEX_FILE"] = str(index_file)
    env["GIT_OBJECT_DIRECTORY"] = str(object_directory)
    env["GIT_ALTERNATE_OBJECT_DIRECTORIES"] = str(alternate_objects)
    env["GIT_AUTHOR_NAME"] = params["author_name"]
    env["GIT_AUTHOR_EMAIL"] = params["author_email"]
    env["GIT_COMMITTER_NAME"] = params["author_name"]
    env["GIT_COMMITTER_EMAIL"] = params["author_email"]
    env["GIT_AUTHOR_DATE"] = params["commit_timestamp"]
    env["GIT_COMMITTER_DATE"] = params["commit_timestamp"]
    env["GIT_CONFIG_COUNT"] = "3"
    env["GIT_CONFIG_KEY_0"] = "core.fsmonitor"
    env["GIT_CONFIG_VALUE_0"] = "false"
    env["GIT_CONFIG_KEY_1"] = "commit.gpgSign"
    env["GIT_CONFIG_VALUE_1"] = "false"
    env["GIT_CONFIG_KEY_2"] = "i18n.commitEncoding"
    env["GIT_CONFIG_VALUE_2"] = "UTF-8"
    return env


def _git_identity_line(kind: str, params: dict[str, Any]) -> bytes:
    parsed = datetime.fromisoformat(params["commit_timestamp"])
    epoch = int(parsed.timestamp())
    offset = parsed.strftime("%z")
    return (
        f"{kind} {params['author_name']} <{params['author_email']}> {epoch} {offset}"
    ).encode("utf-8")


def _verify_commit_object(
    snapshot,
    commit_oid: str,
    tree_oid: str,
    params: dict[str, Any],
    *,
    env: dict[str, str] | None = None,
) -> bool:
    raw = run_git(
        snapshot.git,
        snapshot.workspace_root,
        ["cat-file", "commit", commit_oid],
        env=env,
    ).stdout
    header, separator, message = raw.partition(b"\n\n")
    if separator != b"\n\n" or message != params["message"].encode("utf-8"):
        return False
    lines = header.split(b"\n")
    expected = [
        f"tree {tree_oid}".encode("ascii"),
        f"parent {params['head_oid']}".encode("ascii"),
        _git_identity_line("author", params),
        _git_identity_line("committer", params),
    ]
    return lines == expected


def _parse_index_pack_oid(payload: bytes, oid_length: int) -> str:
    rendered = decode_line(payload, "Installed Git commit pack")
    fields = rendered.split()
    if len(fields) == 2 and fields[0] == "pack":
        candidate = fields[1]
    elif len(fields) == 1:
        candidate = fields[0]
    else:
        raise InvalidGitMutationError("Installed Git commit pack output has an invalid form")
    return validate_oid(candidate, oid_length, "Installed Git commit pack")


def _build_exact_commit_artifacts(
    snapshot,
    index_payload: bytes,
    params: dict[str, Any],
) -> tuple[str, str, bytes]:
    with tempfile.TemporaryDirectory(prefix="codexia-git-commit-build-") as raw:
        root = Path(raw)
        index_file = root / "index"
        object_dir = root / "objects"
        (object_dir / "info").mkdir(parents=True)
        (object_dir / "pack").mkdir()
        index_file.write_bytes(index_payload)
        if sha256_bytes(index_file.read_bytes()) != params["index_sha256"]:
            raise GitMutationExecutionError("Frozen Git index failed exact identity verification")

        env = _commit_env(
            params,
            index_file=index_file,
            object_directory=object_dir,
            alternate_objects=snapshot.git_dir / "objects",
        )
        tree_oid = decode_line(
            run_git(snapshot.git, snapshot.workspace_root, ["write-tree"], env=env).stdout,
            "Git tree object",
        )
        validate_oid(tree_oid, snapshot.oid_length, "Git tree object")
        commit_oid = decode_line(
            run_git(
                snapshot.git,
                snapshot.workspace_root,
                ["commit-tree", tree_oid, "-p", snapshot.head_oid],
                env=env,
                input_bytes=params["message"].encode("utf-8"),
            ).stdout,
            "Git commit object",
        )
        validate_oid(commit_oid, snapshot.oid_length, "Git commit object")
        if not _verify_commit_object(
            snapshot,
            commit_oid,
            tree_oid,
            params,
            env=env,
        ):
            raise GitMutationExecutionError(
                "Constructed temporary Git commit does not match the approved payload"
            )
        pack = run_git(
            snapshot.git,
            snapshot.workspace_root,
            ["pack-objects", "--stdout", "--revs"],
            env=env,
            input_bytes=f"{commit_oid}\n^{snapshot.head_oid}\n".encode("ascii"),
        ).stdout
        if not 1 <= len(pack) <= MAX_COMMIT_PACK_BYTES:
            raise InvalidGitMutationError(
                f"Git commit pack must be between 1 and {MAX_COMMIT_PACK_BYTES} bytes"
            )
        return tree_oid, commit_oid, pack


def prepare_git_commit_proposal(*, workspace: str | Path, message: str) -> GitCommitPreparation:
    message = validate_commit_message(message)
    snapshot = snapshot_repository(workspace)
    index_payload, entries, manifest_digest = read_index(snapshot)
    diff, diff_digest = staged_diff(snapshot)
    author_name, author_email = _validate_identity(
        git_config_value(snapshot, "user.name"),
        git_config_value(snapshot, "user.email"),
    )
    commit_timestamp = _timestamp_now()
    bootstrap = {
        "index_sha256": sha256_bytes(index_payload),
        "message": message,
        "author_name": author_name,
        "author_email": author_email,
        "commit_timestamp": commit_timestamp,
        "head_oid": snapshot.head_oid,
    }
    tree_oid, commit_oid, pack_bytes = _build_exact_commit_artifacts(
        snapshot,
        index_payload,
        bootstrap,
    )

    params = {
        "schema_version": GIT_MUTATION_SCHEMA_VERSION,
        "head_ref": snapshot.head_ref,
        "head_oid": snapshot.head_oid,
        "object_format": snapshot.object_format,
        "repository_identity": _path_identity(snapshot.git_dir),
        "index_size_bytes": len(index_payload),
        "index_sha256": sha256_bytes(index_payload),
        "index_manifest_digest": manifest_digest,
        "staged_diff_sha256": diff_digest,
        "message": message,
        "author_name": author_name,
        "author_email": author_email,
        "commit_timestamp": commit_timestamp,
        "expected_tree_oid": tree_oid,
        "expected_commit_oid": commit_oid,
        "backend": GIT_COMMIT_BACKEND,
        "pack_size_bytes": len(pack_bytes),
        "pack_sha256": sha256_bytes(pack_bytes),
        "git_executable": snapshot.git.to_dict(),
    }
    proposal = ActionProposal.create(
        capability=Capability.GIT_COMMIT,
        action=GIT_COMMIT_ACTION,
        workspace_root=str(snapshot.workspace_root),
        parameters=params,
        summary=(
            f"CAS {snapshot.head_ref} from {snapshot.head_oid} to exact commit {commit_oid} "
            f"built from staged index {params['index_sha256']}."
        ),
    )
    return GitCommitPreparation(
        proposal=proposal,
        approval_preview=_preview(
            snapshot=snapshot,
            index_payload=index_payload,
            entries=entries,
            manifest_digest=manifest_digest,
            diff=diff,
            diff_digest=diff_digest,
            message=message,
            author_name=author_name,
            author_email=author_email,
            commit_timestamp=commit_timestamp,
            tree_oid=tree_oid,
            commit_oid=commit_oid,
            pack_bytes=pack_bytes,
        ),
        pack_bytes=pack_bytes,
    )


def _params(proposal: ActionProposal) -> dict[str, Any]:
    if proposal.capability is not Capability.GIT_COMMIT or proposal.action != GIT_COMMIT_ACTION:
        raise InvalidGitMutationError("Action proposal is not an M2.5 Git commit")
    params = proposal.to_dict()["parameters"]
    expected = {
        "schema_version",
        "head_ref",
        "head_oid",
        "object_format",
        "repository_identity",
        "index_size_bytes",
        "index_sha256",
        "index_manifest_digest",
        "staged_diff_sha256",
        "message",
        "author_name",
        "author_email",
        "commit_timestamp",
        "expected_tree_oid",
        "expected_commit_oid",
        "backend",
        "pack_size_bytes",
        "pack_sha256",
        "git_executable",
    }
    if set(params) != expected or params["schema_version"] != GIT_MUTATION_SCHEMA_VERSION:
        raise InvalidGitMutationError("Git commit proposal schema mismatch")
    validate_head_ref(params["head_ref"])
    oid_len = 40 if params["object_format"] == "sha1" else 64 if params["object_format"] == "sha256" else 0
    if not oid_len:
        raise InvalidGitMutationError("Unsupported Git object format")
    validate_oid(params["head_oid"], oid_len, "Git commit parent")
    validate_oid(params["expected_tree_oid"], oid_len, "Expected Git tree")
    validate_oid(params["expected_commit_oid"], oid_len, "Expected Git commit")
    _validate_path_identity(params["repository_identity"], "Git repository")
    if type(params["index_size_bytes"]) is not int or not 1 <= params["index_size_bytes"] <= MAX_INDEX_BYTES:
        raise InvalidGitMutationError("Git commit index size is invalid")
    require_sha256(params["index_sha256"], "Git index digest")
    require_sha256(params["index_manifest_digest"], "Git index manifest digest")
    require_sha256(params["staged_diff_sha256"], "Git staged diff digest")
    validate_commit_message(params["message"])
    _validate_timestamp(params["commit_timestamp"])
    _validate_identity(params["author_name"], params["author_email"])
    if params["backend"] != GIT_COMMIT_BACKEND:
        raise InvalidGitMutationError("Unsupported M2.5 Git commit backend")
    if type(params["pack_size_bytes"]) is not int or not 1 <= params["pack_size_bytes"] <= MAX_COMMIT_PACK_BYTES:
        raise InvalidGitMutationError("Git commit pack size is invalid")
    require_sha256(params["pack_sha256"], "Git commit pack digest")
    params["git_executable"] = parse_git_identity(params["git_executable"])
    return params


def _revalidate(preparation: GitCommitPreparation, params: dict[str, Any]):
    snapshot = snapshot_repository(
        preparation.proposal.workspace_root,
        git=params["git_executable"],
    )
    if str(snapshot.workspace_root) != preparation.proposal.workspace_root:
        raise GitRepositoryBoundaryError("Git workspace canonical identity changed")
    if _path_identity(snapshot.git_dir) != params["repository_identity"]:
        raise GitMutationPreconditionChangedError(
            "Git repository physical identity changed before authorization consumption"
        )
    if (
        snapshot.head_ref != params["head_ref"]
        or snapshot.head_oid != params["head_oid"]
        or snapshot.object_format != params["object_format"]
    ):
        raise GitMutationPreconditionChangedError(
            "Git HEAD/ref/repository identity changed before authorization consumption"
        )
    revalidate_git_executable(params["git_executable"])
    index_payload, entries, manifest_digest = read_index(snapshot)
    if (
        len(index_payload) != params["index_size_bytes"]
        or sha256_bytes(index_payload) != params["index_sha256"]
        or manifest_digest != params["index_manifest_digest"]
    ):
        raise GitMutationPreconditionChangedError(
            "Git staged index changed before authorization consumption"
        )
    diff, diff_digest = staged_diff(snapshot)
    if diff_digest != params["staged_diff_sha256"]:
        raise GitMutationPreconditionChangedError(
            "Git staged diff changed before authorization consumption"
        )
    author_name, author_email = _validate_identity(
        git_config_value(snapshot, "user.name"),
        git_config_value(snapshot, "user.email"),
    )
    if author_name != params["author_name"] or author_email != params["author_email"]:
        raise GitMutationPreconditionChangedError(
            "Git author identity changed before authorization consumption"
        )
    if (
        len(preparation.pack_bytes) != params["pack_size_bytes"]
        or sha256_bytes(preparation.pack_bytes) != params["pack_sha256"]
    ):
        raise InvalidGitMutationError("Held Git commit pack does not match the proposal")
    tree_oid, commit_oid, _ = _build_exact_commit_artifacts(snapshot, index_payload, params)
    if tree_oid != params["expected_tree_oid"] or commit_oid != params["expected_commit_oid"]:
        raise GitMutationPreconditionChangedError(
            "Exact Git commit identity changed before authorization consumption"
        )
    canonical_preview = _preview(
        snapshot=snapshot,
        index_payload=index_payload,
        entries=entries,
        manifest_digest=manifest_digest,
        diff=diff,
        diff_digest=diff_digest,
        message=params["message"],
        author_name=author_name,
        author_email=author_email,
        commit_timestamp=params["commit_timestamp"],
        tree_oid=params["expected_tree_oid"],
        commit_oid=params["expected_commit_oid"],
        pack_bytes=preparation.pack_bytes,
    )
    if canonical_preview.to_dict() != preparation.approval_preview.to_dict():
        raise InvalidGitMutationError(
            "Displayed Git commit preview does not match the exact proposal state"
        )
    return snapshot, manifest_digest


def _append_error(existing: str | None, extra: str | None) -> str | None:
    if not extra:
        return existing
    return extra if not existing else f"{existing}; {extra}"


def execute_git_commit(
    preparation: GitCommitPreparation,
    *,
    lifecycle: ActionLifecycle,
    authority: LocalApprovalAuthority,
) -> GitCommitObservation:
    if not isinstance(preparation, GitCommitPreparation):
        raise TypeError("preparation must be GitCommitPreparation")
    if lifecycle.proposal != preparation.proposal:
        raise InvalidGitMutationError("Lifecycle is not bound to the Git commit proposal")
    params = _params(preparation.proposal)

    pre_snapshot, pre_manifest_digest = _revalidate(preparation, params)
    pin = WindowsGitNamespacePin.acquire(
        _pin_directories(pre_snapshot),
        locked_files=(
            pre_snapshot.git_dir / "config",
            Path(params["git_executable"].path),
        ),
    )
    execution_id: str | None = None
    outcome = GitMutationOutcome.INCOMPLETE
    observed_head: str | None = None
    error: str | None = None
    manifest_digest = pre_manifest_digest
    try:
        snapshot, manifest_digest = _revalidate(preparation, params)
        lifecycle.consume_authorization(authority=authority)
        execution_id = lifecycle.record_executed()
        try:
            installed = run_git(
                snapshot.git,
                snapshot.workspace_root,
                ["index-pack", "--stdin"],
                input_bytes=preparation.pack_bytes,
            )
            _parse_index_pack_oid(installed.stdout, snapshot.oid_length)
            probe = run_git(
                snapshot.git,
                snapshot.workspace_root,
                ["cat-file", "-e", f"{params['expected_commit_oid']}^{{commit}}"],
                check=False,
            )
            if probe.returncode != 0 or not _verify_commit_object(
                snapshot,
                params["expected_commit_oid"],
                params["expected_tree_oid"],
                params,
            ):
                raise GitMutationExecutionError(
                    "Installed Git commit pack does not expose the exact approved commit"
                )
            update = run_git(
                snapshot.git,
                snapshot.workspace_root,
                [
                    "update-ref",
                    params["head_ref"],
                    params["expected_commit_oid"],
                    params["head_oid"],
                ],
                check=False,
            )
            try:
                observed_head = decode_line(
                    run_git(
                        snapshot.git,
                        snapshot.workspace_root,
                        ["rev-parse", "--verify", params["head_ref"]],
                    ).stdout,
                    "Observed Git HEAD",
                )
            except Exception:
                observed_head = None
            if observed_head == params["expected_commit_oid"]:
                outcome = GitMutationOutcome.APPLIED
            elif update.returncode != 0 and observed_head is not None:
                outcome = GitMutationOutcome.REJECTED
                error = update.stderr.decode("utf-8", errors="replace")[:4096]
            elif update.returncode == 0:
                outcome = GitMutationOutcome.MISMATCH
            else:
                outcome = GitMutationOutcome.INCOMPLETE
                error = update.stderr.decode("utf-8", errors="replace")[:4096]
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            try:
                observed_head = decode_line(
                    run_git(
                        snapshot.git,
                        snapshot.workspace_root,
                        ["rev-parse", "--verify", params["head_ref"]],
                    ).stdout,
                    "Observed Git HEAD",
                )
            except Exception:
                observed_head = None
            if observed_head == params["expected_commit_oid"]:
                outcome = GitMutationOutcome.APPLIED
            else:
                outcome = GitMutationOutcome.INCOMPLETE
    finally:
        cleanup_error = pin.close()
        error = _append_error(error, cleanup_error)

    if execution_id is None:
        # Revalidation/acquisition failures occur before receipt consumption and
        # must remain exceptions rather than pretending execution took place.
        raise GitMutationExecutionError(
            error or "Git commit execution did not reach authorization consumption"
        )

    observation = GitCommitObservation(
        execution_id=execution_id,
        proposal_id=preparation.proposal.proposal_id,
        proposal_digest=preparation.proposal.proposal_digest,
        outcome=outcome,
        head_ref=params["head_ref"],
        previous_head_oid=params["head_oid"],
        expected_commit_oid=params["expected_commit_oid"],
        observed_head_oid=observed_head,
        tree_oid=params["expected_tree_oid"],
        index_manifest_digest=manifest_digest,
        message_sha256=sha256_bytes(params["message"].encode("utf-8")),
        backend=params["backend"],
        pack_size_bytes=params["pack_size_bytes"],
        pack_sha256=params["pack_sha256"],
        error=error,
    )
    lifecycle.record_observed()
    return observation
