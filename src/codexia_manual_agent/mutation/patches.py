from __future__ import annotations

import base64
import binascii
import difflib
import hmac
import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
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
from codexia_manual_agent.mutation.models import (
    MutationOperation,
    PreimageSnapshot,
    PreimageState,
)
from codexia_manual_agent.mutation.workspace import (
    MAX_POSTIMAGE_BYTES,
    _capture_preimage,
    _decode_postimage,
    _normalize_target,
    _parse_preimage,
    _postimage_payload,
    _sha256_bytes,
    _workspace_root,
)

PATCH_ACTION = "workspace.apply_patch.v1"
PATCH_SCHEMA_VERSION = 1
MAX_PATCH_FILES = 32
MAX_PATCH_FILE_BYTES = MAX_POSTIMAGE_BYTES
MAX_PATCH_TOTAL_CONTENT_BYTES = 4 * 1024 * 1024
MAX_PATCH_PREVIEW_BYTES = 512 * 1024


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _digest(value: Any) -> str:
    return sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _require_digest(value: Any, label: str) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise InvalidWorkspaceMutationError(f"{label} must be a SHA-256 hex digest")
    try:
        int(value, 16)
    except ValueError as exc:
        raise InvalidWorkspaceMutationError(
            f"{label} must be a SHA-256 hex digest"
        ) from exc
    return value


def _text(payload: bytes, label: str) -> str:
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise InvalidWorkspaceMutationError(
            f"{label} is not UTF-8 text; use the M2.3 exact write surface for binary content"
        ) from exc


def _change_parameter(
    *,
    operation: MutationOperation,
    target: str,
    expected_preimage: PreimageSnapshot,
    preimage: bytes | None,
    postimage: bytes,
    change_digest: str | None = None,
) -> dict[str, Any]:
    if expected_preimage.state is PreimageState.ABSENT:
        if preimage is not None:
            raise InvalidWorkspaceMutationError(
                "Absent patch preimage cannot carry exact bytes"
            )
        preimage_data = None
    else:
        if preimage is None:
            raise InvalidWorkspaceMutationError(
                "Present patch preimage requires exact bytes"
            )
        if (
            len(preimage) != expected_preimage.size_bytes
            or _sha256_bytes(preimage) != expected_preimage.sha256
        ):
            raise InvalidWorkspaceMutationError(
                "Patch preimage bytes do not match bound identity"
            )
        preimage_data = base64.b64encode(preimage).decode("ascii")
    value: dict[str, Any] = {
        "operation": MutationOperation(operation).value,
        "target": target,
        "expected_preimage": expected_preimage.to_dict(),
        "preimage_data_base64": preimage_data,
        "postimage": _postimage_payload(postimage),
    }
    if change_digest is not None:
        value["change_digest"] = change_digest
    return value


@dataclass(frozen=True, slots=True)
class PatchFileRequest:
    operation: MutationOperation
    target: str
    content: bytes

    def __post_init__(self) -> None:
        try:
            operation = MutationOperation(self.operation)
        except (TypeError, ValueError) as exc:
            raise InvalidWorkspaceMutationError(
                "M2.4.1 patch changes support only create or replace"
            ) from exc
        if operation not in (MutationOperation.CREATE, MutationOperation.REPLACE):
            raise InvalidWorkspaceMutationError(
                "M2.4.1 patch changes support only create or replace"
            )
        if not isinstance(self.target, str) or not self.target.strip():
            raise InvalidWorkspaceMutationError("Patch target must be non-empty text")
        if not isinstance(self.content, bytes):
            raise TypeError("Patch postimage content must be bytes")
        if len(self.content) > MAX_PATCH_FILE_BYTES:
            raise InvalidWorkspaceMutationError(
                f"Patch postimage exceeds {MAX_PATCH_FILE_BYTES} bytes"
            )
        _text(self.content, f"Patch postimage for {self.target}")
        object.__setattr__(self, "operation", operation)


