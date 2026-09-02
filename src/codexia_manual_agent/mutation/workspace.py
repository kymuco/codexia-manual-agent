from __future__ import annotations

import base64
import binascii
import os
import stat
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any
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
    InvalidWorkspaceMutationError,
    WorkspaceMutationBoundaryError,
    WorkspaceMutationPreimageChangedError,
    WorkspaceMutationTargetExistsError,
    WorkspaceMutationTargetMissingError,
)
from codexia_manual_agent.domain.sensitive_paths import is_sensitive_relative_path
from codexia_manual_agent.mutation.bounded_io import hash_bounded_stream
from codexia_manual_agent.mutation.models import (
    MutationOperation,
    MutationTerminationReason,
    PreimageSnapshot,
    PreimageState,
    WorkspaceMutationObservation,
)
from codexia_manual_agent.mutation.parent_anchor import PinnedMutationTarget


CREATE_ACTION = "workspace.create_file.v1"
REPLACE_ACTION = "workspace.replace_file.v1"
MAX_POSTIMAGE_BYTES = 1_048_576
_MAX_PREIMAGE_BYTES = 16_777_216
_PROTECTED_DIRECTORIES = frozenset({".git", ".codexia"})


@dataclass(frozen=True, slots=True)
class _MutationPlan:
    root: Path
    target: str
    target_path: Path
    parent: Path
    operation: MutationOperation
    expected_preimage: PreimageSnapshot
    postimage: bytes
    postimage_sha256: str


@dataclass(slots=True)
class _PendingOutcome:
    observed_preimage: PreimageSnapshot
    reason: MutationTerminationReason
    error: str | None = None


def _workspace_root(workspace: str | Path) -> Path:
    candidate = Path(workspace).expanduser()
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise WorkspaceMutationBoundaryError(
            f"Workspace cannot be resolved: {workspace}"
        ) from exc
    if not resolved.is_dir():
        raise WorkspaceMutationBoundaryError("Workspace root must be a directory")
    return resolved


def _normalize_target(root: Path, target: str | Path) -> tuple[str, Path, Path]:
    supplied = Path(target)
    if supplied.is_absolute():
        raise WorkspaceMutationBoundaryError("Mutation target must be workspace-relative")
    if not supplied.parts or supplied == Path("."):
        raise WorkspaceMutationBoundaryError("Mutation target must name a file")
    if any(part in {"", ".", ".."} for part in supplied.parts):
        raise WorkspaceMutationBoundaryError("Mutation target contains invalid path traversal")

    normalized = Path(*supplied.parts)
    rendered = normalized.as_posix()
    folded_parts = tuple(part.casefold() for part in normalized.parts)
    if any(part in _PROTECTED_DIRECTORIES for part in folded_parts):
        raise WorkspaceMutationBoundaryError(
            "Mutation targets inside .git or .codexia are not allowed"
        )
    if is_sensitive_relative_path(rendered):
        raise WorkspaceMutationBoundaryError(
            f"Sensitive target is excluded from workspace mutation: {rendered}"
        )

    lexical_parent = (
        root.joinpath(*normalized.parts[:-1]) if len(normalized.parts) > 1 else root
    )
    try:
        parent = lexical_parent.resolve(strict=True)
    except OSError as exc:
        raise WorkspaceMutationBoundaryError(
            f"Mutation target parent does not exist: {rendered}"
        ) from exc
    try:
        parent.relative_to(root)
    except ValueError as exc:
        raise WorkspaceMutationBoundaryError("Mutation target parent escapes workspace") from exc
    if not parent.is_dir():
        raise WorkspaceMutationBoundaryError("Mutation target parent must be a directory")

    lexical_absolute = os.path.normcase(os.path.abspath(str(lexical_parent)))
    canonical_parent = os.path.normcase(str(parent))
    if lexical_absolute != canonical_parent:
        raise WorkspaceMutationBoundaryError(
            "Mutation target parent must not traverse a symlink or junction"
        )

    target_path = parent / normalized.name
    return rendered, target_path, parent


def _sha256_bytes(payload: bytes) -> str:
    return sha256(payload).hexdigest()


