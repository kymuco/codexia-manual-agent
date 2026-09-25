from __future__ import annotations

import ast
import hashlib
from pathlib import Path
from uuid import uuid4

import pytest

from codexia_manual_agent.artifact_core import ArtifactAdmission, ArtifactRef
from codexia_manual_agent.completion_core import (
    COMPLETION_CLAIM_ADMITTED_EVENT,
    CompletionAdmissionService,
    CompletionClaim,
    CompletionClaimBasisError,
    CompletionClaimBindingError,
    CompletionClaimStateError,
    CompletionCriteriaRejected,
    CompletionCriterionContext,
    CompletionCriterionResult,
    project_admitted_completion_claims,
)
from codexia_manual_agent.delegation_core import Delegation, DelegationAdmission
from codexia_manual_agent.evidence_core import EvidenceAdmission, EvidenceRef
from codexia_manual_agent.invariant_bridge import (
    InvariantCompletionCriterionBindingError,
    InvariantCompletionCriterionBridge,
    InvariantCompletionCriterionShapeError,
)
from codexia_manual_agent.pack_core import (
    PackAdmission,
    PackBinding,
    PackMemberBinding,
    PackMemberKind,
    PackWorkflowBinding,
)
from codexia_manual_agent.work_core import (
    SqliteWorkStore,
    Work,
    WorkConcurrencyError,
    WorkIngressBinding,
    WorkState,
)
from codexia_manual_agent.workflow_core import (
    WORKFLOW_COMPLETED_EVENT,
    WorkflowAdmission,
    WorkflowBinding,
    WorkflowCandidate,
    WorkflowRun,
)
from codexia_manual_agent.workflow_runtime import (
    WorkflowImplementationOwnershipError,
    validate_generic_workflow_candidate_ownership,
)

PROVIDER_REF = "codexia:g2.36-provider@1.0.0"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _work(store: SqliteWorkStore, *, source_id: str):
    return store.create(
        Work.create(
            objective=f"Admit one exact completion claim for {source_id}",
            ingress=WorkIngressBinding.create(
                source_namespace="standalone.api",
                source_id=source_id,
                payload_digest=_sha(f"payload:{source_id}"),
            ),
        )
    )


def _workflow_binding(label: str) -> WorkflowBinding:
    return WorkflowBinding.create(
        workflow_id=f"codexia:g2.36-{label}",
        version="1.0.0",
        definition_digest=_sha(f"workflow:{label}"),
    )


def _pack(
    binding: WorkflowBinding,
    *,
    label: str,
    definition: str | None = None,
) -> PackBinding:
    return PackBinding.create(
        pack_id=f"codexia:g2.36-pack-{label}",
        version="1.0.0",
        definition_digest=_sha(definition or f"pack:{label}"),
        members=(
            PackMemberBinding.create(
                kind=PackMemberKind.WORKFLOW,
                semantic_id=binding.workflow_id,
                version=binding.version,
                binding_digest=binding.binding_digest,
            ),
        ),
    )


def _started(store: SqliteWorkStore, *, label: str):
    work = _work(store, source_id=label)
    binding = _workflow_binding(label)
    workflow = WorkflowAdmission(store).admit_start(
        WorkflowRun.create(
            snapshot=work,
            binding=binding,
        )
    )
    pack = _pack(binding, label=label)
    pin = PackAdmission(store).admit_workflow_binding(
        PackWorkflowBinding.create(
            workflow=workflow,
            snapshot=store.snapshot(work.work.work_id),
            pack=pack,
        )
    )
    return work, workflow, pin


def _artifact(
    label: str,
    *,
    artifact_id: str | None = None,
) -> ArtifactRef:
    raw = f"artifact:{label}".encode("utf-8")
    return ArtifactRef.create(
        artifact_id=artifact_id,
        content_sha256=hashlib.sha256(raw).hexdigest(),
        size_bytes=len(raw),
        locator=f"provider+opaque://artifact/{label}",
        media_type="application/octet-stream",
    )


