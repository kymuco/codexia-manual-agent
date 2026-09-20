from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from codexia_manual_agent.capability_core import CapabilityBinding
from codexia_manual_agent.pack_core import (
    InvalidPackRecord,
    PackBinding,
    PackMemberBinding,
    PackMemberKind,
)
from codexia_manual_agent.role_core import RoleBinding
from codexia_manual_agent.workflow_core import WorkflowBinding

PACK_DISTRIBUTION_SCHEMA_VERSION = 1


class InvariantPackDistributionError(RuntimeError):
    """Invariant provider returned invalid Codexia Pack distribution data."""


class ManagedPluginServicePort(Protocol):
    """Minimal public PluginService shape required by the bridge."""

    def get(self, plugin_id: str) -> Any: ...


@dataclass(frozen=True, slots=True)
class ResolvedPackDistribution:
    """Validated semantic material from one exact technical plugin build."""

    provider_ref: str
    pack: PackBinding
    workflows: tuple[WorkflowBinding, ...]
    roles: tuple[RoleBinding, ...]
    capabilities: tuple[CapabilityBinding, ...]

    def __post_init__(self) -> None:
        _validate_exact_provider_ref(self.provider_ref)
        if not isinstance(self.pack, PackBinding):
            raise TypeError("pack must be PackBinding")
        if any(not isinstance(item, WorkflowBinding) for item in self.workflows):
            raise TypeError("workflows must contain WorkflowBinding values")
        if any(not isinstance(item, RoleBinding) for item in self.roles):
            raise TypeError("roles must contain RoleBinding values")
        if any(
            not isinstance(item, CapabilityBinding)
            for item in self.capabilities
        ):
            raise TypeError(
                "capabilities must contain CapabilityBinding values"
            )
        _validate_distribution_membership(
            pack=self.pack,
            workflows=self.workflows,
            roles=self.roles,
            capabilities=self.capabilities,
        )


def _validate_exact_provider_ref(value: Any) -> str:
    if not isinstance(value, str):
        raise InvariantPackDistributionError(
            "Invariant plugin reference must be text"
        )
    normalized = value.strip()
    if normalized != value or normalized.count("@") != 1:
        raise InvariantPackDistributionError(
            "Invariant plugin reference must pin one exact version"
        )
    plugin_id, version = normalized.split("@", 1)
    if (
        not plugin_id
        or not version
        or any(char.isspace() for char in normalized)
        or version.lower() == "latest"
    ):
        raise InvariantPackDistributionError(
            "Invariant plugin reference must pin one exact non-latest version"
        )
    return normalized


def _exact_mapping(
    value: Any,
    *,
    keys: set[str],
    record_name: str,
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != keys:
        raise InvariantPackDistributionError(
            f"{record_name} keys are not exact"
        )
    return value


def _parse_list(
    value: Any,
    *,
    record_name: str,
    parser,
) -> tuple[Any, ...]:
    if not isinstance(value, list):
        raise InvariantPackDistributionError(
            f"{record_name} must be a list"
        )
    try:
        return tuple(parser(item) for item in value)
    except (KeyError, TypeError, ValueError) as exc:
        raise InvariantPackDistributionError(
            f"{record_name} contains invalid semantic binding"
        ) from exc


def _workflow_member(binding: WorkflowBinding) -> PackMemberBinding:
    return PackMemberBinding.create(
        kind=PackMemberKind.WORKFLOW,
        semantic_id=binding.workflow_id,
        version=binding.version,
        binding_digest=binding.binding_digest,
    )


def _role_member(binding: RoleBinding) -> PackMemberBinding:
    return PackMemberBinding.create(
        kind=PackMemberKind.ROLE,
        semantic_id=binding.role_id,
        version=binding.version,
        binding_digest=binding.binding_digest,
    )


def _capability_member(binding: CapabilityBinding) -> PackMemberBinding:
    return PackMemberBinding.create(
        kind=PackMemberKind.CAPABILITY,
        semantic_id=binding.capability_id,
        version=binding.version,
        binding_digest=binding.binding_digest,
    )


def _validate_distribution_membership(
    *,
    pack: PackBinding,
    workflows: tuple[WorkflowBinding, ...],
    roles: tuple[RoleBinding, ...],
    capabilities: tuple[CapabilityBinding, ...],
) -> None:
    members = tuple(
        sorted(
            [
                *[_workflow_member(item) for item in workflows],
                *[_role_member(item) for item in roles],
                *[_capability_member(item) for item in capabilities],
            ],
            key=lambda item: item.sort_key,
        )
    )
    if members != pack.members:
        raise InvariantPackDistributionError(
            "semantic definitions do not exactly match PackBinding members"
        )


class InvariantPackDistributionBridge:
    """Resolve Codexia semantics through Invariant's managed plugin facade.

    The bridge deliberately knows only the public service.get(exact_ref) shape.
    It does not access Invariant registry, loader, resolver, capabilities,
    policy, execution contexts, or lifecycle internals.
    """

    def __init__(self, service: ManagedPluginServicePort) -> None:
        self._service = service

    def resolve(self, provider_ref: str) -> ResolvedPackDistribution:
        provider_ref = _validate_exact_provider_ref(provider_ref)
        plugin = self._service.get(provider_ref)
        export = getattr(plugin, "codexia_pack_distribution", None)
        if not callable(export):
            raise InvariantPackDistributionError(
                "Invariant plugin does not expose codexia_pack_distribution()"
            )

        raw = export()
        record = _exact_mapping(
            raw,
            keys={
                "schema_version",
                "pack",
                "workflows",
                "roles",
                "capabilities",
            },
            record_name="Codexia Pack distribution",
        )
        if record["schema_version"] != PACK_DISTRIBUTION_SCHEMA_VERSION:
            raise InvariantPackDistributionError(
                "Unsupported Codexia Pack distribution schema"
            )

        try:
            pack = PackBinding.from_dict(record["pack"])
        except (InvalidPackRecord, KeyError, TypeError, ValueError) as exc:
            raise InvariantPackDistributionError(
                "Pack distribution contains invalid PackBinding"
            ) from exc

        workflows = _parse_list(
            record["workflows"],
            record_name="workflows",
            parser=WorkflowBinding.from_dict,
        )
        roles = _parse_list(
            record["roles"],
            record_name="roles",
            parser=RoleBinding.from_dict,
        )
        capabilities = _parse_list(
            record["capabilities"],
            record_name="capabilities",
            parser=CapabilityBinding.from_dict,
        )

        return ResolvedPackDistribution(
            provider_ref=provider_ref,
            pack=pack,
            workflows=workflows,
            roles=roles,
            capabilities=capabilities,
        )
