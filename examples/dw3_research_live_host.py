from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from codexia_manual_agent.domain.errors import ProviderError
from codexia_manual_agent.domain.models import (
    ProviderConversation,
    ProviderRequest,
    ProviderResponse,
)
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


class _ExternalCwaProvider:
    """Use an independently managed CWA installation through its stable CLI."""

    def __init__(
        self,
        *,
        executable: str,
        auth_file: str,
        profile: str,
        timeout: float,
    ) -> None:
        self._executable = Path(executable).expanduser()
        if not self._executable.is_file():
            raise RuntimeError(
                "CODEXIA_DW3_CWA_EXE must point to the independently managed cwa executable"
            )
        self._auth_file = auth_file
        self._profile = profile
        self._timeout = timeout

    @property
    def provider_id(self) -> str:
        return "cwa-subprocess"

    def send(self, request: ProviderRequest) -> ProviderResponse:
        prompt = request.prompt
        if request.system is not None and request.system.strip():
            prompt = (
                "[Codexia product-runtime system context]\n"
                f"{request.system.strip()}\n\n"
                "[Codexia product-runtime request]\n"
                f"{request.prompt}"
            )

        command = [
            str(self._executable),
            "send",
            prompt,
            "--profile",
            self._profile,
            "--timeout",
            str(self._timeout),
            "--auth-file",
            self._auth_file,
            "--json",
        ]
        if request.conversation is not None and request.conversation.conversation_id:
            command.extend(["--conversation", request.conversation.conversation_id])

        child_env = os.environ.copy()
        child_env["PYTHONIOENCODING"] = "utf-8"
        child_env["PYTHONUTF8"] = "1"
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            env=child_env,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()
            raise ProviderError(
                f"external CWA request failed with exit {completed.returncode}: {detail}"
            )

        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise ProviderError("external CWA did not return JSON") from exc
        if payload.get("ok") is not True or not isinstance(payload.get("text"), str):
            raise ProviderError("external CWA returned an invalid successful execution")

        return ProviderResponse(
            text=payload["text"],
            conversation=ProviderConversation(
                conversation_id=payload.get("conversation_id"),
                message_id=payload.get("message_id"),
                finish_reason=payload.get("finish_reason"),
            ),
            model=payload.get("observed_model"),
            metrics={
                "backend_status": payload.get("backend_status"),
                "transport": payload.get("transport"),
                "runtime_observation": payload.get("runtime_observation"),
                "provenance": payload.get("provenance"),
            },
        )


def _cwa_profile() -> str:
    explicit = os.environ.get("CODEXIA_DW3_CWA_PROFILE", "").strip().upper()
    if explicit:
        if explicit not in {"FAST", "BALANCED", "DEEP"}:
            raise RuntimeError(
                "CODEXIA_DW3_CWA_PROFILE must be FAST, BALANCED, or DEEP"
            )
        return explicit

    reasoning = os.environ.get("CODEXIA_DW3_REASONING_EFFORT", "").strip().lower()
    return {
        "minimal": "FAST",
        "low": "FAST",
        "instant": "FAST",
        "fast": "FAST",
        "medium": "BALANCED",
        "standard": "BALANCED",
        "balanced": "BALANCED",
        "high": "DEEP",
        "extended": "DEEP",
        "deep": "DEEP",
    }.get(reasoning, "DEEP")


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
    cwa_executable = os.environ.get("CODEXIA_DW3_CWA_EXE", "").strip()
    if not cwa_executable:
        raise RuntimeError(
            "CODEXIA_DW3_CWA_EXE is required for live research progression"
        )
    provider = _ExternalCwaProvider(
        executable=cwa_executable,
        auth_file=os.environ.get("CODEXIA_DW3_AUTH_FILE", "auth_data.json"),
        profile=_cwa_profile(),
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
