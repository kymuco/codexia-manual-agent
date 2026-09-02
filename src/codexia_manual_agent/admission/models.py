from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Mapping

from codexia_manual_agent.domain.capabilities import Capability


class CommandFamily(StrEnum):
    PYTHON_VERSION = "python_version"
    GIT_VERSION = "git_version"
    PYTHON_COMPILEALL = "python_compileall"
    PYTHON_UNITTEST_DISCOVER = "python_unittest_discover"


class CommandRisk(StrEnum):
    DIAGNOSTIC = "diagnostic"
    WORKSPACE_MUTATION = "workspace_mutation"
    UNBOUNDED_CHILD_CODE = "unbounded_child_code"


class CommandAdmissionVerdict(StrEnum):
    ADMIT_REQUIRES_HUMAN = "admit_requires_human"
    REJECT_CAPABILITY_ENVELOPE = "reject_capability_envelope"
    REJECT_UNBOUNDED_CHILD_CODE = "reject_unbounded_child_code"


@dataclass(frozen=True, slots=True)
class ModelProcessRequest:
    request_id: str
    family: CommandFamily
    arguments: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.request_id, str) or not self.request_id:
            raise ValueError("request_id must be a non-empty string")
        object.__setattr__(self, "family", CommandFamily(self.family))
        if not isinstance(self.arguments, Mapping):
            raise TypeError("arguments must be a mapping")
        object.__setattr__(self, "arguments", MappingProxyType(dict(self.arguments)))


@dataclass(frozen=True, slots=True)
class CapabilityEnvelope:
    required_capabilities: tuple[Capability, ...]
    bounded: bool
    rationale: str

    def __post_init__(self) -> None:
        normalized = tuple(Capability(item) for item in self.required_capabilities)
        if len(set(normalized)) != len(normalized):
            raise ValueError("Capability envelope cannot contain duplicates")
        if type(self.bounded) is not bool:
            raise TypeError("bounded must be boolean")
        if not isinstance(self.rationale, str) or not self.rationale.strip():
            raise ValueError("rationale must be non-empty")
        object.__setattr__(self, "required_capabilities", normalized)

    def to_dict(self) -> dict[str, Any]:
        return {
            "required_capabilities": [item.value for item in self.required_capabilities],
            "bounded": self.bounded,
            "rationale": self.rationale,
        }


@dataclass(frozen=True, slots=True)
class NormalizedCommand:
    family: CommandFamily
    argv: tuple[str, ...]
    cwd: str
    risk: CommandRisk
    envelope: CapabilityEnvelope

    def __post_init__(self) -> None:
        object.__setattr__(self, "family", CommandFamily(self.family))
        object.__setattr__(self, "risk", CommandRisk(self.risk))
        if not self.argv or any(type(item) is not str or not item for item in self.argv):
            raise ValueError("argv must contain non-empty strings")
        if not isinstance(self.cwd, str) or not self.cwd:
            raise ValueError("cwd must be non-empty")


@dataclass(frozen=True, slots=True)
class CommandAdmission:
    request: ModelProcessRequest
    command: NormalizedCommand
    verdict: CommandAdmissionVerdict
    reason: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "verdict", CommandAdmissionVerdict(self.verdict))
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ValueError("reason must be non-empty")

    @property
    def admitted(self) -> bool:
        return self.verdict is CommandAdmissionVerdict.ADMIT_REQUIRES_HUMAN


@dataclass(frozen=True, slots=True)
class ProcessApprovalPreview:
    request_id: str
    family: CommandFamily
    verdict: CommandAdmissionVerdict
    risk: CommandRisk
    argv: tuple[str, ...]
    cwd: str
    required_capabilities: tuple[Capability, ...]
    bounded: bool
    requires_human: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "family": self.family.value,
            "verdict": self.verdict.value,
            "risk": self.risk.value,
            "argv": list(self.argv),
            "cwd": self.cwd,
            "required_capabilities": [item.value for item in self.required_capabilities],
            "bounded": self.bounded,
            "requires_human": self.requires_human,
            "reason": self.reason,
        }
