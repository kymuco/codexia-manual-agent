from __future__ import annotations

import ast
import hashlib
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import pytest

from codexia_manual_agent.capability_core import (
    CapabilityAdmission,
    CapabilityBinding,
    CapabilityNeed,
    CapabilityNeedState,
    CapabilityOutcome,
)
from codexia_manual_agent.completion_core import (
    CompletionAdmissionService,
    CompletionClaim,
    CompletionCriteriaRejected,
    CompletionCriterionContext,
    CompletionCriterionResult,
)
from codexia_manual_agent.evidence_core import EvidenceAdmission, EvidenceRef
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
    WorkIngressBinding,
    WorkState,
)
from codexia_manual_agent.workflow_core import (
    WorkflowAdmission,
    WorkflowBinding,
    WorkflowRun,
)

PROVIDER_REF = "codexia:g2.41-provider@1.0.0"
PROCESS_EVIDENCE_KIND = "codexia.capability-outcome.succeeded.v1"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _workflow_binding(label: str) -> WorkflowBinding:
    return WorkflowBinding.create(
        workflow_id=f"codexia:g2.41-{label}",
        version="1.0.0",
        definition_digest=_sha(f"workflow:{label}"),
    )


def _capability_binding() -> CapabilityBinding:
    return CapabilityBinding.create(
        capability_id="process",
        version="1.0.0",
        contract_digest=_sha("g2.41-process-contract"),
    )


