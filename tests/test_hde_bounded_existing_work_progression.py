from __future__ import annotations

import ast
import hashlib
from dataclasses import dataclass
from pathlib import Path

import pytest

from codexia_manual_agent.attention_core import (
    ATTENTION_NEED_DECLARED_EVENT,
    AttentionNeed,
)
from codexia_manual_agent.capability_core import (
    CAPABILITY_HANDOFF_ADMITTED_EVENT,
    CAPABILITY_NEED_DECLARED_EVENT,
    CapabilityBinding,
    CapabilityHostRequest,
    CapabilityNeed,
)
from codexia_manual_agent.completion_core import (
    COMPLETION_CLAIM_ADMITTED_EVENT,
    CompletionClaim,
    CompletionCriterionContext,
    CompletionCriterionResult,
)
from codexia_manual_agent.delegation_core import project_delegations
from codexia_manual_agent.pack_core import (
    PACK_WORKFLOW_BOUND_EVENT,
    PackBinding,
    PackMemberBinding,
    PackMemberKind,
)
from codexia_manual_agent.work_core import (
    WORK_CANCELLED_EVENT,
    WORK_COMPLETED_EVENT,
    SqliteWorkStore,
    Work,
    WorkIngressBinding,
    WorkState,
)
from codexia_manual_agent.workflow_core import (
    WORKFLOW_STARTED_EVENT,
    WorkflowBinding,
)
from codexia_manual_agent.workflow_orchestration import (
    BoundedExistingWorkProgressionAmbiguityError,
    BoundedExistingWorkProgressionBindingError,
    BoundedExistingWorkProgressionService,
    BoundedExistingWorkProgressionStatus,
    DurableWorkYieldKind,
)
from codexia_manual_agent.workflow_runtime import WorkflowDelegationProposal


PROVIDER_REF = "codexia:hde-bounded-provider@1.0.0"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _workflow_binding(label: str) -> WorkflowBinding:
    return WorkflowBinding.create(
        workflow_id=f"codexia:hde-{label}",
        version="1.0.0",
        definition_digest=_sha(f"workflow:{label}"),
    )


def _capability_binding(label: str) -> CapabilityBinding:
    return CapabilityBinding.create(
        capability_id=f"hde.{label}.probe",
        version="1.0.0",
        contract_digest=_sha(f"capability:{label}"),
    )


def _member_workflow(binding: WorkflowBinding) -> PackMemberBinding:
    return PackMemberBinding.create(
        kind=PackMemberKind.WORKFLOW,
        semantic_id=binding.workflow_id,
        version=binding.version,
        binding_digest=binding.binding_digest,
    )


def _member_capability(binding: CapabilityBinding) -> PackMemberBinding:
    return PackMemberBinding.create(
        kind=PackMemberKind.CAPABILITY,
        semantic_id=binding.capability_id,
        version=binding.version,
        binding_digest=binding.binding_digest,
    )


class _AcceptedCriterion:
    def __init__(self, binding: WorkflowBinding) -> None:
        self.binding = binding
        self.calls = 0

    def evaluate(
        self,
        _context: CompletionCriterionContext,
    ) -> CompletionCriterionResult:
        self.calls += 1
        return CompletionCriterionResult(
            accepted=True,
            reason="bounded existing-Work completion accepted",
        )


class _Provider:
    def __init__(
        self,
        *,
        label: str,
        implementation,
        capability: CapabilityBinding | None = None,
    ) -> None:
        self.workflow = _workflow_binding(label)
        self.capability = capability
        members = [_member_workflow(self.workflow)]
        if capability is not None:
            members.append(_member_capability(capability))
        self.pack = PackBinding.create(
            pack_id=f"codexia:hde-pack-{label}",
            version="1.0.0",
            definition_digest=_sha(f"pack:{label}"),
            members=tuple(members),
        )
        implementation.binding = self.workflow
        self.implementation = implementation
        self.criterion = _AcceptedCriterion(self.workflow)
        self.distribution_calls = 0
        self.implementation_calls = 0
        self.criterion_calls = 0

    def codexia_pack_distribution(self):
        self.distribution_calls += 1
        return {
            "schema_version": 1,
            "pack": self.pack.to_dict(),
            "workflows": [self.workflow.to_dict()],
            "roles": [],
            "capabilities": (
                [] if self.capability is None else [self.capability.to_dict()]
            ),
        }

    def codexia_workflow_implementation(self, raw_binding):
        self.implementation_calls += 1
        assert raw_binding == self.workflow.to_dict()
        return self.implementation

    def codexia_completion_criterion(self, raw_binding):
        self.criterion_calls += 1
        assert raw_binding == self.workflow.to_dict()
        return self.criterion


