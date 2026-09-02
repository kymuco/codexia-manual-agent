from __future__ import annotations

from typing import Iterable

from codexia_manual_agent.admission.command_families import normalize_command
from codexia_manual_agent.admission.models import (
    CommandAdmission,
    CommandAdmissionVerdict,
    ModelProcessRequest,
)
from codexia_manual_agent.domain.capabilities import Capability


_DEFAULT_CAPABILITIES = frozenset({Capability.EXECUTE_PROCESS})


class CommandAdmissionPolicy:
    """Pure local command-family admission below capability-level authority."""

    def __init__(
        self,
        *,
        available_capabilities: Iterable[Capability] = _DEFAULT_CAPABILITIES,
    ) -> None:
        self._available_capabilities = frozenset(
            Capability(item) for item in available_capabilities
        )

    @property
    def available_capabilities(self) -> frozenset[Capability]:
        return self._available_capabilities

    def evaluate(self, request: ModelProcessRequest) -> CommandAdmission:
        command = normalize_command(request)
        envelope = command.envelope

        if not envelope.bounded:
            return CommandAdmission(
                request=request,
                command=command,
                verdict=CommandAdmissionVerdict.REJECT_UNBOUNDED_CHILD_CODE,
                reason=(
                    "Repository-controlled child code prevents proof of a bounded "
                    "downstream capability envelope."
                ),
            )

        missing = tuple(
            capability
            for capability in envelope.required_capabilities
            if capability not in self._available_capabilities
        )
        if missing:
            names = ", ".join(item.value for item in missing)
            return CommandAdmission(
                request=request,
                command=command,
                verdict=CommandAdmissionVerdict.REJECT_CAPABILITY_ENVELOPE,
                reason=f"Command requires unavailable capabilities: {names}",
            )

        return CommandAdmission(
            request=request,
            command=command,
            verdict=CommandAdmissionVerdict.ADMIT_REQUIRES_HUMAN,
            reason=(
                "The locally fixed argv has a bounded capability envelope; explicit "
                "local human authorization is still required before execution."
            ),
        )
