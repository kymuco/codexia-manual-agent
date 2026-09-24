from __future__ import annotations

import ast
import hashlib
from pathlib import Path

import pytest

from codexia_manual_agent.domain.errors import ProviderError
from codexia_manual_agent.domain.models import ProviderRequest, ProviderResponse
from codexia_manual_agent.pack_core import (
    PackAdmission,
    PackBinding,
    PackMemberBinding,
    PackMemberKind,
    PackWorkflowBinding,
)
from codexia_manual_agent.providers.model_provider_cognition import (
    ModelProviderCognitionPort,
)
from codexia_manual_agent.role_core import (
    CognitionOutcome,
    CognitionPortRequest,
    CognitionTransportPortError,
    ContextProjection,
    RoleAdmission,
    RoleBinding,
    RoleRun,
    RoleRunState,
    project_cognition_handoff,
)
from codexia_manual_agent.work_core import (
    SqliteWorkStore,
    Work,
    WorkConcurrencyError,
    WorkIngressBinding,
)
from codexia_manual_agent.workflow_core import (
    WorkflowAdmission,
    WorkflowBinding,
    WorkflowRun,
)
from codexia_manual_agent.workflow_orchestration import (
    RoleCognitionMaterializationService,
    RoleCognitionProgressionService,
)

INSTRUCTIONS = "Analyze the exact bounded evidence and return one conclusion."
CONTEXT = "artifact=A; evidence=B"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _workflow_binding() -> WorkflowBinding:
    return WorkflowBinding.create(
        workflow_id="codexia:g2.15-cognition",
        version="1.0.0",
        definition_digest=_sha("g2.15-workflow"),
    )


def _role_binding() -> RoleBinding:
    return RoleBinding.create(
        role_id="codexia:g2.15-reviewer",
        version="1.0.0",
        instructions_digest=_sha(INSTRUCTIONS),
    )


def _pack(
    workflow: WorkflowBinding,
    role: RoleBinding,
) -> PackBinding:
    return PackBinding.create(
        pack_id="codexia:g2.15-pack",
        version="1.0.0",
        definition_digest=_sha("g2.15-pack"),
        members=(
            PackMemberBinding.create(
                kind=PackMemberKind.WORKFLOW,
                semantic_id=workflow.workflow_id,
                version=workflow.version,
                binding_digest=workflow.binding_digest,
            ),
            PackMemberBinding.create(
                kind=PackMemberKind.ROLE,
                semantic_id=role.role_id,
                version=role.version,
                binding_digest=role.binding_digest,
            ),
        ),
    )


class _Instructions:
    def resolve(self, _binding: RoleBinding) -> str:
        return INSTRUCTIONS


class _Context:
    def resolve(self, _projection: ContextProjection) -> str:
        return CONTEXT


def _service(
    store: SqliteWorkStore,
    *,
    context=None,
) -> RoleCognitionProgressionService:
    return RoleCognitionProgressionService(
        store=store,
        instructions=_Instructions(),
        context=context or _Context(),
    )


def _materializer(store: SqliteWorkStore) -> RoleCognitionMaterializationService:
    return RoleCognitionMaterializationService(
        store=store,
        instructions=_Instructions(),
        context=_Context(),
    )


def _active_role(
    store: SqliteWorkStore,
    *,
    source_id: str = "g2.15",
):
    workflow_binding = _workflow_binding()
    role_binding = _role_binding()
    work = Work.create(
        objective="Exercise bounded Role cognition progression",
        ingress=WorkIngressBinding.create(
            source_namespace="standalone.api",
            source_id=source_id,
            payload_digest=_sha(f"payload:{source_id}"),
        ),
    )
    initial = store.create(work)
    workflow_run = WorkflowRun.create(
        snapshot=initial,
        binding=workflow_binding,
    )
    workflow = WorkflowAdmission(store).admit_start(workflow_run)
    PackAdmission(store).admit_workflow_binding(
        PackWorkflowBinding.create(
            workflow=workflow,
            snapshot=store.snapshot(work.work_id),
            pack=_pack(workflow_binding, role_binding),
        )
    )
    role_run = RoleRun.create(
        workflow=workflow,
        snapshot=store.snapshot(work.work_id),
        binding=role_binding,
        context=ContextProjection.create(content_digest=_sha(CONTEXT)),
    )
    role = RoleAdmission(store).admit_start(role_run)
    return work, workflow, role


class FakeProvider:
    provider_id = "g2.15-provider"

    def __init__(self) -> None:
        self.calls: list[ProviderRequest] = []

    def send(self, request: ProviderRequest) -> ProviderResponse:
        self.calls.append(request)
        return ProviderResponse(text="bounded provider result")


class RaisingProvider:
    provider_id = "g2.15-raising"

    def __init__(self) -> None:
        self.calls: list[ProviderRequest] = []

    def send(self, request: ProviderRequest) -> ProviderResponse:
        self.calls.append(request)
        raise ProviderError("ambiguous provider transport")


class AsyncPort:
    def __init__(self, port_id: str = "cognition.g2-15-async") -> None:
        self._port_id = port_id
        self.calls = 0

    @property
    def port_id(self) -> str:
        return self._port_id

    def complete(self, _request: CognitionPortRequest):
        self.calls += 1
        return None


class CountingSuccessPort:
    port_id = "cognition.g2-15-counting"

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, request: CognitionPortRequest):
        self.calls += 1
        return CognitionOutcome.succeeded(
            request.request,
            output_text="counting result",
        )


