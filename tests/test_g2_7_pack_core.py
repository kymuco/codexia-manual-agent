from __future__ import annotations

import hashlib

import pytest

from codexia_manual_agent.capability_core import (
    CapabilityAdmission,
    CapabilityBinding,
    CapabilityBindingError,
    CapabilityNeed,
    CapabilityProjectionError,
    project_capability_needs,
)
from codexia_manual_agent.pack_core import (
    InvalidPackRecord,
    PackAdmission,
    PackBinding,
    PackMemberBinding,
    PackMemberKind,
    PackProjectionError,
    PackWorkflowBinding,
    project_pack_workflow_bindings,
    project_workflow_pack_binding,
)
from codexia_manual_agent.role_core import (
    ContextProjection,
    RoleAdmission,
    RoleBinding,
    RoleBindingError,
    RoleProjectionError,
    RoleRun,
    project_role_runs,
)
from codexia_manual_agent.work_core import (
    SqliteWorkStore,
    Work,
    WorkConcurrencyError,
    WorkEvent,
    WorkIngressBinding,
)
from codexia_manual_agent.workflow_core import (
    WorkflowAdmission,
    WorkflowBinding,
    WorkflowCandidate,
    WorkflowRun,
)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _workflow_binding(
    *,
    definition: str = "workflow-v1",
) -> WorkflowBinding:
    return WorkflowBinding.create(
        workflow_id="codexia:pack-proof",
        version="1.0.0",
        definition_digest=_sha(definition),
    )


def _role_binding(
    *,
    instructions: str = "review the exact bounded problem",
) -> RoleBinding:
    return RoleBinding.create(
        role_id="reviewer",
        version="1.0.0",
        instructions_digest=_sha(instructions),
    )


def _capability_binding(
    *,
    contract: str = "process-run-contract-v1",
) -> CapabilityBinding:
    return CapabilityBinding.create(
        capability_id="process",
        version="1.0.0",
        contract_digest=_sha(contract),
    )


def _member_for_workflow(binding: WorkflowBinding) -> PackMemberBinding:
    return PackMemberBinding.create(
        kind=PackMemberKind.WORKFLOW,
        semantic_id=binding.workflow_id,
        version=binding.version,
        binding_digest=binding.binding_digest,
    )


def _member_for_role(binding: RoleBinding) -> PackMemberBinding:
    return PackMemberBinding.create(
        kind=PackMemberKind.ROLE,
        semantic_id=binding.role_id,
        version=binding.version,
        binding_digest=binding.binding_digest,
    )


def _member_for_capability(binding: CapabilityBinding) -> PackMemberBinding:
    return PackMemberBinding.create(
        kind=PackMemberKind.CAPABILITY,
        semantic_id=binding.capability_id,
        version=binding.version,
        binding_digest=binding.binding_digest,
    )


def _pack(
    workflow_binding: WorkflowBinding,
    *,
    roles: tuple[RoleBinding, ...] = (),
    capabilities: tuple[CapabilityBinding, ...] = (),
    pack_id: str = "codexia:proof-pack",
    version: str = "1.0.0",
    definition: str = "proof-pack-definition-v1",
) -> PackBinding:
    members = [_member_for_workflow(workflow_binding)]
    members.extend(_member_for_role(item) for item in roles)
    members.extend(_member_for_capability(item) for item in capabilities)
    return PackBinding.create(
        pack_id=pack_id,
        version=version,
        definition_digest=_sha(definition),
        members=members,
    )


def _started_workflow(
    store: SqliteWorkStore,
    *,
    source_id: str = "g2.7",
    workflow_binding: WorkflowBinding | None = None,
):
    binding = workflow_binding or _workflow_binding()
    work = Work.create(
        objective="Exercise exact Pack semantic binding",
        ingress=WorkIngressBinding.create(
            source_namespace="standalone.api",
            source_id=source_id,
            payload_digest=_sha(f"payload:{source_id}"),
        ),
    )
    initial = store.create(work)
    run = WorkflowRun.create(
        snapshot=initial,
        binding=binding,
    )
    workflow = WorkflowAdmission(store).admit_start(run)
    return work, workflow


