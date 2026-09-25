from __future__ import annotations

import ast
import hashlib
import json
from dataclasses import dataclass, replace
from pathlib import Path
from uuid import uuid4

import pytest

import codexia_manual_agent.delegation_core as delegation_core
from codexia_manual_agent.completion_core import (
    CompletionAdmissionService,
    CompletionClaim,
    CompletionCriterionContext,
    CompletionCriterionResult,
    InvalidWorkCompletion,
    WorkCompletion,
    WorkCompletionAdmissionService,
    WorkCompletionIdentityConflictError,
    WorkCompletionProjectionError,
    project_work_completion,
)
from codexia_manual_agent.delegation_core import (
    Delegation,
    DelegationAdmission,
    DelegationChildrenLiveError,
)
from codexia_manual_agent.pack_core import (
    PackAdmission,
    PackBinding,
    PackMemberBinding,
    PackMemberKind,
    PackWorkflowBinding,
)
from codexia_manual_agent.work_core import (
    WORK_CANCELLED_EVENT,
    WORK_COMPLETED_EVENT,
    SqliteWorkStore,
    Work,
    WorkConcurrencyError,
    WorkEvent,
    WorkIngressBinding,
    WorkState,
    WorkStateError,
    WorkStore,
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
            objective=f"Complete exact Work semantics for {label}",
            ingress=WorkIngressBinding.create(
                source_namespace="standalone.api",
                source_id=f"g2.37-{label}",
                payload_digest=_sha(f"payload:{label}"),
            ),
        )
    )


def _started(store: SqliteWorkStore, *, label: str):
    work = _work(store, label=label)
    workflow_binding = WorkflowBinding.create(
        workflow_id=f"codexia:g2.37-{label}",
        version="1.0.0",
        definition_digest=_sha(f"workflow:{label}"),
    )
    workflow = WorkflowAdmission(store).admit_start(
        WorkflowRun.create(
            snapshot=work,
            binding=workflow_binding,
        )
    )
    pack = PackBinding.create(
        pack_id=f"codexia:g2.37-pack-{label}",
        version="1.0.0",
        definition_digest=_sha(f"pack:{label}"),
        members=(
            PackMemberBinding.create(
                kind=PackMemberKind.WORKFLOW,
                semantic_id=workflow_binding.workflow_id,
                version=workflow_binding.version,
                binding_digest=workflow_binding.binding_digest,
            ),
        ),
    )
    pin = PackAdmission(store).admit_workflow_binding(
        PackWorkflowBinding.create(
            workflow=workflow,
            snapshot=store.snapshot(work.work.work_id),
            pack=pack,
        )
    )
    return work, workflow, pin


class _Criterion:
    def __init__(self, binding: WorkflowBinding) -> None:
        self.binding = binding
        self.calls = 0

    def evaluate(
        self,
        context: CompletionCriterionContext,
    ) -> CompletionCriterionResult:
        self.calls += 1
        return CompletionCriterionResult(
            accepted=True,
            reason="exact completion criterion satisfied",
        )


@dataclass(frozen=True)
class _ResolvedCriterion:
    workflow_binding: WorkflowBinding
    pack_binding_digest: str
    criterion: _Criterion


class _Resolver:
    def __init__(
        self,
        *,
        workflow_binding: WorkflowBinding,
        pack_binding_digest: str,
        criterion: _Criterion,
    ) -> None:
        self.workflow_binding = workflow_binding
        self.pack_binding_digest = pack_binding_digest
        self.criterion = criterion
        self.calls = 0

    def resolve(self, *, provider_ref: str, workflow, pack_binding):
        self.calls += 1
        return _ResolvedCriterion(
            workflow_binding=self.workflow_binding,
            pack_binding_digest=self.pack_binding_digest,
            criterion=self.criterion,
        )


