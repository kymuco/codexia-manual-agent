from __future__ import annotations

import base64
import binascii
import difflib
import hmac
import os
import unicodedata
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from codexia_manual_agent.authority import ActionProposal
from codexia_manual_agent.domain.capabilities import Capability
from codexia_manual_agent.domain.errors import (
    InvalidWorkspaceMutationError,
    WorkspaceMutationBoundaryError,
    WorkspaceMutationTargetExistsError,
    WorkspaceMutationTargetMissingError,
)
from codexia_manual_agent.mutation import patches as _legacy
from codexia_manual_agent.mutation.hardened_workspace import (
    preflight_workspace_mutation_target,
)
from codexia_manual_agent.mutation.models import MutationOperation, PreimageSnapshot, PreimageState

_READ_CHUNK_BYTES = 64 * 1024


def _target_identity_key(
    target_path: Path,
    *,
    cache: dict[str, Any],
) -> str:
    """Return the base duplicate-target key; review repairs may replace this hook."""

    del cache
    return os.path.normcase(os.path.abspath(str(target_path)))


def _read_bounded_file(path: Path, *, max_bytes: int) -> bytes:
    data = bytearray()
    try:
        with path.open("rb") as handle:
            while True:
                remaining = max_bytes + 1 - len(data)
                if remaining <= 0:
                    break
                chunk = handle.read(min(_READ_CHUNK_BYTES, remaining))
                if not chunk:
                    break
                data.extend(chunk)
    except OSError as exc:
        raise InvalidWorkspaceMutationError(f"Cannot read patch preimage: {path}") from exc
    if len(data) > max_bytes:
        raise InvalidWorkspaceMutationError(
            f"Patch preimage exceeds bounded read budget ({len(data)} > {max_bytes})"
        )
    return bytes(data)


def _capture_exact_preimage(
    path: Path,
    *,
    remaining_total_bytes: int,
) -> tuple[PreimageSnapshot, bytes | None]:
    first = _legacy._capture_preimage(path)
    if first.state is PreimageState.ABSENT:
        return first, None
    if first.size_bytes is None or first.size_bytes > _legacy.MAX_PATCH_FILE_BYTES:
        raise InvalidWorkspaceMutationError(
            f"Patch preimage exceeds {_legacy.MAX_PATCH_FILE_BYTES} bytes"
        )
    if first.size_bytes > remaining_total_bytes:
        raise InvalidWorkspaceMutationError(
            "Patch exact before/after content exceeds total proposal budget"
        )
    payload = _read_bounded_file(
        path,
        max_bytes=min(_legacy.MAX_PATCH_FILE_BYTES, remaining_total_bytes),
    )
    second = _legacy._capture_preimage(path)
    if first != second or len(payload) != second.size_bytes:
        raise InvalidWorkspaceMutationError(
            "Patch target changed while exact preimage bytes were being captured"
        )
    if _legacy._sha256_bytes(payload) != second.sha256:
        raise InvalidWorkspaceMutationError(
            "Patch preimage bytes changed while proposal was being prepared"
        )
    _legacy._text(payload, f"Patch preimage for {path.name}")
    return second, payload


