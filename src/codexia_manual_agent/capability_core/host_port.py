from __future__ import annotations

from typing import Protocol

from codexia_manual_agent.capability_core.host_models import CapabilityHostRequest
from codexia_manual_agent.capability_core.models import CapabilityOutcome


class CapabilityHostPort(Protocol):
    """Host-neutral boundary for one already-admitted CapabilityHandoff.

    The port may represent a standalone host, HDE/IRR bridge, remote service, or
    another product assembly. Codexia Core does not know how authority is decided
    or how effects are executed behind this boundary.

    Returning None means only that no terminal CapabilityOutcome is available
    synchronously. It does not prove host receipt, authorization, attempt start,
    or effect occurrence.
    """

    @property
    def host_id(self) -> str: ...

    def submit(self, request: CapabilityHostRequest) -> CapabilityOutcome | None: ...
