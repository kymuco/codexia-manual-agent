from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from hashlib import sha256
from pathlib import Path
from typing import Any, Mapping

from codexia_manual_agent.authority import (
    ActionProposal,
    AuthorizationDecision,
    AuthorizationReceipt,
)
from codexia_manual_agent.capability_core import CapabilityHostRequest
from codexia_manual_agent.domain.errors import AuthorizationConsumedError
from codexia_manual_agent.execution import (
    ProcessExecutionObservation,
    ProcessTerminationReason,
    StreamObservation,
)

PROCESS_ATTEMPT_SCHEMA_VERSION = 1
PROCESS_ATTEMPT_ADAPTER = "standalone-process-attempt-v1"


class StandaloneProcessAttemptError(RuntimeError):
    """Base failure for durable standalone process-attempt evidence."""


class StandaloneProcessAttemptIntegrityError(StandaloneProcessAttemptError):
    """Persisted attempt evidence is missing, malformed, or rebound."""


class StandaloneProcessAttemptState(StrEnum):
    AUTHORIZED_UNCONSUMED = "authorized_unconsumed"
    DENIED = "denied"
    AUTHORITY_CONSUMED = "authority_consumed"
    OBSERVED = "observed"
    REJECTED_BEFORE_CONSUME = "rejected_before_consume"
    ERROR_AFTER_CONSUME = "error_after_consume"


@dataclass(frozen=True, slots=True)
class StandaloneProcessAttemptSnapshot:
    attempt_id: str
    attempt_digest: str
    handoff_id: str
    handoff_digest: str
    need_id: str
    need_digest: str
    work_id: str
    work_digest: str
    proposal: ActionProposal
    receipt: AuthorizationReceipt
    state: StandaloneProcessAttemptState
    authority_consumed: bool
    observation: ProcessExecutionObservation | None = None
    runner_error: Mapping[str, Any] | None = None


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sha256_json(value: Any) -> str:
    return sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def process_attempt_identity(
    request: CapabilityHostRequest,
) -> tuple[str, str]:
    if not isinstance(request, CapabilityHostRequest):
        raise TypeError("request must be CapabilityHostRequest")
    handoff = request.handoff
    need = request.need.need
    attempt_id = f"standalone-process:{handoff.handoff_id}"
    attempt_digest = _sha256_json(
        {
            "schema_version": PROCESS_ATTEMPT_SCHEMA_VERSION,
            "adapter": PROCESS_ATTEMPT_ADAPTER,
            "host_id": handoff.host_id,
            "handoff_id": handoff.handoff_id,
            "handoff_digest": handoff.handoff_digest,
            "need_id": need.need_id,
            "need_digest": need.need_digest,
            "work_id": need.work_id,
            "work_digest": need.work_digest,
        }
    )
    return attempt_id, attempt_digest


def _proposal_from_dict(value: Mapping[str, Any]) -> ActionProposal:
    return ActionProposal(
        schema_version=value["schema_version"],
        proposal_id=value["proposal_id"],
        created_at=value["created_at"],
        capability=value["capability"],
        action=value["action"],
        workspace_root=value["workspace_root"],
        parameters=value["parameters"],
        summary=value["summary"],
        proposal_digest=value["proposal_digest"],
    )


def _receipt_from_dict(value: Mapping[str, Any]) -> AuthorizationReceipt:
    return AuthorizationReceipt(
        schema_version=value["schema_version"],
        receipt_id=value["receipt_id"],
        created_at=value["created_at"],
        proposal_id=value["proposal_id"],
        proposal_digest=value["proposal_digest"],
        decision=value["decision"],
        mode=value["mode"],
        source=value["source"],
        actor=value["actor"],
        reason=value["reason"],
        single_use=value["single_use"],
        receipt_digest=value["receipt_digest"],
    )


def _stream_from_dict(value: Mapping[str, Any]) -> StreamObservation:
    return StreamObservation(
        byte_count=value["byte_count"],
        sha256=value["sha256"],
        data_base64=value["data_base64"],
        truncated=value["truncated"],
        text_utf8=value["text_utf8"],
    )


def _observation_from_dict(
    value: Mapping[str, Any],
) -> ProcessExecutionObservation:
    return ProcessExecutionObservation(
        schema_version=value["schema_version"],
        observation_id=value["observation_id"],
        created_at=value["created_at"],
        proposal_id=value["proposal_id"],
        proposal_digest=value["proposal_digest"],
        receipt_id=value["receipt_id"],
        receipt_digest=value["receipt_digest"],
        execution_id=value["execution_id"],
        started=value["started"],
        pid=value["pid"],
        cwd=value["cwd"],
        resolved_executable=value["resolved_executable"],
        argv=tuple(value["argv"]),
        exit_code=value["exit_code"],
        termination_reason=ProcessTerminationReason(
            value["termination_reason"]
        ),
        duration_ms=value["duration_ms"],
        stdout=_stream_from_dict(value["stdout"]),
        stderr=_stream_from_dict(value["stderr"]),
        error=value["error"],
        observation_digest=value["observation_digest"],
    )