def test_active_role_progresses_through_real_model_provider_adapter(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    _, _, role = _active_role(store)
    provider = FakeProvider()

    completed = _service(store).progress_once(
        work_id=role.run.work_id,
        role_run_id=role.run.role_run_id,
        port=ModelProviderCognitionPort(provider),
    )

    assert completed.state is RoleRunState.COMPLETED
    assert completed.output_text == "bounded provider result"
    assert provider.calls == [
        ProviderRequest(
            prompt=CONTEXT,
            system=INSTRUCTIONS,
            conversation=None,
        )
    ]


def test_admitted_request_without_handoff_is_rematerialized_then_dispatched(
    tmp_path,
) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    work, _, role = _active_role(store, source_id="request-only")
    prepared = _materializer(store).prepare_request(
        work_id=work.work_id,
        role_run_id=role.run.role_run_id,
    )
    requested = RoleAdmission(store).admit_request(prepared.request)
    assert requested.state is RoleRunState.REQUESTED
    before_request_id = requested.request_id
    provider = FakeProvider()

    completed = _service(store).progress_once(
        work_id=work.work_id,
        role_run_id=role.run.role_run_id,
        port=ModelProviderCognitionPort(provider),
    )

    assert completed.state is RoleRunState.COMPLETED
    assert completed.request_id == before_request_id
    assert len(provider.calls) == 1
    handoff = project_cognition_handoff(
        store.events(work.work_id),
        before_request_id,
    )
    assert handoff.port_id == "model-provider:g2.15-provider"


def test_provider_exception_preserves_handoff_and_retry_never_redispatches(
    tmp_path,
) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    work, _, role = _active_role(store, source_id="ambiguous-provider")
    first = RaisingProvider()

    with pytest.raises(CognitionTransportPortError):
        _service(store).progress_once(
            work_id=work.work_id,
            role_run_id=role.run.role_run_id,
            port=ModelProviderCognitionPort(first),
        )

    assert len(first.calls) == 1
    requested = _materializer(store).rematerialize_request(
        work_id=work.work_id,
        role_run_id=role.run.role_run_id,
    )
    handoff = project_cognition_handoff(
        store.events(work.work_id),
        requested.request.request_id,
    )
    assert handoff.port_id == "model-provider:g2.15-raising"

    retry = RaisingProvider()
    recovered = _service(store).progress_once(
        work_id=work.work_id,
        role_run_id=role.run.role_run_id,
        port=ModelProviderCognitionPort(retry),
    )

    assert recovered.state is RoleRunState.REQUESTED
    assert retry.calls == []


def test_restart_after_async_handoff_never_redispatches(tmp_path) -> None:
    path = tmp_path / "work.sqlite"
    store = SqliteWorkStore(path)
    work, _, role = _active_role(store, source_id="restart")
    first = AsyncPort()

    pending = _service(store).progress_once(
        work_id=work.work_id,
        role_run_id=role.run.role_run_id,
        port=first,
    )

    assert pending.state is RoleRunState.REQUESTED
    assert first.calls == 1

    restarted = SqliteWorkStore(path)
    second = AsyncPort()
    recovered = _service(restarted).progress_once(
        work_id=work.work_id,
        role_run_id=role.run.role_run_id,
        port=second,
    )

    assert recovered.state is RoleRunState.REQUESTED
    assert second.calls == 0


def test_terminal_role_progression_is_idempotent_noop(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    work, _, role = _active_role(store, source_id="terminal")
    provider = FakeProvider()
    completed = _service(store).progress_once(
        work_id=work.work_id,
        role_run_id=role.run.role_run_id,
        port=ModelProviderCognitionPort(provider),
    )
    assert completed.state is RoleRunState.COMPLETED
    before = store.events(work.work_id)
    port = CountingSuccessPort()

    recovered = _service(store).progress_once(
        work_id=work.work_id,
        role_run_id=role.run.role_run_id,
        port=port,
    )

    assert recovered == completed
    assert port.calls == 0
    assert store.events(work.work_id) == before


def test_concurrent_change_before_request_admission_never_calls_port(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    work, _, role = _active_role(store, source_id="concurrent")

    class MutatingContext:
        mutated = False

        def resolve(self, _projection: ContextProjection) -> str:
            if not self.mutated:
                self.mutated = True
                current = store.snapshot(work.work_id)
                store.append(
                    work.work_id,
                    expected_revision=current.revision,
                    event=current.next_event(
                        kind="work.external-observation",
                        payload={"source": "concurrent"},
                    ),
                )
            return CONTEXT

    port = CountingSuccessPort()

    with pytest.raises(WorkConcurrencyError):
        _service(store, context=MutatingContext()).progress_once(
            work_id=work.work_id,
            role_run_id=role.run.role_run_id,
            port=port,
        )

    assert port.calls == 0


def test_progression_source_has_no_scheduler_provider_selection_host_or_loop() -> None:
    root = Path(__file__).resolve().parents[1] / "src" / "codexia_manual_agent"
    path = root / "workflow_orchestration" / "cognition_progression.py"
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)

    imported_names: set[str] = set()
    imported_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module is not None:
                imported_modules.add(node.module)
            imported_names.update(alias.name for alias in node.names)

    forbidden = {
        "ModelProvider",
        "ModelProviderCognitionPort",
        "ChatGPTWebProvider",
        "CapabilityHostBridge",
        "StandaloneProcessCapabilityPort",
        "Scheduler",
    }
    assert forbidden.isdisjoint(imported_names)
    assert not any(
        module.endswith((".providers", ".authority", ".execution", ".standalone_host"))
        for module in imported_modules
    )
    assert not any(isinstance(node, ast.While) for node in ast.walk(tree))