def _bind_pack(
    store: SqliteWorkStore,
    workflow,
    pack: PackBinding,
) -> PackWorkflowBinding:
    pin = PackWorkflowBinding.create(
        workflow=workflow,
        snapshot=store.snapshot(workflow.run.work_id),
        pack=pack,
    )
    return PackAdmission(store).admit_workflow_binding(pin)


def _role_run(
    store: SqliteWorkStore,
    workflow,
    binding: RoleBinding,
) -> RoleRun:
    return RoleRun.create(
        workflow=workflow,
        snapshot=store.snapshot(workflow.run.work_id),
        binding=binding,
        context=ContextProjection.create(
            content_digest=_sha("bounded-context"),
        ),
    )


def _capability_need(
    store: SqliteWorkStore,
    workflow,
    binding: CapabilityBinding,
) -> CapabilityNeed:
    return CapabilityNeed.create(
        workflow=workflow,
        snapshot=store.snapshot(workflow.run.work_id),
        binding=binding,
        operation="run",
        parameters={
            "argv": ["python", "-V"],
            "cwd_ref": "workspace",
        },
    )


def test_pack_binding_changes_with_exact_member_binding() -> None:
    workflow = _workflow_binding()
    first_role = _role_binding(instructions="first")
    second_role = _role_binding(instructions="second")

    first = _pack(workflow, roles=(first_role,))
    second = _pack(workflow, roles=(second_role,))

    assert first.pack_id == second.pack_id
    assert first.version == second.version
    assert first.definition_digest == second.definition_digest
    assert first.binding_digest != second.binding_digest


def test_pack_rejects_two_versions_of_same_semantic_identity() -> None:
    workflow = _workflow_binding()
    first = PackMemberBinding.create(
        kind=PackMemberKind.ROLE,
        semantic_id="reviewer",
        version="1.0.0",
        binding_digest=_sha("role-v1"),
    )
    second = PackMemberBinding.create(
        kind=PackMemberKind.ROLE,
        semantic_id="reviewer",
        version="2.0.0",
        binding_digest=_sha("role-v2"),
    )

    with pytest.raises(InvalidPackRecord):
        PackBinding.create(
            pack_id="codexia:duplicate-pack",
            version="1.0.0",
            definition_digest=_sha("duplicate-pack"),
            members=(
                _member_for_workflow(workflow),
                first,
                second,
            ),
        )


def test_pack_record_contains_semantics_not_plugin_runtime_metadata() -> None:
    pack = _pack(
        _workflow_binding(),
        roles=(_role_binding(),),
        capabilities=(_capability_binding(),),
    )

    record = pack.to_dict()

    assert set(record) == {
        "schema_version",
        "pack_id",
        "version",
        "definition_digest",
        "members",
        "binding_digest",
    }
    assert {
        "source",
        "entrypoint",
        "class_name",
        "dependencies",
        "runtime",
        "deps",
        "authority",
        "capability_handles",
    }.isdisjoint(record)


def test_pack_recovery_rejects_invariant_manifest_fields() -> None:
    record = _pack(_workflow_binding()).to_dict()
    record["source"] = {
        "code_path": "plugin.py",
        "class_name": "Plugin",
    }

    with pytest.raises(InvalidPackRecord):
        PackBinding.from_dict(record)