def _evidence(
    label: str,
    *,
    evidence_id: str | None = None,
) -> EvidenceRef:
    return EvidenceRef.create(
        evidence_id=evidence_id or str(uuid4()),
        evidence_digest=_sha(f"evidence:{label}"),
        evidence_kind="external.observation.v1",
        locator=f"provider+opaque://evidence/{label}",
    )


def _claim(
    store: SqliteWorkStore,
    work_id: str,
    workflow,
    pin,
    *,
    artifacts=(),
    evidence=(),
) -> CompletionClaim:
    return CompletionClaim.create(
        snapshot=store.snapshot(work_id),
        workflow=workflow,
        pack_binding=pin,
        summary="The exact Work objective is satisfied.",
        artifact_refs=artifacts,
        evidence_refs=evidence,
    )


class CountingCriterion:
    def __init__(
        self,
        binding: WorkflowBinding,
        *,
        accepted: bool = True,
        reason: str = "completion criteria satisfied",
        hook=None,
    ) -> None:
        self.binding = binding
        self.accepted = accepted
        self.reason = reason
        self.hook = hook
        self.calls = 0
        self.contexts: list[CompletionCriterionContext] = []

    def evaluate(
        self,
        context: CompletionCriterionContext,
    ) -> CompletionCriterionResult:
        self.calls += 1
        self.contexts.append(context)
        if self.hook is not None:
            self.hook(context)
        return CompletionCriterionResult(
            accepted=self.accepted,
            reason=self.reason,
        )


class CriterionPlugin:
    def __init__(
        self,
        *,
        pack: PackBinding,
        workflow_binding: WorkflowBinding,
        criterion,
    ) -> None:
        self.pack = pack
        self.workflow_binding = workflow_binding
        self.criterion = criterion
        self.distribution_calls = 0
        self.criterion_exports = 0
        self.requested_bindings: list[dict[str, object]] = []

    def codexia_pack_distribution(self) -> dict[str, object]:
        self.distribution_calls += 1
        return {
            "schema_version": 1,
            "pack": self.pack.to_dict(),
            "workflows": [self.workflow_binding.to_dict()],
            "roles": [],
            "capabilities": [],
        }

    def codexia_completion_criterion(
        self,
        binding: dict[str, object],
    ):
        self.criterion_exports += 1
        self.requested_bindings.append(binding)
        return self.criterion


class MissingCriterionExportPlugin:
    def __init__(
        self,
        *,
        pack: PackBinding,
        workflow_binding: WorkflowBinding,
    ) -> None:
        self.pack = pack
        self.workflow_binding = workflow_binding

    def codexia_pack_distribution(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "pack": self.pack.to_dict(),
            "workflows": [self.workflow_binding.to_dict()],
            "roles": [],
            "capabilities": [],
        }


class Service:
    def __init__(self, plugin) -> None:
        self.plugin = plugin
        self.requests: list[str] = []

    def get(self, plugin_id: str):
        self.requests.append(plugin_id)
        if plugin_id != PROVIDER_REF:
            raise KeyError(plugin_id)
        return self.plugin


def _admission(store: SqliteWorkStore, plugin):
    service = Service(plugin)
    resolver = InvariantCompletionCriterionBridge(service)
    return CompletionAdmissionService(
        store=store,
        resolver=resolver,
    ), service


def test_accepted_claim_is_durable_but_work_remains_active(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "accepted.sqlite")
    work, workflow, pin = _started(store, label="accepted")
    artifact = _artifact("result")
    evidence = _evidence("verification")
    ArtifactAdmission(store).record(
        store.snapshot(work.work.work_id),
        artifact,
    )
    EvidenceAdmission(store).record(
        store.snapshot(work.work.work_id),
        evidence,
    )
    claim = _claim(
        store,
        work.work.work_id,
        workflow,
        pin,
        artifacts=(artifact,),
        evidence=(evidence,),
    )
    criterion = CountingCriterion(workflow.run.binding)
    plugin = CriterionPlugin(
        pack=pin.pack,
        workflow_binding=workflow.run.binding,
        criterion=criterion,
    )

    admitted = _admission(store, plugin)[0].admit(
        claim,
        provider_ref=PROVIDER_REF,
    )

    assert admitted == claim
    events = store.events(work.work.work_id)
    assert events[-1].kind == COMPLETION_CLAIM_ADMITTED_EVENT
    assert events[-1].event_id == claim.claim_id
    assert project_admitted_completion_claims(events) == (claim,)
    assert store.snapshot(work.work.work_id).state is WorkState.ACTIVE
    assert criterion.calls == 1
    assert criterion.contexts[0].claim == claim


