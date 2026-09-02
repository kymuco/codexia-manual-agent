from __future__ import annotations

from threading import Lock
from typing import Protocol

from codexia_manual_agent.domain.errors import AuthorizationConsumedError


class AuthorizationConsumptionRegistryProtocol(Protocol):
    """Minimal replay-registry contract consumed by LocalApprovalAuthority."""

    def consume(
        self,
        receipt_id: str,
        *,
        receipt_digest: str | None = None,
        proposal_id: str | None = None,
        proposal_digest: str | None = None,
    ) -> None: ...

    def is_consumed(self, receipt_id: str) -> bool: ...


class AuthorizationConsumptionRegistry:
    """Process-local replay registry for single-use authorization receipts."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._consumed_receipts: set[str] = set()

    def consume(
        self,
        receipt_id: str,
        *,
        receipt_digest: str | None = None,
        proposal_id: str | None = None,
        proposal_digest: str | None = None,
    ) -> None:
        # Exact binding kwargs are intentionally accepted for API compatibility
        # with M3 durable registries. The legacy process-local registry has no
        # durable event ledger against which to validate them.
        del receipt_digest, proposal_id, proposal_digest
        if not isinstance(receipt_id, str) or not receipt_id.strip():
            raise ValueError("receipt_id must be a non-empty string")
        with self._lock:
            if receipt_id in self._consumed_receipts:
                raise AuthorizationConsumedError(
                    f"Authorization receipt already consumed: {receipt_id}"
                )
            self._consumed_receipts.add(receipt_id)

    def is_consumed(self, receipt_id: str) -> bool:
        if not isinstance(receipt_id, str) or not receipt_id.strip():
            raise ValueError("receipt_id must be a non-empty string")
        with self._lock:
            return receipt_id in self._consumed_receipts


_PROCESS_AUTHORIZATION_CONSUMPTION_REGISTRY = AuthorizationConsumptionRegistry()


def process_authorization_consumption_registry() -> AuthorizationConsumptionRegistry:
    return _PROCESS_AUTHORIZATION_CONSUMPTION_REGISTRY