def _hash_file(path: Path, *, max_bytes: int) -> tuple[int, str, int]:
    try:
        before = path.stat()
    except OSError as exc:
        raise InvalidWorkspaceMutationError(f"Cannot stat mutation target: {path}") from exc
    if before.st_size > max_bytes:
        raise InvalidWorkspaceMutationError(
            f"Mutation preimage exceeds hashing budget ({before.st_size} > {max_bytes})"
        )
    try:
        with path.open("rb") as handle:
            size, digest = hash_bounded_stream(
                handle,
                max_bytes=max_bytes,
                label="Mutation preimage",
            )
        after = path.stat()
    except OSError as exc:
        raise InvalidWorkspaceMutationError(f"Cannot read mutation target: {path}") from exc
    if (
        size != after.st_size
        or before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
        or getattr(before, "st_ino", 0) != getattr(after, "st_ino", 0)
    ):
        raise WorkspaceMutationPreimageChangedError(
            "Mutation target changed while its preimage was being inspected"
        )
    return after.st_size, digest, stat.S_IMODE(after.st_mode)


def _capture_preimage(target_path: Path) -> PreimageSnapshot:
    if not os.path.lexists(target_path):
        return PreimageSnapshot.absent()
    if target_path.is_symlink():
        raise WorkspaceMutationBoundaryError("Mutation target must not be a symlink")
    if not target_path.is_file():
        raise InvalidWorkspaceMutationError("Mutation target must be a regular file")
    size, digest, mode = _hash_file(target_path, max_bytes=_MAX_PREIMAGE_BYTES)
    return PreimageSnapshot.present(size_bytes=size, digest=digest, mode=mode)


def _postimage_payload(content: bytes) -> dict[str, Any]:
    if not isinstance(content, bytes):
        raise TypeError("Workspace mutation content must be bytes")
    if len(content) > MAX_POSTIMAGE_BYTES:
        raise InvalidWorkspaceMutationError(
            f"Mutation postimage exceeds {MAX_POSTIMAGE_BYTES} bytes"
        )
    return {
        "size_bytes": len(content),
        "sha256": _sha256_bytes(content),
        "data_base64": base64.b64encode(content).decode("ascii"),
    }


def prepare_create_proposal(
    *,
    workspace: str | Path,
    target: str | Path,
    content: bytes,
    summary: str | None = None,
) -> ActionProposal:
    root = _workspace_root(workspace)
    rendered, target_path, _ = _normalize_target(root, target)
    preimage = _capture_preimage(target_path)
    if preimage.state is not PreimageState.ABSENT:
        raise WorkspaceMutationTargetExistsError(f"Create target already exists: {rendered}")
    return ActionProposal.create(
        capability=Capability.WRITE_WORKSPACE,
        action=CREATE_ACTION,
        workspace_root=str(root),
        parameters={
            "operation": MutationOperation.CREATE.value,
            "target": rendered,
            "expected_preimage": preimage.to_dict(),
            "postimage": _postimage_payload(content),
        },
        summary=summary or f"Create workspace file {rendered}.",
    )


def prepare_replace_proposal(
    *,
    workspace: str | Path,
    target: str | Path,
    content: bytes,
    summary: str | None = None,
) -> ActionProposal:
    root = _workspace_root(workspace)
    rendered, target_path, _ = _normalize_target(root, target)
    preimage = _capture_preimage(target_path)
    if preimage.state is not PreimageState.PRESENT:
        raise WorkspaceMutationTargetMissingError(f"Replace target does not exist: {rendered}")
    return ActionProposal.create(
        capability=Capability.WRITE_WORKSPACE,
        action=REPLACE_ACTION,
        workspace_root=str(root),
        parameters={
            "operation": MutationOperation.REPLACE.value,
            "target": rendered,
            "expected_preimage": preimage.to_dict(),
            "postimage": _postimage_payload(content),
        },
        summary=summary or f"Replace workspace file {rendered}.",
    )


def _decode_postimage(data: Any) -> tuple[bytes, str]:
    if not isinstance(data, dict) or set(data) != {"size_bytes", "sha256", "data_base64"}:
        raise InvalidWorkspaceMutationError("Mutation postimage schema is invalid")
    if type(data["size_bytes"]) is not int or not 0 <= data["size_bytes"] <= MAX_POSTIMAGE_BYTES:
        raise InvalidWorkspaceMutationError("Mutation postimage size is invalid")
    if type(data["sha256"]) is not str or len(data["sha256"]) != 64:
        raise InvalidWorkspaceMutationError("Mutation postimage digest is invalid")
    if type(data["data_base64"]) is not str:
        raise InvalidWorkspaceMutationError("Mutation postimage data must be base64 text")
    try:
        payload = base64.b64decode(data["data_base64"], validate=True)
    except (ValueError, binascii.Error) as exc:
        raise InvalidWorkspaceMutationError("Mutation postimage base64 is invalid") from exc
    if len(payload) != data["size_bytes"] or _sha256_bytes(payload) != data["sha256"]:
        raise InvalidWorkspaceMutationError("Mutation postimage identity does not match payload")
    return payload, data["sha256"]


