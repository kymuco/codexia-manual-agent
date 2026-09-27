from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from codexia_manual_agent.attention_core import (
    ATTENTION_RESPONSE_RECORDED_EVENT,
    AttentionAdmission,
    AttentionNeed,
    AttentionResponse,
    project_attention_need,
    project_attention_response,
    project_attention_responses,
)
from codexia_manual_agent.capability_core import (
    CapabilityHostPort,
    CapabilityNeedState,
    project_capability_needs,
)
from codexia_manual_agent.delegation_core import project_delegations
from codexia_manual_agent.invariant_bridge import ManagedPluginServicePort
from codexia_manual_agent.pack_core import (
    project_pack_workflow_bindings,
    project_workflow_pack_binding,
)
from codexia_manual_agent.role_core import (
    CognitionPort,
    RoleRunState,
    project_role_runs,
)
from codexia_manual_agent.work_core import (
    Work,
    WorkEvent,
    WorkIngressBinding,
    WorkSnapshot,
    WorkState,
    WorkStore,
)
from codexia_manual_agent.workflow_core import project_workflow_runs
from codexia_manual_agent.workflow_orchestration import (
    BoundedExistingWorkProgressionResult,
    BoundedExistingWorkProgressionService,
    BoundedExistingWorkProgressionStatus,
    ContextProjectionMaterialPort,
    DurableWorkYield,
    DurableWorkYieldKind,
    RoleInstructionsMaterialPort,
    project_durable_work_yield,
)

STANDALONE_WORK_INGRESS_NAMESPACE = "codexia.standalone.work"
STANDALONE_ANSWER_SOURCE_NAMESPACE = "codexia.standalone.work.answer"
MAX_STATUS_ITEMS = 64
MAX_INSPECT_EVENTS = 256


class StandaloneWorkSurfaceError(RuntimeError):
    """Base failure for the product-facing standalone Gen2 Work surface."""


class StandaloneWorkSurfaceBindingError(StandaloneWorkSurfaceError):
    """Durable Work state differs from the explicitly selected runtime binding."""


class StandaloneWorkSurfaceStateError(StandaloneWorkSurfaceError):
    """The requested operation is invalid for the current Work frontier."""


class StandaloneWorkSurfaceConcurrencyError(StandaloneWorkSurfaceError):
    """A read-only product view crossed a concurrent Work chronology change."""


@dataclass(frozen=True, slots=True)
class StandaloneWorkSelector:
    """Explicit technical+semantic selector supplied by the standalone caller."""

    provider_ref: str
    workflow_id: str
    workflow_version: str

    def __post_init__(self) -> None:
        for label, value in (
            ("provider_ref", self.provider_ref),
            ("workflow_id", self.workflow_id),
            ("workflow_version", self.workflow_version),
        ):
            if type(value) is not str or not value or value != value.strip():
                raise ValueError(f"{label} must be non-empty canonical text")

    def to_dict(self) -> dict[str, str]:
        return {
            "provider_ref": self.provider_ref,
            "workflow_id": self.workflow_id,
            "workflow_version": self.workflow_version,
        }


@dataclass(frozen=True, slots=True)
class StandaloneWorkHost:
    """Explicit local composition supplied by the product host."""

    plugin_service: ManagedPluginServicePort
    cognition_port: CognitionPort | None = None
    capability_port: CapabilityHostPort | None = None
    instructions: RoleInstructionsMaterialPort | None = None
    context: ContextProjectionMaterialPort | None = None

    def __post_init__(self) -> None:
        if not callable(getattr(self.plugin_service, "get", None)):
            raise TypeError("plugin_service must expose callable get(plugin_id)")


StandaloneWorkHostFactory = Callable[[], StandaloneWorkHost]


