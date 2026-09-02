from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from codexia_manual_agent.domain.errors import (
    GitMutationPreconditionChangedError,
    InvalidGitMutationError,
)
from codexia_manual_agent.git_mutation.network_transport import (
    GitNetworkEndpoint,
    parse_network_git_endpoint,
)
from codexia_manual_agent.git_mutation.repository import (
    RepositorySnapshot,
    decode_line,
    run_git,
    validate_head_ref,
    validate_oid,
    validate_remote_name,
)


class NetworkGitPushIntentStateError(
    InvalidGitMutationError,
    GitMutationPreconditionChangedError,
):
    """Mutable intent state is invalid initially and drifted during revalidation."""


@dataclass(frozen=True, slots=True)
class GitNetworkPushIntent:
    remote_name: str
    endpoint: GitNetworkEndpoint
    local_ref: str
    local_oid: str
    destination_ref: str
    tracking_ref: str
    expected_remote_oid: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "remote_name": self.remote_name,
            "endpoint": self.endpoint.to_dict(),
            "review_destination": self.endpoint.review_destination,
            "local_ref": self.local_ref,
            "local_oid": self.local_oid,
            "destination_ref": self.destination_ref,
            "tracking_ref": self.tracking_ref,
            "expected_remote_oid": self.expected_remote_oid,
        }


def _config_values(snapshot: RepositorySnapshot, key: str) -> tuple[str, ...]:
    result = run_git(
        snapshot.git,
        snapshot.workspace_root,
        ["config", "--local", "--get-all", key],
        check=False,
    )
    if result.returncode == 1:
        return ()
    if result.returncode != 0:
        raise InvalidGitMutationError(f"Local Git config {key!r} could not be read")
    try:
        values = tuple(line for line in result.stdout.decode("utf-8").splitlines() if line)
    except UnicodeDecodeError as exc:
        raise InvalidGitMutationError(f"Local Git config {key!r} is not UTF-8") from exc
    if any(any(char in value for char in "\r\n\x00") for value in values):
        raise InvalidGitMutationError(f"Local Git config {key!r} contains control characters")
    return values


def _reject_regexp_config(snapshot: RepositorySnapshot, pattern: str, label: str) -> None:
    result = run_git(
        snapshot.git,
        snapshot.workspace_root,
        ["config", "--local", "--get-regexp", pattern],
        check=False,
    )
    if result.returncode == 0:
        raise InvalidGitMutationError(f"M2.5.1 rejects local Git config that can alter {label}")
    if result.returncode != 1:
        raise InvalidGitMutationError(f"Local Git config could not be checked for {label}")


def _single_network_push_url(snapshot: RepositorySnapshot, remote: str) -> GitNetworkEndpoint:
    # git-config regex matching is case-sensitive against canonicalized keys where
    # section and variable names are lower-case (subsection spelling is preserved).
    _reject_regexp_config(
        snapshot,
        r"^url\..*\.(insteadof|pushinsteadof)$",
        "network destination URL rewriting",
    )
    push_urls = _config_values(snapshot, f"remote.{remote}.pushurl")
    urls = push_urls if push_urls else _config_values(snapshot, f"remote.{remote}.url")
    if len(urls) != 1:
        raise InvalidGitMutationError(
            "M2.5.1 requires exactly one configured network push destination"
        )
    if _config_values(snapshot, f"remote.{remote}.receivepack"):
        raise InvalidGitMutationError("M2.5.1 rejects remote receive-pack overrides")
    if _config_values(snapshot, f"remote.{remote}.mirror"):
        raise InvalidGitMutationError("M2.5.1 rejects mirror push configuration")
    if _config_values(snapshot, "push.pushOption"):
        raise InvalidGitMutationError("M2.5.1 rejects configured push options")
    return parse_network_git_endpoint(urls[0])


def _tracking_ref(remote: str, destination_ref: str) -> str:
    branch = destination_ref.removeprefix("refs/heads/")
    return f"refs/remotes/{remote}/{branch}"


def _expected_remote_oid(
    snapshot: RepositorySnapshot,
    *,
    tracking_ref: str,
) -> str:
    result = run_git(
        snapshot.git,
        snapshot.workspace_root,
        ["rev-parse", "--verify", f"{tracking_ref}^{{commit}}"],
        check=False,
    )
    if result.returncode != 0:
        raise NetworkGitPushIntentStateError(
            "M2.5.1 requires an existing local remote-tracking commit for the destination"
        )
    return validate_oid(
        decode_line(result.stdout, "Network Git expected remote object"),
        snapshot.oid_length,
        "Network Git expected remote object",
    )


def _require_local_fast_forward(
    snapshot: RepositorySnapshot,
    *,
    expected_remote_oid: str,
) -> None:
    if expected_remote_oid == snapshot.head_oid:
        raise NetworkGitPushIntentStateError("Network Git push proposal would be a no-op")
    ancestor = run_git(
        snapshot.git,
        snapshot.workspace_root,
        ["merge-base", "--is-ancestor", expected_remote_oid, snapshot.head_oid],
        check=False,
    )
    if ancestor.returncode == 1:
        raise NetworkGitPushIntentStateError(
            "M2.5.1 refuses a locally provable non-fast-forward network push"
        )
    if ancestor.returncode != 0:
        raise NetworkGitPushIntentStateError("Git could not verify network push ancestry")


def build_network_push_intent(
    snapshot: RepositorySnapshot,
    *,
    remote: str,
    destination_ref: str,
) -> GitNetworkPushIntent:
    if not isinstance(snapshot, RepositorySnapshot):
        raise TypeError("snapshot must be RepositorySnapshot")
    remote = validate_remote_name(remote)
    destination_ref = validate_head_ref(destination_ref)
    endpoint = _single_network_push_url(snapshot, remote)
    tracking_ref = _tracking_ref(remote, destination_ref)
    expected_remote_oid = _expected_remote_oid(snapshot, tracking_ref=tracking_ref)
    _require_local_fast_forward(snapshot, expected_remote_oid=expected_remote_oid)
    return GitNetworkPushIntent(
        remote_name=remote,
        endpoint=endpoint,
        local_ref=snapshot.head_ref,
        local_oid=snapshot.head_oid,
        destination_ref=destination_ref,
        tracking_ref=tracking_ref,
        expected_remote_oid=expected_remote_oid,
    )