class _PluginService:
    def __init__(self, provider: _Provider) -> None:
        self.provider = provider
        self.calls = 0

    def get(self, plugin_id: str):
        self.calls += 1
        if plugin_id != PROVIDER_REF:
            raise KeyError(plugin_id)
        return self.provider


class _ExplodingPluginService:
    def get(self, _plugin_id: str):
        raise AssertionError("durable recovery must not resolve provider")


class _AttentionImplementation:
    binding: WorkflowBinding

    def propose(self, context):
        return AttentionNeed.create(
            workflow=context.workflow,
            snapshot=context.work,
            question="Which exact branch should the delegated Work take?",
            reason="The remaining branch requires human judgment.",
        )


class _CompletionImplementation:
    binding: WorkflowBinding

    def propose(self, context):
        return CompletionClaim.create(
            snapshot=context.work,
            workflow=context.workflow,
            pack_binding=context.pack_binding,
            summary="The exact delegated Work objective is satisfied.",
        )


class _NoProposalImplementation:
    binding: WorkflowBinding

    def propose(self, _context):
        return None


class _CapabilityImplementation:
    binding: WorkflowBinding

    def __init__(self, capability: CapabilityBinding) -> None:
        self.capability = capability

    def propose(self, context):
        if context.capabilities:
            return None
        return CapabilityNeed.create(
            workflow=context.workflow,
            snapshot=context.work,
            binding=self.capability,
            operation="probe",
            parameters={"surface": "bounded-existing-work"},
        )


class _DelegationThenCompletionImplementation:
    binding: WorkflowBinding

    def propose(self, context):
        if not context.owned_children:
            return WorkflowDelegationProposal.create(
                workflow=context.workflow,
                snapshot=context.work,
                child_objective="Remain independently active for the proof.",
            )
        return CompletionClaim.create(
            snapshot=context.work,
            workflow=context.workflow,
            pack_binding=context.pack_binding,
            summary="Parent semantics are satisfied but the child remains live.",
        )


class _AsyncHost:
    host_id = "hde.bounded.async"

    def __init__(self) -> None:
        self.calls = 0

    def submit(self, _request: CapabilityHostRequest) -> None:
        self.calls += 1
        return None


class _ForbiddenHost:
    host_id = "hde.bounded.async"

    def submit(self, _request: CapabilityHostRequest):
        raise AssertionError("existing durable handoff must suppress redispatch")


def _work(store: SqliteWorkStore, *, label: str) -> Work:
    work = Work.create(
        objective=f"Progress existing delegated Work for {label}.",
        ingress=WorkIngressBinding.create(
            source_namespace="hde.irr.worker_handoff",
            source_id=f"sha256:{_sha(label)}",
            payload_digest=_sha(f"payload:{label}"),
        ),
    )
    store.create(work)
    return work


def _service(
    store: SqliteWorkStore,
    provider: _Provider,
    *,
    plugin_service=None,
    capability_port=None,
) -> BoundedExistingWorkProgressionService:
    return BoundedExistingWorkProgressionService(
        store=store,
        plugin_service=plugin_service or _PluginService(provider),
        provider_ref=PROVIDER_REF,
        workflow_id=provider.workflow.workflow_id,
        workflow_version=provider.workflow.version,
        capability_port=capability_port,
    )


def _kinds(store: SqliteWorkStore, work_id: str) -> tuple[str, ...]:
    return tuple(event.kind for event in store.events(work_id))


