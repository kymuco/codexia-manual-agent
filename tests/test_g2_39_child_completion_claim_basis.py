from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
from uuid import uuid4

import pytest

from codexia_manual_agent.completion_core import (
    CompletionAdmissionService,
    CompletionClaim,
    CompletionClaimBasisError,
    CompletionCriterionContext,
    CompletionCriterionResult,
    InvalidCompletionClaim,
    InvalidWorkCompletionRef,
    WorkCompletion,
    WorkCompletionAdmissionService,
    WorkCompletionRef,
)
from codexia_manual_agent.delegation_core import (
    Delegation,
    DelegationAdmission,
)
from codexia_manual_agent.delegation_core.completion import (
    _DelegationCompletionGuard,
)
from codexia_manual_agent.pack_core import (
    PackAdmission,
    PackBinding,
    PackMemberBinding,
    PackMemberKind,
    PackWorkflowBinding,
)
from codexia_manual_agent.work_core import (
    WORK_COMPLETED_EVENT,
    SqliteWorkStore,
    Work,
    WorkIngressBinding,
)
from codexia_manual_agent.workflow_core import (
    WorkflowAdmission,
    WorkflowBinding,
    WorkflowRun,
)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _digest_payload(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _work(store: SqliteWorkStore, *, label: str):
    return store.create(
        Work.create(
            objective=f"G2.39 semantic work for {label}",
            ingress=WorkIngressBinding.create(
                source_namespace="standalone.api",
                source_id=f"g2.39-{label}",
                payload_digest=_sha(f"payload:{label}"),
            ),
        )
    )


def _start_workflow(
    store: SqliteWorkStore,
    work_id: str,
    *,
    label: str,
):
    binding = WorkflowBinding.create(
        workflow_id=f"codexia:g2.39-{label}",
        version="1.0.0",
        definition_digest=_sha(f"workflow:{label}"),
    )
    workflow = WorkflowAdmission(store).admit_start(
        WorkflowRun.create(
            snapshot=store.snapshot(work_id),
            binding=binding,
        )
    )
    pack = PackBinding.create(
        pack_id=f"codexia:g2.39-pack-{label}",
        version="1.0.0",
        definition_digest=_sha(f"pack:{label}"),
        members=(
            PackMemberBinding.create(
                kind=PackMemberKind.WORKFLOW,
                semantic_id=binding.workflow_id,
                version=binding.version,
                binding_digest=binding.binding_digest,
            ),
        ),
    )
    pin = PackAdmission(store).admit_workflow_binding(
        PackWorkflowBinding.create(
            workflow=workflow,
            snapshot=store.snapshot(work_id),
            pack=pack,
        )
    )
    return workflow, pin


class _Criterion:
    def __init__(self, binding: WorkflowBinding) -> None:
        self.binding = binding
        self.calls = 0
        self.contexts: list[CompletionCriterionContext] = []

    def evaluate(
        self,
        context: CompletionCriterionContext,
    ) -> CompletionCriterionResult:
        self.calls += 1
        self.contexts.append(context)
        return CompletionCriterionResult(
            accepted=True,
            reason="exact completion basis accepted",
        )


class _Resolved:
    def __init__(
        self,
        binding: WorkflowBinding,
        pack_digest: str,
        criterion: _Criterion,
    ) -> None:
        self.workflow_binding = binding
        self.pack_binding_digest = pack_digest
        self.criterion = criterion


class _Resolver:
    def __init__(
        self,
        binding: WorkflowBinding,
        pack_digest: str,
        criterion: _Criterion,
    ) -> None:
        self.binding = binding
        self.pack_digest = pack_digest
        self.criterion = criterion

    def resolve(self, *, provider_ref, workflow, pack_binding):
        return _Resolved(
            self.binding,
            self.pack_digest,
            self.criterion,
        )


def _admit_claim(
    store: SqliteWorkStore,
    claim: CompletionClaim,
    workflow,
    pin,
    *,
    criterion: _Criterion | None = None,
):
    criterion = criterion or _Criterion(workflow.run.binding)
    admitted = CompletionAdmissionService(
        store=store,
        resolver=_Resolver(
            workflow.run.binding,
            pin.pack.binding_digest,
            criterion,
        ),
    ).admit(
        claim,
        provider_ref="codexia:g2.39-test-provider@1.0.0",
    )
    return admitted, criterion


def _complete_semantically(
    store: SqliteWorkStore,
    work_id: str,
    *,
    label: str,
) -> WorkCompletion:
    workflow, pin = _start_workflow(
        store,
        work_id,
        label=f"child-{label}",
    )
    claim = CompletionClaim.create(
        snapshot=store.snapshot(work_id),
        workflow=workflow,
        pack_binding=pin,
        summary="The child objective is semantically satisfied.",
    )
    _admit_claim(store, claim, workflow, pin)
    admission_event = store.events(work_id)[-1]
    completion = WorkCompletion.create(
        snapshot=store.snapshot(work_id),
        claim=claim,
        claim_admission_event=admission_event,
    )
    WorkCompletionAdmissionService(store).admit(completion)
    return completion


def _parent_with_child(
    store: SqliteWorkStore,
    *,
    label: str,
):
    parent = _work(store, label=f"parent-{label}")
    workflow, pin = _start_workflow(
        store,
        parent.work.work_id,
        label=f"parent-{label}",
    )
    delegation = DelegationAdmission(store).admit(
        Delegation.create(
            parent=store.snapshot(parent.work.work_id),
            child_objective=f"Child objective for {label}",
        )
    )
    return parent, workflow, pin, delegation


def test_work_completion_ref_round_trip_is_exact() -> None:
    ref = WorkCompletionRef.create(
        work_id=str(uuid4()),
        completion_event_id=str(uuid4()),
        completion_digest=_sha("completion"),
    )

    assert WorkCompletionRef.from_dict(ref.to_dict()) == ref


def test_work_completion_ref_rejects_shape_and_digest_drift() -> None:
    ref = WorkCompletionRef.create(
        work_id=str(uuid4()),
        completion_event_id=str(uuid4()),
        completion_digest=_sha("completion"),
    )
    payload = ref.to_dict()
    payload["unexpected"] = True

    with pytest.raises(
        InvalidWorkCompletionRef,
        match="keys are not exact",
    ):
        WorkCompletionRef.from_dict(payload)

    with pytest.raises(
        InvalidWorkCompletionRef,
        match="SHA-256",
    ):
        WorkCompletionRef.create(
            work_id=ref.work_id,
            completion_event_id=ref.completion_event_id,
            completion_digest="ABC",
        )


def test_work_completion_to_ref_preserves_exact_terminal_identity(
    tmp_path,
) -> None:
    store = SqliteWorkStore(tmp_path / "to-ref.sqlite")
    work = _work(store, label="to-ref")
    completion = _complete_semantically(
        store,
        work.work.work_id,
        label="to-ref",
    )

    ref = completion.to_ref()

    assert ref.work_id == completion.work_id
    assert ref.completion_event_id == completion.completion_id
    assert ref.completion_digest == completion.completion_digest


def test_completion_claim_v2_canonicalizes_child_completion_refs(
    tmp_path,
) -> None:
    store = SqliteWorkStore(tmp_path / "canonical.sqlite")
    parent = _work(store, label="canonical")
    workflow, pin = _start_workflow(
        store,
        parent.work.work_id,
        label="canonical",
    )
    first = WorkCompletionRef.create(
        work_id=str(uuid4()),
        completion_event_id=str(uuid4()),
        completion_digest=_sha("first"),
    )
    second = WorkCompletionRef.create(
        work_id=str(uuid4()),
        completion_event_id=str(uuid4()),
        completion_digest=_sha("second"),
    )

    claim = CompletionClaim.create(
        snapshot=store.snapshot(parent.work.work_id),
        workflow=workflow,
        pack_binding=pin,
        summary="Detached child basis is structurally captured.",
        child_completion_refs=(second, first),
    )

    assert claim.schema_version == 2
    assert claim.child_completion_refs == tuple(
        sorted((first, second), key=lambda item: item.work_id)
    )
    assert claim.to_dict()["child_completion_refs"] == [
        item.to_dict() for item in claim.child_completion_refs
    ]


def test_completion_claim_rejects_duplicate_child_work_identity(
    tmp_path,
) -> None:
    store = SqliteWorkStore(tmp_path / "duplicate.sqlite")
    parent = _work(store, label="duplicate")
    workflow, pin = _start_workflow(
        store,
        parent.work.work_id,
        label="duplicate",
    )
    work_id = str(uuid4())
    first = WorkCompletionRef.create(
        work_id=work_id,
        completion_event_id=str(uuid4()),
        completion_digest=_sha("first"),
    )
    second = WorkCompletionRef.create(
        work_id=work_id,
        completion_event_id=str(uuid4()),
        completion_digest=_sha("second"),
    )

    with pytest.raises(
        InvalidCompletionClaim,
        match="repeat child Work identity",
    ):
        CompletionClaim.create(
            snapshot=store.snapshot(parent.work.work_id),
            workflow=workflow,
            pack_binding=pin,
            summary="Duplicate child identity is ambiguous.",
            child_completion_refs=(first, second),
        )


def test_completion_claim_v1_round_trip_preserves_legacy_shape(
    tmp_path,
) -> None:
    store = SqliteWorkStore(tmp_path / "legacy-v1.sqlite")
    parent = _work(store, label="legacy-v1")
    workflow, pin = _start_workflow(
        store,
        parent.work.work_id,
        label="legacy-v1",
    )
    current = CompletionClaim.create(
        snapshot=store.snapshot(parent.work.work_id),
        workflow=workflow,
        pack_binding=pin,
        summary="Legacy v1 claim remains decodable.",
    )
    payload = current.to_dict()
    payload.pop("child_completion_refs")
    payload["schema_version"] = 1
    payload["claim_digest"] = _digest_payload(
        {
            key: value
            for key, value in payload.items()
            if key != "claim_digest"
        }
    )

    legacy = CompletionClaim.from_dict(payload)

    assert legacy.schema_version == 1
    assert legacy.child_completion_refs == ()
    assert legacy.to_dict() == payload


def test_completion_claim_v1_still_admits_after_schema_extension(
    tmp_path,
) -> None:
    store = SqliteWorkStore(tmp_path / "legacy-admit.sqlite")
    parent = _work(store, label="legacy-admit")
    workflow, pin = _start_workflow(
        store,
        parent.work.work_id,
        label="legacy-admit",
    )
    current = CompletionClaim.create(
        snapshot=store.snapshot(parent.work.work_id),
        workflow=workflow,
        pack_binding=pin,
        summary="Legacy v1 admission remains valid.",
    )
    payload = current.to_dict()
    payload.pop("child_completion_refs")
    payload["schema_version"] = 1
    payload["claim_digest"] = _digest_payload(
        {
            key: value
            for key, value in payload.items()
            if key != "claim_digest"
        }
    )
    legacy = CompletionClaim.from_dict(payload)

    admitted, criterion = _admit_claim(
        store,
        legacy,
        workflow,
        pin,
    )

    assert admitted == legacy
    assert criterion.calls == 1


def test_owned_structured_child_completion_is_valid_claim_basis(
    tmp_path,
) -> None:
    store = SqliteWorkStore(tmp_path / "owned.sqlite")
    parent, workflow, pin, delegation = _parent_with_child(
        store,
        label="owned",
    )
    completion = _complete_semantically(
        store,
        delegation.child_work.work_id,
        label="owned",
    )
    claim = CompletionClaim.create(
        snapshot=store.snapshot(parent.work.work_id),
        workflow=workflow,
        pack_binding=pin,
        summary="Parent objective accepts exact child completion.",
        child_completion_refs=(completion.to_ref(),),
    )
    criterion = _Criterion(workflow.run.binding)

    admitted, criterion = _admit_claim(
        store,
        claim,
        workflow,
        pin,
        criterion=criterion,
    )

    assert admitted == claim
    assert criterion.calls == 1
    assert criterion.contexts[0].claim.child_completion_refs == (
        completion.to_ref(),
    )


def test_non_owned_child_completion_is_rejected_before_criterion(
    tmp_path,
) -> None:
    store = SqliteWorkStore(tmp_path / "non-owned.sqlite")
    parent = _work(store, label="non-owned-parent")
    workflow, pin = _start_workflow(
        store,
        parent.work.work_id,
        label="non-owned-parent",
    )
    outsider = _work(store, label="non-owned-child")
    completion = _complete_semantically(
        store,
        outsider.work.work_id,
        label="non-owned-child",
    )
    claim = CompletionClaim.create(
        snapshot=store.snapshot(parent.work.work_id),
        workflow=workflow,
        pack_binding=pin,
        summary="An unrelated WorkCompletion cannot justify parent completion.",
        child_completion_refs=(completion.to_ref(),),
    )
    criterion = _Criterion(workflow.run.binding)

    with pytest.raises(
        CompletionClaimBasisError,
        match="outside owned children",
    ):
        _admit_claim(
            store,
            claim,
            workflow,
            pin,
            criterion=criterion,
        )

    assert criterion.calls == 0


def test_active_owned_child_cannot_be_claimed_as_completed_basis(
    tmp_path,
) -> None:
    store = SqliteWorkStore(tmp_path / "active.sqlite")
    parent, workflow, pin, delegation = _parent_with_child(
        store,
        label="active",
    )
    ref = WorkCompletionRef.create(
        work_id=delegation.child_work.work_id,
        completion_event_id=str(uuid4()),
        completion_digest=_sha("not-yet-completed"),
    )
    claim = CompletionClaim.create(
        snapshot=store.snapshot(parent.work.work_id),
        workflow=workflow,
        pack_binding=pin,
        summary="Active child must not satisfy child completion basis.",
        child_completion_refs=(ref,),
    )

    with pytest.raises(
        CompletionClaimBasisError,
        match="is not terminal",
    ):
        _admit_claim(store, claim, workflow, pin)


def test_legacy_raw_completed_child_is_not_semantic_completion_basis(
    tmp_path,
) -> None:
    store = SqliteWorkStore(tmp_path / "raw.sqlite")
    parent, workflow, pin, delegation = _parent_with_child(
        store,
        label="raw",
    )
    child = store.snapshot(delegation.child_work.work_id)
    terminal = _DelegationCompletionGuard(store).admit(
        child.next_event(
            kind=WORK_COMPLETED_EVENT,
            payload={"summary": "legacy raw completion"},
        )
    )
    ref = WorkCompletionRef.create(
        work_id=delegation.child_work.work_id,
        completion_event_id=terminal.terminal_event_id,
        completion_digest=_sha("not-a-structured-completion"),
    )
    claim = CompletionClaim.create(
        snapshot=store.snapshot(parent.work.work_id),
        workflow=workflow,
        pack_binding=pin,
        summary="Raw historical completion is not semantic basis.",
        child_completion_refs=(ref,),
    )

    with pytest.raises(
        CompletionClaimBasisError,
        match="lacks valid structured WorkCompletion",
    ):
        _admit_claim(store, claim, workflow, pin)


def test_child_completion_digest_drift_fails_before_criterion(
    tmp_path,
) -> None:
    store = SqliteWorkStore(tmp_path / "digest-drift.sqlite")
    parent, workflow, pin, delegation = _parent_with_child(
        store,
        label="digest-drift",
    )
    completion = _complete_semantically(
        store,
        delegation.child_work.work_id,
        label="digest-drift",
    )
    drifted = WorkCompletionRef.create(
        work_id=completion.work_id,
        completion_event_id=completion.completion_id,
        completion_digest=_sha("different-completion"),
    )
    claim = CompletionClaim.create(
        snapshot=store.snapshot(parent.work.work_id),
        workflow=workflow,
        pack_binding=pin,
        summary="Digest drift must fail closed.",
        child_completion_refs=(drifted,),
    )
    criterion = _Criterion(workflow.run.binding)

    with pytest.raises(
        CompletionClaimBasisError,
        match="changed child WorkCompletion digest",
    ):
        _admit_claim(
            store,
            claim,
            workflow,
            pin,
            criterion=criterion,
        )

    assert criterion.calls == 0


def test_child_completion_event_identity_drift_fails_before_criterion(
    tmp_path,
) -> None:
    store = SqliteWorkStore(tmp_path / "event-drift.sqlite")
    parent, workflow, pin, delegation = _parent_with_child(
        store,
        label="event-drift",
    )
    completion = _complete_semantically(
        store,
        delegation.child_work.work_id,
        label="event-drift",
    )
    drifted = WorkCompletionRef.create(
        work_id=completion.work_id,
        completion_event_id=str(uuid4()),
        completion_digest=completion.completion_digest,
    )
    claim = CompletionClaim.create(
        snapshot=store.snapshot(parent.work.work_id),
        workflow=workflow,
        pack_binding=pin,
        summary="Terminal event drift must fail closed.",
        child_completion_refs=(drifted,),
    )
    criterion = _Criterion(workflow.run.binding)

    with pytest.raises(
        CompletionClaimBasisError,
        match="changed child completion event identity",
    ):
        _admit_claim(
            store,
            claim,
            workflow,
            pin,
            criterion=criterion,
        )

    assert criterion.calls == 0


def test_g2_39_child_basis_does_not_create_child_result_or_auto_completion(
) -> None:
    root = Path(__file__).resolve().parents[1] / "src" / "codexia_manual_agent"
    paths = [
        root / "completion_core" / "models.py",
        root / "completion_core" / "work_completion_ref.py",
        root / "completion_core" / "admission.py",
    ]
    forbidden_names = {
        "ChildResult",
        "ParentCompletion",
        "WorkGraph",
        "WorkCompletionAdmissionService",
        "WORK_COMPLETED_EVENT",
        "Scheduler",
    }

    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported_names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported_names.update(alias.name for alias in node.names)

        assert forbidden_names.isdisjoint(imported_names)
        assert not any(isinstance(node, ast.While) for node in ast.walk(tree))
