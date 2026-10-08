from __future__ import annotations

"""Pure, read-only evidence comparison for one existing cognition handoff.

This module has no WorkStore, provider, network, or admission dependencies.
A caller must independently recover and verify the durable Work frontier
and authenticate provider readback. This v0 can corroborate *context* but
cannot promote observations to provider-correlated proof or admit outcomes.
"""

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_MAX_RESPONSE_CHARS = 131_072


class ReconciliationVerdict(StrEnum):
    CONTEXT_CORROBORATED = "context_corroborated"
    UNVERIFIABLE = "unverifiable"
    CONFLICT = "conflict"


class ReadbackState(StrEnum):
    COMPLETED = "completed"
    PENDING = "pending"
    NOT_FOUND = "not_found"
    READ_UNAVAILABLE = "read_unavailable"
    UNSUPPORTED = "unsupported"


def _nonempty(value: str, label: str) -> None:
    if type(value) is not str or not value or value.strip() != value:
        raise ValueError(f"{label} must be nonempty canonical text")


def _sha(value: str, label: str) -> None:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{label} must be a lowercase SHA-256")


def _json_sha(payload: dict[str, Any]) -> str:
    raw = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True, slots=True)
class PendingCognition:
    """Host-projected scope; fields alone do NOT prove actual Work state."""

    work_id: str
    workflow_run_id: str
    role_run_id: str
    request_id: str
    request_digest: str
    handoff_id: str
    handoff_digest: str
    port_id: str
    provider_namespace: str
    expected_wire_sha256: str
    expected_work_revision: int
    expected_head_digest: str

    def __post_init__(self) -> None:
        for name in (
            "work_id",
            "workflow_run_id",
            "role_run_id",
            "request_id",
            "handoff_id",
            "port_id",
            "provider_namespace",
        ):
            _nonempty(getattr(self, name), name)
        for name in (
            "request_digest",
            "handoff_digest",
            "expected_wire_sha256",
            "expected_head_digest",
        ):
            _sha(getattr(self, name), name)
        if (
            type(self.expected_work_revision) is not int
            or self.expected_work_revision < 1
        ):
            raise ValueError("expected_work_revision must be a positive integer")


@dataclass(frozen=True, slots=True)
class ReadbackObservation:
    """Provider-read assertions: UNTRUSTED until host authenticates source.

    Optional fields permit incomplete observations. No boolean here grants
    authorization or establishes provider-side causality.
    """

    state: ReadbackState
    port_id: str
    provider_namespace: str
    request_id: str | None = None
    observed_wire_sha256: str | None = None
    request_message_id: str | None = None
    response_message_id: str | None = None
    response_candidates: int | None = None
    branch_verified: bool | None = None
    request_precedes_response: bool | None = None
    intervening_user_turns: int | None = None
    finish_reason: str | None = None
    response_text: str | None = None
    reported_response_sha256: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "state", ReadbackState(self.state))
        _nonempty(self.port_id, "port_id")
        _nonempty(self.provider_namespace, "provider_namespace")
        for name in ("response_candidates", "intervening_user_turns"):
            value = getattr(self, name)
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError(f"{name} must be a nonnegative integer or null")
        for name in ("branch_verified", "request_precedes_response"):
            value = getattr(self, name)
            if value is not None and type(value) is not bool:
                raise TypeError(f"{name} must be boolean or null")
        if self.response_text is not None and type(self.response_text) is not str:
            raise TypeError("response_text must be text or null")


@dataclass(frozen=True, slots=True)
class ReconciliationDecision:
    """Read-only, non-authoritative evidence report (never a role outcome)."""

    verdict: ReconciliationVerdict
    reason: str
    request_id: str
    handoff_id: str
    response_message_id: str | None
    response_sha256: str | None
    report_digest: str

    @property
    def eligible_for_automatic_admission(self) -> bool:
        # No provider receipt authentication or admission policy in v0.
        return False


