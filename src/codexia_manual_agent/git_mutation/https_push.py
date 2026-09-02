from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from codexia_manual_agent.authority import ActionLifecycle, ActionProposal, LocalApprovalAuthority
from codexia_manual_agent.domain.capabilities import Capability
from codexia_manual_agent.domain.errors import (
    GitMutationExecutionError,
    GitMutationPreconditionChangedError,
    GitRepositoryBoundaryError,
    InvalidGitMutationError,
)
from codexia_manual_agent.git_mutation.https_transport import (
    HttpsTransportBinding,
    bind_https_transport,
    close_https_transport,
    https_git_config_args,
    https_git_environment,
    materialize_https_credentials,
    revalidate_https_transport,
)
from codexia_manual_agent.git_mutation.models import (
    GIT_MUTATION_SCHEMA_VERSION,
    GIT_PUSH_ACTION,
    GitMutationOutcome,
)
from codexia_manual_agent.git_mutation.network_intent import (
    GitNetworkPushIntent,
    build_network_push_intent,
)
from codexia_manual_agent.git_mutation.network_transport import GitNetworkTransport
from codexia_manual_agent.git_mutation.repository import (
    GIT_PUSH_TIMEOUT_SECONDS,
    MAX_GIT_CONFIG_BYTES,
    parse_git_identity,
    read_local_config_identity,
    require_sha256,
    run_git,
    snapshot_repository,
    validate_head_ref,
    validate_oid,
    validate_remote_name,
)
from codexia_manual_agent.git_mutation.windows_namespace import WindowsGitNamespacePin


GIT_HTTPS_NETWORK_PUSH_BACKEND = "network-https-bound-git-default.v1"


@dataclass(frozen=True, slots=True)
class HttpsNetworkGitPushApprovalPreview:
    local_oid: str
    local_ref: str
    remote_name: str
    remote_url: str
    review_destination: str
    route_address: str
    route_family: str
    destination_ref: str
    tracking_ref: str
    expected_remote_oid: str
    tls_backend: str
    ca_bundle_path: str
    ca_bundle_sha256: str
    credential_source_path: str
    credential_source_size_bytes: int
    credential_username: str
    credential_shell_path: str
    credential_shell_sha256: str
    credential_mode: str
    backend: str = GIT_HTTPS_NETWORK_PUSH_BACKEND
    requires_human: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": GIT_PUSH_ACTION,
            "local_oid": self.local_oid,
            "local_ref": self.local_ref,
            "remote_name": self.remote_name,
            "remote_url": self.remote_url,
            "review_destination": self.review_destination,
            "route_address": self.route_address,
            "route_family": self.route_family,
            "destination_ref": self.destination_ref,
            "tracking_ref": self.tracking_ref,
            "expected_remote_oid": self.expected_remote_oid,
            "tls_backend": self.tls_backend,
            "ca_bundle_path": self.ca_bundle_path,
            "ca_bundle_sha256": self.ca_bundle_sha256,
            "credential_source_path": self.credential_source_path,
            "credential_source_size_bytes": self.credential_source_size_bytes,
            "credential_username": self.credential_username,
            "credential_shell_path": self.credential_shell_path,
            "credential_shell_sha256": self.credential_shell_sha256,
            "credential_mode": self.credential_mode,
            "backend": self.backend,
            "requires_human": self.requires_human,
        }


@dataclass(frozen=True, slots=True)
class HttpsNetworkGitPushPreparation:
    proposal: ActionProposal
    approval_preview: HttpsNetworkGitPushApprovalPreview
    https_binding: HttpsTransportBinding

    def __post_init__(self) -> None:
        if not isinstance(self.proposal, ActionProposal):
            raise TypeError("proposal must be ActionProposal")
        if not isinstance(self.approval_preview, HttpsNetworkGitPushApprovalPreview):
            raise TypeError("approval_preview must be HttpsNetworkGitPushApprovalPreview")
        if not isinstance(self.https_binding, HttpsTransportBinding):
            raise TypeError("https_binding must be HttpsTransportBinding")


