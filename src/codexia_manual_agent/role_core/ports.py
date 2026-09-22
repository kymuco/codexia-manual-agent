from __future__ import annotations

from typing import Protocol

from codexia_manual_agent.role_core.models import CognitionOutcome
from codexia_manual_agent.role_core.transport_models import CognitionPortRequest


class CognitionPort(Protocol):
    """Transport boundary for one bounded cognition operation.

    The port receives the exact admitted CognitionRequest plus its durable
    routing handoff. It is not given WorkStore, admission objects, workspace
    handles, tools, capabilities, permissions, or ambient authority.
    """

    @property
    def port_id(self) -> str: ...

    def complete(
        self,
        request: CognitionPortRequest,
    ) -> CognitionOutcome | None: ...