def test_attention_yields_in_three_bounded_steps_and_restart_is_zero_execution(
    tmp_path,
) -> None:
    path = tmp_path / "attention.sqlite"
    store = SqliteWorkStore(path)
    work = _work(store, label="attention")
    provider = _Provider(
        label="attention",
        implementation=_AttentionImplementation(),
    )

    result = _service(store, provider).progress(
        work.work_id,
        max_steps=3,
    )

    assert result.status is BoundedExistingWorkProgressionStatus.YIELDED
    assert result.steps_used == 3
    assert result.frontier.kind is DurableWorkYieldKind.ATTENTION
    assert result.frontier.attention is not None
    assert _kinds(store, work.work_id) == (
        WORKFLOW_STARTED_EVENT,
        PACK_WORKFLOW_BOUND_EVENT,
        ATTENTION_NEED_DECLARED_EVENT,
    )

    restarted = SqliteWorkStore(path)
    recovered = _service(
        restarted,
        provider,
        plugin_service=_ExplodingPluginService(),
    ).progress(
        work.work_id,
        max_steps=3,
    )

    assert recovered.status is BoundedExistingWorkProgressionStatus.YIELDED
    assert recovered.steps_used == 0
    assert recovered.frontier.attention == result.frontier.attention
    assert restarted.events(work.work_id) == store.events(work.work_id)


def test_completion_yields_only_after_guarded_terminal_step(tmp_path) -> None:
    path = tmp_path / "completion.sqlite"
    store = SqliteWorkStore(path)
    work = _work(store, label="completion")
    provider = _Provider(
        label="completion",
        implementation=_CompletionImplementation(),
    )

    result = _service(store, provider).progress(
        work.work_id,
        max_steps=4,
    )

    assert result.status is BoundedExistingWorkProgressionStatus.YIELDED
    assert result.steps_used == 4
    assert result.frontier.kind is DurableWorkYieldKind.COMPLETION
    assert result.frontier.completion is not None
    assert result.frontier.snapshot.state is WorkState.COMPLETED
    assert _kinds(store, work.work_id) == (
        WORKFLOW_STARTED_EVENT,
        PACK_WORKFLOW_BOUND_EVENT,
        COMPLETION_CLAIM_ADMITTED_EVENT,
        WORK_COMPLETED_EVENT,
    )

    restarted = SqliteWorkStore(path)
    recovered = _service(
        restarted,
        provider,
        plugin_service=_ExplodingPluginService(),
    ).progress(
        work.work_id,
        max_steps=2,
    )
    assert recovered.status is BoundedExistingWorkProgressionStatus.YIELDED
    assert recovered.steps_used == 0
    assert recovered.frontier.completion == result.frontier.completion


def test_bound_exhaustion_resumes_same_work_without_replaying_activation(
    tmp_path,
) -> None:
    path = tmp_path / "budget.sqlite"
    store = SqliteWorkStore(path)
    work = _work(store, label="budget")
    provider = _Provider(
        label="budget",
        implementation=_AttentionImplementation(),
    )
    service = _service(store, provider)

    first = service.progress(work.work_id, max_steps=2)

    assert first.status is BoundedExistingWorkProgressionStatus.BOUND_EXHAUSTED
    assert first.steps_used == 2
    assert first.frontier.kind is DurableWorkYieldKind.NONE
    assert _kinds(store, work.work_id) == (
        WORKFLOW_STARTED_EVENT,
        PACK_WORKFLOW_BOUND_EVENT,
    )

    restarted = SqliteWorkStore(path)
    second = _service(restarted, provider).progress(
        work.work_id,
        max_steps=1,
    )

    assert second.status is BoundedExistingWorkProgressionStatus.YIELDED
    assert second.steps_used == 1
    assert second.frontier.kind is DurableWorkYieldKind.ATTENTION
    kinds = _kinds(restarted, work.work_id)
    assert kinds.count(WORKFLOW_STARTED_EVENT) == 1
    assert kinds.count(PACK_WORKFLOW_BOUND_EVENT) == 1
    assert kinds.count(ATTENTION_NEED_DECLARED_EVENT) == 1