def test_rejected_criterion_does_not_mutate_work(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "rejected.sqlite")
    work, workflow, pin = _started(store, label="rejected")
    claim = _claim(store, work.work.work_id, workflow, pin)
    before = store.snapshot(work.work.work_id)
    criterion = CountingCriterion(
        workflow.run.binding,
        accepted=False,
        reason="required verification is missing",
    )
    plugin = CriterionPlugin(
        pack=pin.pack,
        workflow_binding=workflow.run.binding,
        criterion=criterion,
    )

    with pytest.raises(
        CompletionCriteriaRejected,
        match="required verification is missing",
    ):
        _admission(store, plugin)[0].admit(
            claim,
            provider_ref=PROVIDER_REF,
        )

    after = store.snapshot(work.work.work_id)
    assert after == before
    assert project_admitted_completion_claims(
        store.events(work.work.work_id)
    ) == ()
    assert criterion.calls == 1


def test_unrecorded_artifact_basis_fails_before_criterion(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "artifact-basis.sqlite")
    work, workflow, pin = _started(store, label="artifact-basis")
    detached = _artifact("detached")
    claim = _claim(
        store,
        work.work.work_id,
        workflow,
        pin,
        artifacts=(detached,),
    )
    criterion = CountingCriterion(workflow.run.binding)
    plugin = CriterionPlugin(
        pack=pin.pack,
        workflow_binding=workflow.run.binding,
        criterion=criterion,
    )

    with pytest.raises(
        CompletionClaimBasisError,
        match="ArtifactRef outside Work chronology",
    ):
        _admission(store, plugin)[0].admit(
            claim,
            provider_ref=PROVIDER_REF,
        )

    assert criterion.calls == 0


def test_unrecorded_evidence_basis_fails_before_criterion(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "evidence-basis.sqlite")
    work, workflow, pin = _started(store, label="evidence-basis")
    detached = _evidence("detached")
    claim = _claim(
        store,
        work.work.work_id,
        workflow,
        pin,
        evidence=(detached,),
    )
    criterion = CountingCriterion(workflow.run.binding)
    plugin = CriterionPlugin(
        pack=pin.pack,
        workflow_binding=workflow.run.binding,
        criterion=criterion,
    )

    with pytest.raises(
        CompletionClaimBasisError,
        match="EvidenceRef outside Work chronology",
    ):
        _admission(store, plugin)[0].admit(
            claim,
            provider_ref=PROVIDER_REF,
        )

    assert criterion.calls == 0


def test_artifact_basis_same_identity_semantic_drift_fails_closed(
    tmp_path,
) -> None:
    store = SqliteWorkStore(tmp_path / "artifact-drift.sqlite")
    work, workflow, pin = _started(store, label="artifact-drift")
    artifact_id = str(uuid4())
    durable = _artifact("durable", artifact_id=artifact_id)
    ArtifactAdmission(store).record(
        store.snapshot(work.work.work_id),
        durable,
    )
    drifted = _artifact("drifted", artifact_id=artifact_id)
    claim = _claim(
        store,
        work.work.work_id,
        workflow,
        pin,
        artifacts=(drifted,),
    )
    criterion = CountingCriterion(workflow.run.binding)
    plugin = CriterionPlugin(
        pack=pin.pack,
        workflow_binding=workflow.run.binding,
        criterion=criterion,
    )

    with pytest.raises(
        CompletionClaimBasisError,
        match="changed durable ArtifactRef semantics",
    ):
        _admission(store, plugin)[0].admit(
            claim,
            provider_ref=PROVIDER_REF,
        )

    assert criterion.calls == 0


