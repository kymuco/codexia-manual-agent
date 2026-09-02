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
from codexia_manual_agent.git_mutation.ssh_execution import (
    SshExecutionPlan,
    build_isolated_ssh_execution_plan,
    close_ssh_execution_plan,
    materialize_ssh_execution_plan,
    revalidate_ssh_execution_plan,
    ssh_git_environment,
)
from codexia_manual_agent.git_mutation.ssh_transport import bind_ssh_transport
from codexia_manual_agent.git_mutation.windows_namespace import WindowsGitNamespacePin


GIT_SSH_NETWORK_PUSH_BACKEND = "network-ssh-direct.v1"


@dataclass(frozen=True, slots=True)
class NetworkGitPushApprovalPreview:
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
    ssh_user: str
    host_key_type: str
    host_key_fingerprint_sha256: str
    identity_source_path: str
    identity_source_sha256: str
    backend: str = GIT_SSH_NETWORK_PUSH_BACKEND
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
            "ssh_user": self.ssh_user,
            "host_key_type": self.host_key_type,
            "host_key_fingerprint_sha256": self.host_key_fingerprint_sha256,
            "identity_source_path": self.identity_source_path,
            "identity_source_sha256": self.identity_source_sha256,
            "backend": self.backend,
            "requires_human": self.requires_human,
        }


@dataclass(frozen=True, slots=True)
class NetworkGitPushPreparation:
    proposal: ActionProposal
    approval_preview: NetworkGitPushApprovalPreview
    ssh_plan: SshExecutionPlan

    def __post_init__(self) -> None:
        if not isinstance(self.proposal, ActionProposal):
            raise TypeError("proposal must be ActionProposal")
        if not isinstance(self.approval_preview, NetworkGitPushApprovalPreview):
            raise TypeError("approval_preview must be NetworkGitPushApprovalPreview")
        if not isinstance(self.ssh_plan, SshExecutionPlan):
            raise TypeError("ssh_plan must be SshExecutionPlan")


@dataclass(frozen=True, slots=True)
class NetworkGitPushObservation:
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


def _plan_manifest(plan: SshExecutionPlan) -> dict[str, Any]:
    binding = plan.binding
    host_source = binding.host_key_pin.source_file
    return {
        "backend": plan.backend,
        "endpoint": binding.endpoint.to_dict(),
        "route": plan.route.to_dict(),
        "git_shell": plan.git_shell.to_dict(),
        "ssh_executable": binding.ssh_executable.to_dict(),
        "identity_source": binding.identity_file.to_dict(),
        "host_key_pin": binding.host_key_pin.to_dict(),
        "credential_mode": binding.credential_mode,
        "host_key_mode": binding.host_key_mode,
        "bundle_identity_size_bytes": binding.identity_file.size_bytes,
        "bundle_identity_sha256": binding.identity_file.sha256,
        "bundle_known_hosts_size_bytes": host_source.size_bytes,
        "bundle_known_hosts_sha256": host_source.sha256,
        "ssh_command_sha256": plan.ssh_command_sha256,
    }


def _preview(intent: GitNetworkPushIntent, plan: SshExecutionPlan) -> NetworkGitPushApprovalPreview:
    binding = plan.binding
    if binding.endpoint.ssh_user is None:
        raise InvalidGitMutationError("SSH network push requires an explicit SSH user")
    return NetworkGitPushApprovalPreview(
        local_oid=intent.local_oid,
        local_ref=intent.local_ref,
        remote_name=intent.remote_name,
        remote_url=intent.endpoint.original_url,
        review_destination=intent.endpoint.review_destination,
        route_address=plan.route.address,
        route_family=plan.route.family,
        destination_ref=intent.destination_ref,
        tracking_ref=intent.tracking_ref,
        expected_remote_oid=intent.expected_remote_oid,
        ssh_user=binding.endpoint.ssh_user,
        host_key_type=binding.host_key_pin.key_type,
        host_key_fingerprint_sha256=binding.host_key_pin.fingerprint_sha256,
        identity_source_path=binding.identity_file.path,
        identity_source_sha256=binding.identity_file.sha256,
    )