def _parse_preimage(data: Any) -> PreimageSnapshot:
    if not isinstance(data, dict) or set(data) != {"state", "size_bytes", "sha256", "mode"}:
        raise InvalidWorkspaceMutationError("Mutation preimage schema is invalid")
    try:
        return PreimageSnapshot(
            state=PreimageState(data["state"]),
            size_bytes=data["size_bytes"],
            sha256=data["sha256"],
            mode=data["mode"],
        )
    except (TypeError, ValueError) as exc:
        raise InvalidWorkspaceMutationError("Mutation preimage is invalid") from exc


def _validate_proposal(proposal: ActionProposal) -> _MutationPlan:
    if proposal.capability is not Capability.WRITE_WORKSPACE:
        raise InvalidWorkspaceMutationError("Workspace mutation requires write_workspace capability")
    if proposal.action not in {CREATE_ACTION, REPLACE_ACTION}:
        raise InvalidWorkspaceMutationError("Unsupported workspace mutation action")
    params = proposal.to_dict()["parameters"]
    if set(params) != {"operation", "target", "expected_preimage", "postimage"}:
        raise InvalidWorkspaceMutationError("Workspace mutation proposal schema is invalid")
    try:
        operation = MutationOperation(params["operation"])
    except ValueError as exc:
        raise InvalidWorkspaceMutationError("Unsupported workspace mutation operation") from exc
    expected_action = CREATE_ACTION if operation is MutationOperation.CREATE else REPLACE_ACTION
    if proposal.action != expected_action:
        raise InvalidWorkspaceMutationError("Mutation action and operation disagree")

    root = _workspace_root(proposal.workspace_root)
    if str(root) != proposal.workspace_root:
        raise WorkspaceMutationBoundaryError("Mutation proposal workspace root is not canonical")
    rendered, target_path, parent = _normalize_target(root, params["target"])
    if rendered != params["target"]:
        raise WorkspaceMutationBoundaryError("Mutation proposal target is not canonical")
    expected_preimage = _parse_preimage(params["expected_preimage"])
    if operation is MutationOperation.CREATE and expected_preimage.state is not PreimageState.ABSENT:
        raise InvalidWorkspaceMutationError("Create proposal must bind an absent preimage")
    if operation is MutationOperation.REPLACE and expected_preimage.state is not PreimageState.PRESENT:
        raise InvalidWorkspaceMutationError("Replace proposal must bind a present preimage")
    postimage, postimage_sha256 = _decode_postimage(params["postimage"])
    return _MutationPlan(
        root=root,
        target=rendered,
        target_path=target_path,
        parent=parent,
        operation=operation,
        expected_preimage=expected_preimage,
        postimage=postimage,
        postimage_sha256=postimage_sha256,
    )


def _preimage_reason(
    operation: MutationOperation,
    expected: PreimageSnapshot,
    observed: PreimageSnapshot,
) -> MutationTerminationReason:
    if operation is MutationOperation.CREATE and observed.state is PreimageState.PRESENT:
        return MutationTerminationReason.TARGET_APPEARED
    if operation is MutationOperation.REPLACE and observed.state is PreimageState.ABSENT:
        return MutationTerminationReason.TARGET_DISAPPEARED
    return MutationTerminationReason.PREIMAGE_CHANGED


def _record(
    lifecycle: ActionLifecycle,
    *,
    mutation_id: str,
    plan: _MutationPlan,
    observed_preimage: PreimageSnapshot,
    applied: bool,
    reason: MutationTerminationReason,
    receipt,
    postimage_size_bytes: int | None = None,
    postimage_sha256: str | None = None,
    error: str | None = None,
) -> WorkspaceMutationObservation:
    observation = WorkspaceMutationObservation.create(
        proposal_id=lifecycle.proposal.proposal_id,
        proposal_digest=lifecycle.proposal.proposal_digest,
        receipt_id=receipt.receipt_id,
        receipt_digest=receipt.receipt_digest,
        mutation_id=mutation_id,
        operation=plan.operation,
        target=plan.target,
        expected_preimage=plan.expected_preimage,
        observed_preimage=observed_preimage,
        applied=applied,
        postimage_size_bytes=postimage_size_bytes,
        postimage_sha256=postimage_sha256,
        termination_reason=reason,
        error=error,
    )
    lifecycle.record_observed(observation.observation_id)
    return observation