def _admit_claim(
    store: SqliteWorkStore,
    work_id: str,
    workflow,
    pin,
):
    claim = CompletionClaim.create(
        snapshot=store.snapshot(work_id),
        workflow=workflow,
        pack_binding=pin,
        summary="The exact Work objective is satisfied.",
    )
    criterion = _Criterion(workflow.run.binding)
    resolver = _Resolver(
        workflow_binding=workflow.run.binding,
        pack_binding_digest=pin.pack.binding_digest,
        criterion=criterion,
    )
    admitted = CompletionAdmissionService(
        store=store,
        resolver=resolver,
    ).admit(
        claim,
        provider_ref="codexia:g2.37-test-provider@1.0.0",
    )
    assert admitted == claim
    admission_event = store.events(work_id)[-1]
    return claim, admission_event, criterion, resolver


def _completion(
    store: SqliteWorkStore,
    claim: CompletionClaim,
    admission_event: WorkEvent,
    *,
    completion_id: str | None = None,
) -> WorkCompletion:
    return WorkCompletion.create(
        snapshot=store.snapshot(claim.work_id),
        claim=claim,
        claim_admission_event=admission_event,
        completion_id=completion_id,
    )


def _cancel(store: SqliteWorkStore, work_id: str) -> None:
    snapshot = store.snapshot(work_id)
    store.append(
        work_id,
        expected_revision=snapshot.revision,
        event=snapshot.next_event(
            kind=WORK_CANCELLED_EVENT,
            payload={"reason": "g2.37 terminal-child proof"},
        ),
    )


def test_work_completion_binds_exact_admitted_claim_and_terminal_event(
    tmp_path,
) -> None:
    store = SqliteWorkStore(tmp_path / "contract.sqlite")
    work, workflow, pin = _started(store, label="contract")
    claim, admission_event, _, _ = _admit_claim(
        store,
        work.work.work_id,
        workflow,
        pin,
    )

    completion = _completion(store, claim, admission_event)
    event = completion.to_event()

    assert completion.work_id == claim.work_id
    assert completion.work_digest == claim.work_digest
    assert completion.claim_id == claim.claim_id
    assert completion.claim_digest == claim.claim_digest
    assert (
        completion.claim_admission_sequence
        == admission_event.sequence
    )
    assert (
        completion.claim_admission_event_digest
        == admission_event.event_digest
    )
    assert event.kind == WORK_COMPLETED_EVENT
    assert event.event_id == completion.completion_id
    assert event.created_at == completion.created_at
    assert event.sequence == admission_event.sequence + 1
    assert event.previous_event_digest == admission_event.event_digest
    assert event.to_dict()["payload"] == {
        "work_completion": completion.to_dict()
    }


def test_work_completion_round_trip_is_exact(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "roundtrip.sqlite")
    work, workflow, pin = _started(store, label="roundtrip")
    claim, admission_event, _, _ = _admit_claim(
        store,
        work.work.work_id,
        workflow,
        pin,
    )
    completion = _completion(store, claim, admission_event)

    assert WorkCompletion.from_dict(completion.to_dict()) == completion


def test_work_completion_rejects_tampered_digest(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "tamper.sqlite")
    work, workflow, pin = _started(store, label="tamper")
    claim, admission_event, _, _ = _admit_claim(
        store,
        work.work.work_id,
        workflow,
        pin,
    )
    completion = _completion(store, claim, admission_event)
    payload = completion.to_dict()
    payload["claim_digest"] = _sha("different-claim")

    with pytest.raises(InvalidWorkCompletion, match="digest mismatch"):
        WorkCompletion.from_dict(payload)


@pytest.mark.parametrize("schema_version", [True, 1.0])
def test_work_completion_rejects_schema_type_drift_even_with_valid_digest(
    tmp_path,
    schema_version,
) -> None:
    label = "bool" if schema_version is True else "float"
    store = SqliteWorkStore(tmp_path / f"schema-{label}.sqlite")
    work, workflow, pin = _started(store, label=f"schema-{label}")
    claim, admission_event, _, _ = _admit_claim(
        store,
        work.work.work_id,
        workflow,
        pin,
    )
    completion = _completion(store, claim, admission_event)
    payload = completion.to_dict()
    payload["schema_version"] = schema_version
    payload["completion_digest"] = _digest_payload(
        {
            key: value
            for key, value in payload.items()
            if key != "completion_digest"
        }
    )

    with pytest.raises(
        InvalidWorkCompletion,
        match="Unsupported WorkCompletion schema",
    ):
        WorkCompletion.from_dict(payload)