def verify_readback(
    scope: PendingCognition,
    observation: ReadbackObservation | None,
) -> ReconciliationDecision:
    """Compare one *already read* candidate against exact expected metadata.

    No I/O, no transport, no retry, no durable write, no trust escalation.
    Even CONTEXT_CORROBORATED is only a candidate for a separate review.
    """

    if not isinstance(scope, PendingCognition):
        raise TypeError("scope must be PendingCognition")
    if observation is not None and not isinstance(observation, ReadbackObservation):
        raise TypeError("observation must be ReadbackObservation or None")

    response_sha = None
    if observation is not None and observation.response_text is not None:
        try:
            response_sha = hashlib.sha256(
                observation.response_text.encode("utf-8")
            ).hexdigest()
        except UnicodeEncodeError:
            response_sha = None

    def decide(verdict: ReconciliationVerdict, reason: str) -> ReconciliationDecision:
        payload = {
            "schema_version": 1,
            "scope": asdict(scope),
            "observation": (
                None
                if observation is None
                else {
                    **{
                        key: value
                        for key, value in asdict(observation).items()
                        if key != "response_text"
                    },
                    "computed_response_sha256": response_sha,
                }
            ),
            "verdict": verdict.value,
            "reason": reason,
        }
        return ReconciliationDecision(
            verdict=verdict,
            reason=reason,
            request_id=scope.request_id,
            handoff_id=scope.handoff_id,
            response_message_id=(
                None if observation is None else observation.response_message_id
            ),
            response_sha256=response_sha,
            report_digest=_json_sha(payload),
        )

    if observation is None:
        return decide(ReconciliationVerdict.UNVERIFIABLE, "no_observation")
    if observation.response_text is not None and response_sha is None:
        return decide(ReconciliationVerdict.CONFLICT, "response_not_utf8")
    if (
        observation.port_id != scope.port_id
        or observation.provider_namespace != scope.provider_namespace
    ):
        return decide(ReconciliationVerdict.CONFLICT, "provider_binding_mismatch")
    if observation.state is not ReadbackState.COMPLETED:
        return decide(
            ReconciliationVerdict.UNVERIFIABLE,
            f"provider_{observation.state.value}",
        )
    if observation.request_id is None or observation.observed_wire_sha256 is None:
        return decide(ReconciliationVerdict.UNVERIFIABLE, "missing_request_binding")
    if observation.request_id != scope.request_id:
        return decide(ReconciliationVerdict.CONFLICT, "request_identity_mismatch")
    if observation.observed_wire_sha256 != scope.expected_wire_sha256:
        return decide(ReconciliationVerdict.CONFLICT, "wire_input_mismatch")
    if observation.response_candidates is None:
        return decide(ReconciliationVerdict.UNVERIFIABLE, "candidate_count_unknown")
    if observation.response_candidates > 1:
        return decide(ReconciliationVerdict.CONFLICT, "multiple_response_candidates")
    if observation.response_candidates != 1:
        return decide(ReconciliationVerdict.UNVERIFIABLE, "no_response_candidate")
    if (
        observation.request_message_id is None
        or observation.response_message_id is None
    ):
        return decide(ReconciliationVerdict.UNVERIFIABLE, "missing_message_identity")
    if observation.request_message_id == observation.response_message_id:
        return decide(ReconciliationVerdict.CONFLICT, "message_identity_collision")
    if observation.request_precedes_response is False:
        return decide(ReconciliationVerdict.CONFLICT, "response_order_conflict")
    if observation.request_precedes_response is None:
        return decide(ReconciliationVerdict.UNVERIFIABLE, "response_order_unknown")
    if observation.intervening_user_turns is None:
        return decide(ReconciliationVerdict.UNVERIFIABLE, "intervening_turns_unknown")
    if observation.intervening_user_turns:
        return decide(ReconciliationVerdict.CONFLICT, "intervening_user_turn")
    if observation.branch_verified is False:
        return decide(ReconciliationVerdict.CONFLICT, "branch_conflict")
    if observation.branch_verified is None:
        return decide(ReconciliationVerdict.UNVERIFIABLE, "branch_lineage_unknown")
    if observation.finish_reason != "stop":
        return decide(ReconciliationVerdict.UNVERIFIABLE, "not_final_response")
    if not observation.response_text:
        return decide(ReconciliationVerdict.UNVERIFIABLE, "no_response_text")
    if len(observation.response_text) > _MAX_RESPONSE_CHARS:
        return decide(ReconciliationVerdict.CONFLICT, "response_out_of_bounds")
    if observation.reported_response_sha256 is None:
        return decide(ReconciliationVerdict.UNVERIFIABLE, "missing_response_digest")
    if response_sha != observation.reported_response_sha256:
        return decide(ReconciliationVerdict.CONFLICT, "response_digest_mismatch")

    # An authenticated original provider receipt is NOT modeled or verified
    # here. Context match cannot become exact provider-correlated proof.
    return decide(ReconciliationVerdict.CONTEXT_CORROBORATED, "unique_context_match")