@dataclass(frozen=True, slots=True)
class HttpsNetworkGitPushObservation:
    execution_id: str
    proposal_id: str
    proposal_digest: str
    outcome: GitMutationOutcome
    local_oid: str
    remote_url: str
    review_destination: str
    route_address: str
    destination_ref: str
    expected_remote_oid: str
    observed_remote_oid: str | None
    remote_observation_complete: bool
    backend: str
    error: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": GIT_MUTATION_SCHEMA_VERSION,
            "execution_id": self.execution_id,
            "proposal_id": self.proposal_id,
            "proposal_digest": self.proposal_digest,
            "outcome": self.outcome.value,
            "local_oid": self.local_oid,
            "remote_url": self.remote_url,
            "review_destination": self.review_destination,
            "route_address": self.route_address,
            "destination_ref": self.destination_ref,
            "expected_remote_oid": self.expected_remote_oid,
            "observed_remote_oid": self.observed_remote_oid,
            "remote_observation_complete": self.remote_observation_complete,
            "backend": self.backend,
            "error": self.error,
        }


def _transport_manifest(binding: HttpsTransportBinding) -> dict[str, Any]:
    return {
        "backend": GIT_HTTPS_NETWORK_PUSH_BACKEND,
        "endpoint": binding.endpoint.to_dict(),
        "route": binding.route.to_dict(),
        "git_shell": binding.git_shell.to_dict(),
        "git_remote_https": binding.git_remote_https.to_dict(),
        "git_remote_https_resolved_target": binding.git_remote_https_resolved_target,
        "credential_source": binding.credential_source.to_public_dict(),
        "credential_helper_command_sha256": binding.credential_helper_command_sha256,
        "ca_bundle": binding.ca_bundle.to_dict(),
        "tls_backend": binding.tls_backend,
        "credential_mode": binding.credential_mode,
        "route_mode": binding.route_mode,
    }


def _preview(
    intent: GitNetworkPushIntent,
    binding: HttpsTransportBinding,
) -> HttpsNetworkGitPushApprovalPreview:
    return HttpsNetworkGitPushApprovalPreview(
        local_oid=intent.local_oid,
        local_ref=intent.local_ref,
        remote_name=intent.remote_name,
        remote_url=intent.endpoint.original_url,
        review_destination=intent.endpoint.review_destination,
        route_address=binding.route.address,
        route_family=binding.route.family,
        destination_ref=intent.destination_ref,
        tracking_ref=intent.tracking_ref,
        expected_remote_oid=intent.expected_remote_oid,
        tls_backend=binding.tls_backend,
        ca_bundle_path=binding.ca_bundle.path,
        ca_bundle_sha256=binding.ca_bundle.sha256,
        credential_source_path=binding.credential_source.path,
        credential_source_size_bytes=binding.credential_source.size_bytes,
        credential_username=binding.credential_source.username,
        credential_shell_path=binding.git_shell.path,
        credential_shell_sha256=binding.git_shell.sha256,
        credential_mode=binding.credential_mode,
    )


def prepare_https_network_git_push_proposal(
    *,
    workspace: str | Path,
    remote: str,
    destination_ref: str,
    credential_file: str | Path,
    ca_bundle_file: str | Path,
) -> HttpsNetworkGitPushPreparation:
    remote = validate_remote_name(remote)
    destination_ref = validate_head_ref(destination_ref)
    snapshot = snapshot_repository(workspace)
    intent = build_network_push_intent(
        snapshot,
        remote=remote,
        destination_ref=destination_ref,
    )
    if intent.endpoint.transport is not GitNetworkTransport.HTTPS:
        raise InvalidGitMutationError("HTTPS network push requires an https:// remote")
    binding: HttpsTransportBinding | None = None
    try:
        binding = bind_https_transport(
            snapshot,
            intent.endpoint,
            remote_name=remote,
            credential_file=credential_file,
            ca_bundle_file=ca_bundle_file,
        )
        local_config_size, local_config_sha256 = read_local_config_identity(snapshot)
        params = {
            "schema_version": GIT_MUTATION_SCHEMA_VERSION,
            "backend": GIT_HTTPS_NETWORK_PUSH_BACKEND,
            "local_oid": intent.local_oid,
            "local_ref": intent.local_ref,
            "object_format": snapshot.object_format,
            "remote_name": remote,
            "remote_url": intent.endpoint.original_url,
            "destination_ref": destination_ref,
            "tracking_ref": intent.tracking_ref,
            "expected_remote_oid": intent.expected_remote_oid,
            "local_config_size_bytes": local_config_size,
            "local_config_sha256": local_config_sha256,
            "intent": intent.to_dict(),
            "transport": _transport_manifest(binding),
            "git_executable": snapshot.git.to_dict(),
        }
        proposal = ActionProposal.create(
            capability=Capability.GIT_PUSH,
            action=GIT_PUSH_ACTION,
            workspace_root=str(snapshot.workspace_root),
            parameters=params,
            summary=(
                f"Push exact commit {intent.local_oid} to {intent.endpoint.review_destination} "
                f"{destination_ref} only if remote still equals {intent.expected_remote_oid}; "
                f"HTTPS route {binding.route.address}, TLS {binding.tls_backend}, "
                f"CA {binding.ca_bundle.sha256}, frozen read-only credential source "
                f"{binding.credential_source.path}."
            ),
        )
        return HttpsNetworkGitPushPreparation(
            proposal=proposal,
            approval_preview=_preview(intent, binding),
            https_binding=binding,
        )
    except BaseException:
        if binding is not None:
            close_https_transport(binding)
        raise