def test_evidence_basis_same_identity_semantic_drift_fails_closed(
    tmp_path,
) -> None:
    store = SqliteWorkStore(tmp_path / "evidence-drift.sqlite")
    work, workflow, pin = _started(store, label="evidence-drift")
    evidence_id = str(uuid4())
    durable = _evidence("durable", evidence_id=evidence_id)
    EvidenceAdmission(store).record(
        store.snapshot(work.work.work_id),
        durable,
    )
    drifted = _evidence("drifted", evidence_id=evidence_id)
    claim = _claim(
        store,
        work.work.work_id,
        workflow,
        pin,
        evidence=(drifted,),
    )
    criterion = CountingCriterion(workflow.run.binding)
    plugin = CriterionPlugin(
        pack=pin.pack,
        workflow_binding=workflow.run.binding,
        criterion=criterion,
    )

    with pytest.raises(
        CompletionClaimBasisError,
        match="changed durable EvidenceRef semantics",
    ):
        _admission(store, plugin)[0].admit(
            claim,
            provider_ref=PROVIDER_REF,
        )

    assert criterion.calls == 0


def test_detached_pack_pin_is_rejected_before_criterion_resolution(
    tmp_path,
) -> None:
    store = SqliteWorkStore(tmp_path / "detached-pin.sqlite")
    work = _work(store, source_id="detached-pin")
    binding = _workflow_binding("detached-pin")
    workflow = WorkflowAdmission(store).admit_start(
        WorkflowRun.create(snapshot=work, binding=binding)
    )
    pack = _pack(binding, label="detached-pin")
    after_start = store.snapshot(work.work.work_id)
    detached_pin = PackWorkflowBinding.create(
        workflow=workflow,
        snapshot=after_start,
        pack=pack,
    )
    progressed = store.append(
        work.work.work_id,
        expected_revision=after_start.revision,
        event=after_start.next_event(
            kind="work.progress",
            payload={"step": "after detached pin creation"},
        ),
    )
    claim = CompletionClaim.create(
        snapshot=progressed,
        workflow=workflow,
        pack_binding=detached_pin,
        summary="Detached Pack pin must not be canonical completion provenance.",
    )
    criterion = CountingCriterion(binding)
    plugin = CriterionPlugin(
        pack=pack,
        workflow_binding=binding,
        criterion=criterion,
    )

    with pytest.raises(
        CompletionClaimBindingError,
        match="not durably admitted",
    ):
        _admission(store, plugin)[0].admit(
            claim,
            provider_ref=PROVIDER_REF,
        )

    assert criterion.calls == 0


def test_stale_claim_fails_before_criterion_resolution(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "stale.sqlite")
    work, workflow, pin = _started(store, label="stale")
    claim = _claim(store, work.work.work_id, workflow, pin)
    current = store.snapshot(work.work.work_id)
    store.append(
        work.work.work_id,
        expected_revision=current.revision,
        event=current.next_event(
            kind="work.concurrent-progress",
            payload={"step": 1},
        ),
    )
    criterion = CountingCriterion(workflow.run.binding)
    plugin = CriterionPlugin(
        pack=pin.pack,
        workflow_binding=workflow.run.binding,
        criterion=criterion,
    )

    with pytest.raises(WorkConcurrencyError):
        _admission(store, plugin)[0].admit(
            claim,
            provider_ref=PROVIDER_REF,
        )

    assert criterion.calls == 0


def test_terminal_workflow_is_rejected_even_if_detached_claim_was_shaped(
    tmp_path,
) -> None:
    store = SqliteWorkStore(tmp_path / "terminal-workflow.sqlite")
    work, workflow, pin = _started(store, label="terminal-workflow")
    current = store.snapshot(work.work.work_id)
    terminal_candidate = WorkflowCandidate.create(
        run_snapshot=workflow,
        work_snapshot=current,
        event_kind=WORKFLOW_COMPLETED_EVENT,
        payload={"reason": "workflow closed before claim admission"},
    )
    WorkflowAdmission(store).admit_candidate(terminal_candidate)
    claim = CompletionClaim.create(
        snapshot=store.snapshot(work.work.work_id),
        workflow=workflow,
        pack_binding=pin,
        summary="A stale active WorkflowRun object must not bypass recovery.",
    )
    criterion = CountingCriterion(workflow.run.binding)
    plugin = CriterionPlugin(
        pack=pin.pack,
        workflow_binding=workflow.run.binding,
        criterion=criterion,
    )

    with pytest.raises(
        CompletionClaimStateError,
        match="active WorkflowRun",
    ):
        _admission(store, plugin)[0].admit(
            claim,
            provider_ref=PROVIDER_REF,
        )

    assert criterion.calls == 0