def _failure_reason(exc: Exception) -> MutationTerminationReason:
    if isinstance(exc, WorkspaceMutationBoundaryError):
        return MutationTerminationReason.BOUNDARY_CHANGED
    return MutationTerminationReason.WRITE_ERROR


def _inspection_failure(
    lifecycle: ActionLifecycle,
    *,
    mutation_id: str,
    plan: _MutationPlan,
    receipt,
    exc: Exception,
) -> WorkspaceMutationObservation:
    return _record(
        lifecycle,
        mutation_id=mutation_id,
        plan=plan,
        observed_preimage=plan.expected_preimage,
        applied=False,
        reason=_failure_reason(exc),
        receipt=receipt,
        error=f"post-consumption inspection failed: {type(exc).__name__}: {exc}",
    )


def _pending_inspection_failure(
    plan: _MutationPlan,
    exc: Exception,
) -> _PendingOutcome:
    return _PendingOutcome(
        observed_preimage=plan.expected_preimage,
        reason=_failure_reason(exc),
        error=f"post-consumption inspection failed: {type(exc).__name__}: {exc}",
    )


def _append_error(existing: str | None, extra: str) -> str:
    return extra if existing is None else f"{existing}; {extra}"


def _staging_mode(plan: _MutationPlan) -> int:
    if plan.operation is MutationOperation.CREATE:
        return 0o644
    if plan.expected_preimage.mode is None:
        raise InvalidWorkspaceMutationError("Replace preimage is missing its permission mode")
    return plan.expected_preimage.mode