def _member_for_workflow(binding: WorkflowBinding) -> PackMemberBinding:
    return PackMemberBinding.create(
        kind=PackMemberKind.WORKFLOW,
        semantic_id=binding.workflow_id,
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


def _work(store: SqliteWorkStore, *, label: str) -> Work:
    work = Work.create(
        objective=f"Exercise G2.41 completion criterion capability context: {label}",
        ingress=WorkIngressBinding.create(
            source_namespace="standalone.api",
            source_id=f"g2.41-{label}",
            payload_digest=_sha(f"payload:{label}"),
        ),
    )
    store.create(work)
    return work


def _start(
    store: SqliteWorkStore,
    work: Work,
    *,
    label: str,
):
    workflow_binding = _workflow_binding(label)
    workflow = WorkflowAdmission(store).admit_start(
        WorkflowRun.create(
            snapshot=store.snapshot(work.work_id),
            binding=workflow_binding,
        )
    )
    capability_binding = _capability_binding()
    pack = PackBinding.create(
        pack_id=f"codexia:g2.41-pack-{label}",
        version="1.0.0",
        definition_digest=_sha(f"pack:{label}"),
        members=(
            _member_for_workflow(workflow_binding),
            _member_for_capability(capability_binding),
        ),
    )
    pin = PackAdmission(store).admit_workflow_binding(
        PackWorkflowBinding.create(
            workflow=workflow,
            snapshot=store.snapshot(work.work_id),
            pack=pack,
        )
    )
    return workflow, pin, capability_binding


def _admit_succeeded_capability(
    store: SqliteWorkStore,
    workflow,
    binding: CapabilityBinding,
    *,
    label: str,
):
    pending = CapabilityAdmission(store).admit_need(
        CapabilityNeed.create(
            workflow=workflow,
            snapshot=store.snapshot(workflow.run.work_id),
            binding=binding,
            operation="run",
            parameters={"probe": label},
        )
    )
    outcome = CapabilityOutcome.succeeded(
        pending,
        attempt_id=f"attempt:{label}",
        attempt_digest=_sha(f"attempt:{label}"),
        observation={"probe": label, "exit_code": 0},
    )
    return CapabilityAdmission(store).admit_outcome(outcome)


def _outcome_evidence(capability) -> EvidenceRef:
    assert capability.outcome is not None
    outcome = capability.outcome
    return EvidenceRef.create(
        evidence_id=outcome.outcome_id,
        evidence_digest=outcome.outcome_digest,
        evidence_kind=PROCESS_EVIDENCE_KIND,
        locator=f"work-event://{outcome.work_id}/{outcome.outcome_id}",
    )


class _OutcomeBoundCriterion:
    def __init__(self, binding: WorkflowBinding) -> None:
        self._binding = binding
        self.calls = 0
        self.contexts: list[CompletionCriterionContext] = []

    @property
    def binding(self) -> WorkflowBinding:
        return self._binding

    def evaluate(
        self,
        context: CompletionCriterionContext,
    ) -> CompletionCriterionResult:
        self.calls += 1
        self.contexts.append(context)

        succeeded = tuple(
            capability
            for capability in context.capabilities
            if capability.state is CapabilityNeedState.SUCCEEDED
            and capability.outcome is not None
        )
        if len(succeeded) != 1:
            return CompletionCriterionResult(
                accepted=False,
                reason="Exactly one succeeded CapabilityOutcome is required.",
            )

        outcome = succeeded[0].outcome
        assert outcome is not None
        if len(context.claim.evidence_refs) != 1:
            return CompletionCriterionResult(
                accepted=False,
                reason="Exactly one CapabilityOutcome EvidenceRef is required.",
            )
        evidence = context.claim.evidence_refs[0]
        if (
            evidence.evidence_id != outcome.outcome_id
            or evidence.evidence_digest != outcome.outcome_digest
            or evidence.evidence_kind != PROCESS_EVIDENCE_KIND
        ):
            return CompletionCriterionResult(
                accepted=False,
                reason="Completion evidence does not bind the succeeded CapabilityOutcome.",
            )

        return CompletionCriterionResult(
            accepted=True,
            reason="Completion evidence binds the succeeded CapabilityOutcome.",
        )


@dataclass(frozen=True)
class _ResolvedCriterion:
    workflow_binding: WorkflowBinding
    pack_binding_digest: str
    criterion: _OutcomeBoundCriterion


class _Resolver:
    def __init__(
        self,
        *,
        binding: WorkflowBinding,
        pack_digest: str,
        criterion: _OutcomeBoundCriterion,
    ) -> None:
        self.binding = binding
        self.pack_digest = pack_digest
        self.criterion = criterion

    def resolve(self, *, provider_ref, workflow, pack_binding):
        assert provider_ref == PROVIDER_REF
        return _ResolvedCriterion(
            workflow_binding=self.binding,
            pack_binding_digest=self.pack_digest,
            criterion=self.criterion,
        )


def _admission(
    store: SqliteWorkStore,
    workflow,
    pin,
    criterion: _OutcomeBoundCriterion,
) -> CompletionAdmissionService:
    return CompletionAdmissionService(
        store=store,
        resolver=_Resolver(
            binding=workflow.run.binding,
            pack_digest=pin.pack.binding_digest,
            criterion=criterion,
        ),
    )


def test_completion_criterion_receives_exact_succeeded_capability_outcome(
    tmp_path,
) -> None:
    store = SqliteWorkStore(tmp_path / "succeeded.sqlite")
    work = _work(store, label="succeeded")
    workflow, pin, binding = _start(store, work, label="succeeded")
    capability = _admit_succeeded_capability(
        store,
        workflow,
        binding,
        label="succeeded",
    )
    evidence = _outcome_evidence(capability)
    EvidenceAdmission(store).record(
        store.snapshot(work.work_id),
        evidence,
    )
    claim = CompletionClaim.create(
        snapshot=store.snapshot(work.work_id),
        workflow=workflow,
        pack_binding=pin,
        summary="The exact succeeded capability satisfies this Work.",
        evidence_refs=(evidence,),
    )
    criterion = _OutcomeBoundCriterion(workflow.run.binding)

    admitted = _admission(
        store,
        workflow,
        pin,
        criterion,
    ).admit(
        claim,
        provider_ref=PROVIDER_REF,
    )

    assert admitted == claim
    assert criterion.calls == 1
    assert criterion.contexts[0].capabilities == (capability,)
    assert criterion.contexts[0].capabilities[0].outcome == capability.outcome
    assert store.snapshot(work.work_id).state is WorkState.ACTIVE


def test_forged_success_evidence_without_capability_outcome_is_rejected(
    tmp_path,
) -> None:
    store = SqliteWorkStore(tmp_path / "forged-evidence.sqlite")
    work = _work(store, label="forged-evidence")
    workflow, pin, _ = _start(store, work, label="forged-evidence")

    evidence_id = str(uuid4())
    forged = EvidenceRef.create(
        evidence_id=evidence_id,
        evidence_digest=_sha("invented-success-outcome"),
        evidence_kind=PROCESS_EVIDENCE_KIND,
        locator=f"work-event://{work.work_id}/{evidence_id}",
    )
    EvidenceAdmission(store).record(
        store.snapshot(work.work_id),
        forged,
    )
    claim = CompletionClaim.create(
        snapshot=store.snapshot(work.work_id),
        workflow=workflow,
        pack_binding=pin,
        summary="A self-consistent EvidenceRef is not a CapabilityOutcome.",
        evidence_refs=(forged,),
    )
    criterion = _OutcomeBoundCriterion(workflow.run.binding)
    before = store.events(work.work_id)

    with pytest.raises(
        CompletionCriteriaRejected,
        match="succeeded CapabilityOutcome",
    ):
        _admission(
            store,
            workflow,
            pin,
            criterion,
        ).admit(
            claim,
            provider_ref=PROVIDER_REF,
        )

    assert criterion.calls == 1
    assert criterion.contexts[0].capabilities == ()
    assert store.events(work.work_id) == before
    assert store.snapshot(work.work_id).state is WorkState.ACTIVE


def test_completion_context_filters_capabilities_to_exact_workflow(
    tmp_path,
) -> None:
    store = SqliteWorkStore(tmp_path / "workflow-filter.sqlite")
    work = _work(store, label="workflow-filter")
    first, first_pin, binding = _start(store, work, label="first")
    first_capability = _admit_succeeded_capability(
        store,
        first,
        binding,
        label="first",
    )

    second_binding = _workflow_binding("second")
    second = WorkflowAdmission(store).admit_start(
        WorkflowRun.create(
            snapshot=store.snapshot(work.work_id),
            binding=second_binding,
        )
    )
    second_pack = PackBinding.create(
        pack_id="codexia:g2.41-pack-second",
        version="1.0.0",
        definition_digest=_sha("pack:second"),
        members=(
            _member_for_workflow(second_binding),
            _member_for_capability(binding),
        ),
    )
    second_pin = PackAdmission(store).admit_workflow_binding(
        PackWorkflowBinding.create(
            workflow=second,
            snapshot=store.snapshot(work.work_id),
            pack=second_pack,
        )
    )
    _admit_succeeded_capability(
        store,
        second,
        binding,
        label="second",
    )

    evidence = _outcome_evidence(first_capability)
    EvidenceAdmission(store).record(
        store.snapshot(work.work_id),
        evidence,
    )
    claim = CompletionClaim.create(
        snapshot=store.snapshot(work.work_id),
        workflow=first,
        pack_binding=first_pin,
        summary="Only the first Workflow capability may satisfy this claim.",
        evidence_refs=(evidence,),
    )
    criterion = _OutcomeBoundCriterion(first.run.binding)

    admitted = _admission(
        store,
        first,
        first_pin,
        criterion,
    ).admit(
        claim,
        provider_ref=PROVIDER_REF,
    )

    assert admitted == claim
    assert criterion.contexts[0].capabilities == (first_capability,)
    assert all(
        capability.need.workflow_run_id == first.run.workflow_run_id
        for capability in criterion.contexts[0].capabilities
    )
    assert second_pin.workflow_run_id == second.run.workflow_run_id


def test_capabilities_field_is_appended_for_context_compatibility() -> None:
    assert tuple(CompletionCriterionContext.__dataclass_fields__) == (
        "claim",
        "work",
        "workflow",
        "pack_binding",
        "capabilities",
    )


def test_g2_41_adds_no_host_authority_scheduler_or_completion_read_set() -> None:
    root = Path(__file__).resolve().parents[1] / "src" / "codexia_manual_agent"
    paths = [
        root / "completion_core" / "boundary.py",
        root / "completion_core" / "admission.py",
    ]
    forbidden_names = {
        "CapabilityHostPort",
        "CapabilityProgressionService",
        "StandaloneProcessCapabilityPort",
        "AuthorizationReceipt",
        "ProcessExecutor",
        "Scheduler",
        "WorkCompletionAdmissionService",
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
