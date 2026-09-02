from __future__ import annotations

import hmac
import json
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any


from codexia_manual_agent.agent.patch_protocol import (
    ModelPatchOperation,
    ModelPatchRequest,
)
from codexia_manual_agent.authority import ActionProposal
from codexia_manual_agent.domain.capabilities import Capability
from codexia_manual_agent.domain.errors import InvalidWorkspaceMutationError
from codexia_manual_agent.mutation.models import MutationOperation
from codexia_manual_agent.mutation.patch_preview_budget_repairs import (
    build_patch_approval_preview,
    parse_patch_proposal,
    prepare_patch_proposal,
)
from codexia_manual_agent.mutation.patches import (
    PATCH_ACTION,
    PatchApprovalPreview,
    PatchFileRequest,
)
from codexia_manual_agent.mutation.workspace import _normalize_target, _workspace_root


MODEL_PATCH_PREPARATION_SCHEMA_VERSION = 1


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _preparation_digest(
    *,
    request_digest: str,
    proposal_digest: str,
    change_set_digest: str,
) -> str:
    return sha256(
        _canonical_json(
            {
                "schema_version": MODEL_PATCH_PREPARATION_SCHEMA_VERSION,
                "request_digest": request_digest,
                "proposal_digest": proposal_digest,
                "change_set_digest": change_set_digest,
            }
        ).encode("utf-8")
    ).hexdigest()


def _summary(request: ModelPatchRequest) -> str:
    return (
        f"M2.4.6 model patch request {request.request_id} "
        f"[request_digest={request.request_digest}]; "
        "explicit local human authorization remains required."
    )


def _mutation_operation(operation: ModelPatchOperation) -> MutationOperation:
    mapping = {
        ModelPatchOperation.CREATE: MutationOperation.CREATE,
        ModelPatchOperation.REPLACE: MutationOperation.REPLACE,
    }
    return mapping[ModelPatchOperation(operation)]