@pytest.mark.parametrize(
    "created_at",
    [
        "2026-01-01T00:00:00Z",
        "20260101T000000+0000",
        "2026-01-01 00:00:00+00:00",
        "2026-W01-4T00:00:00+00:00",
    ],
)
def test_work_completion_rejects_noncanonical_timestamp_spellings(
    tmp_path,
    created_at: str,
) -> None:
    store = SqliteWorkStore(tmp_path / "timestamp.sqlite")
    work, workflow, pin = _started(store, label="timestamp")
    claim, admission_event, _, _ = _admit_claim(
        store,
        work.work.work_id,
        workflow,
        pin,
    )

    with pytest.raises(InvalidWorkCompletion, match="canonical ISO-8601"):
        WorkCompletion.create(
            snapshot=store.snapshot(work.work.work_id),
            claim=claim,
            claim_admission_event=admission_event,
            created_at=created_at,
        )


def test_claim_admission_remains_nonterminal_until_work_completion(
    tmp_path,
) -> None:
    store = SqliteWorkStore(tmp_path / "lifecycle.sqlite")
    work, workflow, pin = _started(store, label="lifecycle")
    claim, admission_event, criterion, resolver = _admit_claim(
        store,
        work.work.work_id,
        workflow,
        pin,
    )
    assert store.snapshot(work.work.work_id).state is WorkState.ACTIVE

    completion = _completion(store, claim, admission_event)
    terminal = WorkCompletionAdmissionService(store).admit(completion)

    assert terminal.state is WorkState.COMPLETED
    assert terminal.terminal_event_id == completion.completion_id
    assert project_work_completion(
        store.events(work.work.work_id)
    ) == completion
    assert criterion.calls == 1
    assert resolver.calls == 1


def test_work_completion_restart_recovers_exact_terminal_record(
    tmp_path,
) -> None:
    path = tmp_path / "restart.sqlite"
    store = SqliteWorkStore(path)
    work, workflow, pin = _started(store, label="restart")
    claim, admission_event, _, _ = _admit_claim(
        store,
        work.work.work_id,
        workflow,
        pin,
    )
    completion = _completion(store, claim, admission_event)
    WorkCompletionAdmissionService(store).admit(completion)

    restarted = SqliteWorkStore(path)

    assert project_work_completion(
        restarted.events(work.work.work_id)
    ) == completion
    assert restarted.snapshot(work.work.work_id).state is WorkState.COMPLETED


def test_exact_terminal_retry_is_idempotent_without_rechecking_criterion(
    tmp_path,
) -> None:
    store = SqliteWorkStore(tmp_path / "retry.sqlite")
    work, workflow, pin = _started(store, label="retry")
    claim, admission_event, criterion, resolver = _admit_claim(
        store,
        work.work.work_id,
        workflow,
        pin,
    )
    completion = _completion(store, claim, admission_event)
    service = WorkCompletionAdmissionService(store)

    first = service.admit(completion)
    events_after_first = store.events(work.work.work_id)
    second = service.admit(completion)

    assert second == first
    assert store.events(work.work.work_id) == events_after_first
    assert criterion.calls == 1
    assert resolver.calls == 1


def test_different_completion_cannot_replace_terminal_work_completion(
    tmp_path,
) -> None:
    store = SqliteWorkStore(tmp_path / "identity-conflict.sqlite")
    work, workflow, pin = _started(store, label="identity-conflict")
    claim, admission_event, _, _ = _admit_claim(
        store,
        work.work.work_id,
        workflow,
        pin,
    )
    first = _completion(store, claim, admission_event)
    second = _completion(
        store,
        claim,
        admission_event,
        completion_id=str(uuid4()),
    )
    service = WorkCompletionAdmissionService(store)
    service.admit(first)

    with pytest.raises(WorkCompletionIdentityConflictError):
        service.admit(second)