def test_no_proposal_becomes_quiescent_without_inventing_durable_state(
    tmp_path,
) -> None:
    store = SqliteWorkStore(tmp_path / "quiescent.sqlite")
    work = _work(store, label="quiescent")
    provider = _Provider(
        label="quiescent",
        implementation=_NoProposalImplementation(),
    )

    result = _service(store, provider).progress(
        work.work_id,
        max_steps=8,
    )

    assert result.status is BoundedExistingWorkProgressionStatus.QUIESCENT
    assert result.steps_used == 3
    assert result.frontier.kind is DurableWorkYieldKind.NONE
    assert result.frontier.snapshot.state is WorkState.ACTIVE
    assert _kinds(store, work.work_id) == (
        WORKFLOW_STARTED_EVENT,
        PACK_WORKFLOW_BOUND_EVENT,
    )


def test_async_capability_handoff_is_never_redispatched_after_restart(
    tmp_path,
) -> None:
    path = tmp_path / "async-capability.sqlite"
    store = SqliteWorkStore(path)
    work = _work(store, label="async-capability")
    capability = _capability_binding("async-capability")
    provider = _Provider(
        label="async-capability",
        implementation=_CapabilityImplementation(capability),
        capability=capability,
    )
    host = _AsyncHost()

    first = _service(
        store,
        provider,
        capability_port=host,
    ).progress(
        work.work_id,
        max_steps=4,
    )

    assert first.status is BoundedExistingWorkProgressionStatus.BOUND_EXHAUSTED
    assert first.steps_used == 4
    assert first.frontier.kind is DurableWorkYieldKind.NONE
    assert host.calls == 1
    kinds = _kinds(store, work.work_id)
    assert kinds.count(CAPABILITY_NEED_DECLARED_EVENT) == 1
    assert kinds.count(CAPABILITY_HANDOFF_ADMITTED_EVENT) == 1

    restarted = SqliteWorkStore(path)
    recovered = _service(
        restarted,
        provider,
        plugin_service=_ExplodingPluginService(),
        capability_port=_ForbiddenHost(),
    ).progress(
        work.work_id,
        max_steps=2,
    )

    assert recovered.status is BoundedExistingWorkProgressionStatus.QUIESCENT
    assert recovered.steps_used == 0
    assert recovered.frontier.kind is DurableWorkYieldKind.NONE
    assert restarted.events(work.work_id) == store.events(work.work_id)


def test_live_child_blocks_parent_completion_without_recursive_progression(
    tmp_path,
) -> None:
    store = SqliteWorkStore(tmp_path / "live-child.sqlite")
    work = _work(store, label="live-child")
    provider = _Provider(
        label="live-child",
        implementation=_DelegationThenCompletionImplementation(),
    )

    result = _service(store, provider).progress(
        work.work_id,
        max_steps=8,
    )

    assert result.status is BoundedExistingWorkProgressionStatus.QUIESCENT
    assert result.steps_used == 4
    assert result.frontier.kind is DurableWorkYieldKind.NONE
    assert store.snapshot(work.work_id).state is WorkState.ACTIVE
    assert _kinds(store, work.work_id)[-1] == COMPLETION_CLAIM_ADMITTED_EVENT

    delegations = project_delegations(store.events(work.work_id))
    assert len(delegations) == 1
    child = store.snapshot(delegations[0].child_work.work_id)
    assert child.state is WorkState.ACTIVE
    assert child.revision == 0
    assert store.events(child.work.work_id) == ()


