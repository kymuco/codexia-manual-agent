from __future__ import annotations

import ast
import hashlib
import json
from dataclasses import replace

import pytest

from codexia_manual_agent.pack_core import (
    PackAdmission,
    PackBinding,
    PackMemberBinding,
    PackMemberKind,
    PackWorkflowBinding,
)
from codexia_manual_agent.role_core import (
    CognitionRequest,
    ContextProjection,
    InvalidRoleRecord,
    RoleAdmission,
    RoleBinding,
    RoleRun,
    RoleRunState,
    project_role_run,
)
from codexia_manual_agent.work_core import (
    SqliteWorkStore,
    Work,
    WorkConcurrencyError,
    WorkIngressBinding,
)
from codexia_manual_agent.workflow_core import (
    WORKFLOW_COMPLETED_EVENT,
    WorkflowAdmission,
    WorkflowBinding,
    WorkflowCandidate,
    WorkflowRun,
)
from codexia_manual_agent.workflow_orchestration import (
    RoleCognitionMaterialBindingError,
    RoleCognitionMaterializationService,
    RoleCognitionPackRequiredError,
    RoleCognitionReadConflictError,
    RoleCognitionStateError,
)

INSTRUCTIONS = "Analyze the exact bounded evidence and return one conclusion."
CONTEXT = "artifact=A; evidence=B; unresolved=C"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _workflow_binding() -> WorkflowBinding:
    return WorkflowBinding.create(
        workflow_id="codexia:cognition-workflow",
        version="1.0.0",
        definition_digest=_sha("cognition-workflow-v1"),
    )


def _role_binding() -> RoleBinding:
    return RoleBinding.create(
        role_id="codexia:reviewer",
        version="1.0.0",
        instructions_digest=_sha(INSTRUCTIONS),
    )


