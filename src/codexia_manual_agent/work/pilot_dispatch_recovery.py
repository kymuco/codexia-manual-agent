from __future__ import annotations

from dataclasses import replace
import hmac
from typing import Any

from codexia_manual_agent.work.contracts import (
    InvalidWorkRecordError,
    _exact_keys,
    _validate_digest,
    _validate_uuid,
)
from codexia_manual_agent.work.pilot_provider import ProviderWriteNotSubmittedError
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


class SupervisorDispatchNotSubmittedError(SupervisorPersistenceError):
    """One claimed dispatch was durably re-armed after exact no-submit proof."""


class M66RecoverableBackgroundWorkSupervisor(BackgroundWorkSupervisor):
    """M6.6 extension that durably re-arms only proven non-submitted dispatches."""

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
            error.write_may_have_been_submitted is not False
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

    def _apply_event(self, snapshot: SupervisorWorkSnapshot | None, event: Any):
        if (
            event.kind is SupervisorEventKind.EXTERNAL_OBSERVED
            and isinstance(event.payload, dict)
            and M66_DISPATCH_NOT_SUBMITTED_KEY in event.payload
        ):
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
            if not isinstance(proof["failure_kind"], str) or not proof[
                "failure_kind"
            ].strip():
                raise InvalidWorkRecordError(
                    "dispatch-not-submitted failure_kind must be non-empty text"
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
        return super()._apply_event(snapshot, event)


__all__ = [
    "M66_DISPATCH_NOT_SUBMITTED_KEY",
    "M66RecoverableBackgroundWorkSupervisor",
    "SupervisorDispatchNotSubmittedError",
]
