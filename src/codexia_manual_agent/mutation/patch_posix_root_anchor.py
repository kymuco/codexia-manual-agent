from __future__ import annotations

import os
import stat
import unicodedata
from collections.abc import Iterable
from pathlib import Path

from codexia_manual_agent.authority import ActionProposal
from codexia_manual_agent.domain.capabilities import Capability
from codexia_manual_agent.domain.errors import (
    InvalidWorkspaceMutationError,
    WorkspaceMutationBoundaryError,
    WorkspaceMutationPreimageChangedError,
    WorkspaceMutationTargetExistsError,
    WorkspaceMutationTargetMissingError,
)
from codexia_manual_agent.mutation import patch_final_review_repairs as _final
from codexia_manual_agent.mutation import patch_hardening as _hard
from codexia_manual_agent.mutation import patch_portability_repairs as _port
from codexia_manual_agent.mutation import patch_review_repairs as _review
from codexia_manual_agent.mutation import patches as _legacy
from codexia_manual_agent.mutation.models import MutationOperation, PreimageSnapshot, PreimageState

_base_prepare_patch_proposal = _port.prepare_patch_proposal
_CASE_PROBE_SCAN_LIMIT = 64


def _stat_identity(value: os.stat_result) -> tuple[int, int]:
    return int(value.st_dev), int(value.st_ino)


def _same_directory(left: os.stat_result, right: os.stat_result) -> bool:
    return _stat_identity(left) == _stat_identity(right)


def _pin_posix_workspace_before_validation(workspace: str | Path) -> tuple[Path, int, os.stat_result]:
    """Pin the workspace inode before path validation can race with replacement.

    The first authority-bearing operation is the directory open itself. We then
    resolve the user's workspace spelling while that handle is retained and prove
    that the resolved canonical path still names the same inode. A symlinked
    workspace spelling is therefore supported without making later target reads
    depend on a mutable path lookup.
    """

    raw = str(Path(workspace).expanduser())
    if "\x00" in raw:
        raise WorkspaceMutationBoundaryError("Patch workspace must not contain NUL bytes")
    absolute_input = Path(os.path.normpath(os.path.abspath(raw)))
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        root_fd = os.open(absolute_input, flags)
    except OSError as exc:
        raise WorkspaceMutationBoundaryError(
            f"Workspace cannot be pinned: {workspace}"
        ) from exc

    try:
        pinned = os.fstat(root_fd)
        if not stat.S_ISDIR(pinned.st_mode):
            raise WorkspaceMutationBoundaryError("Workspace root must be a directory")

        # This preserves the existing canonical workspace rendering, but the
        # result is accepted only if it still identifies the inode pinned first.
        root = _legacy._workspace_root(absolute_input)
        check_fd = _port._open_posix_directory_chain(root)
        try:
            checked = os.fstat(check_fd)
        finally:
            os.close(check_fd)
        if not _same_directory(pinned, checked):
            raise WorkspaceMutationPreimageChangedError(
                "Patch workspace identity changed during validation"
            )
        return root, root_fd, pinned
    except BaseException:
        os.close(root_fd)
        raise


def _open_parent_from_root_fd(root_fd: int, parent_parts: tuple[str, ...]) -> int:
    """Open a target parent only relative to the already-pinned workspace fd."""

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.dup(root_fd)
    except OSError as exc:
        raise WorkspaceMutationBoundaryError("Patch workspace anchor cannot be duplicated") from exc

    try:
        for component in parent_parts:
            try:
                next_fd = os.open(component, flags, dir_fd=fd)
            except OSError as exc:
                raise WorkspaceMutationBoundaryError(
                    "Patch target parent cannot be pinned relative to workspace"
                ) from exc
            os.close(fd)
            fd = next_fd
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode):
            raise WorkspaceMutationBoundaryError("Patch target parent is not a directory")
        return fd
    except BaseException:
        os.close(fd)
        raise