def _pack(
    workflow: WorkflowBinding,
    role: RoleBinding,
) -> PackBinding:
    return PackBinding.create(
        pack_id="codexia:cognition-pack",
        version="1.0.0",
        definition_digest=_sha("cognition-pack-v1"),
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
    def __init__(self, value: str = INSTRUCTIONS) -> None:
        self.value = value
        self.calls: list[RoleBinding] = []

    def resolve(self, binding: RoleBinding) -> str:
        self.calls.append(binding)
        return self.value


class _Context:
    def __init__(self, value: str = CONTEXT) -> None:
        self.value = value
        self.calls: list[ContextProjection] = []

    def resolve(self, projection: ContextProjection) -> str:
        self.calls.append(projection)
        return self.value


def _started(
    store: SqliteWorkStore,
    *,
    with_pack: bool = True,
    source_id: str = "g2.12",
):
    workflow_binding = _workflow_binding()
    role_binding = _role_binding()
    work = Work.create(
        objective="Exercise exact Role cognition materialization",
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

    pin = None
    if with_pack:
        pin = PackAdmission(store).admit_workflow_binding(
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
        context=ContextProjection.create(
            content_digest=_sha(CONTEXT),
        ),
    )
    role = RoleAdmission(store).admit_start(role_run)
    return work, workflow, role, pin


def _materializer(
    store,
    *,
    instructions: _Instructions | None = None,
    context: _Context | None = None,
):
    return RoleCognitionMaterializationService(
        store=store,
        instructions=instructions or _Instructions(),
        context=context or _Context(),
    )


def test_prepare_request_resolves_exact_material_without_work_mutation(
    tmp_path,
) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    work, _, role, pin = _started(store)
    assert pin is not None
    instructions = _Instructions()
    context = _Context()
    before = store.events(work.work_id)

    result = _materializer(
        store,
        instructions=instructions,
        context=context,
    ).prepare_request(
        work_id=work.work_id,
        role_run_id=role.run.role_run_id,
    )

    assert result.admitted is False
    assert result.request.role_run_id == role.run.role_run_id
    assert result.request.instructions == INSTRUCTIONS
    assert result.request.context == CONTEXT
    assert result.request.instructions_digest == _sha(INSTRUCTIONS)
    assert result.request.context_digest == _sha(CONTEXT)
    assert result.pack_binding_digest == pin.pack.binding_digest
    assert result.role_binding_digest == role.run.binding.binding_digest
    assert (
        result.context_projection_digest
        == role.run.context.projection_digest
    )
    assert store.events(work.work_id) == before
    assert instructions.calls == [role.run.binding]
    assert context.calls == [role.run.context]


def test_prepared_request_requires_explicit_role_admission(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    work, _, role, _ = _started(store)
    prepared = _materializer(store).prepare_request(
        work_id=work.work_id,
        role_run_id=role.run.role_run_id,
    )
    before = store.events(work.work_id)

    requested = RoleAdmission(store).admit_request(prepared.request)

    assert len(store.events(work.work_id)) == len(before) + 1
    assert requested.state is RoleRunState.REQUESTED
    assert requested.request_id == prepared.request.request_id
    assert requested.request_digest == prepared.request.request_digest


def test_plaintext_is_not_persisted_when_request_is_admitted(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    work, _, role, _ = _started(store)
    prepared = _materializer(store).prepare_request(
        work_id=work.work_id,
        role_run_id=role.run.role_run_id,
    )
    RoleAdmission(store).admit_request(prepared.request)

    chronology = json.dumps(
        [event.to_dict() for event in store.events(work.work_id)],
        ensure_ascii=False,
    )

    assert INSTRUCTIONS not in chronology
    assert CONTEXT not in chronology
    assert prepared.request.instructions_digest in chronology
    assert prepared.request.context_digest in chronology


def test_restart_rematerializes_same_exact_admitted_request(tmp_path) -> None:
    path = tmp_path / "work.sqlite"
    store = SqliteWorkStore(path)
    work, _, role, _ = _started(store)
    prepared = _materializer(store).prepare_request(
        work_id=work.work_id,
        role_run_id=role.run.role_run_id,
    )
    RoleAdmission(store).admit_request(prepared.request)

    restarted = SqliteWorkStore(path)
    recovered = _materializer(restarted).rematerialize_request(
        work_id=work.work_id,
        role_run_id=role.run.role_run_id,
    )

    assert recovered.admitted is True
    assert recovered.request == prepared.request
    assert recovered.request.request_id == prepared.request.request_id
    assert recovered.request.request_digest == prepared.request.request_digest
    assert recovered.request.instructions == INSTRUCTIONS
    assert recovered.request.context == CONTEXT


def test_wrong_instruction_material_fails_closed_without_mutation(
    tmp_path,
) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    work, _, role, _ = _started(store)
    before = store.events(work.work_id)

    with pytest.raises(RoleCognitionMaterialBindingError):
        _materializer(
            store,
            instructions=_Instructions("different instructions"),
        ).prepare_request(
            work_id=work.work_id,
            role_run_id=role.run.role_run_id,
        )

    assert store.events(work.work_id) == before


def test_wrong_context_material_fails_closed_without_mutation(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    work, _, role, _ = _started(store)
    before = store.events(work.work_id)

    with pytest.raises(RoleCognitionMaterialBindingError):
        _materializer(
            store,
            context=_Context("different context"),
        ).prepare_request(
            work_id=work.work_id,
            role_run_id=role.run.role_run_id,
        )

    assert store.events(work.work_id) == before


def test_rematerialization_rejects_changed_material_after_restart(
    tmp_path,
) -> None:
    path = tmp_path / "work.sqlite"
    store = SqliteWorkStore(path)
    work, _, role, _ = _started(store)
    prepared = _materializer(store).prepare_request(
        work_id=work.work_id,
        role_run_id=role.run.role_run_id,
    )
    RoleAdmission(store).admit_request(prepared.request)

    restarted = SqliteWorkStore(path)

    with pytest.raises(RoleCognitionMaterialBindingError):
        _materializer(
            restarted,
            context=_Context("mutated context"),
        ).rematerialize_request(
            work_id=work.work_id,
            role_run_id=role.run.role_run_id,
        )


def test_prepare_rejects_second_request_after_admission(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    work, _, role, _ = _started(store)
    prepared = _materializer(store).prepare_request(
        work_id=work.work_id,
        role_run_id=role.run.role_run_id,
    )
    RoleAdmission(store).admit_request(prepared.request)

    with pytest.raises(RoleCognitionStateError):
        _materializer(store).prepare_request(
            work_id=work.work_id,
            role_run_id=role.run.role_run_id,
        )


def test_rematerialize_requires_durable_requested_state(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    work, _, role, _ = _started(store)

    with pytest.raises(RoleCognitionStateError):
        _materializer(store).rematerialize_request(
            work_id=work.work_id,
            role_run_id=role.run.role_run_id,
        )


def test_materialization_requires_durable_pack_pin(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    work, _, role, _ = _started(
        store,
        with_pack=False,
        source_id="unpacked",
    )

    with pytest.raises(RoleCognitionPackRequiredError):
        _materializer(store).prepare_request(
            work_id=work.work_id,
            role_run_id=role.run.role_run_id,
        )


def test_terminal_workflow_blocks_new_cognition_materialization(
    tmp_path,
) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    work, workflow, role, _ = _started(store)
    completion = WorkflowCandidate.create(
        run_snapshot=workflow,
        work_snapshot=store.snapshot(work.work_id),
        event_kind=WORKFLOW_COMPLETED_EVENT,
    )
    WorkflowAdmission(store).admit_candidate(completion)

    with pytest.raises(RoleCognitionStateError):
        _materializer(store).prepare_request(
            work_id=work.work_id,
            role_run_id=role.run.role_run_id,
        )


def test_inconsistent_recovery_read_fails_closed(tmp_path) -> None:
    backing = SqliteWorkStore(tmp_path / "work.sqlite")
    work, _, role, _ = _started(backing)

    class ConflictingReadStore:
        def events(self, work_id: str):
            return backing.events(work_id)

        def snapshot(self, work_id: str):
            snapshot = backing.snapshot(work_id)
            return replace(snapshot, revision=snapshot.revision + 1)

    with pytest.raises(RoleCognitionReadConflictError):
        _materializer(ConflictingReadStore()).prepare_request(
            work_id=work.work_id,
            role_run_id=role.run.role_run_id,
        )


def test_post_prepare_concurrency_is_rejected_by_existing_admission_cas(
    tmp_path,
) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    work, _, role, _ = _started(store)
    prepared = _materializer(store).prepare_request(
        work_id=work.work_id,
        role_run_id=role.run.role_run_id,
    )

    current = store.snapshot(work.work_id)
    store.append(
        work.work_id,
        expected_revision=current.revision,
        event=current.next_event(
            kind="work.external-observation",
            payload={"source": "concurrent"},
        ),
    )

    with pytest.raises(WorkConcurrencyError):
        RoleAdmission(store).admit_request(prepared.request)


def test_material_sources_are_separate_and_each_resolved_once(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    work, _, role, _ = _started(store)
    instructions = _Instructions()
    context = _Context()

    _materializer(
        store,
        instructions=instructions,
        context=context,
    ).prepare_request(
        work_id=work.work_id,
        role_run_id=role.run.role_run_id,
    )

    assert instructions.calls == [role.run.binding]
    assert context.calls == [role.run.context]


def test_from_durable_dict_rejects_non_exact_record_shape(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    work, _, role, _ = _started(store)
    prepared = _materializer(store).prepare_request(
        work_id=work.work_id,
        role_run_id=role.run.role_run_id,
    )
    record = prepared.request.durable_dict()
    record["unexpected"] = True

    with pytest.raises(InvalidRoleRecord):
        CognitionRequest.from_durable_dict(
            record,
            instructions=INSTRUCTIONS,
            context=CONTEXT,
        )


def test_materialization_source_has_no_admission_model_or_execution_surface() -> None:
    root = (
        __import__("pathlib").Path(__file__).resolve().parents[1]
        / "src"
        / "codexia_manual_agent"
    )
    path = root / "workflow_orchestration" / "role_cognition.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))

    imported_names: set[str] = set()
    imported_modules: set[str] = set()
    called_attributes: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module is not None:
                imported_modules.add(node.module)
            imported_names.update(alias.name for alias in node.names)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
        ):
            called_attributes.add(node.func.attr)

    forbidden_names = {
        "RoleAdmission",
        "CognitionPort",
        "WorkflowAdmission",
        "CapabilityAdmission",
        "CapabilityHostBridge",
        "ProcessExecutor",
        "Scheduler",
    }
    assert forbidden_names.isdisjoint(imported_names)
    assert "append" not in called_attributes
    assert "complete" not in called_attributes
    assert not any(isinstance(node, ast.While) for node in ast.walk(tree))
    assert not any(
        module.endswith((".admission", ".authority", ".execution"))
        for module in imported_modules
    )
