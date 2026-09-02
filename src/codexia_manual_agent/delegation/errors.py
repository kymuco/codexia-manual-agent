from __future__ import annotations

from codexia_manual_agent.domain.errors import CodexiaError


class DelegationError(CodexiaError):
    """Base class for M2.6 bounded orchestration failures."""


class InvalidDelegationError(DelegationError):
    """A delegation envelope or escalation record violates the M2.6 contract."""


class DelegationAuthorityError(DelegationError):
    """Delegated work attempted to acquire or use authority it does not carry."""


class DelegationBudgetError(DelegationError):
    """Delegated work attempted to reserve or consume more than its remaining budget."""


class DelegationStateError(DelegationError):
    """An orchestration transition is invalid for the current delegation state."""


class DelegationReplayError(DelegationError):
    """A model control request id was replayed or rebound to a different payload."""


class EscalationBindingError(DelegationError):
    """A continuation decision is not bound to the exact pending escalation."""


class DelegationPersistenceError(DelegationError):
    """The M3.2 durable delegation store cannot safely complete an operation."""


class DelegationPersistenceIntegrityError(DelegationPersistenceError):
    """Persisted M3.2 delegation chronology or derived indexes are inconsistent."""