class SqliteStandaloneProcessAttemptStore:
    """Host-local durable evidence for one exact process attempt per handoff.

    This store is both:
      - the process-attempt journal; and
      - an AuthorizationConsumptionRegistryProtocol implementation.

    It does not own Work lifecycle, CapabilityOutcome admission, or authority
    policy. The receipt is decided before prepare(); consume() only enforces the
    existing single-use authority contract durably.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @property
    def path(self) -> Path:
        return self._path

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self._path,
            timeout=30.0,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS standalone_process_attempts (
                    attempt_id TEXT PRIMARY KEY,
                    attempt_digest TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    handoff_id TEXT NOT NULL UNIQUE,
                    handoff_digest TEXT NOT NULL,
                    need_id TEXT NOT NULL,
                    need_digest TEXT NOT NULL,
                    work_id TEXT NOT NULL,
                    work_digest TEXT NOT NULL,
                    proposal_json TEXT NOT NULL,
                    receipt_json TEXT NOT NULL,
                    receipt_id TEXT NOT NULL UNIQUE,
                    receipt_digest TEXT NOT NULL,
                    authority_consumed_at TEXT,
                    observation_json TEXT,
                    runner_error_json TEXT
                )
                """
            )

    def prepare(
        self,
        request: CapabilityHostRequest,
        *,
        proposal: ActionProposal,
        receipt: AuthorizationReceipt,
    ) -> StandaloneProcessAttemptSnapshot:
        if not isinstance(request, CapabilityHostRequest):
            raise TypeError("request must be CapabilityHostRequest")
        if not isinstance(proposal, ActionProposal):
            raise TypeError("proposal must be ActionProposal")
        if not isinstance(receipt, AuthorizationReceipt):
            raise TypeError("receipt must be AuthorizationReceipt")
        if (
            receipt.proposal_id != proposal.proposal_id
            or receipt.proposal_digest != proposal.proposal_digest
        ):
            raise StandaloneProcessAttemptIntegrityError(
                "AuthorizationReceipt is not bound to exact ActionProposal"
            )

        attempt_id, attempt_digest = process_attempt_identity(request)
        handoff = request.handoff
        need = request.need.need
        proposal_json = _canonical_json(proposal.to_dict())
        receipt_json = _canonical_json(receipt.to_dict())
        created_at = datetime.now(UTC).isoformat()

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT *
                FROM standalone_process_attempts
                WHERE attempt_id = ? OR handoff_id = ?
                """,
                (attempt_id, handoff.handoff_id),
            ).fetchone()
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO standalone_process_attempts (
                        attempt_id,
                        attempt_digest,
                        created_at,
                        handoff_id,
                        handoff_digest,
                        need_id,
                        need_digest,
                        work_id,
                        work_digest,
                        proposal_json,
                        receipt_json,
                        receipt_id,
                        receipt_digest
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        attempt_id,
                        attempt_digest,
                        created_at,
                        handoff.handoff_id,
                        handoff.handoff_digest,
                        need.need_id,
                        need.need_digest,
                        need.work_id,
                        need.work_digest,
                        proposal_json,
                        receipt_json,
                        receipt.receipt_id,
                        receipt.receipt_digest,
                    ),
                )
            else:
                expected = {
                    "attempt_id": attempt_id,
                    "attempt_digest": attempt_digest,
                    "handoff_id": handoff.handoff_id,
                    "handoff_digest": handoff.handoff_digest,
                    "need_id": need.need_id,
                    "need_digest": need.need_digest,
                    "work_id": need.work_id,
                    "work_digest": need.work_digest,
                    "proposal_json": proposal_json,
                    "receipt_json": receipt_json,
                    "receipt_id": receipt.receipt_id,
                    "receipt_digest": receipt.receipt_digest,
                }
                if any(existing[key] != value for key, value in expected.items()):
                    raise StandaloneProcessAttemptIntegrityError(
                        "Existing process attempt changed exact binding"
                    )
        return self.recover(attempt_id)

    def recover(self, attempt_id: str) -> StandaloneProcessAttemptSnapshot:
        if not isinstance(attempt_id, str) or not attempt_id:
            raise ValueError("attempt_id must be non-empty text")
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM standalone_process_attempts
                WHERE attempt_id = ?
                """,
                (attempt_id,),
            ).fetchone()
        if row is None:
            raise StandaloneProcessAttemptIntegrityError(
                f"Unknown standalone process attempt: {attempt_id}"
            )

        proposal_data = json.loads(row["proposal_json"])
        receipt_data = json.loads(row["receipt_json"])
        proposal = _proposal_from_dict(proposal_data)
        receipt = _receipt_from_dict(receipt_data)
        if (
            proposal.proposal_id != receipt.proposal_id
            or proposal.proposal_digest != receipt.proposal_digest
            or receipt.receipt_id != row["receipt_id"]
            or receipt.receipt_digest != row["receipt_digest"]
        ):
            raise StandaloneProcessAttemptIntegrityError(
                "Persisted proposal/receipt binding is corrupt"
            )

        observation = None
        if row["observation_json"] is not None:
            observation = _observation_from_dict(
                json.loads(row["observation_json"])
            )
            if (
                observation.proposal_id != proposal.proposal_id
                or observation.proposal_digest != proposal.proposal_digest
                or observation.receipt_id != receipt.receipt_id
                or observation.receipt_digest != receipt.receipt_digest
            ):
                raise StandaloneProcessAttemptIntegrityError(
                    "Persisted process observation changed authority binding"
                )

        runner_error = None
        if row["runner_error_json"] is not None:
            loaded = json.loads(row["runner_error_json"])
            if not isinstance(loaded, dict):
                raise StandaloneProcessAttemptIntegrityError(
                    "Persisted runner error is not an object"
                )
            runner_error = loaded

        consumed = row["authority_consumed_at"] is not None
        if receipt.decision is AuthorizationDecision.DENY:
            if consumed or observation is not None:
                raise StandaloneProcessAttemptIntegrityError(
                    "Denied attempt cannot consume authority or observe execution"
                )
            state = StandaloneProcessAttemptState.DENIED
        elif observation is not None:
            if not consumed or runner_error is not None:
                raise StandaloneProcessAttemptIntegrityError(
                    "Observed attempt has inconsistent durable state"
                )
            state = StandaloneProcessAttemptState.OBSERVED
        elif runner_error is not None:
            state = (
                StandaloneProcessAttemptState.ERROR_AFTER_CONSUME
                if consumed
                else StandaloneProcessAttemptState.REJECTED_BEFORE_CONSUME
            )
        elif consumed:
            state = StandaloneProcessAttemptState.AUTHORITY_CONSUMED
        else:
            state = StandaloneProcessAttemptState.AUTHORIZED_UNCONSUMED

        base = {
            "schema_version": PROCESS_ATTEMPT_SCHEMA_VERSION,
            "adapter": PROCESS_ATTEMPT_ADAPTER,
            "host_id": "standalone.local",
            "handoff_id": row["handoff_id"],
            "handoff_digest": row["handoff_digest"],
            "need_id": row["need_id"],
            "need_digest": row["need_digest"],
            "work_id": row["work_id"],
            "work_digest": row["work_digest"],
        }
        if _sha256_json(base) != row["attempt_digest"]:
            raise StandaloneProcessAttemptIntegrityError(
                "Persisted attempt digest changed exact binding"
            )

        return StandaloneProcessAttemptSnapshot(
            attempt_id=row["attempt_id"],
            attempt_digest=row["attempt_digest"],
            handoff_id=row["handoff_id"],
            handoff_digest=row["handoff_digest"],
            need_id=row["need_id"],
            need_digest=row["need_digest"],
            work_id=row["work_id"],
            work_digest=row["work_digest"],
            proposal=proposal,
            receipt=receipt,
            state=state,
            authority_consumed=consumed,
            observation=observation,
            runner_error=runner_error,
        )

    def recover_for_handoff(
        self,
        handoff_id: str,
    ) -> StandaloneProcessAttemptSnapshot | None:
        if not isinstance(handoff_id, str) or not handoff_id:
            raise ValueError("handoff_id must be non-empty text")
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT attempt_id
                FROM standalone_process_attempts
                WHERE handoff_id = ?
                """,
                (handoff_id,),
            ).fetchone()
        if row is None:
            return None
        return self.recover(str(row["attempt_id"]))

    def consume(
        self,
        receipt_id: str,
        *,
        receipt_digest: str | None = None,
        proposal_id: str | None = None,
        proposal_digest: str | None = None,
    ) -> None:
        values = {
            "receipt_id": receipt_id,
            "receipt_digest": receipt_digest,
            "proposal_id": proposal_id,
            "proposal_digest": proposal_digest,
        }
        if any(not isinstance(value, str) or not value for value in values.values()):
            raise StandaloneProcessAttemptIntegrityError(
                "Durable attempt consumption requires exact receipt/proposal binding"
            )

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT *
                FROM standalone_process_attempts
                WHERE receipt_id = ?
                """,
                (receipt_id,),
            ).fetchone()
            if row is None:
                raise StandaloneProcessAttemptIntegrityError(
                    "Authorization receipt has no durable process attempt"
                )
            proposal = _proposal_from_dict(json.loads(row["proposal_json"]))
            receipt = _receipt_from_dict(json.loads(row["receipt_json"]))
            if (
                receipt.receipt_digest != receipt_digest
                or proposal.proposal_id != proposal_id
                or proposal.proposal_digest != proposal_digest
                or receipt.proposal_id != proposal.proposal_id
                or receipt.proposal_digest != proposal.proposal_digest
            ):
                raise StandaloneProcessAttemptIntegrityError(
                    "Authorization consumption changed durable attempt binding"
                )
            if receipt.decision is not AuthorizationDecision.ALLOW:
                raise StandaloneProcessAttemptIntegrityError(
                    "Denied authorization cannot be consumed"
                )
            if row["authority_consumed_at"] is not None:
                raise AuthorizationConsumedError(
                    f"Authorization receipt already consumed: {receipt_id}"
                )
            connection.execute(
                """
                UPDATE standalone_process_attempts
                SET authority_consumed_at = ?
                WHERE attempt_id = ?
                """,
                (datetime.now(UTC).isoformat(), row["attempt_id"]),
            )

    def is_consumed(self, receipt_id: str) -> bool:
        if not isinstance(receipt_id, str) or not receipt_id:
            raise ValueError("receipt_id must be non-empty text")
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT authority_consumed_at
                FROM standalone_process_attempts
                WHERE receipt_id = ?
                """,
                (receipt_id,),
            ).fetchone()
        return row is not None and row["authority_consumed_at"] is not None

    def record_observation(
        self,
        attempt_id: str,
        observation: ProcessExecutionObservation,
    ) -> StandaloneProcessAttemptSnapshot:
        if not isinstance(observation, ProcessExecutionObservation):
            raise TypeError("observation must be ProcessExecutionObservation")
        raw = _canonical_json(observation.to_dict())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT *
                FROM standalone_process_attempts
                WHERE attempt_id = ?
                """,
                (attempt_id,),
            ).fetchone()
            if row is None:
                raise StandaloneProcessAttemptIntegrityError(
                    "Process observation has no durable attempt"
                )
            if row["authority_consumed_at"] is None:
                raise StandaloneProcessAttemptIntegrityError(
                    "Process observation precedes durable authority consumption"
                )
            proposal = _proposal_from_dict(json.loads(row["proposal_json"]))
            receipt = _receipt_from_dict(json.loads(row["receipt_json"]))
            if (
                observation.proposal_id != proposal.proposal_id
                or observation.proposal_digest != proposal.proposal_digest
                or observation.receipt_id != receipt.receipt_id
                or observation.receipt_digest != receipt.receipt_digest
            ):
                raise StandaloneProcessAttemptIntegrityError(
                    "Process observation changed durable attempt binding"
                )
            existing = row["observation_json"]
            if existing is not None:
                if existing != raw:
                    raise StandaloneProcessAttemptIntegrityError(
                        "Process attempt already has another observation"
                    )
            else:
                if row["runner_error_json"] is not None:
                    raise StandaloneProcessAttemptIntegrityError(
                        "Runner error and terminal observation cannot both be durable"
                    )
                connection.execute(
                    """
                    UPDATE standalone_process_attempts
                    SET observation_json = ?
                    WHERE attempt_id = ?
                    """,
                    (raw, attempt_id),
                )
        return self.recover(attempt_id)

    def record_runner_error(
        self,
        attempt_id: str,
        *,
        error_type: str,
        detail: str,
    ) -> StandaloneProcessAttemptSnapshot:
        if not isinstance(error_type, str) or not error_type:
            raise ValueError("error_type must be non-empty text")
        if not isinstance(detail, str) or not detail:
            raise ValueError("detail must be non-empty text")
        raw = _canonical_json(
            {
                "error_type": error_type,
                "detail": detail[:16_000],
            }
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT observation_json, runner_error_json
                FROM standalone_process_attempts
                WHERE attempt_id = ?
                """,
                (attempt_id,),
            ).fetchone()
            if row is None:
                raise StandaloneProcessAttemptIntegrityError(
                    "Runner error has no durable process attempt"
                )
            if row["observation_json"] is None:
                existing = row["runner_error_json"]
                if existing is not None:
                    if existing != raw:
                        raise StandaloneProcessAttemptIntegrityError(
                            "Process attempt already has another runner error"
                        )
                else:
                    connection.execute(
                        """
                        UPDATE standalone_process_attempts
                        SET runner_error_json = ?
                        WHERE attempt_id = ?
                        """,
                        (raw, attempt_id),
                    )
        return self.recover(attempt_id)