def test_intervening_parent_event_invalidates_prepared_work_completion(
    tmp_path,
) -> None:
    store = SqliteWorkStore(tmp_path / "stale.sqlite")
    work, workflow, pin = _started(store, label="stale")
    claim, admission_event, _, _ = _admit_claim(
        store,
        work.work.work_id,
        workflow,
        pin,
    )
    completion = _completion(store, claim, admission_event)
    current = store.snapshot(work.work.work_id)
    store.append(
        work.work.work_id,
        expected_revision=current.revision,
        event=current.next_event(
            kind="work.external-observation",
            payload={"after_claim": True},
        ),
    )

    with pytest.raises(WorkConcurrencyError):
        WorkCompletionAdmissionService(store).admit(completion)

    assert store.snapshot(work.work.work_id).state is WorkState.ACTIVE
    assert project_work_completion(store.events(work.work.work_id)) is None


def test_newer_admitted_claim_supersedes_prepared_completion_for_old_claim(
    tmp_path,
) -> None:
    store = SqliteWorkStore(tmp_path / "superseded.sqlite")
    work, workflow, pin = _started(store, label="superseded")
    first_claim, first_event, _, _ = _admit_claim(
        store,
        work.work.work_id,
        workflow,
        pin,
    )
    first_completion = _completion(store, first_claim, first_event)

    second_claim, second_event, _, _ = _admit_claim(
        store,
        work.work.work_id,
        workflow,
        pin,
    )
    assert second_claim.claim_id != first_claim.claim_id
    assert second_event.sequence > first_event.sequence

    with pytest.raises(WorkConcurrencyError):
        WorkCompletionAdmissionService(store).admit(first_completion)

    second_completion = _completion(store, second_claim, second_event)
    terminal = WorkCompletionAdmissionService(store).admit(
        second_completion
    )
    assert terminal.state is WorkState.COMPLETED


def test_live_owned_child_blocks_terminal_but_same_completion_can_retry(
    tmp_path,
) -> None:
    store = SqliteWorkStore(tmp_path / "live-child.sqlite")
    work, workflow, pin = _started(store, label="live-child")
    delegation = DelegationAdmission(store).admit(
        Delegation.create(
            parent=store.snapshot(work.work.work_id),
            child_objective="Remain live at first terminal attempt.",
        )
    )
    claim, admission_event, _, _ = _admit_claim(
        store,
        work.work.work_id,
        workflow,
        pin,
    )
    completion = _completion(store, claim, admission_event)
    service = WorkCompletionAdmissionService(store)

    with pytest.raises(DelegationChildrenLiveError) as exc_info:
        service.admit(completion)

    assert exc_info.value.child_work_ids == (
        delegation.child_work.work_id,
    )
    assert store.snapshot(work.work.work_id).state is WorkState.ACTIVE
    assert project_work_completion(store.events(work.work.work_id)) is None

    _cancel(store, delegation.child_work.work_id)
    terminal = service.admit(completion)

    assert terminal.state is WorkState.COMPLETED
    assert project_work_completion(
        store.events(work.work.work_id)
    ) == completion


def test_public_work_store_still_cannot_publish_structured_completion(
    tmp_path,
) -> None:
    store = SqliteWorkStore(tmp_path / "store-bypass.sqlite")
    work, workflow, pin = _started(store, label="store-bypass")
    claim, admission_event, _, _ = _admit_claim(
        store,
        work.work.work_id,
        workflow,
        pin,
    )
    completion = _completion(store, claim, admission_event)
    event = completion.to_event()

    with pytest.raises(
        WorkStateError,
        match="guarded completion admission boundary",
    ):
        store.append(
            work.work.work_id,
            expected_revision=event.sequence - 1,
            event=event,
        )


def test_raw_delegation_terminal_writer_is_no_longer_public_surface() -> None:
    assert not hasattr(delegation_core, "DelegationCompletionGuard")
    assert "append_completion" not in WorkStore.__dict__
    assert not hasattr(SqliteWorkStore, "append_completion")