def test_multiple_unresolved_lanes_fail_closed_instead_of_scheduling(
    tmp_path,
) -> None:
    store = SqliteWorkStore(tmp_path / "ambiguous.sqlite")
    work = _work(store, label="ambiguous")
    first_binding = _capability_binding("ambiguous-a")
    second_binding = _capability_binding("ambiguous-b")

    class _FirstImplementation:
        binding: WorkflowBinding

        def __init__(self) -> None:
            self.calls = 0

        def propose(self, context):
            self.calls += 1
            binding = first_binding if self.calls == 1 else second_binding
            return CapabilityNeed.create(
                workflow=context.workflow,
                snapshot=context.work,
                binding=binding,
                operation="probe",
                parameters={"call": self.calls},
            )

    implementation = _FirstImplementation()
    provider = _Provider(
        label="ambiguous",
        implementation=implementation,
        capability=first_binding,
    )

    # The exact Pack cannot truthfully contain a second CapabilityBinding that
    # the provider did not distribute, so construct the ambiguous durable state
    # explicitly after activation with a Pack that contains both capabilities.
    provider.pack = PackBinding.create(
        pack_id="codexia:hde-pack-ambiguous",
        version="1.0.0",
        definition_digest=_sha("pack:ambiguous-two"),
        members=(
            _member_workflow(provider.workflow),
            _member_capability(first_binding),
            _member_capability(second_binding),
        ),
    )
    provider.capability = first_binding

    # Override the distribution to expose both exact bindings.
    def _distribution():
        provider.distribution_calls += 1
        return {
            "schema_version": 1,
            "pack": provider.pack.to_dict(),
            "workflows": [provider.workflow.to_dict()],
            "roles": [],
            "capabilities": [
                first_binding.to_dict(),
                second_binding.to_dict(),
            ],
        }

    provider.codexia_pack_distribution = _distribution

    service = _service(store, provider)
    service.progress(work.work_id, max_steps=3)

    # First pending CapabilityNeed prevents Workflow progression, so use the
    # existing specialized admission path to create a second unresolved lane.
    from codexia_manual_agent.capability_core import CapabilityAdmission
    from codexia_manual_agent.workflow_core import project_workflow_runs

    workflow = project_workflow_runs(store.events(work.work_id))[0]
    second = CapabilityNeed.create(
        workflow=workflow,
        snapshot=store.snapshot(work.work_id),
        binding=second_binding,
        operation="probe",
        parameters={"manual": 2},
    )
    CapabilityAdmission(store).admit_need(second)

    with pytest.raises(
        BoundedExistingWorkProgressionAmbiguityError,
        match="multiple unresolved",
    ):
        service.progress(work.work_id, max_steps=1)


def test_cancelled_work_returns_terminal_non_yield_without_provider(
    tmp_path,
) -> None:
    store = SqliteWorkStore(tmp_path / "cancelled.sqlite")
    work = _work(store, label="cancelled")
    snapshot = store.snapshot(work.work_id)
    store.append(
        work.work_id,
        expected_revision=snapshot.revision,
        event=snapshot.next_event(
            kind=WORK_CANCELLED_EVENT,
            payload={"reason": "external cancellation"},
        ),
    )
    provider = _Provider(
        label="cancelled",
        implementation=_AttentionImplementation(),
    )

    result = _service(
        store,
        provider,
        plugin_service=_ExplodingPluginService(),
    ).progress(
        work.work_id,
        max_steps=5,
    )

    assert result.status is BoundedExistingWorkProgressionStatus.TERMINAL_NON_YIELD
    assert result.steps_used == 0
    assert result.frontier.kind is DurableWorkYieldKind.NONE
    assert result.frontier.snapshot.state is WorkState.CANCELLED


def test_preactivation_chronology_is_not_silently_adopted(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "preactivation.sqlite")
    work = _work(store, label="preactivation")
    snapshot = store.snapshot(work.work_id)
    store.append(
        work.work_id,
        expected_revision=snapshot.revision,
        event=snapshot.next_event(
            kind="work.external-observation",
            payload={"source": "before-codexia-activation"},
        ),
    )
    provider = _Provider(
        label="preactivation",
        implementation=_AttentionImplementation(),
    )

    with pytest.raises(
        BoundedExistingWorkProgressionBindingError,
        match="already has durable chronology",
    ):
        _service(store, provider).progress(
            work.work_id,
            max_steps=1,
        )


def test_bounded_progression_has_no_hde_irr_scheduler_or_work_creation() -> None:
    path = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "codexia_manual_agent"
        / "workflow_orchestration"
        / "existing_work_progression.py"
    )
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

    forbidden_names = {
        "AuthorizationReceipt",
        "GovernanceDecision",
        "ProcessExecutor",
        "Queue",
        "Scheduler",
        "WorkerNeed",
        "WorkerResult",
    }
    assert forbidden_names.isdisjoint(imported_names)
    assert not any(
        module.startswith(("hde_", "intent_resolution_runtime"))
        for module in imported_modules
    )
    assert not any(
        module.endswith((".standalone_host", ".authority", ".execution"))
        for module in imported_modules
    )
    assert not any(isinstance(node, ast.While) for node in ast.walk(tree))
    assert "Work.create(" not in source
    assert ".create(child" not in source
    assert "progress(child" not in source