def prepare_network_git_push_proposal(
    *,
    workspace: str | Path,
    remote: str,
    destination_ref: str,
    identity_file: str | Path,
    host_key_file: str | Path,
) -> NetworkGitPushPreparation:
    remote = validate_remote_name(remote)
    destination_ref = validate_head_ref(destination_ref)
    snapshot = snapshot_repository(workspace)
    intent = build_network_push_intent(
        snapshot,
        remote=remote,
        destination_ref=destination_ref,
    )
    if intent.endpoint.transport is not GitNetworkTransport.SSH:
        raise InvalidGitMutationError("SSH network push requires an SSH remote")
    binding = bind_ssh_transport(
        snapshot,
        intent.endpoint,
        identity_file=identity_file,
        host_key_file=host_key_file,
    )
    plan: SshExecutionPlan | None = None
    try:
        plan = build_isolated_ssh_execution_plan(snapshot, binding)
        local_config_size, local_config_sha256 = read_local_config_identity(snapshot)
        params = {
            "schema_version": GIT_MUTATION_SCHEMA_VERSION,
            "backend": GIT_SSH_NETWORK_PUSH_BACKEND,
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
            "transport": _plan_manifest(plan),
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
                f"SSH route {plan.route.address}, host key {binding.host_key_pin.fingerprint_sha256}."
            ),
        )
        return NetworkGitPushPreparation(
            proposal=proposal,
            approval_preview=_preview(intent, plan),
            ssh_plan=plan,
        )
    except BaseException:
        if plan is not None:
            close_ssh_execution_plan(plan)
        raise


def close_network_git_push_preparation(preparation: NetworkGitPushPreparation) -> None:
    if not isinstance(preparation, NetworkGitPushPreparation):
        raise TypeError("preparation must be NetworkGitPushPreparation")
    close_ssh_execution_plan(preparation.ssh_plan)


def _params(proposal: ActionProposal) -> dict[str, Any]:
    if proposal.capability is not Capability.GIT_PUSH or proposal.action != GIT_PUSH_ACTION:
        raise InvalidGitMutationError("Action proposal is not an M2.5.1 network Git push")
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
        raise InvalidGitMutationError("Network Git push proposal schema mismatch")
    if params["backend"] != GIT_SSH_NETWORK_PUSH_BACKEND:
        raise InvalidGitMutationError("Unsupported M2.5.1 network Git push backend")
    oid_len = (
        40
        if params["object_format"] == "sha1"
        else 64
        if params["object_format"] == "sha256"
        else 0
    )
    if not oid_len:
        raise InvalidGitMutationError("Unsupported network Git object format")
    validate_oid(params["local_oid"], oid_len, "Network Git local object")
    validate_oid(params["expected_remote_oid"], oid_len, "Network Git expected remote object")
    validate_head_ref(params["local_ref"])
    validate_head_ref(params["destination_ref"])
    validate_remote_name(params["remote_name"])
    if (
        type(params["local_config_size_bytes"]) is not int
        or not 1 <= params["local_config_size_bytes"] <= MAX_GIT_CONFIG_BYTES
    ):
        raise InvalidGitMutationError("Network Git local config size is invalid")
    require_sha256(params["local_config_sha256"], "Network Git local config digest")
    if not isinstance(params["intent"], dict) or not isinstance(params["transport"], dict):
        raise InvalidGitMutationError("Network Git bound intent/transport schema mismatch")
    params["git_executable"] = parse_git_identity(params["git_executable"])
    return params


def _revalidate(preparation: NetworkGitPushPreparation, params: dict[str, Any]):
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
            "Git local ref/object changed before network push authorization consumption"
        )
    config_size, config_sha256 = read_local_config_identity(snapshot)
    if (
        config_size != params["local_config_size_bytes"]
        or config_sha256 != params["local_config_sha256"]
    ):
        raise GitMutationPreconditionChangedError(
            "Local Git config changed before network push authorization consumption"
        )
    intent = build_network_push_intent(
        snapshot,
        remote=params["remote_name"],
        destination_ref=params["destination_ref"],
    )
    if intent.to_dict() != params["intent"]:
        raise GitMutationPreconditionChangedError(
            "Network Git push intent changed before authorization consumption"
        )
    if (
        intent.local_oid != params["local_oid"]
        or intent.expected_remote_oid != params["expected_remote_oid"]
        or intent.tracking_ref != params["tracking_ref"]
        or intent.endpoint.original_url != params["remote_url"]
    ):
        raise GitMutationPreconditionChangedError(
            "Network Git push bound ref/URL state changed before authorization consumption"
        )
    revalidate_ssh_execution_plan(preparation.ssh_plan, require_materialized=False)
    if _plan_manifest(preparation.ssh_plan) != params["transport"]:
        raise GitMutationPreconditionChangedError(
            "Network SSH transport identity changed before authorization consumption"
        )
    canonical_preview = _preview(intent, preparation.ssh_plan)
    if canonical_preview.to_dict() != preparation.approval_preview.to_dict():
        raise InvalidGitMutationError(
            "Displayed network Git push preview does not match the exact proposal state"
        )
    return snapshot, intent