def close_https_network_git_push_preparation(
    preparation: HttpsNetworkGitPushPreparation,
) -> None:
    if not isinstance(preparation, HttpsNetworkGitPushPreparation):
        raise TypeError("preparation must be HttpsNetworkGitPushPreparation")
    close_https_transport(preparation.https_binding)


def _params(proposal: ActionProposal) -> dict[str, Any]:
    if proposal.capability is not Capability.GIT_PUSH or proposal.action != GIT_PUSH_ACTION:
        raise InvalidGitMutationError("Action proposal is not an M2.5.1 HTTPS Git push")
    params = proposal.to_dict()["parameters"]
    expected = {
        "schema_version",
        "backend",
        "local_oid",
        "local_ref",
        "object_format",
        "remote_name",
        "remote_url",
        "destination_ref",
        "tracking_ref",
        "expected_remote_oid",
        "local_config_size_bytes",
        "local_config_sha256",
        "intent",
        "transport",
        "git_executable",
    }
    if set(params) != expected or params["schema_version"] != GIT_MUTATION_SCHEMA_VERSION:
        raise InvalidGitMutationError("HTTPS Git push proposal schema mismatch")
    if params["backend"] != GIT_HTTPS_NETWORK_PUSH_BACKEND:
        raise InvalidGitMutationError("Unsupported M2.5.1 HTTPS Git push backend")
    oid_len = (
        40
        if params["object_format"] == "sha1"
        else 64
        if params["object_format"] == "sha256"
        else 0
    )
    if not oid_len:
        raise InvalidGitMutationError("Unsupported HTTPS Git object format")
    validate_oid(params["local_oid"], oid_len, "HTTPS Git local object")
    validate_oid(params["expected_remote_oid"], oid_len, "HTTPS Git expected remote object")
    validate_head_ref(params["local_ref"])
    validate_head_ref(params["destination_ref"])
    validate_remote_name(params["remote_name"])
    if (
        type(params["local_config_size_bytes"]) is not int
        or not 1 <= params["local_config_size_bytes"] <= MAX_GIT_CONFIG_BYTES
    ):
        raise InvalidGitMutationError("HTTPS Git local config size is invalid")
    require_sha256(params["local_config_sha256"], "HTTPS Git local config digest")
    if not isinstance(params["intent"], dict) or not isinstance(params["transport"], dict):
        raise InvalidGitMutationError("HTTPS Git bound intent/transport schema mismatch")
    params["git_executable"] = parse_git_identity(params["git_executable"])
    return params


