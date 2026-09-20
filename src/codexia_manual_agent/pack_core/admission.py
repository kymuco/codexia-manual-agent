from __future__ import annotations

import hmac

from codexia_manual_agent.pack_core.models import PackWorkflowBinding
from codexia_manual_agent.pack_core.projection import (
    PackProjectionError,
    project_workflow_pack_binding,
)
from codexia_manual_agent.work_core import WorkConcurrencyError, WorkStore
from codexia_manual_agent.workflow_core import (
    WorkflowRunState,
    project_workflow_run,
)


class PackAdmissionError(RuntimeError):
    """Base failure for the G2.7 semantic Pack admission boundary."""


class PackBindingError(PackAdmissionError):
    """Pack pin changed exact Work/Workflow/Pack semantics."""


class PackWorkflowStateError(PackAdmissionError):
    """Pack pin is invalid for the recovered WorkflowRun state."""


class PackAdmission:
    """Admit one exact semantic Pack pin immediately after workflow.started."""

    def __init__(self, store: WorkStore) -> None:
        self._store = store

    def admit_workflow_binding(
        self,
        binding: PackWorkflowBinding,
    ) -> PackWorkflowBinding:
        if not isinstance(binding, PackWorkflowBinding):
            raise TypeError("binding must be PackWorkflowBinding")

        events = self._store.events(binding.work_id)

        # Exact retry remains idempotent after an ambiguous caller ACK.
        if any(event.event_id == binding.binding_id for event in events):
            self._store.append(
                binding.work_id,
                expected_revision=binding.start_revision,
                event=binding.to_event(),
            )
            recovered = project_workflow_pack_binding(
                self._store.events(binding.work_id),
                binding.workflow_run_id,
            )
            if recovered != binding:
                raise PackBindingError(
                    "Pack binding identity reused for different exact binding"
                )
            return recovered

        existing = project_workflow_pack_binding(
            events,
            binding.workflow_run_id,
        )
        if existing is not None:
            raise PackBindingError(
                "WorkflowRun is already pinned to a Pack"
            )

        workflow = project_workflow_run(
            events,
            binding.workflow_run_id,
        )
        if workflow.state is not WorkflowRunState.ACTIVE:
            raise PackWorkflowStateError(
                "Pack cannot bind a terminal WorkflowRun"
            )
        if not hmac.compare_digest(
            workflow.run.run_digest,
            binding.workflow_run_digest,
        ):
            raise PackBindingError(
                "PackWorkflowBinding changed WorkflowRun binding"
            )
        if workflow.run.work_id != binding.work_id:
            raise PackBindingError(
                "PackWorkflowBinding crossed Work identity"
            )
        if not hmac.compare_digest(
            workflow.run.work_digest,
            binding.work_digest,
        ):
            raise PackBindingError(
                "PackWorkflowBinding changed Work binding"
            )

        current = self._store.snapshot(binding.work_id)
        if current.revision != binding.start_revision:
            raise WorkConcurrencyError(
                f"Stale Work revision: expected={binding.start_revision} "
                f"actual={current.revision}"
            )
        if current.last_event_digest != binding.start_event_digest:
            raise WorkConcurrencyError(
                "PackWorkflowBinding does not bind exact current chronology"
            )

        self._store.append(
            binding.work_id,
            expected_revision=binding.start_revision,
            event=binding.to_event(),
        )
        recovered = project_workflow_pack_binding(
            self._store.events(binding.work_id),
            binding.workflow_run_id,
        )
        if recovered is None:  # pragma: no cover - defensive
            raise PackProjectionError(
                "Admitted PackWorkflowBinding did not recover"
            )
        return recovered
