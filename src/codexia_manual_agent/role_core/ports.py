from __future__ import annotations

from typing import Protocol

from codexia_manual_agent.role_core.models import CognitionOutcome, CognitionRequest


class CognitionPort(Protocol):
    """Transport boundary for one bounded cognition operation.

    The port receives only the exact CognitionRequest. It is not given WorkStore,
    workflow/role admission objects, workspace handles, tools, capabilities,
    permissions, or ambient authority.
    """

    @property
    def port_id(self) -> str: ...

    def complete(self, request: CognitionRequest) -> CognitionOutcome: ...