def test_prepared_pack_pin_is_not_truth_before_admission(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    _, workflow = _started_workflow(store)
    pin = PackWorkflowBinding.create(
        workflow=workflow,
        snapshot=store.snapshot(workflow.run.work_id),
        pack=_pack(workflow.run.binding),
    )

    assert project_workflow_pack_binding(
        store.events(workflow.run.work_id),
        workflow.run.workflow_run_id,
    ) is None
    assert pin.binding_id not in {
        event.event_id for event in store.events(workflow.run.work_id)
    }


def test_pack_pin_is_first_event_after_workflow_start_and_recovers(tmp_path) -> None:
    path = tmp_path / "work.sqlite"
    store = SqliteWorkStore(path)
    _, workflow = _started_workflow(store)
    pack = _pack(workflow.run.binding)
    pin = _bind_pack(store, workflow, pack)

    events = store.events(workflow.run.work_id)
    start = workflow.run.to_start_event()
    pin_event = next(
        event for event in events if event.event_id == pin.binding_id
    )

    assert pin_event.sequence == start.sequence + 1
    assert pin_event.previous_event_digest == start.event_digest
    assert project_workflow_pack_binding(
        events,
        workflow.run.workflow_run_id,
    ) == pin

    restarted = SqliteWorkStore(path)
    assert project_workflow_pack_binding(
        restarted.events(workflow.run.work_id),
        workflow.run.workflow_run_id,
    ) == pin


def test_exact_pack_pin_retry_is_idempotent_after_later_progress(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    _, workflow = _started_workflow(store)
    pin = _bind_pack(
        store,
        workflow,
        _pack(workflow.run.binding),
    )

    current = store.snapshot(workflow.run.work_id)
    candidate = WorkflowCandidate.create(
        run_snapshot=workflow,
        work_snapshot=current,
        event_kind="workflow.note",
        payload={"note": "later progression"},
    )
    WorkflowAdmission(store).admit_candidate(candidate)
    before = len(store.events(workflow.run.work_id))

    retried = PackAdmission(store).admit_workflow_binding(pin)

    assert retried == pin
    assert len(store.events(workflow.run.work_id)) == before


def test_late_pack_pin_is_rejected_after_workflow_progress(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    _, workflow = _started_workflow(store)
    immediate = store.snapshot(workflow.run.work_id)
    pin = PackWorkflowBinding.create(
        workflow=workflow,
        snapshot=immediate,
        pack=_pack(workflow.run.binding),
    )
    candidate = WorkflowCandidate.create(
        run_snapshot=workflow,
        work_snapshot=immediate,
        event_kind="workflow.note",
        payload={"note": "wins the revision"},
    )
    WorkflowAdmission(store).admit_candidate(candidate)

    with pytest.raises(WorkConcurrencyError):
        PackAdmission(store).admit_workflow_binding(pin)


def test_pack_must_contain_exact_requesting_workflow(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    _, workflow = _started_workflow(store)
    other = _workflow_binding(definition="other-definition")
    pack = _pack(other)

    with pytest.raises(InvalidPackRecord):
        PackWorkflowBinding.create(
            workflow=workflow,
            snapshot=store.snapshot(workflow.run.work_id),
            pack=pack,
        )


def test_role_member_in_pack_is_admitted(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    _, workflow = _started_workflow(store)
    role = _role_binding()
    _bind_pack(
        store,
        workflow,
        _pack(workflow.run.binding, roles=(role,)),
    )

    run = _role_run(store, workflow, role)
    admitted = RoleAdmission(store).admit_start(run)

    assert admitted.run == run


def test_role_not_in_pack_is_rejected_at_admission(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    _, workflow = _started_workflow(store)
    _bind_pack(
        store,
        workflow,
        _pack(workflow.run.binding),
    )
    run = _role_run(store, workflow, _role_binding())

    with pytest.raises(RoleBindingError):
        RoleAdmission(store).admit_start(run)


def test_same_role_id_version_with_changed_digest_is_rejected(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    _, workflow = _started_workflow(store)
    packed = _role_binding(instructions="packed")
    changed = _role_binding(instructions="changed")
    _bind_pack(
        store,
        workflow,
        _pack(workflow.run.binding, roles=(packed,)),
    )

    with pytest.raises(RoleBindingError):
        RoleAdmission(store).admit_start(
            _role_run(store, workflow, changed)
        )


def test_capability_member_in_pack_is_admitted(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    _, workflow = _started_workflow(store)
    capability = _capability_binding()
    _bind_pack(
        store,
        workflow,
        _pack(
            workflow.run.binding,
            capabilities=(capability,),
        ),
    )

    need = _capability_need(store, workflow, capability)
    admitted = CapabilityAdmission(store).admit_need(need)

    assert admitted.need == need


def test_capability_not_in_pack_is_rejected_at_admission(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    _, workflow = _started_workflow(store)
    _bind_pack(
        store,
        workflow,
        _pack(workflow.run.binding),
    )
    need = _capability_need(
        store,
        workflow,
        _capability_binding(),
    )

    with pytest.raises(CapabilityBindingError):
        CapabilityAdmission(store).admit_need(need)


def test_same_capability_id_version_with_changed_digest_is_rejected(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    _, workflow = _started_workflow(store)
    packed = _capability_binding(contract="packed")
    changed = _capability_binding(contract="changed")
    _bind_pack(
        store,
        workflow,
        _pack(
            workflow.run.binding,
            capabilities=(packed,),
        ),
    )

    with pytest.raises(CapabilityBindingError):
        CapabilityAdmission(store).admit_need(
            _capability_need(store, workflow, changed)
        )


def test_raw_role_bypass_is_rejected_during_recovery(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    _, workflow = _started_workflow(store)
    _bind_pack(
        store,
        workflow,
        _pack(workflow.run.binding),
    )
    run = _role_run(store, workflow, _role_binding())
    candidate = run.to_start_candidate()

    store.append(
        run.work_id,
        expected_revision=candidate.expected_revision,
        event=candidate.event,
    )

    with pytest.raises(RoleProjectionError):
        project_role_runs(store.events(run.work_id))


def test_raw_capability_bypass_is_rejected_during_recovery(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    _, workflow = _started_workflow(store)
    _bind_pack(
        store,
        workflow,
        _pack(workflow.run.binding),
    )
    need = _capability_need(
        store,
        workflow,
        _capability_binding(),
    )
    candidate = need.to_workflow_candidate()

    store.append(
        need.work_id,
        expected_revision=candidate.expected_revision,
        event=candidate.event,
    )

    with pytest.raises(CapabilityProjectionError):
        project_capability_needs(store.events(need.work_id))


def test_unbound_workflow_preserves_role_and_capability_compatibility(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    _, workflow = _started_workflow(store)
    role = _role_binding()
    capability = _capability_binding()

    admitted_role = RoleAdmission(store).admit_start(
        _role_run(store, workflow, role)
    )
    admitted_need = CapabilityAdmission(store).admit_need(
        _capability_need(store, workflow, capability)
    )

    assert admitted_role.run.binding == role
    assert admitted_need.need.binding == capability


def test_two_workflow_runs_can_pin_different_pack_versions(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    work, first_workflow = _started_workflow(store)
    first = _bind_pack(
        store,
        first_workflow,
        _pack(
            first_workflow.run.binding,
            version="1.0.0",
            definition="pack-v1",
        ),
    )

    second_run = WorkflowRun.create(
        snapshot=store.snapshot(work.work_id),
        binding=_workflow_binding(definition="workflow-v2"),
    )
    second_workflow = WorkflowAdmission(store).admit_start(second_run)
    second = _bind_pack(
        store,
        second_workflow,
        _pack(
            second_workflow.run.binding,
            version="2.0.0",
            definition="pack-v2",
        ),
    )

    pins = project_pack_workflow_bindings(
        store.events(work.work_id)
    )

    assert pins == (first, second)
    assert first.pack.binding_digest != second.pack.binding_digest


def test_raw_late_pack_event_is_rejected_during_recovery(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    _, workflow = _started_workflow(store)
    immediate = store.snapshot(workflow.run.work_id)
    pin = PackWorkflowBinding.create(
        workflow=workflow,
        snapshot=immediate,
        pack=_pack(workflow.run.binding),
    )

    candidate = WorkflowCandidate.create(
        run_snapshot=workflow,
        work_snapshot=immediate,
        event_kind="workflow.note",
        payload={"note": "intervening event"},
    )
    WorkflowAdmission(store).admit_candidate(candidate)
    current = store.snapshot(workflow.run.work_id)

    forged_outer = WorkEvent.create(
        work_id=pin.work_id,
        sequence=current.revision + 1,
        kind="pack.workflow-bound",
        payload={"pack_workflow_binding": pin.to_dict()},
        previous_event_digest=current.last_event_digest,
        event_id=pin.binding_id,
        created_at=pin.created_at,
    )
    store.append(
        pin.work_id,
        expected_revision=current.revision,
        event=forged_outer,
    )

    with pytest.raises(PackProjectionError):
        project_pack_workflow_bindings(
            store.events(pin.work_id)
        )