def _probe_parent_case_sensitivity(parent_fd: int) -> bool | None:
    """Infer case behavior using only entries in the pinned parent namespace."""

    try:
        names = os.listdir(parent_fd)
    except OSError:
        return None

    for name in names[:_CASE_PROBE_SCAN_LIMIT]:
        alternate = _review._case_variant(name)
        if alternate is None or alternate == name:
            continue
        try:
            original = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError:
            continue
        try:
            candidate = os.stat(alternate, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return True
        except OSError:
            continue
        return _stat_identity(original) != _stat_identity(candidate)
    return None


def _namespace_key(rendered: str, *, case_sensitive: bool | None) -> str:
    normalized = unicodedata.normalize("NFC", rendered)
    if case_sensitive is True:
        return normalized
    return unicodedata.normalize("NFC", normalized.casefold())


def _capture_preimage_from_parent_fd(
    parent_fd: int,
    *,
    target_name: str,
    max_bytes: int,
) -> tuple[PreimageSnapshot, bytes | None]:
    """Capture exact preimage bytes without reopening any parent by pathname."""

    try:
        before = os.stat(target_name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return PreimageSnapshot.absent(), None
    except OSError as exc:
        raise InvalidWorkspaceMutationError("Cannot stat patch preimage") from exc

    if stat.S_ISLNK(before.st_mode):
        raise WorkspaceMutationBoundaryError("Patch target must not be a symlink")
    if not stat.S_ISREG(before.st_mode):
        raise InvalidWorkspaceMutationError("Patch target must be a regular file")
    if before.st_size > max_bytes:
        raise InvalidWorkspaceMutationError(f"Patch preimage exceeds {max_bytes} bytes")

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(target_name, flags, dir_fd=parent_fd)
    except OSError as exc:
        raise WorkspaceMutationPreimageChangedError(
            "Patch target changed while its preimage was being opened"
        ) from exc

    try:
        opened = os.fstat(fd)
        if not _final._same_file_metadata(before, opened):
            raise WorkspaceMutationPreimageChangedError(
                "Patch target changed while its preimage was being opened"
            )
        payload, digest = _final._read_fd_payload(fd, max_bytes=max_bytes)
        after = os.fstat(fd)
        second_size, second_digest = _final._hash_fd_again(fd, max_bytes=max_bytes)
    finally:
        os.close(fd)

    try:
        entry_after = os.stat(target_name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError as exc:
        raise WorkspaceMutationPreimageChangedError(
            "Patch target disappeared while its preimage was being captured"
        ) from exc
    except OSError as exc:
        raise InvalidWorkspaceMutationError("Cannot re-stat patch preimage") from exc

    if (
        not _final._same_file_metadata(opened, after)
        or after.st_dev != entry_after.st_dev
        or after.st_ino != entry_after.st_ino
        or after.st_size != entry_after.st_size
        or len(payload) != after.st_size
        or second_size != after.st_size
        or digest != second_digest
    ):
        raise WorkspaceMutationPreimageChangedError(
            "Patch target changed while exact preimage bytes were being captured"
        )

    return (
        PreimageSnapshot.present(
            size_bytes=after.st_size,
            digest=digest,
            mode=stat.S_IMODE(after.st_mode),
        ),
        payload,
    )


def _verify_parent_still_names_anchor(
    root_fd: int,
    parent_parts: tuple[str, ...],
    pinned_parent: os.stat_result,
) -> None:
    try:
        current_fd = _open_parent_from_root_fd(root_fd, parent_parts)
    except WorkspaceMutationBoundaryError as exc:
        raise WorkspaceMutationPreimageChangedError(
            "Patch target parent changed while preimage bytes were being captured"
        ) from exc
    try:
        current = os.fstat(current_fd)
    finally:
        os.close(current_fd)
    if not _same_directory(pinned_parent, current):
        raise WorkspaceMutationPreimageChangedError(
            "Patch target parent identity changed during preimage capture"
        )


def _verify_workspace_path_still_names_anchor(
    root: Path,
    pinned_root: os.stat_result,
) -> None:
    try:
        current_fd = _port._open_posix_directory_chain(root)
    except WorkspaceMutationBoundaryError as exc:
        raise WorkspaceMutationPreimageChangedError(
            "Patch workspace path changed during proposal preparation"
        ) from exc
    try:
        current = os.fstat(current_fd)
    finally:
        os.close(current_fd)
    if not _same_directory(pinned_root, current):
        raise WorkspaceMutationPreimageChangedError(
            "Patch workspace identity changed during proposal preparation"
        )


def _prepare_posix_patch_proposal(
    *,
    workspace: str | Path,
    changes: Iterable[_legacy.PatchFileRequest],
    summary: str | None,
) -> ActionProposal:
    root, root_fd, pinned_root = _pin_posix_workspace_before_validation(workspace)
    try:
        requests = _review._bounded_requests(changes)
        if any(not isinstance(request, _legacy.PatchFileRequest) for request in requests):
            raise TypeError("Patch changes must be PatchFileRequest instances")

        total_content = sum(len(request.content) for request in requests)
        if total_content > _legacy.MAX_PATCH_TOTAL_CONTENT_BYTES:
            raise InvalidWorkspaceMutationError(
                "Patch exact before/after content exceeds total proposal budget "
                f"({total_content} > {_legacy.MAX_PATCH_TOTAL_CONTENT_BYTES})"
            )

        prepared: list[tuple[str, _legacy.PatchFileChange]] = []
        seen: set[str] = set()
        case_cache: dict[tuple[int, int], bool | None] = {}

        for request in requests:
            _final.preflight_workspace_mutation_target(request.target)
            rendered = _port._lexical_target(request.target)
            parts = Path(rendered).parts
            parent_parts = tuple(parts[:-1])
            target_name = parts[-1]

            parent_fd = _open_parent_from_root_fd(root_fd, parent_parts)
            try:
                pinned_parent = os.fstat(parent_fd)
                parent_identity = _stat_identity(pinned_parent)
                if parent_identity not in case_cache:
                    case_cache[parent_identity] = _probe_parent_case_sensitivity(parent_fd)
                key = _namespace_key(
                    rendered,
                    case_sensitive=case_cache[parent_identity],
                )
                if key in seen:
                    raise InvalidWorkspaceMutationError(
                        f"Patch proposal contains duplicate target: {rendered}"
                    )
                seen.add(key)

                snapshot, preimage = _capture_preimage_from_parent_fd(
                    parent_fd,
                    target_name=target_name,
                    max_bytes=_legacy.MAX_PATCH_FILE_BYTES,
                )
                _verify_parent_still_names_anchor(root_fd, parent_parts, pinned_parent)
            finally:
                os.close(parent_fd)

            if snapshot.state is PreimageState.PRESENT:
                if snapshot.size_bytes is None or snapshot.size_bytes > _legacy.MAX_PATCH_FILE_BYTES:
                    raise InvalidWorkspaceMutationError(
                        f"Patch preimage exceeds {_legacy.MAX_PATCH_FILE_BYTES} bytes"
                    )
                if snapshot.size_bytes > _legacy.MAX_PATCH_TOTAL_CONTENT_BYTES - total_content:
                    raise InvalidWorkspaceMutationError(
                        "Patch exact before/after content exceeds total proposal budget"
                    )
                if preimage is None or len(preimage) != snapshot.size_bytes:
                    raise WorkspaceMutationPreimageChangedError(
                        "Patch preimage payload does not match pinned snapshot"
                    )
                _legacy._text(preimage, f"Patch preimage for {target_name}")
                total_content += len(preimage)
            elif preimage is not None:
                raise InvalidWorkspaceMutationError("Absent patch preimage must not carry bytes")

            if request.operation is MutationOperation.CREATE:
                if snapshot.state is not PreimageState.ABSENT:
                    raise WorkspaceMutationTargetExistsError(
                        f"Create target already exists: {rendered}"
                    )
            else:
                if snapshot.state is not PreimageState.PRESENT:
                    raise WorkspaceMutationTargetMissingError(
                        f"Replace target does not exist: {rendered}"
                    )
                if preimage == request.content:
                    raise InvalidWorkspaceMutationError(
                        f"Replace patch is a no-op for {rendered}"
                    )

            prepared.append(
                (
                    rendered,
                    _legacy.PatchFileChange.create(
                        operation=request.operation,
                        target=rendered,
                        expected_preimage=snapshot,
                        preimage=preimage,
                        postimage=request.content,
                    ),
                )
            )

        prepared.sort(key=lambda item: item[0])

        # Internal construction has already performed the live, pinned namespace
        # checks. Reuse the self-contained structural/digest validator rather than
        # reopening mutable paths through the public direct-construction checks.
        token = _final._SELF_CONTAINED_PARSE.set(True)
        try:
            change_set = _legacy.PatchChangeSet.create(
                workspace_root=str(root),
                changes=[item[1] for item in prepared],
            )
        finally:
            _final._SELF_CONTAINED_PARSE.reset(token)

        _hard._check_preview_budget(change_set.changes)
        _verify_workspace_path_still_names_anchor(root, pinned_root)

        return ActionProposal.create(
            capability=Capability.WRITE_WORKSPACE,
            action=_legacy.PATCH_ACTION,
            workspace_root=str(root),
            parameters=change_set.to_parameters(),
            summary=summary or f"Apply {len(change_set.changes)}-file workspace patch.",
        )
    finally:
        os.close(root_fd)


def prepare_patch_proposal(
    *,
    workspace: str | Path,
    changes: Iterable[_legacy.PatchFileRequest],
    summary: str | None = None,
) -> ActionProposal:
    if os.name == "nt":
        return _base_prepare_patch_proposal(
            workspace=workspace,
            changes=changes,
            summary=summary,
        )
    return _prepare_posix_patch_proposal(
        workspace=workspace,
        changes=changes,
        summary=summary,
    )


def install_patch_posix_root_anchor() -> None:
    if getattr(_final, "_M24_PATCH_POSIX_ROOT_ANCHOR_INSTALLED", False):
        return

    # Seal every ordinary proposal-preparation entrypoint to the anchored wrapper.
    _port.prepare_patch_proposal = prepare_patch_proposal
    _final.prepare_patch_proposal = prepare_patch_proposal
    _review.prepare_patch_proposal = prepare_patch_proposal
    _hard.prepare_patch_proposal = prepare_patch_proposal
    _legacy.prepare_patch_proposal = prepare_patch_proposal
    _final._M24_PATCH_POSIX_ROOT_ANCHOR_INSTALLED = True


install_patch_posix_root_anchor()

parse_patch_proposal = _port.parse_patch_proposal
build_patch_approval_preview = _port.build_patch_approval_preview

__all__ = [
    "build_patch_approval_preview",
    "parse_patch_proposal",
    "prepare_patch_proposal",
]