def _revalidate(
    preparation: HttpsNetworkGitPushPreparation,
    params: dict[str, Any],
):
    snapshot = snapshot_repository(
        preparation.proposal.workspace_root,
        git=params["git_executable"],
    )
    if str(snapshot.workspace_root) != preparation.proposal.workspace_root:
        raise GitRepositoryBoundaryError("Git workspace canonical identity changed")
    if (
        snapshot.object_format != params["object_format"]
        or snapshot.head_oid != params["local_oid"]
        or snapshot.head_ref != params["local_ref"]
    ):
        raise GitMutationPreconditionChangedError(
            "Git local ref/object changed before HTTPS push authorization consumption"
        )
    config_size, config_sha256 = read_local_config_identity(snapshot)
    if (
        config_size != params["local_config_size_bytes"]
        or config_sha256 != params["local_config_sha256"]
    ):
        raise GitMutationPreconditionChangedError(
            "Local Git config changed before HTTPS push authorization consumption"
        )
    intent = build_network_push_intent(
        snapshot,
        remote=params["remote_name"],
        destination_ref=params["destination_ref"],
    )
    if intent.to_dict() != params["intent"]:
        raise GitMutationPreconditionChangedError(
            "HTTPS Git push intent changed before authorization consumption"
        )
    if (
        intent.local_oid != params["local_oid"]
        or intent.expected_remote_oid != params["expected_remote_oid"]
        or intent.tracking_ref != params["tracking_ref"]
        or intent.endpoint.original_url != params["remote_url"]
        or intent.endpoint.transport is not GitNetworkTransport.HTTPS
    ):
        raise GitMutationPreconditionChangedError(
            "HTTPS Git push bound ref/URL state changed before authorization consumption"
        )
    revalidate_https_transport(preparation.https_binding, require_materialized=False)
    if _transport_manifest(preparation.https_binding) != params["transport"]:
        raise GitMutationPreconditionChangedError(
            "HTTPS transport identity changed before authorization consumption"
        )
    canonical_preview = _preview(intent, preparation.https_binding)
    if canonical_preview.to_dict() != preparation.approval_preview.to_dict():
        raise InvalidGitMutationError(
            "Displayed HTTPS Git push preview does not match the exact proposal state"
        )
    return snapshot, intent


def _observe_remote(
    snapshot,
    intent: GitNetworkPushIntent,
    binding: HttpsTransportBinding,
) -> tuple[bool, str | None]:
    try:
        result = run_git(
            snapshot.git,
            snapshot.workspace_root,
            [
                *https_git_config_args(binding),
                "ls-remote",
                "--refs",
                "--",
                intent.endpoint.original_url,
                intent.destination_ref,
            ],
            env=https_git_environment(snapshot, binding),
            timeout=GIT_PUSH_TIMEOUT_SECONDS,
            check=False,
        )
    except Exception:
        return False, None
    if result.returncode != 0:
        return False, None
    lines = [line for line in result.stdout.splitlines() if line]
    if not lines:
        return True, None
    if len(lines) != 1:
        return False, None
    try:
        oid_raw, ref_raw = lines[0].split(b"\t", 1)
        oid = oid_raw.decode("ascii")
        ref = ref_raw.decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return False, None
    if ref != intent.destination_ref:
        return False, None
    try:
        return True, validate_oid(oid, snapshot.oid_length, "Observed HTTPS Git object")
    except InvalidGitMutationError:
        return False, None


def _critical_transport_files(
    snapshot,
    binding: HttpsTransportBinding,
) -> tuple[Path, ...]:
    return (
        Path(snapshot.git.path),
        Path(binding.git_shell.path),
        Path(binding.git_remote_https.path),
        Path(binding.git_remote_https_resolved_target),
        Path(binding.credential_source.path),
        Path(binding.ca_bundle.path),
    )


def _acquire_transport_pins(
    snapshot,
    binding: HttpsTransportBinding,
) -> list[WindowsGitNamespacePin]:
    pins: list[WindowsGitNamespacePin] = []
    try:
        pins.append(
            WindowsGitNamespacePin.acquire(
                (snapshot.git_dir,),
                locked_files=(
                    snapshot.git_dir / "config",
                    *_critical_transport_files(snapshot, binding),
                ),
            )
        )
        pins.append(
            WindowsGitNamespacePin.acquire(
                (Path(binding.credential_bundle_root),),
            )
        )
        return pins
    except BaseException:
        for pin in reversed(pins):
            try:
                pin.close()
            except BaseException:
                pass
        raise


def _append_error(existing: str | None, extra: str | None) -> str | None:
    if not extra:
        return existing
    return extra if not existing else f"{existing}; {extra}"