@dataclass(frozen=True, slots=True)
class PatchFileChange:
    operation: MutationOperation
    target: str
    expected_preimage: PreimageSnapshot
    preimage: bytes | None
    postimage: bytes
    change_digest: str

    @property
    def postimage_sha256(self) -> str:
        return _sha256_bytes(self.postimage)

    def _parameter(self, *, with_digest: bool) -> dict[str, Any]:
        return _change_parameter(
            operation=self.operation,
            target=self.target,
            expected_preimage=self.expected_preimage,
            preimage=self.preimage,
            postimage=self.postimage,
            change_digest=(self.change_digest if with_digest else None),
        )

    @classmethod
    def create(
        cls,
        *,
        operation: MutationOperation,
        target: str,
        expected_preimage: PreimageSnapshot,
        preimage: bytes | None,
        postimage: bytes,
    ) -> "PatchFileChange":
        operation = MutationOperation(operation)
        digest = _digest(
            _change_parameter(
                operation=operation,
                target=target,
                expected_preimage=expected_preimage,
                preimage=preimage,
                postimage=postimage,
            )
        )
        return cls(
            operation=operation,
            target=target,
            expected_preimage=expected_preimage,
            preimage=preimage,
            postimage=postimage,
            change_digest=digest,
        )

    def __post_init__(self) -> None:
        object.__setattr__(self, "operation", MutationOperation(self.operation))
        if not isinstance(self.target, str) or not self.target:
            raise InvalidWorkspaceMutationError("Patch target must be non-empty")
        if (
            self.operation is MutationOperation.CREATE
            and self.expected_preimage.state is not PreimageState.ABSENT
        ):
            raise InvalidWorkspaceMutationError(
                "Create patch must bind an absent preimage"
            )
        if (
            self.operation is MutationOperation.REPLACE
            and self.expected_preimage.state is not PreimageState.PRESENT
        ):
            raise InvalidWorkspaceMutationError(
                "Replace patch must bind a present preimage"
            )
        if not isinstance(self.postimage, bytes):
            raise TypeError("Patch postimage must be bytes")
        if len(self.postimage) > MAX_PATCH_FILE_BYTES:
            raise InvalidWorkspaceMutationError(
                f"Patch postimage exceeds {MAX_PATCH_FILE_BYTES} bytes"
            )
        _text(self.postimage, f"Patch postimage for {self.target}")
        if self.preimage is not None:
            if len(self.preimage) > MAX_PATCH_FILE_BYTES:
                raise InvalidWorkspaceMutationError(
                    f"Patch preimage exceeds {MAX_PATCH_FILE_BYTES} bytes"
                )
            _text(self.preimage, f"Patch preimage for {self.target}")
        if self.operation is MutationOperation.REPLACE and self.preimage == self.postimage:
            raise InvalidWorkspaceMutationError("Replace patch cannot be a no-op")
        _require_digest(self.change_digest, "Patch change digest")
        expected = _digest(self._parameter(with_digest=False))
        if not hmac.compare_digest(expected, self.change_digest):
            raise InvalidWorkspaceMutationError(
                "Patch change digest does not match payload"
            )

    def to_parameter_dict(self) -> dict[str, Any]:
        return self._parameter(with_digest=True)