class WorkspaceMutationExecutor:
    """Human-authorized create/replace primitive with cleanup-complete observations."""

    def execute(
        self,
        lifecycle: ActionLifecycle,
        *,
        authority: LocalApprovalAuthority,
    ) -> WorkspaceMutationObservation:
        if lifecycle.phase is not ActionPhase.AUTHORIZED:
            raise InvalidActionTransitionError("Workspace mutation requires AUTHORIZED lifecycle")
        if lifecycle.authorization is None:
            raise InvalidActionTransitionError("Authorized workspace mutation has no receipt")

        plan = _validate_proposal(lifecycle.proposal)
        receipt = lifecycle.authorization

        with PinnedMutationTarget(
            root=plan.root,
            parent=plan.parent,
            target_name=plan.target_path.name,
        ) as pinned:
            before_consume = pinned.capture_preimage(max_bytes=_MAX_PREIMAGE_BYTES)
            if before_consume != plan.expected_preimage:
                raise WorkspaceMutationPreimageChangedError(
                    "Workspace mutation preimage changed before authorization consumption"
                )

            mutation_id = str(uuid4())
            lifecycle.consume_authorization(authority=authority)
            lifecycle.record_executed(mutation_id)

            try:
                observed = pinned.capture_preimage(max_bytes=_MAX_PREIMAGE_BYTES)
            except (
                InvalidWorkspaceMutationError,
                WorkspaceMutationBoundaryError,
                WorkspaceMutationPreimageChangedError,
            ) as exc:
                return _inspection_failure(
                    lifecycle,
                    mutation_id=mutation_id,
                    plan=plan,
                    receipt=receipt,
                    exc=exc,
                )
            if observed != plan.expected_preimage:
                return _record(
                    lifecycle,
                    mutation_id=mutation_id,
                    plan=plan,
                    observed_preimage=observed,
                    applied=False,
                    reason=_preimage_reason(plan.operation, plan.expected_preimage, observed),
                    receipt=receipt,
                )

            staged = None
            committed = False
            cleanup_error: str | None = None
            pending: _PendingOutcome | None = None

            try:
                staged = pinned.write_temp(plan.postimage, mode=_staging_mode(plan))

                try:
                    observed = pinned.capture_preimage(max_bytes=_MAX_PREIMAGE_BYTES)
                except (
                    InvalidWorkspaceMutationError,
                    WorkspaceMutationBoundaryError,
                    WorkspaceMutationPreimageChangedError,
                ) as exc:
                    pending = _pending_inspection_failure(plan, exc)
                else:
                    if observed != plan.expected_preimage:
                        pending = _PendingOutcome(
                            observed_preimage=observed,
                            reason=_preimage_reason(
                                plan.operation,
                                plan.expected_preimage,
                                observed,
                            ),
                        )

                if pending is None:
                    if plan.operation is MutationOperation.CREATE:
                        try:
                            pinned.commit_create(staged)
                        except FileExistsError:
                            try:
                                observed = pinned.capture_preimage(
                                    max_bytes=_MAX_PREIMAGE_BYTES
                                )
                            except (
                                InvalidWorkspaceMutationError,
                                WorkspaceMutationBoundaryError,
                                WorkspaceMutationPreimageChangedError,
                            ) as exc:
                                pending = _pending_inspection_failure(plan, exc)
                            else:
                                pending = _PendingOutcome(
                                    observed_preimage=observed,
                                    reason=MutationTerminationReason.TARGET_APPEARED,
                                )
                        else:
                            committed = True
                    else:
                        pinned.commit_replace(staged)
                        committed = True

                if committed:
                    try:
                        pinned.close_staged(staged)
                    except OSError as exc:
                        cleanup_error = _append_error(
                            cleanup_error,
                            "staging handle cleanup failed after commit: "
                            f"{type(exc).__name__}: {exc}",
                        )
                    finally:
                        staged = None
                    pinned.fsync_parent()
            except (
                InvalidWorkspaceMutationError,
                WorkspaceMutationBoundaryError,
                WorkspaceMutationPreimageChangedError,
                OSError,
            ) as exc:
                if committed:
                    cleanup_error = _append_error(
                        cleanup_error,
                        f"post-commit housekeeping failed: {type(exc).__name__}: {exc}",
                    )
                elif pending is None:
                    pending = _PendingOutcome(
                        observed_preimage=observed,
                        reason=_failure_reason(exc),
                        error=f"{type(exc).__name__}: {exc}",
                    )
            finally:
                if staged is not None:
                    try:
                        if committed:
                            pinned.close_staged(staged)
                        else:
                            pinned.discard_staged(staged)
                    except OSError as exc:
                        cleanup_error = _append_error(
                            cleanup_error,
                            f"staging cleanup failed: {type(exc).__name__}: {exc}",
                        )
                    finally:
                        staged = None

            if pending is not None:
                error = pending.error
                if cleanup_error:
                    error = _append_error(error, cleanup_error)
                return _record(
                    lifecycle,
                    mutation_id=mutation_id,
                    plan=plan,
                    observed_preimage=pending.observed_preimage,
                    applied=False,
                    reason=pending.reason,
                    receipt=receipt,
                    error=error,
                )

            if not committed:
                return _record(
                    lifecycle,
                    mutation_id=mutation_id,
                    plan=plan,
                    observed_preimage=observed,
                    applied=False,
                    reason=MutationTerminationReason.WRITE_ERROR,
                    receipt=receipt,
                    error=_append_error(
                        cleanup_error,
                        "Mutation ended without a commit or an explicit abort outcome",
                    ),
                )

            try:
                post = pinned.capture_preimage(max_bytes=_MAX_PREIMAGE_BYTES)
            except (
                InvalidWorkspaceMutationError,
                WorkspaceMutationBoundaryError,
                WorkspaceMutationPreimageChangedError,
            ) as exc:
                error = str(exc)
                if cleanup_error:
                    error = _append_error(cleanup_error, error)
                return _record(
                    lifecycle,
                    mutation_id=mutation_id,
                    plan=plan,
                    observed_preimage=observed,
                    applied=True,
                    reason=MutationTerminationReason.POSTIMAGE_MISMATCH,
                    receipt=receipt,
                    error=error,
                )
            if (
                post.state is not PreimageState.PRESENT
                or post.size_bytes != len(plan.postimage)
                or post.sha256 != plan.postimage_sha256
            ):
                return _record(
                    lifecycle,
                    mutation_id=mutation_id,
                    plan=plan,
                    observed_preimage=observed,
                    applied=True,
                    reason=MutationTerminationReason.POSTIMAGE_MISMATCH,
                    receipt=receipt,
                    postimage_size_bytes=post.size_bytes,
                    postimage_sha256=post.sha256,
                    error=cleanup_error,
                )
            return _record(
                lifecycle,
                mutation_id=mutation_id,
                plan=plan,
                observed_preimage=observed,
                applied=True,
                reason=MutationTerminationReason.APPLIED,
                receipt=receipt,
                postimage_size_bytes=post.size_bytes,
                postimage_sha256=post.sha256,
                error=cleanup_error,
            )