@dataclass(frozen=True, slots=True)
class ModelPatchApprovalPreview:
    request_id: str
    request_digest: str
    proposal_id: str
    proposal_digest: str
    change_set_digest: str
    preparation_digest: str
    requires_human: bool
    patch: PatchApprovalPreview

    def __post_init__(self) -> None:
        if not isinstance(self.request_id, str) or not self.request_id:
            raise InvalidWorkspaceMutationError(
                "Model patch approval preview request_id is required"
            )
        for label, value in (
            ("request digest", self.request_digest),
            ("proposal digest", self.proposal_digest),
            ("change-set digest", self.change_set_digest),
            ("preparation digest", self.preparation_digest),
        ):
            if not isinstance(value, str) or len(value) != 64:
                raise InvalidWorkspaceMutationError(
                    f"Model patch approval preview {label} must be SHA-256 hex"
                )
            try:
                int(value, 16)
            except ValueError as exc:
                raise InvalidWorkspaceMutationError(
                    f"Model patch approval preview {label} must be SHA-256 hex"
                ) from exc
        if not isinstance(self.proposal_id, str) or not self.proposal_id:
            raise InvalidWorkspaceMutationError(
                "Model patch approval preview proposal_id is required"
            )
        if self.requires_human is not True:
            raise InvalidWorkspaceMutationError(
                "Model patch approval preview must require a human decision"
            )
        if not isinstance(self.patch, PatchApprovalPreview):
            raise TypeError("patch must be PatchApprovalPreview")
        if self.patch.requires_human is not True:
            raise InvalidWorkspaceMutationError(
                "Underlying patch approval preview must require a human decision"
            )
        if not hmac.compare_digest(
            self.patch.change_set_digest,
            self.change_set_digest,
        ):
            raise InvalidWorkspaceMutationError(
                "Model patch preview change-set binding does not match"
            )
        expected = _preparation_digest(
            request_digest=self.request_digest,
            proposal_digest=self.proposal_digest,
            change_set_digest=self.change_set_digest,
        )
        if not hmac.compare_digest(expected, self.preparation_digest):
            raise InvalidWorkspaceMutationError(
                "Model patch preparation digest does not match provenance binding"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "request_digest": self.request_digest,
            "proposal_id": self.proposal_id,
            "proposal_digest": self.proposal_digest,
            "change_set_digest": self.change_set_digest,
            "preparation_digest": self.preparation_digest,
            "requires_human": self.requires_human,
            "patch": self.patch.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class ModelPatchPreparation:
    schema_version: int
    request: ModelPatchRequest
    proposal: ActionProposal
    approval_preview: ModelPatchApprovalPreview

    def __post_init__(self) -> None:
        if self.schema_version != MODEL_PATCH_PREPARATION_SCHEMA_VERSION:
            raise InvalidWorkspaceMutationError(
                "Unsupported model patch preparation schema"
            )
        if not isinstance(self.request, ModelPatchRequest):
            raise TypeError("request must be ModelPatchRequest")
        if not isinstance(self.proposal, ActionProposal):
            raise TypeError("proposal must be ActionProposal")
        if not isinstance(self.approval_preview, ModelPatchApprovalPreview):
            raise TypeError("approval_preview must be ModelPatchApprovalPreview")
        if self.proposal.capability is not Capability.WRITE_WORKSPACE:
            raise InvalidWorkspaceMutationError(
                "Model patch preparation must produce write_workspace capability"
            )
        if self.proposal.action != PATCH_ACTION:
            raise InvalidWorkspaceMutationError(
                "Model patch preparation must produce the exact M2.4 patch action"
            )
        if self.proposal.summary != _summary(self.request):
            raise InvalidWorkspaceMutationError(
                "Model patch proposal provenance summary does not match request"
            )

        change_set = parse_patch_proposal(self.proposal)
        root = _workspace_root(self.proposal.workspace_root)
        requested: list[tuple[str, MutationOperation, bytes]] = []
        for change in self.request.changes:
            rendered, _, _ = _normalize_target(root, change.target)
            requested.append(
                (
                    rendered,
                    _mutation_operation(change.operation),
                    change.content_bytes,
                )
            )
        requested.sort(key=lambda item: item[0])
        proposed = [
            (change.target, change.operation, change.postimage)
            for change in change_set.changes
        ]
        if requested != proposed:
            raise InvalidWorkspaceMutationError(
                "Model patch request content does not match prepared proposal"
            )

        preview = self.approval_preview
        if (
            preview.request_id != self.request.request_id
            or not hmac.compare_digest(
                preview.request_digest,
                self.request.request_digest,
            )
            or preview.proposal_id != self.proposal.proposal_id
            or not hmac.compare_digest(
                preview.proposal_digest,
                self.proposal.proposal_digest,
            )
            or not hmac.compare_digest(
                preview.change_set_digest,
                change_set.change_set_digest,
            )
        ):
            raise InvalidWorkspaceMutationError(
                "Model patch preparation preview is not bound to exact request/proposal"
            )

        canonical_patch_preview = build_patch_approval_preview(self.proposal)
        if preview.patch.to_dict() != canonical_patch_preview.to_dict():
            raise InvalidWorkspaceMutationError(
                "Model patch displayed preview does not match exact proposal"
            )


def prepare_model_patch_proposal(
    request: ModelPatchRequest,
    *,
    workspace: str | Path,
) -> ModelPatchPreparation:
    """Convert bounded model intent into an unapproved local M2.4 patch proposal."""

    if not isinstance(request, ModelPatchRequest):
        raise TypeError("request must be ModelPatchRequest")

    proposal = prepare_patch_proposal(
        workspace=workspace,
        changes=tuple(
            PatchFileRequest(
                operation=_mutation_operation(change.operation),
                target=change.target,
                content=change.content_bytes,
            )
            for change in request.changes
        ),
        summary=_summary(request),
    )
    patch_preview = build_patch_approval_preview(proposal)
    preparation_digest = _preparation_digest(
        request_digest=request.request_digest,
        proposal_digest=proposal.proposal_digest,
        change_set_digest=patch_preview.change_set_digest,
    )
    approval_preview = ModelPatchApprovalPreview(
        request_id=request.request_id,
        request_digest=request.request_digest,
        proposal_id=proposal.proposal_id,
        proposal_digest=proposal.proposal_digest,
        change_set_digest=patch_preview.change_set_digest,
        preparation_digest=preparation_digest,
        requires_human=True,
        patch=patch_preview,
    )
    return ModelPatchPreparation(
        schema_version=MODEL_PATCH_PREPARATION_SCHEMA_VERSION,
        request=request,
        proposal=proposal,
        approval_preview=approval_preview,
    )