def _base64_length(size_bytes: int) -> int:
    return ((size_bytes + 2) // 3) * 4


def _decode_preimage(snapshot: PreimageSnapshot, value: Any) -> bytes | None:
    if snapshot.state is PreimageState.ABSENT:
        if value is not None:
            raise InvalidWorkspaceMutationError("Absent patch preimage data must be null")
        return None
    if snapshot.size_bytes is None or snapshot.size_bytes > _legacy.MAX_PATCH_FILE_BYTES:
        raise InvalidWorkspaceMutationError(
            f"Patch preimage exceeds {_legacy.MAX_PATCH_FILE_BYTES} bytes"
        )
    if not isinstance(value, str):
        raise InvalidWorkspaceMutationError(
            "Present patch preimage data must be base64 text"
        )
    if len(value) != _base64_length(snapshot.size_bytes):
        raise InvalidWorkspaceMutationError(
            "Patch preimage base64 length does not match declared size"
        )
    try:
        payload = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise InvalidWorkspaceMutationError("Patch preimage base64 is invalid") from exc
    if (
        len(payload) != snapshot.size_bytes
        or _legacy._sha256_bytes(payload) != snapshot.sha256
    ):
        raise InvalidWorkspaceMutationError(
            "Patch preimage identity does not match payload"
        )
    return payload


def _postimage_declared_size(data: Any) -> int:
    if not isinstance(data, dict) or set(data) != {"size_bytes", "sha256", "data_base64"}:
        raise InvalidWorkspaceMutationError("Patch postimage schema is invalid")
    size = data["size_bytes"]
    if type(size) is not int or not 0 <= size <= _legacy.MAX_PATCH_FILE_BYTES:
        raise InvalidWorkspaceMutationError("Patch postimage size is invalid")
    encoded = data["data_base64"]
    if not isinstance(encoded, str):
        raise InvalidWorkspaceMutationError("Patch postimage data must be base64 text")
    if len(encoded) != _base64_length(size):
        raise InvalidWorkspaceMutationError(
            "Patch postimage base64 length does not match declared size"
        )
    return size


def _decode_postimage_bounded(data: Any) -> tuple[bytes, str]:
    _postimage_declared_size(data)
    return _legacy._decode_postimage(data)


def _escape_display_text(value: str) -> str:
    rendered: list[str] = []
    for char in value:
        code = ord(char)
        category = unicodedata.category(char)
        if char == "\\":
            rendered.append("\\\\")
        elif char == "\t":
            rendered.append("\\t")
        elif char == "\r":
            rendered.append("\\r")
        elif category in {"Cc", "Cf"} or char in {"\u2028", "\u2029"}:
            if code <= 0xFFFF:
                rendered.append(f"\\u{code:04x}")
            else:
                rendered.append(f"\\U{code:08x}")
        else:
            rendered.append(char)
    return "".join(rendered)


def _display_lines(payload: bytes, label: str) -> tuple[list[str], bool]:
    text = _legacy._text(payload, label)
    if not text:
        return [], False
    has_final_lf = text.endswith("\n")
    body = text[:-1] if has_final_lf else text
    return (
        [_escape_display_text(segment) + "\n" for segment in body.split("\n")],
        not has_final_lf,
    )


def _render_diff(change: _legacy.PatchFileChange) -> str:
    safe_target = _escape_display_text(change.target)
    if change.preimage is None:
        before_lines, before_missing_lf = [], False
    else:
        before_lines, before_missing_lf = _display_lines(
            change.preimage,
            f"Patch preimage for {change.target}",
        )
    after_lines, after_missing_lf = _display_lines(
        change.postimage,
        f"Patch postimage for {change.target}",
    )
    rendered = list(
        difflib.unified_diff(
            before_lines,
            after_lines,
            fromfile=(
                "/dev/null"
                if change.operation is MutationOperation.CREATE
                else f"a/{safe_target}"
            ),
            tofile=f"b/{safe_target}",
            lineterm="\n",
        )
    )
    if not rendered and change.operation is MutationOperation.CREATE and not after_lines:
        rendered = [
            "--- /dev/null\n",
            f"+++ b/{safe_target}\n",
            "@@ -0,0 +0,0 @@\n",
            "\\ Codexia: empty postimage (0 bytes)\n",
        ]
    if before_missing_lf:
        rendered.append("\\ Codexia: preimage has no final LF\n")
    if after_missing_lf:
        rendered.append("\\ Codexia: postimage has no final LF\n")
    return "".join(rendered)


def _check_preview_budget(changes: Sequence[_legacy.PatchFileChange]) -> None:
    total = 0
    for change in changes:
        total += len(_render_diff(change).encode("utf-8"))
        if total > _legacy.MAX_PATCH_PREVIEW_BYTES:
            raise InvalidWorkspaceMutationError(
                "Patch human-readable preview exceeds review budget "
                f"({total} > {_legacy.MAX_PATCH_PREVIEW_BYTES})"
            )


def prepare_patch_proposal(
    *,
    workspace: str | Path,
    changes: Sequence[_legacy.PatchFileRequest],
    summary: str | None = None,
) -> ActionProposal:
    root = _legacy._workspace_root(workspace)
    requests = tuple(changes)
    if not 1 <= len(requests) <= _legacy.MAX_PATCH_FILES:
        raise InvalidWorkspaceMutationError(
            f"Patch proposal must contain 1..{_legacy.MAX_PATCH_FILES} files"
        )
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
    target_identity_cache: dict[str, Any] = {}
    for request in requests:
        preflight_workspace_mutation_target(request.target)
        rendered, target_path, _ = _legacy._normalize_target(root, request.target)
        key = _target_identity_key(target_path, cache=target_identity_cache)
        if key in seen:
            raise InvalidWorkspaceMutationError(
                f"Patch proposal contains duplicate target: {rendered}"
            )
        seen.add(key)

        snapshot, preimage = _capture_exact_preimage(
            target_path,
            remaining_total_bytes=_legacy.MAX_PATCH_TOTAL_CONTENT_BYTES - total_content,
        )
        if preimage is not None:
            total_content += len(preimage)
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
    change_set = _legacy.PatchChangeSet.create(
        workspace_root=str(root),
        changes=[item[1] for item in prepared],
    )
    _check_preview_budget(change_set.changes)
    return ActionProposal.create(
        capability=Capability.WRITE_WORKSPACE,
        action=_legacy.PATCH_ACTION,
        workspace_root=str(root),
        parameters=change_set.to_parameters(),
        summary=summary or f"Apply {len(change_set.changes)}-file workspace patch.",
    )


def parse_patch_proposal(proposal: ActionProposal) -> _legacy.PatchChangeSet:
    if not isinstance(proposal, ActionProposal):
        raise TypeError("proposal must be an ActionProposal")
    if proposal.capability is not Capability.WRITE_WORKSPACE:
        raise InvalidWorkspaceMutationError(
            "Patch proposal requires write_workspace capability"
        )
    if proposal.action != _legacy.PATCH_ACTION:
        raise InvalidWorkspaceMutationError(
            "Action proposal is not an M2.4 patch proposal"
        )

    params = proposal.to_dict()["parameters"]
    if set(params) != {"schema_version", "change_set_digest", "changes"}:
        raise InvalidWorkspaceMutationError("Patch proposal parameter schema is invalid")
    if params["schema_version"] != _legacy.PATCH_SCHEMA_VERSION:
        raise InvalidWorkspaceMutationError("Unsupported patch proposal schema version")
    _legacy._require_digest(params["change_set_digest"], "Patch change-set digest")
    if not isinstance(params["changes"], Sequence) or isinstance(
        params["changes"], (str, bytes)
    ):
        raise InvalidWorkspaceMutationError("Patch changes must be a sequence")
    if not 1 <= len(params["changes"]) <= _legacy.MAX_PATCH_FILES:
        raise InvalidWorkspaceMutationError(
            f"Patch proposal must contain 1..{_legacy.MAX_PATCH_FILES} files"
        )

    root = _legacy._workspace_root(proposal.workspace_root)
    if str(root) != proposal.workspace_root:
        raise WorkspaceMutationBoundaryError(
            "Patch proposal workspace root is not canonical"
        )
    parsed: list[_legacy.PatchFileChange] = []
    seen: set[str] = set()
    target_identity_cache: dict[str, Any] = {}
    total_content = 0
    for raw in params["changes"]:
        if not isinstance(raw, Mapping):
            raise InvalidWorkspaceMutationError("Patch change entry must be an object")
        if set(raw) != {
            "operation",
            "target",
            "expected_preimage",
            "preimage_data_base64",
            "postimage",
            "change_digest",
        }:
            raise InvalidWorkspaceMutationError("Patch change schema is invalid")
        try:
            operation = MutationOperation(raw["operation"])
        except (TypeError, ValueError) as exc:
            raise InvalidWorkspaceMutationError("Patch operation is invalid") from exc
        if not isinstance(raw["target"], str):
            raise InvalidWorkspaceMutationError("Patch target must be text")
        preflight_workspace_mutation_target(raw["target"])
        rendered, target_path, _ = _legacy._normalize_target(root, raw["target"])
        if rendered != raw["target"]:
            raise WorkspaceMutationBoundaryError("Patch target is not canonical")
        key = _target_identity_key(target_path, cache=target_identity_cache)
        if key in seen:
            raise InvalidWorkspaceMutationError("Patch proposal contains duplicate targets")
        seen.add(key)

        snapshot = _legacy._parse_preimage(raw["expected_preimage"])
        preimage_size = 0
        if snapshot.state is PreimageState.PRESENT:
            if snapshot.size_bytes is None or snapshot.size_bytes > _legacy.MAX_PATCH_FILE_BYTES:
                raise InvalidWorkspaceMutationError(
                    f"Patch preimage exceeds {_legacy.MAX_PATCH_FILE_BYTES} bytes"
                )
            preimage_size = snapshot.size_bytes
        postimage_size = _postimage_declared_size(raw["postimage"])
        projected_total = total_content + preimage_size + postimage_size
        if projected_total > _legacy.MAX_PATCH_TOTAL_CONTENT_BYTES:
            raise InvalidWorkspaceMutationError(
                "Patch exact before/after content exceeds total proposal budget "
                f"({projected_total} > {_legacy.MAX_PATCH_TOTAL_CONTENT_BYTES})"
            )

        preimage = _decode_preimage(snapshot, raw["preimage_data_base64"])
        postimage, _ = _decode_postimage_bounded(raw["postimage"])
        total_content = projected_total
        _legacy._text(postimage, f"Patch postimage for {rendered}")
        if preimage is not None:
            _legacy._text(preimage, f"Patch preimage for {rendered}")
        if operation is MutationOperation.CREATE and snapshot.state is not PreimageState.ABSENT:
            raise InvalidWorkspaceMutationError("Create patch must bind an absent preimage")
        if operation is MutationOperation.REPLACE and snapshot.state is not PreimageState.PRESENT:
            raise InvalidWorkspaceMutationError("Replace patch must bind a present preimage")
        if operation is MutationOperation.REPLACE and preimage == postimage:
            raise InvalidWorkspaceMutationError("Replace patch cannot be a no-op")
        parsed.append(
            _legacy.PatchFileChange(
                operation=operation,
                target=rendered,
                expected_preimage=snapshot,
                preimage=preimage,
                postimage=postimage,
                change_digest=raw["change_digest"],
            )
        )

    change_set = _legacy.PatchChangeSet(
        workspace_root=str(root),
        changes=tuple(parsed),
        change_set_digest=params["change_set_digest"],
    )
    _check_preview_budget(change_set.changes)
    return change_set


def build_patch_approval_preview(proposal: ActionProposal) -> _legacy.PatchApprovalPreview:
    change_set = parse_patch_proposal(proposal)
    changes = tuple(
        _legacy.PatchFilePreview(
            operation=change.operation,
            target=_escape_display_text(change.target),
            preimage_size_bytes=change.expected_preimage.size_bytes,
            preimage_sha256=change.expected_preimage.sha256,
            postimage_size_bytes=len(change.postimage),
            postimage_sha256=change.postimage_sha256,
            change_digest=change.change_digest,
            unified_diff=_render_diff(change),
        )
        for change in change_set.changes
    )
    return _legacy.PatchApprovalPreview(
        action=_legacy.PATCH_ACTION,
        workspace_root=_escape_display_text(change_set.workspace_root),
        change_set_digest=change_set.change_set_digest,
        file_count=len(changes),
        total_preimage_bytes=sum(item.preimage_size_bytes or 0 for item in changes),
        total_postimage_bytes=sum(item.postimage_size_bytes for item in changes),
        requires_human=True,
        changes=changes,
    )


_original_change_set_post_init = _legacy.PatchChangeSet.__post_init__


def _hardened_change_set_post_init(self: _legacy.PatchChangeSet) -> None:
    normalized = tuple(self.changes)
    if any(not isinstance(change, _legacy.PatchFileChange) for change in normalized):
        raise InvalidWorkspaceMutationError(
            "Patch change set entries must be PatchFileChange objects"
        )
    object.__setattr__(self, "changes", normalized)
    _original_change_set_post_init(self)


def install_patch_hardening() -> None:
    if getattr(_legacy, "_M24_PATCH_HARDENING_INSTALLED", False):
        return
    _legacy.PatchChangeSet.__post_init__ = _hardened_change_set_post_init
    _legacy.prepare_patch_proposal = prepare_patch_proposal
    _legacy.parse_patch_proposal = parse_patch_proposal
    _legacy.build_patch_approval_preview = build_patch_approval_preview
    _legacy._M24_PATCH_HARDENING_INSTALLED = True


install_patch_hardening()
