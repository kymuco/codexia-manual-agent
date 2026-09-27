from __future__ import annotations

import json
import os
import sys

from codexia_manual_agent.providers.chatgpt_web import ChatGPTWebProvider
from codexia_manual_agent.providers.model_provider_cognition import (
    ModelProviderCognitionPort,
)
from codexia_manual_agent.research_work import (
    ResearchContextMaterialPort,
    ResearchInstructionsMaterialPort,
    ResearchWorkMaterializer,
)
from codexia_manual_agent.standalone_work import StandaloneWorkHost
from codexia_manual_agent.work_core import SqliteWorkStore
from codexia_manual_agent.workflow_runtime import (
    ResearchCompletionCriterionV1,
    ResearchWorkflowImplementationV1,
    research_pack_binding,
    research_role_bindings,
    research_workflow_binding,
)

PROVIDER_REF = "codexia:research-pack-provider@1.0.0"
DEFAULT_STORE = ".codexia/dw3-research.sqlite3"


class _ResearchProvider:
    """Exact in-process provider for the DW3 product pilot.

    This deliberately avoids adding plugin discovery or lifecycle ownership to
    Codexia. The production semantic objects remain the existing DW2 bindings.
    """

    def codexia_pack_distribution(self):
        return {
            "schema_version": 1,
            "pack": research_pack_binding().to_dict(),
            "workflows": [research_workflow_binding().to_dict()],
            "roles": [item.to_dict() for item in research_role_bindings()],
            "capabilities": [],
        }

    def codexia_workflow_implementation(self, raw_binding):
        expected = research_workflow_binding()
        if raw_binding != expected.to_dict():
            raise ValueError("unsupported or non-exact Research WorkflowBinding")
        return ResearchWorkflowImplementationV1()

    def codexia_completion_criterion(self, raw_binding):
        expected = research_workflow_binding()
        if raw_binding != expected.to_dict():
            raise ValueError("unsupported or non-exact Research WorkflowBinding")
        return ResearchCompletionCriterionV1()


class _PluginService:
    def __init__(self) -> None:
        self._provider = _ResearchProvider()

    def get(self, plugin_id: str):
        if plugin_id != PROVIDER_REF:
            raise KeyError(plugin_id)
        return self._provider


def _store_path() -> str:
    return os.environ.get("CODEXIA_DW3_STORE", DEFAULT_STORE)


def _required_work_id() -> str:
    work_id = os.environ.get("CODEXIA_DW3_WORK_ID", "").strip()
    if not work_id:
        raise RuntimeError(
            "CODEXIA_DW3_WORK_ID is required for live research progression"
        )
    return work_id


def create_host() -> StandaloneWorkHost:
    """Trusted host factory used by the existing `codexia work` CLI.

    During `work start`, CODEXIA_DW3_WORK_ID is intentionally absent and only
    exact Pack resolution is needed. During `work advance`, the environment
    binds the host to the already-created durable Work so its exact research
    context can be reconstructed from SQLite.
    """

    service = _PluginService()
    raw_work_id = os.environ.get("CODEXIA_DW3_WORK_ID", "").strip()
    if not raw_work_id:
        return StandaloneWorkHost(plugin_service=service)

    store = SqliteWorkStore(_store_path())
    provider = ChatGPTWebProvider(
        auth_file=os.environ.get("CODEXIA_DW3_AUTH_FILE", "auth_data.json"),
        model=os.environ.get("CODEXIA_DW3_MODEL") or None,
        reasoning_effort=os.environ.get("CODEXIA_DW3_REASONING_EFFORT") or None,
        timeout=float(os.environ.get("CODEXIA_DW3_TIMEOUT", "180")),
    )
    return StandaloneWorkHost(
        plugin_service=service,
        cognition_port=ModelProviderCognitionPort(provider),
        instructions=ResearchInstructionsMaterialPort(),
        context=ResearchContextMaterialPort(
            store=store,
            work_id=raw_work_id,
        ),
    )


def materialize() -> dict[str, object]:
    """Materialize the already-durable research outputs without running cognition."""

    result = ResearchWorkMaterializer(SqliteWorkStore(_store_path())).materialize(
        _required_work_id()
    )
    return {
        "evidence_refs": [item.to_dict() for item in result.evidence_refs],
        "artifact_refs": [item.to_dict() for item in result.artifact_refs],
    }


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args != ["materialize"]:
        print(
            "usage: python examples/dw3_research_live_host.py materialize",
            file=sys.stderr,
        )
        return 2
    print(json.dumps(materialize(), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
