from __future__ import annotations

from dataclasses import replace
import hmac
from typing import Any

from codexia_manual_agent.work.contracts import (
    InvalidWorkRecordError,
    WorkActorKind,
    WorkStatement,
    _exact_keys,
    _validate_digest,
    _validate_uuid,
)
from codexia_manual_agent.work.pilot_provider import (
    CWA_WRITE_NOT_SUBMITTED,
    ProviderWriteNotSubmittedError,
)
from codexia_manual_agent.work.supervisor import (
    BackgroundWorkSupervisor,
    SupervisorDispatchLease,
    SupervisorEventKind,
    SupervisorIntegrityError,
    SupervisorPersistenceError,
    SupervisorStateError,
    SupervisorStatus,
    SupervisorWorkSnapshot,
)

M66_DISPATCH_NOT_SUBMITTED_KEY = "m6_6_dispatch_not_submitted"
M66_DISPATCH_NOT_SUBMITTED_SCHEMA_VERSION = 1
M66_HUMAN_DISPATCH_REARM_KEY = "m6_6_human_dispatch_rearm"
M66_HUMAN_DISPATCH_REARM_SCHEMA_VERSION = 1


class SupervisorDispatchNotSubmittedError(SupervisorPersistenceError):
    """One claimed dispatch was durably re-armed after exact no-submit proof."""


