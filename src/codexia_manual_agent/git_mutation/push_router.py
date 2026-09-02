from __future__ import annotations

from pathlib import Path
from typing import TypeAlias

from codexia_manual_agent.domain.errors import InvalidGitMutationError
from codexia_manual_agent.git_mutation.https_push import (
    HttpsNetworkGitPushObservation,
    HttpsNetworkGitPushPreparation,
    close_https_network_git_push_preparation,
    execute_https_network_git_push,
    prepare_https_network_git_push_proposal,
)
from codexia_manual_agent.git_mutation.models import GitPushObservation, GitPushPreparation
from codexia_manual_agent.git_mutation.network_push import (
    NetworkGitPushObservation,
    NetworkGitPushPreparation,
    close_network_git_push_preparation,
    execute_network_git_push,
    prepare_network_git_push_proposal,
)
from codexia_manual_agent.git_mutation.push import execute_git_push, prepare_git_push_proposal


GovernedGitPushPreparation: TypeAlias = (
    GitPushPreparation | NetworkGitPushPreparation | HttpsNetworkGitPushPreparation
)
GovernedGitPushObservation: TypeAlias = (
    GitPushObservation | NetworkGitPushObservation | HttpsNetworkGitPushObservation
)


def _present(value: str | Path | None) -> bool:
    return value is not None and str(value) != ""


def prepare_governed_git_push_proposal(
    *,
    workspace: str | Path,
    remote: str,
    destination_ref: str,
    transport: str = "file",
    ssh_identity_file: str | Path | None = None,
    ssh_host_key_file: str | Path | None = None,
    https_credential_file: str | Path | None = None,
    https_ca_bundle: str | Path | None = None,
) -> GovernedGitPushPreparation:
    if transport not in {"file", "ssh", "https"}:
        raise InvalidGitMutationError("Git push transport must be file, ssh, or https")

    ssh_values = (_present(ssh_identity_file), _present(ssh_host_key_file))
    https_values = (_present(https_credential_file), _present(https_ca_bundle))

    if transport == "file":
        if any(ssh_values) or any(https_values):
            raise InvalidGitMutationError(
                "Local file Git push does not accept SSH or HTTPS trust inputs"
            )
        return prepare_git_push_proposal(
            workspace=workspace,
            remote=remote,
            destination_ref=destination_ref,
        )

    if transport == "ssh":
        if ssh_values != (True, True):
            raise InvalidGitMutationError(
                "SSH Git push requires both explicit identity and host-key files"
            )
        if any(https_values):
            raise InvalidGitMutationError("SSH Git push cannot accept HTTPS trust inputs")
        return prepare_network_git_push_proposal(
            workspace=workspace,
            remote=remote,
            destination_ref=destination_ref,
            identity_file=ssh_identity_file,  # type: ignore[arg-type]
            host_key_file=ssh_host_key_file,  # type: ignore[arg-type]
        )

    if https_values != (True, True):
        raise InvalidGitMutationError(
            "HTTPS Git push requires both explicit credential-source and CA-bundle files"
        )
    if any(ssh_values):
        raise InvalidGitMutationError("HTTPS Git push cannot accept SSH trust inputs")
    return prepare_https_network_git_push_proposal(
        workspace=workspace,
        remote=remote,
        destination_ref=destination_ref,
        credential_file=https_credential_file,  # type: ignore[arg-type]
        ca_bundle_file=https_ca_bundle,  # type: ignore[arg-type]
    )


def execute_governed_git_push(
    preparation: GovernedGitPushPreparation,
    *,
    lifecycle,
    authority,
) -> GovernedGitPushObservation:
    if isinstance(preparation, NetworkGitPushPreparation):
        return execute_network_git_push(
            preparation,
            lifecycle=lifecycle,
            authority=authority,
        )
    if isinstance(preparation, HttpsNetworkGitPushPreparation):
        return execute_https_network_git_push(
            preparation,
            lifecycle=lifecycle,
            authority=authority,
        )
    if isinstance(preparation, GitPushPreparation):
        return execute_git_push(
            preparation,
            lifecycle=lifecycle,
            authority=authority,
        )
    raise TypeError("preparation is not a governed Git push preparation")


def close_governed_git_push_preparation(preparation: GovernedGitPushPreparation) -> None:
    if isinstance(preparation, NetworkGitPushPreparation):
        close_network_git_push_preparation(preparation)
    elif isinstance(preparation, HttpsNetworkGitPushPreparation):
        close_https_network_git_push_preparation(preparation)