def _canonical_digest(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class StandaloneWorkSurface:
    """Small product surface over existing Gen2 durable Work semantics."""

    def __init__(self, store: WorkStore) -> None:
        self._store = store

    def start(
        self,
        *,
        objective: str,
        selector: StandaloneWorkSelector,
        host_factory: StandaloneWorkHostFactory,
        source_id: str | None = None,
    ) -> dict[str, Any]:
        if type(objective) is not str or not objective.strip():
            raise ValueError("objective must be non-empty text")
        if not isinstance(selector, StandaloneWorkSelector):
            raise TypeError("selector must be StandaloneWorkSelector")
        if not callable(host_factory):
            raise TypeError("host_factory must be callable")

        source_id = source_id or str(uuid4())
        if type(source_id) is not str or not source_id or source_id != source_id.strip():
            raise ValueError("source_id must be non-empty canonical text")

        payload_digest = _canonical_digest(
            {
                "schema": "codexia.standalone.work.start.v1",
                "source_id": source_id,
                "objective": objective,
                "selector": selector.to_dict(),
            }
        )
        work = Work.create(
            objective=objective,
            ingress=WorkIngressBinding.create(
                source_namespace=STANDALONE_WORK_INGRESS_NAMESPACE,
                source_id=source_id,
                payload_digest=payload_digest,
            ),
        )
        snapshot = self._store.create(work)
        events = self._store.events(snapshot.work.work_id)

        missing_activation_steps = self._missing_activation_steps(
            snapshot,
            events,
            selector=selector,
        )
        if missing_activation_steps:
            host = host_factory()
            self._progression_service(
                selector=selector,
                host=host,
            ).progress(
                snapshot.work.work_id,
                max_steps=missing_activation_steps,
            )

        self._require_exact_activation(
            snapshot.work.work_id,
            selector=selector,
        )
        return self.status(snapshot.work.work_id)

    def status(self, work_id: str) -> dict[str, Any]:
        snapshot, events, frontier = self._read_exact_surface(work_id)
        return self._status_from_exact(
            snapshot=snapshot,
            events=events,
            frontier=frontier,
        )

    def advance(
        self,
        work_id: str,
        *,
        selector: StandaloneWorkSelector,
        host_factory: StandaloneWorkHostFactory,
        max_steps: int,
    ) -> dict[str, Any]:
        if not isinstance(selector, StandaloneWorkSelector):
            raise TypeError("selector must be StandaloneWorkSelector")
        if not callable(host_factory):
            raise TypeError("host_factory must be callable")

        snapshot, events, frontier = self._read_exact_surface(work_id)
        self._require_selector_matches_durable_work(
            snapshot,
            events,
            selector=selector,
        )

        if frontier.kind is not DurableWorkYieldKind.NONE:
            result = BoundedExistingWorkProgressionResult(
                status=BoundedExistingWorkProgressionStatus.YIELDED,
                frontier=frontier,
                steps_used=0,
            )
        elif snapshot.state is not WorkState.ACTIVE:
            result = BoundedExistingWorkProgressionResult(
                status=BoundedExistingWorkProgressionStatus.TERMINAL_NON_YIELD,
                frontier=frontier,
                steps_used=0,
            )
        else:
            host = host_factory()
            result = self._progression_service(
                selector=selector,
                host=host,
            ).progress(
                work_id,
                max_steps=max_steps,
            )

        return {
            "progression": self._progression_dict(result),
            "status": self.status(work_id),
        }

    def inspect(
        self,
        work_id: str,
        *,
        offset: int = 0,
        limit: int = 100,
    ) -> dict[str, Any]:
        if type(offset) is not int or offset < 0:
            raise ValueError("offset must be a non-negative integer")
        if type(limit) is not int or limit <= 0 or limit > MAX_INSPECT_EVENTS:
            raise ValueError(f"limit must be an integer in [1, {MAX_INSPECT_EVENTS}]")

        snapshot, events, frontier = self._read_exact_surface(work_id)
        page = events[offset : offset + limit]
        return {
            "status": self._status_from_exact(
                snapshot=snapshot,
                events=events,
                frontier=frontier,
            ),
            "chronology": {
                "offset": offset,
                "limit": limit,
                "total_events": len(events),
                "has_more": offset + len(page) < len(events),
                "events": [event.to_dict() for event in page],
            },
        }

    def answer(
        self,
        work_id: str,
        *,
        response_text: str,
        attention_id: str | None = None,
        source_id: str | None = None,
    ) -> dict[str, Any]:
        if type(response_text) is not str or not response_text.strip():
            raise ValueError("response_text must be non-empty text")

        snapshot, events, frontier = self._read_exact_surface(work_id)
        if (
            attention_id is None
            and frontier.kind is not DurableWorkYieldKind.ATTENTION
        ):
            recovered = self._recover_head_answer_retry(
                events,
                response_text=response_text,
                source_id=source_id,
            )
            if recovered is not None:
                return {
                    "response": recovered.to_dict(),
                    "status": self.status(work_id),
                    "idempotent": True,
                }

        target = self._resolve_answer_target(
            events,
            frontier=frontier,
            attention_id=attention_id,
        )
        source_id = source_id or (
            f"attention:{target.attention_id}:"
            f"{_canonical_digest({'response_text': response_text})}"
        )

        for existing in project_attention_responses(events):
            if (
                existing.source_namespace == STANDALONE_ANSWER_SOURCE_NAMESPACE
                and existing.source_id == source_id
            ):
                expected_payload_digest = _canonical_digest(
                    {"response_text": response_text}
                )
                if (
                    existing.attention_id == target.attention_id
                    and existing.response_text == response_text
                    and existing.source_payload_digest == expected_payload_digest
                ):
                    return {
                        "response": existing.to_dict(),
                        "status": self.status(work_id),
                        "idempotent": True,
                    }
                raise StandaloneWorkSurfaceBindingError(
                    "answer source identity is already bound to different evidence"
                )

        if (
            frontier.kind is not DurableWorkYieldKind.ATTENTION
            or frontier.attention is None
            or frontier.attention.attention_id != target.attention_id
        ):
            raise StandaloneWorkSurfaceStateError(
                "AttentionNeed is not the exact current Work return frontier"
            )

        response = AttentionResponse.create(
            need=target,
            snapshot=snapshot,
            response_text=response_text,
            source_namespace=STANDALONE_ANSWER_SOURCE_NAMESPACE,
            source_id=source_id,
        )
        admitted = AttentionAdmission(self._store).admit_response(response)
        return {
            "response": admitted.to_dict(),
            "status": self.status(work_id),
            "idempotent": False,
        }

    def _progression_service(
        self,
        *,
        selector: StandaloneWorkSelector,
        host: StandaloneWorkHost,
    ) -> BoundedExistingWorkProgressionService:
        if not isinstance(host, StandaloneWorkHost):
            raise TypeError("host_factory must return StandaloneWorkHost")
        return BoundedExistingWorkProgressionService(
            store=self._store,
            plugin_service=host.plugin_service,
            provider_ref=selector.provider_ref,
            workflow_id=selector.workflow_id,
            workflow_version=selector.workflow_version,
            cognition_port=host.cognition_port,
            capability_port=host.capability_port,
            instructions=host.instructions,
            context=host.context,
        )

    def _missing_activation_steps(
        self,
        snapshot: WorkSnapshot,
        events: tuple[WorkEvent, ...],
        *,
        selector: StandaloneWorkSelector,
    ) -> int:
        workflows = project_workflow_runs(events)
        if not workflows:
            if snapshot.revision != 0 or events:
                raise StandaloneWorkSurfaceBindingError(
                    "unconfigured standalone Work already has durable chronology"
                )
            return 2

        if len(workflows) != 1:
            raise StandaloneWorkSurfaceBindingError(
                "standalone Work must contain exactly one WorkflowRun"
            )
        workflow = workflows[0]
        binding = workflow.run.binding
        if (
            binding.workflow_id != selector.workflow_id
            or binding.version != selector.workflow_version
        ):
            raise StandaloneWorkSurfaceBindingError(
                "existing WorkflowRun differs from selected Workflow"
            )

        pin = project_workflow_pack_binding(
            events,
            workflow.run.workflow_run_id,
        )
        if pin is None:
            if snapshot.revision != 1 or len(events) != 1:
                raise StandaloneWorkSurfaceBindingError(
                    "unbound WorkflowRun is not the exact activation frontier"
                )
            return 1
        return 0

    def _require_exact_activation(
        self,
        work_id: str,
        *,
        selector: StandaloneWorkSelector,
    ) -> None:
        snapshot, events, _ = self._read_exact_surface(work_id)
        self._require_selector_matches_durable_work(
            snapshot,
            events,
            selector=selector,
        )
        workflows = project_workflow_runs(events)
        if len(workflows) != 1:
            raise StandaloneWorkSurfaceBindingError(
                "start did not establish one exact WorkflowRun"
            )
        if project_workflow_pack_binding(
            events,
            workflows[0].run.workflow_run_id,
        ) is None:
            raise StandaloneWorkSurfaceBindingError(
                "start did not establish exact Pack pin"
            )

    def _require_selector_matches_durable_work(
        self,
        snapshot: WorkSnapshot,
        events: tuple[WorkEvent, ...],
        *,
        selector: StandaloneWorkSelector,
    ) -> None:
        workflows = project_workflow_runs(events)
        if not workflows:
            raise StandaloneWorkSurfaceBindingError(
                "Work has no configured WorkflowRun"
            )
        if len(workflows) != 1:
            raise StandaloneWorkSurfaceBindingError("Work has multiple WorkflowRuns")
        binding = workflows[0].run.binding
        if (
            binding.workflow_id != selector.workflow_id
            or binding.version != selector.workflow_version
        ):
            raise StandaloneWorkSurfaceBindingError(
                "configured Workflow differs from explicit selector"
            )
        pin = project_workflow_pack_binding(
            events,
            workflows[0].run.workflow_run_id,
        )
        if pin is None:
            raise StandaloneWorkSurfaceBindingError(
                "configured Workflow has no exact Pack pin"
            )
        if pin.work_id != snapshot.work.work_id:
            raise StandaloneWorkSurfaceBindingError("Pack pin crossed Work identity")

    def _read_exact_surface(
        self,
        work_id: str,
    ) -> tuple[WorkSnapshot, tuple[WorkEvent, ...], DurableWorkYield]:
        if type(work_id) is not str or not work_id:
            raise ValueError("work_id must be non-empty text")

        before = self._store.snapshot(work_id)
        events = self._store.events(work_id)
        after = self._store.snapshot(work_id)
        if before != after:
            raise StandaloneWorkSurfaceConcurrencyError(
                "Work changed during product read; retry the read"
            )
        if before.work.work_id != work_id:
            raise StandaloneWorkSurfaceBindingError(
                "Work snapshot crossed requested Work identity"
            )
        if any(event.work_id != work_id for event in events):
            raise StandaloneWorkSurfaceBindingError(
                "Work chronology crossed requested Work identity"
            )
        if before.revision != len(events):
            raise StandaloneWorkSurfaceBindingError(
                "Work revision differs from durable chronology"
            )
        if before.revision == 0:
            if events or before.last_event_digest is not None:
                raise StandaloneWorkSurfaceBindingError(
                    "revision-zero Work has inconsistent chronology"
                )
        else:
            if not events:
                raise StandaloneWorkSurfaceBindingError(
                    "nonzero Work revision has no chronology"
                )
            head = events[-1]
            if (
                head.sequence != before.revision
                or head.event_digest != before.last_event_digest
            ):
                raise StandaloneWorkSurfaceBindingError(
                    "Work snapshot does not bind exact chronology head"
                )

        frontier = project_durable_work_yield(work_id, store=self._store)
        if frontier.snapshot != before:
            raise StandaloneWorkSurfaceConcurrencyError(
                "Work changed while projecting durable return frontier"
            )
        return before, events, frontier

    def _status_from_exact(
        self,
        *,
        snapshot: WorkSnapshot,
        events: tuple[WorkEvent, ...],
        frontier: DurableWorkYield,
    ) -> dict[str, Any]:
        workflows = project_workflow_runs(events)
        pins = project_pack_workflow_bindings(events)
        roles = project_role_runs(events)
        capabilities = project_capability_needs(events)
        delegations = project_delegations(events)
        responses = project_attention_responses(events)

        unresolved_roles = [
            item
            for item in roles
            if item.state in {RoleRunState.ACTIVE, RoleRunState.REQUESTED}
        ]
        unresolved_capabilities = [
            item
            for item in capabilities
            if item.state is CapabilityNeedState.PENDING
        ]
        child_rows: list[dict[str, Any]] = []
        live_children = 0
        for index, delegation in enumerate(delegations):
            child = self._store.snapshot(delegation.child_work.work_id)
            if child.state is WorkState.ACTIVE:
                live_children += 1
            if index < MAX_STATUS_ITEMS:
                child_rows.append(
                    {
                        "delegation_id": delegation.delegation_id,
                        "work_id": child.work.work_id,
                        "state": child.state.value,
                        "revision": child.revision,
                        "objective": child.work.objective,
                    }
                )

        yield_data: dict[str, Any] = {"kind": frontier.kind.value}
        if frontier.attention is not None:
            yield_data["attention"] = frontier.attention.to_dict()
        if frontier.completion is not None:
            yield_data["completion"] = frontier.completion.to_dict()

        workflow_rows: list[dict[str, Any]] = []
        for item in workflows:
            pin = project_workflow_pack_binding(events, item.run.workflow_run_id)
            workflow_rows.append(
                {
                    "workflow_run_id": item.run.workflow_run_id,
                    "state": item.state.value,
                    "binding": item.run.binding.to_dict(),
                    "pack": pin.pack.to_dict() if pin is not None else None,
                }
            )

        return {
            "work": {
                "work_id": snapshot.work.work_id,
                "work_digest": snapshot.work.work_digest,
                "objective": snapshot.work.objective,
                "state": snapshot.state.value,
                "revision": snapshot.revision,
                "terminal_event_id": snapshot.terminal_event_id,
                "ingress": snapshot.work.ingress.to_dict(),
            },
            "workflow": workflow_rows,
            "yield": yield_data,
            "unresolved": {
                "roles": [
                    {
                        "role_run_id": item.run.role_run_id,
                        "state": item.state.value,
                        "role_id": item.run.binding.role_id,
                        "version": item.run.binding.version,
                    }
                    for item in unresolved_roles[:MAX_STATUS_ITEMS]
                ],
                "roles_total": len(unresolved_roles),
                "capabilities": [
                    {
                        "need_id": item.need.need_id,
                        "state": item.state.value,
                        "capability_id": item.need.binding.capability_id,
                        "version": item.need.binding.version,
                    }
                    for item in unresolved_capabilities[:MAX_STATUS_ITEMS]
                ],
                "capabilities_total": len(unresolved_capabilities),
                "children": child_rows,
                "children_total": len(delegations),
                "children_live_total": live_children,
            },
            "attention_response_count": len(responses),
            "pack_pin_count": len(pins),
            "event_count": len(events),
            "latest_event": events[-1].to_dict() if events else None,
        }

    def _recover_head_answer_retry(
        self,
        events: tuple[WorkEvent, ...],
        *,
        response_text: str,
        source_id: str | None,
    ) -> AttentionResponse | None:
        if (
            not events
            or events[-1].kind != ATTENTION_RESPONSE_RECORDED_EVENT
        ):
            return None

        response = project_attention_response(
            events,
            events[-1].event_id,
        )
        if (
            response.source_namespace
            != STANDALONE_ANSWER_SOURCE_NAMESPACE
            or response.response_text != response_text
        ):
            return None

        expected_source_id = source_id or (
            f"attention:{response.attention_id}:"
            f"{_canonical_digest({'response_text': response_text})}"
        )
        if response.source_id != expected_source_id:
            return None
        if response.source_payload_digest != _canonical_digest(
            {"response_text": response_text}
        ):
            raise StandaloneWorkSurfaceBindingError(
                "durable answer payload digest changed exact response evidence"
            )
        return response

    def _resolve_answer_target(
        self,
        events: tuple[WorkEvent, ...],
        *,
        frontier: DurableWorkYield,
        attention_id: str | None,
    ) -> AttentionNeed:
        if attention_id is None:
            if (
                frontier.kind is not DurableWorkYieldKind.ATTENTION
                or frontier.attention is None
            ):
                raise StandaloneWorkSurfaceStateError(
                    "Work has no current AttentionNeed to answer"
                )
            return frontier.attention

        if type(attention_id) is not str or not attention_id:
            raise ValueError("attention_id must be non-empty text")
        return project_attention_need(events, attention_id)

    @staticmethod
    def _progression_dict(
        result: BoundedExistingWorkProgressionResult,
    ) -> dict[str, Any]:
        return {
            "status": result.status.value,
            "steps_used": result.steps_used,
            "yield": {
                "kind": result.frontier.kind.value,
                "attention": (
                    result.frontier.attention.to_dict()
                    if result.frontier.attention is not None
                    else None
                ),
                "completion": (
                    result.frontier.completion.to_dict()
                    if result.frontier.completion is not None
                    else None
                ),
            },
        }