def test_exact_retry_does_not_re_resolve_or_re_evaluate_criterion(
    tmp_path,
) -> None:
    store = SqliteWorkStore(tmp_path / "retry.sqlite")
    work, workflow, pin = _started(store, label="retry")
    claim = _claim(store, work.work.work_id, workflow, pin)
    criterion = CountingCriterion(workflow.run.binding)
    plugin = CriterionPlugin(
        pack=pin.pack,
        workflow_binding=workflow.run.binding,
        criterion=criterion,
    )
    service, technical = _admission(store, plugin)

    first = service.admit(claim, provider_ref=PROVIDER_REF)
    requests_after_first = tuple(technical.requests)
    second = service.admit(claim, provider_ref=PROVIDER_REF)

    assert first == claim
    assert second == claim
    assert criterion.calls == 1
    assert plugin.distribution_calls == 1
    assert plugin.criterion_exports == 1
    assert tuple(technical.requests) == requests_after_first
    assert len(project_admitted_completion_claims(
        store.events(work.work.work_id)
    )) == 1


def test_concurrent_work_change_after_criterion_acceptance_fails_final_cas(
    tmp_path,
) -> None:
    store = SqliteWorkStore(tmp_path / "criterion-race.sqlite")
    work, workflow, pin = _started(store, label="criterion-race")
    claim = _claim(store, work.work.work_id, workflow, pin)

    def mutate(context: CompletionCriterionContext) -> None:
        store.append(
            context.work.work.work_id,
            expected_revision=context.work.revision,
            event=context.work.next_event(
                kind="work.concurrent-progress",
                payload={"source": "malicious-criterion-hook"},
            ),
        )

    criterion = CountingCriterion(
        workflow.run.binding,
        hook=mutate,
    )
    plugin = CriterionPlugin(
        pack=pin.pack,
        workflow_binding=workflow.run.binding,
        criterion=criterion,
    )

    with pytest.raises(WorkConcurrencyError):
        _admission(store, plugin)[0].admit(
            claim,
            provider_ref=PROVIDER_REF,
        )

    assert criterion.calls == 1
    assert project_admitted_completion_claims(
        store.events(work.work.work_id)
    ) == ()


def test_missing_completion_criterion_export_fails_closed(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "missing-export.sqlite")
    work, workflow, pin = _started(store, label="missing-export")
    claim = _claim(store, work.work.work_id, workflow, pin)
    plugin = MissingCriterionExportPlugin(
        pack=pin.pack,
        workflow_binding=workflow.run.binding,
    )

    with pytest.raises(
        InvariantCompletionCriterionShapeError,
        match="codexia_completion_criterion",
    ):
        _admission(store, plugin)[0].admit(
            claim,
            provider_ref=PROVIDER_REF,
        )

    assert project_admitted_completion_claims(
        store.events(work.work.work_id)
    ) == ()


def test_provider_distribution_must_match_exact_pinned_pack(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "provider-pack.sqlite")
    work, workflow, pin = _started(store, label="provider-pack")
    claim = _claim(store, work.work.work_id, workflow, pin)
    different_pack = _pack(
        workflow.run.binding,
        label="provider-pack",
        definition="different-pack-semantics",
    )
    criterion = CountingCriterion(workflow.run.binding)
    plugin = CriterionPlugin(
        pack=different_pack,
        workflow_binding=workflow.run.binding,
        criterion=criterion,
    )

    with pytest.raises(
        InvariantCompletionCriterionBindingError,
        match="exact pinned Pack",
    ):
        _admission(store, plugin)[0].admit(
            claim,
            provider_ref=PROVIDER_REF,
        )

    assert criterion.calls == 0