def _observe_remote(
    snapshot,
    intent: GitNetworkPushIntent,
    plan: SshExecutionPlan,
) -> tuple[bool, str | None]:
    try:
        result = run_git(
            snapshot.git,
            snapshot.workspace_root,
            [
                "ls-remote",
                "--refs",
                "--",
                intent.endpoint.original_url,
                intent.destination_ref,
            ],
            env=ssh_git_environment(plan),
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
        return True, validate_oid(oid, snapshot.oid_length, "Observed network Git object")
    except InvalidGitMutationError:
        return False, None


def _append_error(existing: str | None, extra: str | None) -> str | None:
    if not extra:
        return existing
    return extra if not existing else f"{existing}; {extra}"


def execute_network_git_push(
    preparation: NetworkGitPushPreparation,
    *,
    lifecycle: ActionLifecycle,
    authority: LocalApprovalAuthority,
) -> NetworkGitPushObservation:
    if not isinstance(preparation, NetworkGitPushPreparation):
        raise TypeError("preparation must be NetworkGitPushPreparation")
    if lifecycle.proposal != preparation.proposal:
        raise InvalidGitMutationError("Lifecycle is not bound to the network Git push proposal")
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
        pins.append(
            WindowsGitNamespacePin.acquire(
                (pre_snapshot.git_dir,),
                locked_files=(
                    pre_snapshot.git_dir / "config",
                    Path(params["git_executable"].path),
                    Path(preparation.ssh_plan.git_shell.path),
                    Path(preparation.ssh_plan.binding.ssh_executable.path),
                    Path(preparation.ssh_plan.binding.identity_file.path),
                    Path(preparation.ssh_plan.binding.host_key_pin.source_file.path),
                ),
            )
        )
        pins.append(
            WindowsGitNamespacePin.acquire(
                (
                    Path(preparation.ssh_plan.bundle_root),
                    Path(f"{preparation.ssh_plan.bundle_identity_path}.pub"),
                    Path(preparation.ssh_plan.certificate_block_path),
                )
            )
        )
        snapshot, intent = _revalidate(preparation, params)

        # All local execution inputs become exact and immutable before the one-shot
        # receipt is consumed. Failure here remains pre-consumption and opens no network.
        materialize_ssh_execution_plan(preparation.ssh_plan)
        materialized_pin = WindowsGitNamespacePin.acquire(
            (Path(preparation.ssh_plan.bundle_root),),
            locked_files=(
                Path(preparation.ssh_plan.bundle_identity_path),
                Path(preparation.ssh_plan.bundle_known_hosts_path),
            ),
        )
        revalidate_ssh_execution_plan(
            preparation.ssh_plan,
            require_materialized=True,
        )

        lifecycle.consume_authorization(authority=authority)
        execution_id = lifecycle.record_executed()
        try:
            push = run_git(
                snapshot.git,
                snapshot.workspace_root,
                [
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
                env=ssh_git_environment(preparation.ssh_plan),
                timeout=GIT_PUSH_TIMEOUT_SECONDS,
                check=False,
            )
            observation_complete, observed = _observe_remote(
                snapshot,
                intent,
                preparation.ssh_plan,
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
                preparation.ssh_plan,
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
        close_ssh_execution_plan(preparation.ssh_plan)

    if execution_id is None:
        raise GitMutationExecutionError(
            error or "Network Git push execution did not reach authorization consumption"
        )

    observation = NetworkGitPushObservation(
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
