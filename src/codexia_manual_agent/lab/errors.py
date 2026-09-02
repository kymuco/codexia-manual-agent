from __future__ import annotations

from codexia_manual_agent.domain.errors import CodexiaError


class LabError(CodexiaError):
    """Base class for M4 computational-lab failures."""


class InvalidLabRecordError(LabError):
    """A hypothesis, experiment, evidence, or conclusion violates an M4 contract."""


class EvidenceBindingError(LabError):
    """Evidence or a conclusion is not bound to the exact declared experiment lineage."""


class LabIdentityConflictError(LabError):
    """A durable lab identity is duplicated, rebound, or otherwise ambiguous."""


class LabRegistryStateError(LabError):
    """A durable registry transition is invalid for the current experiment/run state."""


class LabPersistenceError(LabError):
    """A durable M4 lab-registry operation could not be committed or recovered."""


class LabPersistenceIntegrityError(LabPersistenceError):
    """Persisted M4 lab-registry state failed integrity or semantic replay checks."""
