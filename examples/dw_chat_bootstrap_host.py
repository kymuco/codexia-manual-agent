"""Explicit bootstrap-only Pack provider for the Delegated ChatGPT Work pilot.

Used only to pin a real standalone Gen2 Work. The provider intentionally
exposes no WorkflowImplementation: this v0 cannot auto-advance or complete.
"""
from __future__ import annotations

from hashlib import sha256

from codexia_manual_agent.pack_core import (
    PackBinding,
    PackMemberBinding,
    PackMemberKind,
)
from codexia_manual_agent.standalone_work import StandaloneWorkHost
from codexia_manual_agent.workflow_core import WorkflowBinding

PROVIDER_REF = "codexia:delegated-chat-pilot-provider@0.1.0"
WORKFLOW_ID = "codexia:delegated-chat-work"
WORKFLOW_VERSION = "0.1.0"

def _sha(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


def _workflow() -> WorkflowBinding:
    return WorkflowBinding.create(
        workflow_id=WORKFLOW_ID,
        version=WORKFLOW_VERSION,
        definition_digest=_sha("delegated-chat-work-bootstrap-only-v0"),
    )


def _pack() -> PackBinding:
    workflow = _workflow()
    return PackBinding.create(
        pack_id="codexia:delegated-chat-pilot-pack",
        version="0.1.0",
        definition_digest=_sha("delegated-chat-pilot-pack-bootstrap-only-v0"),
        members=[
            PackMemberBinding.create(
                kind=PackMemberKind.WORKFLOW,
                semantic_id=workflow.workflow_id,
                version=workflow.version,
                binding_digest=workflow.binding_digest,
            )
        ],
    )


class _PilotProvider:
    def codexia_pack_distribution(self):
        return {
            "schema_version": 1,
            "pack": _pack().to_dict(),
            "workflows": [_workflow().to_dict()],
            "roles": [],
            "capabilities": [],
        }


class _PluginService:
    def get(self, plugin_id: str):
        if plugin_id != PROVIDER_REF:
            raise KeyError(plugin_id)
        return _PilotProvider()


def make_host() -> StandaloneWorkHost:
    """Trusted local host factory for `codexia work start` only."""
    return StandaloneWorkHost(plugin_service=_PluginService())