def test_criterion_binding_drift_fails_closed(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "criterion-binding.sqlite")
    work, workflow, pin = _started(store, label="criterion-binding")
    claim = _claim(store, work.work.work_id, workflow, pin)
    drifted_binding = WorkflowBinding.create(
        workflow_id=workflow.run.binding.workflow_id,
        version=workflow.run.binding.version,
        definition_digest=_sha("different-workflow-semantics"),
    )
    criterion = CountingCriterion(drifted_binding)
    plugin = CriterionPlugin(
        pack=pin.pack,
        workflow_binding=workflow.run.binding,
        criterion=criterion,
    )

    with pytest.raises(
        InvariantCompletionCriterionBindingError,
        match="changed exact WorkflowBinding",
    ):
        _admission(store, plugin)[0].admit(
            claim,
            provider_ref=PROVIDER_REF,
        )

    assert criterion.calls == 0


def test_claim_admission_does_not_apply_parent_child_terminal_guard(
    tmp_path,
) -> None:
    store = SqliteWorkStore(tmp_path / "live-child.sqlite")
    work, workflow, pin = _started(store, label="live-child")
    delegation = Delegation.create(
        parent=store.snapshot(work.work.work_id),
        child_objective="Remain active while parent claim is admitted.",
    )
    DelegationAdmission(store).admit(delegation)
    assert (
        store.snapshot(delegation.child_work.work_id).state
        is WorkState.ACTIVE
    )

    claim = _claim(store, work.work.work_id, workflow, pin)
    criterion = CountingCriterion(workflow.run.binding)
    plugin = CriterionPlugin(
        pack=pin.pack,
        workflow_binding=workflow.run.binding,
        criterion=criterion,
    )

    admitted = _admission(store, plugin)[0].admit(
        claim,
        provider_ref=PROVIDER_REF,
    )

    assert admitted == claim
    assert store.snapshot(work.work.work_id).state is WorkState.ACTIVE
    assert (
        store.snapshot(delegation.child_work.work_id).state
        is WorkState.ACTIVE
    )


def test_generic_workflow_cannot_manufacture_completion_admission(
    tmp_path,
) -> None:
    store = SqliteWorkStore(tmp_path / "ownership.sqlite")
    work, workflow, _ = _started(store, label="ownership")
    current = store.snapshot(work.work.work_id)
    forged = WorkflowCandidate.create(
        run_snapshot=workflow,
        work_snapshot=current,
        event_kind=COMPLETION_CLAIM_ADMITTED_EVENT,
        payload={"forged": True},
    )

    with pytest.raises(
        WorkflowImplementationOwnershipError,
        match="cannot manufacture",
    ):
        validate_generic_workflow_candidate_ownership(forged)


def test_completion_criterion_does_not_expand_pack_member_taxonomy() -> None:
    assert {member.value for member in PackMemberKind} == {
        "workflow",
        "role",
        "capability",
    }


def test_g2_36_admission_does_not_invent_terminal_authority_or_cleanup() -> None:
    root = Path(__file__).resolve().parents[1] / "src" / "codexia_manual_agent"
    paths = [
        root / "completion_core" / "admission.py",
        root / "completion_core" / "boundary.py",
        root / "invariant_bridge" / "completion_criterion.py",
    ]
    forbidden_names = {
        "WORK_COMPLETED_EVENT",
        "WorkCompletion",
        "DelegationCompletionGuard",
        "AuthorizationReceipt",
        "ProcessExecutor",
        "CleanupProof",
        "Scheduler",
    }
    forbidden_modules = {
        "authority",
        "execution",
        "mutation",
        "simple_work",
    }

    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported_names: set[str] = set()
        imported_modules: set[str] = set()
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
            any(part in module for part in forbidden_modules)
            for module in imported_modules
        )
        assert not any(isinstance(node, ast.While) for node in ast.walk(tree))
