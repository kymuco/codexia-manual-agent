from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

from codexia_manual_agent.capability_core import (
    CapabilityAdmission,
    CapabilityBinding,
    CapabilityHostBridge,
    CapabilityNeed,
    CapabilityNeedState,
    CapabilityOutcomeStatus,
)
from codexia_manual_agent.execution import ProcessLimits
from codexia_manual_agent.standalone_host import (
    PROCESS_CAPABILITY_ID,
    PROCESS_CAPABILITY_VERSION,
    PROCESS_OPERATION,
    STANDALONE_PROCESS_HOST_ID,
    StandaloneProcessCapabilityPort,
    StandaloneProcessHostError,
)
from codexia_manual_agent.work_core import SqliteWorkStore, Work, WorkIngressBinding
from codexia_manual_agent.workflow_core import (
    WorkflowAdmission,
    WorkflowBinding,
    WorkflowRun,
)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _binding(*, contract: str = "process-contract-v1") -> CapabilityBinding:
    return CapabilityBinding.create(
        capability_id=PROCESS_CAPABILITY_ID,
        version=PROCESS_CAPABILITY_VERSION,
        contract_digest=_sha(contract),
    )


def _parameters(
    *,
    argv: list[str] | None = None,
    cwd_ref: str = "workspace",
    cwd: str = ".",
    limits: ProcessLimits | None = None,
) -> dict[str, object]:
    return {
        "argv": argv or [sys.executable, "-V"],
        "cwd_ref": cwd_ref,
        "cwd": cwd,
        "limits": (limits or ProcessLimits()).to_dict(),
    }


def _pending_need(
    store: SqliteWorkStore,
    *,
    binding: CapabilityBinding,
    parameters: dict[str, object] | None = None,
    operation: str = PROCESS_OPERATION,
):
    work = Work.create(
        objective="Exercise standalone process host adapter",
        ingress=WorkIngressBinding.create(
            source_namespace="standalone.api",
            source_id=f"g2.6-{hashlib.sha256(str(id(store)).encode()).hexdigest()[:12]}",
            payload_digest=_sha("payload"),
        ),
    )
    initial = store.create(work)
    workflow_run = WorkflowRun.create(
        snapshot=initial,
        binding=WorkflowBinding.create(
            workflow_id="codexia:g2.6-test",
            version="1.0.0",
            definition_digest=_sha("workflow-definition"),
        ),
    )
    workflow = WorkflowAdmission(store).admit_start(workflow_run)
    need = CapabilityNeed.create(
        workflow=workflow,
        snapshot=store.snapshot(work.work_id),
        binding=binding,
        operation=operation,
        parameters=parameters or _parameters(),
    )
    pending = CapabilityAdmission(store).admit_need(need)
    return workflow_run, pending


def test_adapter_requires_explicit_boolean_approval(tmp_path) -> None:
    with pytest.raises(TypeError):
        StandaloneProcessCapabilityPort(
            workspace=tmp_path,
            binding=_binding(),
            approved=None,  # type: ignore[arg-type]
        )


def test_adapter_rejects_non_process_binding_at_configuration(tmp_path) -> None:
    binding = CapabilityBinding.create(
        capability_id="filesystem",
        version=PROCESS_CAPABILITY_VERSION,
        contract_digest=_sha("filesystem-contract"),
    )

    with pytest.raises(StandaloneProcessHostError):
        StandaloneProcessCapabilityPort(
            workspace=tmp_path,
            binding=binding,
            approved=True,
        )


