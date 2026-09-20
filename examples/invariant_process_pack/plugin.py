from __future__ import annotations

import json
from hashlib import sha256
from typing import Any

from invariant.spec import BasePlugin


def _digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return sha256(encoded.encode("utf-8")).hexdigest()


def _workflow_binding() -> dict[str, Any]:
    base = {
        "schema_version": 1,
        "workflow_id": "codexia:standalone-process",
        "version": "1.0.0",
        "definition_digest": sha256(
            b"standalone-process-workflow-v1"
        ).hexdigest(),
    }
    return {**base, "binding_digest": _digest(base)}


def _capability_binding() -> dict[str, Any]:
    base = {
        "schema_version": 1,
        "capability_id": "process",
        "version": "1.0.0",
        "contract_digest": sha256(
            b"process-run-contract-v1"
        ).hexdigest(),
    }
    return {**base, "binding_digest": _digest(base)}


def _member(
    *,
    kind: str,
    semantic_id: str,
    version: str,
    binding_digest: str,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "kind": kind,
        "semantic_id": semantic_id,
        "version": version,
        "binding_digest": binding_digest,
    }


def _pack_binding(
    workflow: dict[str, Any],
    capability: dict[str, Any],
) -> dict[str, Any]:
    members = sorted(
        [
            _member(
                kind="workflow",
                semantic_id=workflow["workflow_id"],
                version=workflow["version"],
                binding_digest=workflow["binding_digest"],
            ),
            _member(
                kind="capability",
                semantic_id=capability["capability_id"],
                version=capability["version"],
                binding_digest=capability["binding_digest"],
            ),
        ],
        key=lambda item: (
            item["kind"],
            item["semantic_id"],
            item["version"],
            item["binding_digest"],
        ),
    )
    base = {
        "schema_version": 1,
        "pack_id": "codexia:standalone-process-pack",
        "version": "1.0.0",
        "definition_digest": sha256(
            b"standalone-process-pack-v1"
        ).hexdigest(),
        "members": members,
    }
    return {**base, "binding_digest": _digest(base)}


class ProcessPackProvider(BasePlugin):
    def initialize(self, **deps) -> None:
        pass

    def shutdown(self) -> None:
        pass

    def codexia_pack_distribution(self) -> dict[str, Any]:
        workflow = _workflow_binding()
        capability = _capability_binding()
        return {
            "schema_version": 1,
            "pack": _pack_binding(workflow, capability),
            "workflows": [workflow],
            "roles": [],
            "capabilities": [capability],
        }