@dataclass(frozen=True, slots=True)
class PatchChangeSet:
    workspace_root: str
    changes: tuple[PatchFileChange, ...]
    change_set_digest: str

    @classmethod
    def create(
        cls,
        *,
        workspace_root: str,
        changes: Sequence[PatchFileChange],
    ) -> "PatchChangeSet":
        normalized = tuple(changes)
        digest = _digest(
            {
                "schema_version": PATCH_SCHEMA_VERSION,
                "workspace_root": workspace_root,
                "changes": [change.to_parameter_dict() for change in normalized],
            }
        )
        return cls(workspace_root, normalized, digest)

    def __post_init__(self) -> None:
        if not isinstance(self.workspace_root, str) or not self.workspace_root:
            raise InvalidWorkspaceMutationError("Patch workspace_root must be non-empty")
        if not 1 <= len(self.changes) <= MAX_PATCH_FILES:
            raise InvalidWorkspaceMutationError(
                f"Patch change set must contain 1..{MAX_PATCH_FILES} files"
            )
        targets = [change.target for change in self.changes]
        if targets != sorted(targets) or len(set(targets)) != len(targets):
            raise InvalidWorkspaceMutationError(
                "Patch changes must have unique sorted canonical targets"
            )
        total = sum(
            len(change.postimage) + (len(change.preimage) if change.preimage else 0)
            for change in self.changes
        )
        if total > MAX_PATCH_TOTAL_CONTENT_BYTES:
            raise InvalidWorkspaceMutationError(
                "Patch exact before/after content exceeds total proposal budget "
                f"({total} > {MAX_PATCH_TOTAL_CONTENT_BYTES})"
            )
        _require_digest(self.change_set_digest, "Patch change-set digest")
        expected = _digest(
            {
                "schema_version": PATCH_SCHEMA_VERSION,
                "workspace_root": self.workspace_root,
                "changes": [change.to_parameter_dict() for change in self.changes],
            }
        )
        if not hmac.compare_digest(expected, self.change_set_digest):
            raise InvalidWorkspaceMutationError(
                "Patch change-set digest does not match payload"
            )

    def to_parameters(self) -> dict[str, Any]:
        return {
            "schema_version": PATCH_SCHEMA_VERSION,
            "change_set_digest": self.change_set_digest,
            "changes": [change.to_parameter_dict() for change in self.changes],
        }


@dataclass(frozen=True, slots=True)
class PatchFilePreview:
    operation: MutationOperation
    target: str
    preimage_size_bytes: int | None
    preimage_sha256: str | None
    postimage_size_bytes: int
    postimage_sha256: str
    change_digest: str
    unified_diff: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation": self.operation.value,
            "target": self.target,
            "preimage_size_bytes": self.preimage_size_bytes,
            "preimage_sha256": self.preimage_sha256,
            "postimage_size_bytes": self.postimage_size_bytes,
            "postimage_sha256": self.postimage_sha256,
            "change_digest": self.change_digest,
            "unified_diff": self.unified_diff,
        }


@dataclass(frozen=True, slots=True)
class PatchApprovalPreview:
    action: str
    workspace_root: str
    change_set_digest: str
    file_count: int
    total_preimage_bytes: int
    total_postimage_bytes: int
    requires_human: bool
    changes: tuple[PatchFilePreview, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "workspace_root": self.workspace_root,
            "change_set_digest": self.change_set_digest,
            "file_count": self.file_count,
            "total_preimage_bytes": self.total_preimage_bytes,
            "total_postimage_bytes": self.total_postimage_bytes,
            "requires_human": self.requires_human,
            "changes": [change.to_dict() for change in self.changes],
        }


def _capture_exact_preimage(path: Path) -> tuple[PreimageSnapshot, bytes | None]:
    first = _capture_preimage(path)
    if first.state is PreimageState.ABSENT:
        return first, None
    if first.size_bytes is None or first.size_bytes > MAX_PATCH_FILE_BYTES:
        raise InvalidWorkspaceMutationError(
            f"Patch preimage exceeds {MAX_PATCH_FILE_BYTES} bytes"
        )
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise InvalidWorkspaceMutationError(f"Cannot read patch preimage: {path}") from exc
    second = _capture_preimage(path)
    if first != second or len(payload) != second.size_bytes:
        raise InvalidWorkspaceMutationError(
            "Patch target changed while exact preimage bytes were being captured"
        )
    if _sha256_bytes(payload) != second.sha256:
        raise InvalidWorkspaceMutationError(
            "Patch preimage bytes changed while proposal was being prepared"
        )
    _text(payload, f"Patch preimage for {path.name}")
    return second, payload