def test_approved_process_uses_existing_authority_and_execution_path(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    binding = _binding()
    _, pending = _pending_need(store, binding=binding)
    port = StandaloneProcessCapabilityPort(
        workspace=tmp_path,
        binding=binding,
        approved=True,
        actor="test-human",
    )

    resolved = CapabilityHostBridge(store).dispatch_once(pending, port)

    assert resolved.state is CapabilityNeedState.SUCCEEDED
    assert resolved.outcome is not None
    assert resolved.outcome.status is CapabilityOutcomeStatus.SUCCEEDED
    assert resolved.outcome.attempt_id.startswith("standalone-process:")

    observation = resolved.outcome.observation
    assert observation["adapter"] == "standalone-process-host-v1"
    assert observation["authorization"]["decision"] == "allow"
    assert observation["authorization"]["source"] == "human"
    assert observation["proposal"]["proposal_digest"]
    assert observation["execution"]["observation_digest"]
    assert observation["execution"]["started"] is True
    assert observation["execution"]["exit_code"] == 0
    assert observation["execution"]["termination_reason"] == "exited"


def test_denied_process_becomes_known_failed_outcome_without_execution(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    binding = _binding()
    _, pending = _pending_need(store, binding=binding)
    port = StandaloneProcessCapabilityPort(
        workspace=tmp_path,
        binding=binding,
        approved=False,
        actor="test-human",
        reason="not approved",
    )

    resolved = CapabilityHostBridge(store).dispatch_once(pending, port)

    assert resolved.state is CapabilityNeedState.FAILED
    assert resolved.outcome is not None
    assert resolved.outcome.status is CapabilityOutcomeStatus.FAILED
    assert resolved.outcome.observation["stage"] == "rejected_before_observed_effect"
    assert (
        resolved.outcome.observation["error_type"]
        == "AuthorizationDeniedError"
    )


def test_nonzero_exit_is_known_failed_outcome_with_observation(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    binding = _binding()
    _, pending = _pending_need(
        store,
        binding=binding,
        parameters=_parameters(
            argv=[sys.executable, "-c", "raise SystemExit(7)"],
        ),
    )
    port = StandaloneProcessCapabilityPort(
        workspace=tmp_path,
        binding=binding,
        approved=True,
    )

    resolved = CapabilityHostBridge(store).dispatch_once(pending, port)

    assert resolved.state is CapabilityNeedState.FAILED
    assert resolved.outcome is not None
    execution = resolved.outcome.observation["execution"]
    assert execution["started"] is True
    assert execution["termination_reason"] == "exited"
    assert execution["exit_code"] == 7


def test_timeout_is_known_failed_outcome(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    binding = _binding()
    _, pending = _pending_need(
        store,
        binding=binding,
        parameters=_parameters(
            argv=[sys.executable, "-c", "import time; time.sleep(2)"],
            limits=ProcessLimits(timeout_seconds=0.1),
        ),
    )
    port = StandaloneProcessCapabilityPort(
        workspace=tmp_path,
        binding=binding,
        approved=True,
    )

    resolved = CapabilityHostBridge(store).dispatch_once(pending, port)

    assert resolved.state is CapabilityNeedState.FAILED
    assert resolved.outcome is not None
    assert (
        resolved.outcome.observation["execution"]["termination_reason"]
        == "timeout"
    )


def test_exact_binding_mismatch_fails_closed(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    supported = _binding(contract="supported-contract")
    _, pending = _pending_need(
        store,
        binding=_binding(contract="different-contract"),
    )
    port = StandaloneProcessCapabilityPort(
        workspace=tmp_path,
        binding=supported,
        approved=True,
    )

    resolved = CapabilityHostBridge(store).dispatch_once(pending, port)

    assert resolved.state is CapabilityNeedState.FAILED
    assert resolved.outcome is not None
    assert resolved.outcome.observation["error_type"] == "StandaloneProcessHostError"


@pytest.mark.parametrize(
    ("parameters", "operation"),
    [
        (_parameters(cwd_ref="host-path"), PROCESS_OPERATION),
        ({"argv": [sys.executable, "-V"]}, PROCESS_OPERATION),
        (_parameters(), "shell"),
    ],
)
def test_unsupported_need_semantics_fail_closed(
    tmp_path,
    parameters: dict[str, object],
    operation: str,
) -> None:
    store = SqliteWorkStore(tmp_path / f"{operation}.sqlite")
    binding = _binding()
    _, pending = _pending_need(
        store,
        binding=binding,
        parameters=parameters,
        operation=operation,
    )
    port = StandaloneProcessCapabilityPort(
        workspace=tmp_path,
        binding=binding,
        approved=True,
    )

    resolved = CapabilityHostBridge(store).dispatch_once(pending, port)

    assert resolved.state is CapabilityNeedState.FAILED
    assert resolved.outcome is not None
    assert resolved.outcome.observation["error_type"] == "StandaloneProcessHostError"


def test_workspace_root_is_host_configuration_not_need_payload(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    binding = _binding()
    _, pending = _pending_need(store, binding=binding)
    parameters = pending.need.to_dict()["parameters"]

    assert str(tmp_path.resolve()) not in str(parameters)
    assert parameters["cwd_ref"] == "workspace"

    port = StandaloneProcessCapabilityPort(
        workspace=tmp_path,
        binding=binding,
        approved=True,
    )
    assert port.host_id == STANDALONE_PROCESS_HOST_ID


def test_unexpected_service_exception_becomes_unknown_outcome(tmp_path) -> None:
    class ExplodingService:
        def run(self, **kwargs):
            raise RuntimeError("synthetic adapter ambiguity")

    store = SqliteWorkStore(tmp_path / "work.sqlite")
    binding = _binding()
    _, pending = _pending_need(store, binding=binding)
    port = StandaloneProcessCapabilityPort(
        workspace=tmp_path,
        binding=binding,
        approved=True,
        service=ExplodingService(),  # type: ignore[arg-type]
    )

    resolved = CapabilityHostBridge(store).dispatch_once(pending, port)

    assert resolved.state is CapabilityNeedState.OUTCOME_UNKNOWN
    assert resolved.outcome is not None
    assert resolved.outcome.status is CapabilityOutcomeStatus.UNKNOWN
    assert resolved.outcome.observation["stage"] == "adapter_exception"
    assert resolved.outcome.observation["error_type"] == "RuntimeError"


def test_standalone_host_depends_on_core_not_core_on_standalone_host() -> None:
    root = Path(__file__).resolve().parents[1] / "src" / "codexia_manual_agent"
    core = root / "capability_core"

    for path in core.glob("*.py"):
        assert "standalone_host" not in path.read_text(encoding="utf-8")