def execute_https_network_git_push(
    preparation: HttpsNetworkGitPushPreparation,
    *,
    lifecycle: ActionLifecycle,
    authority: LocalApprovalAuthority,
) -> HttpsNetworkGitPushObservation:
    if not isinstance(preparation, HttpsNetworkGitPushPreparation):
        raise TypeError("preparation must be HttpsNetworkGitPushPreparation")
    if lifecycle.proposal != preparation.proposal:
        raise InvalidGitMutationError("Lifecycle is not bound to the HTTPS Git push proposal")
    params = _params(preparation.proposal)

    pins: list[WindowsGitNamespacePin] = []
    materialized_pin: WindowsGitNamespacePin | None = None
    execution_id: str | None = None
    error: str | None = None
    outcome = GitMutationOutcome.INCOMPLETE
    observed: str | None = None
    observation_complete = False
    try:
        pre_snapshot, _pre_intent = _revalidate(preparation, params)
        pins = _acquire_transport_pins(pre_snapshot, preparation.https_binding)
        snapshot, intent = _revalidate(preparation, params)

        # Freeze the exact derived credential response and its namespace before
        # consuming the one-shot receipt. Failure here is pre-consumption and opens
        # no network.
        materialize_https_credentials(preparation.https_binding)
        materialized_pin = WindowsGitNamespacePin.acquire(
            (Path(preparation.https_binding.credential_bundle_root),),
            locked_files=(Path(preparation.https_binding.credential_bundle_path),),
        )
        revalidate_https_transport(
            preparation.https_binding,
            require_materialized=True,
        )

        lifecycle.consume_authorization(authority=authority)
        execution_id = lifecycle.record_executed()
        try:
            config_args = https_git_config_args(preparation.https_binding)
            env = https_git_environment(snapshot, preparation.https_binding)
            push = run_git(
                snapshot.git,
                snapshot.workspace_root,
                [
                    *config_args,
                    "-c",
                    "push.negotiate=false",
                    "push",
                    "--porcelain",
                    "--no-verify",
                    "--no-signed",
                    "--no-follow-tags",
                    "--no-recurse-submodules",
                    "--no-force-if-includes",
                    f"--force-with-lease={intent.destination_ref}:{intent.expected_remote_oid}",
                    "--",
                    intent.endpoint.original_url,
                    f"{intent.local_oid}:{intent.destination_ref}",
                ],
                env=env,
                timeout=GIT_PUSH_TIMEOUT_SECONDS,
                check=False,
            )
            observation_complete, observed = _observe_remote(
                snapshot,
                intent,
                preparation.https_binding,
            )
            if observation_complete and observed == intent.local_oid:
                outcome = GitMutationOutcome.APPLIED
            elif push.returncode != 0 and observation_complete:
                outcome = GitMutationOutcome.REJECTED
                error = push.stderr.decode("utf-8", errors="replace")[:4096]
            elif push.returncode == 0 and observation_complete:
                outcome = GitMutationOutcome.MISMATCH
            else:
                outcome = GitMutationOutcome.INCOMPLETE
                if push.returncode != 0:
                    error = push.stderr.decode("utf-8", errors="replace")[:4096]
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            observation_complete, observed = _observe_remote(
                snapshot,
                intent,
                preparation.https_binding,
            )
            if observation_complete and observed == intent.local_oid:
                outcome = GitMutationOutcome.APPLIED
            else:
                outcome = GitMutationOutcome.INCOMPLETE
    finally:
        if materialized_pin is not None:
            error = _append_error(error, materialized_pin.close())
        for pin in reversed(pins):
            error = _append_error(error, pin.close())
        close_https_transport(preparation.https_binding)

    if execution_id is None:
        raise GitMutationExecutionError(
            error or "HTTPS Git push execution did not reach authorization consumption"
        )

    observation = HttpsNetworkGitPushObservation(
        execution_id=execution_id,
        proposal_id=preparation.proposal.proposal_id,
        proposal_digest=preparation.proposal.proposal_digest,
        outcome=outcome,
        local_oid=params["local_oid"],
        remote_url=params["remote_url"],
        review_destination=preparation.approval_preview.review_destination,
        route_address=preparation.approval_preview.route_address,
        destination_ref=params["destination_ref"],
        expected_remote_oid=params["expected_remote_oid"],
        observed_remote_oid=observed,
        remote_observation_complete=observation_complete,
        backend=params["backend"],
        error=error,
    )
    lifecycle.record_observed()
    return observation