def _decode_preimage(snapshot: PreimageSnapshot, value: Any) -> bytes | None:
    if snapshot.state is PreimageState.ABSENT:
        if value is not None:
            raise InvalidWorkspaceMutationError(
                "Absent patch preimage data must be null"
            )
        return None
    if not isinstance(value, str):
        raise InvalidWorkspaceMutationError(
            "Present patch preimage data must be base64 text"
        )
    try:
        payload = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise InvalidWorkspaceMutationError("Patch preimage base64 is invalid") from exc
    if len(payload) != snapshot.size_bytes or _sha256_bytes(payload) != snapshot.sha256:
        raise InvalidWorkspaceMutationError(
            "Patch preimage identity does not match payload"
        )
    return payload


def _render_diff(change: PatchFileChange) -> str:
    before = "" if change.preimage is None else _text(
        change.preimage, f"Patch preimage for {change.target}"
    )
    after = _text(change.postimage, f"Patch postimage for {change.target}")
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=(
                "/dev/null"
                if change.operation is MutationOperation.CREATE
                else f"a/{change.target}"
            ),
            tofile=f"b/{change.target}",
            lineterm="\n",
        )
    )


def _check_preview_budget(changes: Sequence[PatchFileChange]) -> None:
    size = sum(len(_render_diff(change).encode("utf-8")) for change in changes)
    if size > MAX_PATCH_PREVIEW_BYTES:
        raise InvalidWorkspaceMutationError(
            f"Patch human-readable preview exceeds review budget "
            f"({size} > {MAX_PATCH_PREVIEW_BYTES})"
        )


def prepare_patch_proposal(
    *,
    workspace: str | Path,
    changes: Sequence[PatchFileRequest],
    summary: str | None = None,
) -> ActionProposal:
    root = _workspace_root(workspace)
    requests = tuple(changes)
    if not 1 <= len(requests) <= MAX_PATCH_FILES:
        raise InvalidWorkspaceMutationError(
            f"Patch proposal must contain 1..{MAX_PATCH_FILES} files"
        )

    prepared: list[tuple[str, PatchFileChange]] = []
    seen: set[str] = set()
    for request in requests:
        if not isinstance(request, PatchFileRequest):
            raise TypeError("Patch changes must be PatchFileRequest instances")
        rendered, target_path, _ = _normalize_target(root, request.target)
        key = os.path.normcase(os.path.abspath(str(target_path)))
        if key in seen:
            raise InvalidWorkspaceMutationError(
                f"Patch proposal contains duplicate target: {rendered}"
            )
        seen.add(key)

        snapshot, preimage = _capture_exact_preimage(target_path)
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
                PatchFileChange.create(
                    operation=request.operation,
                    target=rendered,
                    expected_preimage=snapshot,
                    preimage=preimage,
                    postimage=request.content,
                ),
            )
        )

    prepared.sort(key=lambda item: item[0])
    change_set = PatchChangeSet.create(
        workspace_root=str(root),
        changes=[item[1] for item in prepared],
    )
    _check_preview_budget(change_set.changes)
    return ActionProposal.create(
        capability=Capability.WRITE_WORKSPACE,
        action=PATCH_ACTION,
        workspace_root=str(root),
        parameters=change_set.to_parameters(),
        summary=summary or f"Apply {len(change_set.changes)}-file workspace patch.",
    )