class M66RecoverableBackgroundWorkSupervisor(BackgroundWorkSupervisor):
    """M6.6 recovery extensions for exact non-submitted and human-authorized re-arm."""

    def execute_claimed_chat(self, lease: SupervisorDispatchLease, peer_loop: Any):
        try:
            return super().execute_claimed_chat(lease, peer_loop)
        except ProviderWriteNotSubmittedError as error:
            recovered = self.record_dispatch_not_submitted(lease, error=error)
            dispatch = recovered.pending_dispatch
            if dispatch is None or recovered.status is not SupervisorStatus.PREPARED:
                raise SupervisorIntegrityError(
                    "No-submit recovery did not restore exact PREPARED dispatch"
                ) from error
            raise SupervisorDispatchNotSubmittedError(
                "provider proved product write was not submitted; exact dispatch "
                f"re-armed without automatic retry: {dispatch.dispatch_digest}"
            ) from error

    def record_dispatch_not_submitted(
        self,
        lease: SupervisorDispatchLease,
        *,
        error: ProviderWriteNotSubmittedError,
    ) -> SupervisorWorkSnapshot:
        if not isinstance(lease, SupervisorDispatchLease):
            raise SupervisorStateError("lease must be a SupervisorDispatchLease")
        if not isinstance(error, ProviderWriteNotSubmittedError):
            raise SupervisorStateError(
                "dispatch recovery requires ProviderWriteNotSubmittedError"
            )
        if (
            error.failure_kind != CWA_WRITE_NOT_SUBMITTED
            or error.write_may_have_been_submitted is not False
            or error.reconciliation_required is not False
            or error.automatic_retry_allowed is not False
            or error.manual_retry_safe_after_repair is not True
        ):
            raise SupervisorStateError(
                "provider failure does not prove a non-submitted dispatch"
            )

        snapshot = self.recover(lease.work_id)
        dispatch = snapshot.pending_dispatch
        if (
            snapshot.status is not SupervisorStatus.IN_FLIGHT
            or dispatch is None
            or snapshot.in_flight_claim_id != lease.claim_id
            or not hmac.compare_digest(dispatch.dispatch_digest, lease.dispatch_digest)
        ):
            raise SupervisorStateError(
                "No-submit proof does not bind exact IN_FLIGHT dispatch claim"
            )

        proof = {
            "schema_version": M66_DISPATCH_NOT_SUBMITTED_SCHEMA_VERSION,
            "dispatch_digest": dispatch.dispatch_digest,
            "claim_id": lease.claim_id,
            "failure_kind": error.failure_kind,
            "request_stage": error.request_stage,
            "write_may_have_been_submitted": False,
            "reconciliation_required": False,
            "automatic_retry_allowed": False,
            "manual_retry_safe_after_repair": True,
        }
        return self._append(
            snapshot,
            SupervisorEventKind.EXTERNAL_OBSERVED,
            {M66_DISPATCH_NOT_SUBMITTED_KEY: proof},
        )

    def record_human_dispatch_rearm(
        self,
        work_id: str,
        *,
        expected_dispatch_digest: str,
        expected_claim_id: str,
        authorization: WorkStatement,
    ) -> SupervisorWorkSnapshot:
        if not isinstance(authorization, WorkStatement):
            raise SupervisorStateError("human re-arm authorization must be a WorkStatement")
        if authorization.author_kind is not WorkActorKind.HUMAN:
            raise SupervisorStateError("dispatch re-arm requires explicit HUMAN authorization")
        _validate_digest(expected_dispatch_digest, "expected_dispatch_digest")
        _validate_uuid(expected_claim_id, "expected_claim_id")

        snapshot = self.recover(work_id)
        dispatch = snapshot.pending_dispatch
        if (
            snapshot.status is not SupervisorStatus.IN_FLIGHT
            or dispatch is None
            or snapshot.in_flight_claim_id is None
        ):
            raise SupervisorStateError(
                "Human dispatch re-arm requires exact IN_FLIGHT pending dispatch"
            )
        if snapshot.in_flight_claim_id != expected_claim_id:
            raise SupervisorStateError("Human dispatch re-arm claim id is stale or mismatched")
        if not hmac.compare_digest(dispatch.dispatch_digest, expected_dispatch_digest):
            raise SupervisorStateError(
                "Human dispatch re-arm digest does not bind current pending dispatch"
            )

        payload = {
            "schema_version": M66_HUMAN_DISPATCH_REARM_SCHEMA_VERSION,
            "dispatch_digest": dispatch.dispatch_digest,
            "claim_id": snapshot.in_flight_claim_id,
            "authorization": authorization.to_dict(),
        }
        return self._append(
            snapshot,
            SupervisorEventKind.EXTERNAL_OBSERVED,
            {M66_HUMAN_DISPATCH_REARM_KEY: payload},
        )

    def _apply_event(self, snapshot: SupervisorWorkSnapshot | None, event: Any):
        if (
            event.kind is SupervisorEventKind.EXTERNAL_OBSERVED
            and isinstance(event.payload, dict)
            and M66_DISPATCH_NOT_SUBMITTED_KEY in event.payload
        ):
            return self._apply_dispatch_not_submitted_event(snapshot, event)
        if (
            event.kind is SupervisorEventKind.EXTERNAL_OBSERVED
            and isinstance(event.payload, dict)
            and M66_HUMAN_DISPATCH_REARM_KEY in event.payload
        ):
            return self._apply_human_dispatch_rearm_event(snapshot, event)
        return super()._apply_event(snapshot, event)

    def _apply_dispatch_not_submitted_event(
        self,
        snapshot: SupervisorWorkSnapshot | None,
        event: Any,
    ) -> SupervisorWorkSnapshot:
        if snapshot is None:
            raise SupervisorIntegrityError(
                "No-submit recovery cannot precede supervisor registration"
            )
        if (
            snapshot.status is not SupervisorStatus.IN_FLIGHT
            or snapshot.pending_dispatch is None
            or snapshot.in_flight_claim_id is None
        ):
            raise SupervisorIntegrityError(
                "No-submit recovery requires exact IN_FLIGHT dispatch"
            )
        value = _exact_keys(
            event.payload,
            {M66_DISPATCH_NOT_SUBMITTED_KEY},
            "M6.6 dispatch-not-submitted event",
        )
        proof = _exact_keys(
            value[M66_DISPATCH_NOT_SUBMITTED_KEY],
            {
                "schema_version",
                "dispatch_digest",
                "claim_id",
                "failure_kind",
                "request_stage",
                "write_may_have_been_submitted",
                "reconciliation_required",
                "automatic_retry_allowed",
                "manual_retry_safe_after_repair",
            },
            "M6.6 dispatch-not-submitted proof",
        )
        if proof["schema_version"] != M66_DISPATCH_NOT_SUBMITTED_SCHEMA_VERSION:
            raise InvalidWorkRecordError(
                "Unsupported M6.6 dispatch-not-submitted schema"
            )
        _validate_digest(proof["dispatch_digest"], "dispatch_digest")
        _validate_uuid(proof["claim_id"], "claim_id")
        if proof["failure_kind"] != CWA_WRITE_NOT_SUBMITTED:
            raise SupervisorIntegrityError(
                "Dispatch recovery payload has non-authoritative failure kind"
            )
        if proof["request_stage"] is not None and not isinstance(
            proof["request_stage"], str
        ):
            raise InvalidWorkRecordError(
                "dispatch-not-submitted request_stage must be text or null"
            )
        if (
            proof["write_may_have_been_submitted"] is not False
            or proof["reconciliation_required"] is not False
            or proof["automatic_retry_allowed"] is not False
            or proof["manual_retry_safe_after_repair"] is not True
        ):
            raise SupervisorIntegrityError(
                "Dispatch recovery payload does not prove exact non-submission"
            )
        if not hmac.compare_digest(
            proof["dispatch_digest"],
            snapshot.pending_dispatch.dispatch_digest,
        ):
            raise SupervisorIntegrityError(
                "Dispatch recovery proof binds a different dispatch"
            )
        if proof["claim_id"] != snapshot.in_flight_claim_id:
            raise SupervisorIntegrityError(
                "Dispatch recovery proof binds a different claim"
            )
        return replace(
            snapshot,
            status=SupervisorStatus.PREPARED,
            in_flight_claim_id=None,
            last_sequence=event.sequence,
            last_event_digest=event.event_digest,
        )

    def _apply_human_dispatch_rearm_event(
        self,
        snapshot: SupervisorWorkSnapshot | None,
        event: Any,
    ) -> SupervisorWorkSnapshot:
        if snapshot is None:
            raise SupervisorIntegrityError(
                "Human dispatch re-arm cannot precede supervisor registration"
            )
        if (
            snapshot.status is not SupervisorStatus.IN_FLIGHT
            or snapshot.pending_dispatch is None
            or snapshot.in_flight_claim_id is None
        ):
            raise SupervisorIntegrityError(
                "Human dispatch re-arm requires exact IN_FLIGHT dispatch"
            )
        value = _exact_keys(
            event.payload,
            {M66_HUMAN_DISPATCH_REARM_KEY},
            "M6.6 human dispatch re-arm event",
        )
        proof = _exact_keys(
            value[M66_HUMAN_DISPATCH_REARM_KEY],
            {
                "schema_version",
                "dispatch_digest",
                "claim_id",
                "authorization",
            },
            "M6.6 human dispatch re-arm proof",
        )
        if proof["schema_version"] != M66_HUMAN_DISPATCH_REARM_SCHEMA_VERSION:
            raise InvalidWorkRecordError("Unsupported M6.6 human dispatch re-arm schema")
        _validate_digest(proof["dispatch_digest"], "dispatch_digest")
        _validate_uuid(proof["claim_id"], "claim_id")
        authorization = WorkStatement.from_dict(proof["authorization"])
        if authorization.author_kind is not WorkActorKind.HUMAN:
            raise SupervisorIntegrityError(
                "Persisted dispatch re-arm authorization is not HUMAN-authored"
            )
        if not hmac.compare_digest(
            proof["dispatch_digest"],
            snapshot.pending_dispatch.dispatch_digest,
        ):
            raise SupervisorIntegrityError(
                "Human dispatch re-arm binds a different dispatch"
            )
        if proof["claim_id"] != snapshot.in_flight_claim_id:
            raise SupervisorIntegrityError(
                "Human dispatch re-arm binds a different claim"
            )
        return replace(
            snapshot,
            status=SupervisorStatus.PREPARED,
            in_flight_claim_id=None,
            last_sequence=event.sequence,
            last_event_digest=event.event_digest,
        )


__all__ = [
    "M66_DISPATCH_NOT_SUBMITTED_KEY",
    "M66_HUMAN_DISPATCH_REARM_KEY",
    "M66RecoverableBackgroundWorkSupervisor",
    "SupervisorDispatchNotSubmittedError",
]