def test_private_store_corruption_without_work_completion_fails_projection(
    tmp_path,
) -> None:
    store = SqliteWorkStore(tmp_path / "projection.sqlite")
    work, workflow, pin = _started(store, label="projection")
    _, admission_event, _, _ = _admit_claim(
        store,
        work.work.work_id,
        workflow,
        pin,
    )
    current = store.snapshot(work.work.work_id)
    forged = WorkEvent.create(
        work_id=work.work.work_id,
        sequence=current.revision + 1,
        kind=WORK_COMPLETED_EVENT,
        payload={"summary": "legacy raw completion"},
        previous_event_digest=current.last_event_digest,
    )
    store._append_completion(
        work.work.work_id,
        expected_revision=current.revision,
        event=forged,
    )

    with pytest.raises(
        WorkCompletionProjectionError,
        match="payload is not exact WorkCompletion",
    ):
        project_work_completion(store.events(work.work.work_id))


def test_work_completion_projection_rejects_changed_claim_digest(
    tmp_path,
) -> None:
    store = SqliteWorkStore(tmp_path / "projection-digest.sqlite")
    work, workflow, pin = _started(store, label="projection-digest")
    claim, admission_event, _, _ = _admit_claim(
        store,
        work.work.work_id,
        workflow,
        pin,
    )
    completion = _completion(store, claim, admission_event)
    payload = completion.to_dict()
    payload["claim_digest"] = _sha("different-claim")
    payload["completion_digest"] = _digest_payload(
        {
            key: value
            for key, value in payload.items()
            if key != "completion_digest"
        }
    )
    forged_completion = WorkCompletion.from_dict(payload)
    forged_event = forged_completion.to_event()
    current = store.snapshot(work.work.work_id)
    store._append_completion(
        work.work.work_id,
        expected_revision=current.revision,
        event=forged_event,
    )

    with pytest.raises(
        WorkCompletionProjectionError,
        match="changed CompletionClaim digest",
    ):
        project_work_completion(store.events(work.work.work_id))


def test_work_completion_record_is_not_cleanup_or_parent_hde_state() -> None:
    fields = set(WorkCompletion.__dataclass_fields__)
    assert fields == {
        "schema_version",
        "completion_id",
        "created_at",
        "work_id",
        "work_digest",
        "claim_id",
        "claim_digest",
        "claim_admission_sequence",
        "claim_admission_event_digest",
        "completion_digest",
    }
    forbidden = {
        "cleanup",
        "cleanup_proof",
        "parent_hde_completion",
        "authority",
        "permission",
        "scheduler",
    }
    assert forbidden.isdisjoint(fields)


def test_g2_37_terminal_service_does_not_duplicate_completion_policy_or_store_writer(
) -> None:
    root = Path(__file__).resolve().parents[1] / "src" / "codexia_manual_agent"
    terminal_path = root / "completion_core" / "terminal.py"
    model_path = root / "completion_core" / "work_completion.py"

    terminal_tree = ast.parse(terminal_path.read_text(encoding="utf-8"))
    model_tree = ast.parse(model_path.read_text(encoding="utf-8"))

    forbidden_names = {
        "CompletionCriterionBoundary",
        "InvariantCompletionCriterionBridge",
        "ArtifactRef",
        "EvidenceRef",
        "CleanupProof",
        "AuthorizationReceipt",
        "ProcessExecutor",
        "Scheduler",
    }
    imported_names: set[str] = set()
    imported_modules: set[str] = set()
    for tree in (terminal_tree, model_tree):
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_names.update(alias.name for alias in node.names)
                imported_modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported_names.update(alias.name for alias in node.names)
                if node.module is not None:
                    imported_modules.add(node.module)

    assert forbidden_names.isdisjoint(imported_names)
    assert not any(
        part in module
        for module in imported_modules
        for part in (
            "artifact_core",
            "evidence_core",
            "invariant_bridge",
            "authority",
            "execution",
            "mutation",
        )
    )
    assert all(
        not (
            isinstance(node, ast.Attribute)
            and node.attr == "_append_completion"
        )
        for node in ast.walk(terminal_tree)
    )
    assert not any(
        isinstance(node, ast.While)
        for tree in (terminal_tree, model_tree)
        for node in ast.walk(tree)
    )