def parse_patch_proposal(proposal: ActionProposal) -> PatchChangeSet:
    if not isinstance(proposal, ActionProposal):
        raise TypeError("proposal must be an ActionProposal")
    if proposal.capability is not Capability.WRITE_WORKSPACE:
        raise InvalidWorkspaceMutationError(
            "Patch proposal requires write_workspace capability"
        )
    if proposal.action != PATCH_ACTION:
        raise InvalidWorkspaceMutationError(
            "Action proposal is not an M2.4 patch proposal"
        )

    params = proposal.to_dict()["parameters"]
    if set(params) != {"schema_version", "change_set_digest", "changes"}:
        raise InvalidWorkspaceMutationError(
            "Patch proposal parameter schema is invalid"
        )
    if params["schema_version"] != PATCH_SCHEMA_VERSION:
        raise InvalidWorkspaceMutationError(
            "Unsupported patch proposal schema version"
        )
    _require_digest(params["change_set_digest"], "Patch change-set digest")
    if not isinstance(params["changes"], Sequence) or isinstance(
        params["changes"], (str, bytes)
    ):
        raise InvalidWorkspaceMutationError("Patch changes must be a sequence")

    root = _workspace_root(proposal.workspace_root)
    if str(root) != proposal.workspace_root:
        raise WorkspaceMutationBoundaryError(
            "Patch proposal workspace root is not canonical"
        )
    parsed: list[PatchFileChange] = []
    seen: set[str] = set()
    for raw in params["changes"]:
        if not isinstance(raw, Mapping):
            raise InvalidWorkspaceMutationError(
                "Patch change entry must be an object"
            )
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
        rendered, target_path, _ = _normalize_target(root, raw["target"])
        if rendered != raw["target"]:
            raise WorkspaceMutationBoundaryError("Patch target is not canonical")
        key = os.path.normcase(os.path.abspath(str(target_path)))
        if key in seen:
            raise InvalidWorkspaceMutationError(
                "Patch proposal contains duplicate targets"
            )
        seen.add(key)

        snapshot = _parse_preimage(raw["expected_preimage"])
        preimage = _decode_preimage(snapshot, raw["preimage_data_base64"])
        postimage, _ = _decode_postimage(raw["postimage"])
        if preimage is not None and len(preimage) > MAX_PATCH_FILE_BYTES:
            raise InvalidWorkspaceMutationError(
                f"Patch preimage exceeds {MAX_PATCH_FILE_BYTES} bytes"
            )
        if len(postimage) > MAX_PATCH_FILE_BYTES:
            raise InvalidWorkspaceMutationError(
                f"Patch postimage exceeds {MAX_PATCH_FILE_BYTES} bytes"
            )
        _text(postimage, f"Patch postimage for {rendered}")
        if preimage is not None:
            _text(preimage, f"Patch preimage for {rendered}")
        if (
            operation is MutationOperation.CREATE
            and snapshot.state is not PreimageState.ABSENT
        ):
            raise InvalidWorkspaceMutationError(
                "Create patch must bind an absent preimage"
            )
        if (
            operation is MutationOperation.REPLACE
            and snapshot.state is not PreimageState.PRESENT
        ):
            raise InvalidWorkspaceMutationError(
                "Replace patch must bind a present preimage"
            )
        if operation is MutationOperation.REPLACE and preimage == postimage:
            raise InvalidWorkspaceMutationError("Replace patch cannot be a no-op")
        parsed.append(
            PatchFileChange(
                operation=operation,
                target=rendered,
                expected_preimage=snapshot,
                preimage=preimage,
                postimage=postimage,
                change_digest=raw["change_digest"],
            )
        )

    change_set = PatchChangeSet(
        workspace_root=str(root),
        changes=tuple(parsed),
        change_set_digest=params["change_set_digest"],
    )
    _check_preview_budget(change_set.changes)
    return change_set


def build_patch_approval_preview(proposal: ActionProposal) -> PatchApprovalPreview:
    change_set = parse_patch_proposal(proposal)
    changes = tuple(
        PatchFilePreview(
            operation=change.operation,
            target=change.target,
            preimage_size_bytes=change.expected_preimage.size_bytes,
            preimage_sha256=change.expected_preimage.sha256,
            postimage_size_bytes=len(change.postimage),
            postimage_sha256=change.postimage_sha256,
            change_digest=change.change_digest,
            unified_diff=_render_diff(change),
        )
        for change in change_set.changes
    )
    return PatchApprovalPreview(
        action=PATCH_ACTION,
        workspace_root=change_set.workspace_root,
        change_set_digest=change_set.change_set_digest,
        file_count=len(changes),
        total_preimage_bytes=sum(item.preimage_size_bytes or 0 for item in changes),
        total_postimage_bytes=sum(item.postimage_size_bytes for item in changes),
        requires_human=True,
        changes=changes,
    )
