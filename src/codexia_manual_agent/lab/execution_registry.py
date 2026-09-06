from __future__ import annotations

import hmac
import json
import re
import sqlite3
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from hashlib import sha256
from pathlib import Path
from typing import Any, Mapping
from uuid import UUID, uuid4

from codexia_manual_agent.lab.errors import (
    EvidenceBindingError,
    InvalidLabRecordError,
    LabIdentityConflictError,
    LabPersistenceError,
    LabPersistenceIntegrityError,
    LabRegistryStateError,
)
from codexia_manual_agent.lab.execution_evidence import (
    RunExecutionAuthorization,
    RunExecutionBinding,
    RunExecutionEvidence,
    run_execution_authorization_from_dict,
    run_execution_binding_from_dict,
    run_execution_evidence_from_dict,
)
from codexia_manual_agent.lab.registry import RegisteredRunSnapshot, SqliteLabRegistry


RUN_EXECUTION_EVENT_SCHEMA_VERSION = 1
MAX_RUN_EXECUTION_EVENT_PAYLOAD_BYTES = 2_097_152
MAX_RUN_EXECUTION_EVENT_RAW_JSON_CHARS = MAX_RUN_EXECUTION_EVENT_PAYLOAD_BYTES
MAX_RUN_EXECUTION_EVENT_TIMESTAMP_CHARS = 64

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class RunExecutionEventKind(StrEnum):
    BINDING_REGISTERED = "binding_registered"
    AUTHORIZATION_REGISTERED = "authorization_registered"
    EVIDENCE_REGISTERED = "evidence_registered"


class RunExecutionPhase(StrEnum):
    BOUND = "bound"
    AUTHORIZED = "authorized"
    OBSERVED = "observed"


def _normalize_kind(value: Any) -> RunExecutionEventKind:
    try:
        return RunExecutionEventKind(value)
    except (TypeError, ValueError) as exc:
        raise InvalidLabRecordError("Unknown M4.3.1 run execution event kind") from exc


def _validate_uuid(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise InvalidLabRecordError(f"{field_name} must be a canonical UUID")
    try:
        parsed = UUID(value)
    except (TypeError, ValueError, AttributeError) as exc:
        raise InvalidLabRecordError(f"{field_name} must be a canonical UUID") from exc
    if str(parsed) != value:
        raise InvalidLabRecordError(
            f"{field_name} must use canonical lowercase hyphenated UUID form"
        )
    return value


def _validate_digest(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise InvalidLabRecordError(f"{field_name} must be lowercase SHA-256 hex")
    return value


def _validate_timestamp(value: Any, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_RUN_EXECUTION_EVENT_TIMESTAMP_CHARS
        or "\x00" in value
    ):
        raise InvalidLabRecordError(
            f"{field_name} must be bounded canonical timezone-aware ISO-8601"
        )
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise InvalidLabRecordError(f"{field_name} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.isoformat() != value:
        raise InvalidLabRecordError(
            f"{field_name} must be canonical timezone-aware ISO-8601"
        )
    return value


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise LabPersistenceIntegrityError(
                f"Persisted run execution JSON contains duplicate key: {key}"
            )
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise LabPersistenceIntegrityError(
        f"Persisted run execution JSON contains non-finite constant: {value}"
    )


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, RecursionError) as exc:
        raise InvalidLabRecordError(
            "Run execution event payload must be bounded JSON-compatible data"
        ) from exc


def _load_canonical_json(raw: Any) -> Mapping[str, Any]:
    if not isinstance(raw, str):
        raise LabPersistenceIntegrityError("Persisted run execution JSON must be text")
    if len(raw) > MAX_RUN_EXECUTION_EVENT_RAW_JSON_CHARS:
        raise LabPersistenceIntegrityError(
            "Persisted run execution JSON exceeds the pre-parse character budget"
        )
    try:
        value = json.loads(
            raw,
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_constant,
        )
    except LabPersistenceIntegrityError:
        raise
    except (TypeError, ValueError, RecursionError, json.JSONDecodeError) as exc:
        raise LabPersistenceIntegrityError(
            "Persisted run execution event payload is not valid JSON"
        ) from exc
    if not isinstance(value, Mapping):
        raise LabPersistenceIntegrityError(
            "Persisted run execution event payload must be an object"
        )
    try:
        canonical = _canonical_json(value)
    except InvalidLabRecordError as exc:
        raise LabPersistenceIntegrityError(
            "Persisted run execution JSON violates canonical bounds"
        ) from exc
    if canonical != raw:
        raise LabPersistenceIntegrityError(
            "Persisted run execution event payload is not canonical JSON"
        )
    return value


def _exact_keys(value: Any, expected: set[str], label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise InvalidLabRecordError(f"{label} must be an object")
    actual = set(value)
    if actual != expected:
        raise InvalidLabRecordError(
            f"{label} keys mismatch; missing={sorted(expected - actual)}, "
            f"extra={sorted(actual - expected)}"
        )
    return value


def _validate_payload(
    kind: RunExecutionEventKind | str,
    payload: Mapping[str, Any],
) -> Mapping[str, Any]:
    normalized = _normalize_kind(kind)
    if normalized is RunExecutionEventKind.BINDING_REGISTERED:
        value = _exact_keys(payload, {"binding"}, normalized.value)
        run_execution_binding_from_dict(value["binding"])
    elif normalized is RunExecutionEventKind.AUTHORIZATION_REGISTERED:
        value = _exact_keys(payload, {"authorization"}, normalized.value)
        run_execution_authorization_from_dict(value["authorization"])
    elif normalized is RunExecutionEventKind.EVIDENCE_REGISTERED:
        value = _exact_keys(payload, {"evidence"}, normalized.value)
        run_execution_evidence_from_dict(value["evidence"])
    else:  # pragma: no cover - enum exhaustiveness
        raise InvalidLabRecordError("Unknown M4.3.1 run execution event kind")
    encoded = _canonical_json(payload).encode("utf-8")
    if len(encoded) > MAX_RUN_EXECUTION_EVENT_PAYLOAD_BYTES:
        raise InvalidLabRecordError("Run execution event payload exceeds M4.3.1 byte budget")
    return payload


def _event_digest(value: Mapping[str, Any]) -> str:
    return sha256(_canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class RunExecutionEventReceipt:
    schema_version: int
    event_id: str
    run_id: str
    sequence: int
    created_at: str
    kind: RunExecutionEventKind
    payload: Mapping[str, Any]
    previous_event_digest: str | None
    event_digest: str

    @classmethod
    def create(
        cls,
        *,
        run_id: str,
        sequence: int,
        kind: RunExecutionEventKind | str,
        payload: Mapping[str, Any],
        previous_event_digest: str | None,
        event_id: str | None = None,
        created_at: str | None = None,
    ) -> "RunExecutionEventReceipt":
        event_id = event_id or str(uuid4())
        created_at = created_at or datetime.now(timezone.utc).isoformat()
        normalized_kind = _normalize_kind(kind)
        _validate_uuid(event_id, "event_id")
        _validate_uuid(run_id, "run_id")
        if type(sequence) is not int or sequence < 0:
            raise InvalidLabRecordError("Run execution event sequence must be non-negative")
        _validate_timestamp(created_at, "created_at")
        if sequence == 0:
            if previous_event_digest is not None:
                raise InvalidLabRecordError("First run execution event cannot have a previous digest")
        else:
            _validate_digest(previous_event_digest, "previous_event_digest")
        _validate_payload(normalized_kind, payload)
        base = {
            "schema_version": RUN_EXECUTION_EVENT_SCHEMA_VERSION,
            "event_id": event_id,
            "run_id": run_id,
            "sequence": sequence,
            "created_at": created_at,
            "kind": normalized_kind.value,
            "payload": payload,
            "previous_event_digest": previous_event_digest,
        }
        return cls(
            schema_version=RUN_EXECUTION_EVENT_SCHEMA_VERSION,
            event_id=event_id,
            run_id=run_id,
            sequence=sequence,
            created_at=created_at,
            kind=normalized_kind,
            payload=payload,
            previous_event_digest=previous_event_digest,
            event_digest=_event_digest(base),
        )

    def __post_init__(self) -> None:
        if self.schema_version != RUN_EXECUTION_EVENT_SCHEMA_VERSION:
            raise InvalidLabRecordError("Unsupported M4.3.1 run execution event schema")
        _validate_uuid(self.event_id, "event_id")
        _validate_uuid(self.run_id, "run_id")
        if type(self.sequence) is not int or self.sequence < 0:
            raise InvalidLabRecordError("Run execution event sequence must be non-negative")
        _validate_timestamp(self.created_at, "created_at")
        kind = _normalize_kind(self.kind)
        if self.sequence == 0:
            if self.previous_event_digest is not None:
                raise InvalidLabRecordError("First run execution event cannot have a previous digest")
        else:
            _validate_digest(self.previous_event_digest, "previous_event_digest")
        _validate_payload(kind, self.payload)
        _validate_digest(self.event_digest, "event_digest")
        object.__setattr__(self, "kind", kind)
        if not hmac.compare_digest(_event_digest(self._base_payload()), self.event_digest):
            raise InvalidLabRecordError("Run execution event digest does not match payload")

    def _base_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "run_id": self.run_id,
            "sequence": self.sequence,
            "created_at": self.created_at,
            "kind": self.kind.value,
            "payload": self.payload,
            "previous_event_digest": self.previous_event_digest,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._base_payload(), "event_digest": self.event_digest}


@dataclass(slots=True)
class _RunExecutionReplayState:
    binding: RunExecutionBinding
    authorization: RunExecutionAuthorization | None = None
    evidence: RunExecutionEvidence | None = None


@dataclass(frozen=True, slots=True)
class RunExecutionRecovery:
    run: RegisteredRunSnapshot
    binding: RunExecutionBinding
    authorization: RunExecutionAuthorization | None
    evidence: RunExecutionEvidence | None
    events: tuple[RunExecutionEventReceipt, ...]

    @property
    def phase(self) -> RunExecutionPhase:
        if self.evidence is not None:
            return RunExecutionPhase.OBSERVED
        if self.authorization is not None:
            return RunExecutionPhase.AUTHORIZED
        return RunExecutionPhase.BOUND

    @property
    def execution_succeeded(self) -> bool:
        return self.evidence is not None and self.evidence.execution_succeeded


def _apply_event(
    state: _RunExecutionReplayState | None,
    kind: RunExecutionEventKind,
    payload: Mapping[str, Any],
    *,
    run: RegisteredRunSnapshot,
) -> _RunExecutionReplayState:
    value = _validate_payload(kind, payload)
    if kind is RunExecutionEventKind.BINDING_REGISTERED:
        if state is not None:
            raise LabRegistryStateError("binding_registered can appear only once")
        binding = run_execution_binding_from_dict(value["binding"])
        if binding.run.to_dict() != run.run.to_dict():
            raise EvidenceBindingError(
                "Execution binding does not bind the exact durable M4 run"
            )
        return _RunExecutionReplayState(binding=binding)

    if state is None:
        raise LabRegistryStateError("Run execution chronology must start with binding_registered")

    if kind is RunExecutionEventKind.AUTHORIZATION_REGISTERED:
        if state.authorization is not None:
            raise LabRegistryStateError("Run execution authorization is already registered")
        if state.evidence is not None:
            raise LabRegistryStateError("Authorization cannot appear after execution evidence")
        authorization = run_execution_authorization_from_dict(value["authorization"])
        if (
            authorization.binding_id != state.binding.binding_id
            or not hmac.compare_digest(
                authorization.binding_digest,
                state.binding.binding_digest,
            )
            or authorization.proposal_id != state.binding.proposal.proposal_id
            or not hmac.compare_digest(
                authorization.proposal_digest,
                state.binding.proposal.proposal_digest,
            )
        ):
            raise EvidenceBindingError(
                "Authorization does not bind the exact durable run execution binding"
            )
        state.authorization = authorization

    elif kind is RunExecutionEventKind.EVIDENCE_REGISTERED:
        if state.authorization is None:
            raise LabRegistryStateError(
                "Execution evidence requires a durable authorization event first"
            )
        if state.evidence is not None:
            raise LabRegistryStateError("Run execution evidence is already registered")
        evidence = run_execution_evidence_from_dict(value["evidence"])
        authorization = state.authorization
        binding = state.binding
        if (
            evidence.binding_id != binding.binding_id
            or not hmac.compare_digest(evidence.binding_digest, binding.binding_digest)
            or evidence.authorization_id != authorization.authorization_id
            or not hmac.compare_digest(
                evidence.authorization_digest,
                authorization.authorization_digest,
            )
            or evidence.run_id != binding.run.run_id
            or evidence.experiment_id != binding.run.experiment_id
            or not hmac.compare_digest(evidence.run_digest, binding.run.run_digest)
            or not hmac.compare_digest(
                evidence.manifest_digest,
                binding.run.manifest_digest,
            )
            or evidence.proposal_id != binding.proposal.proposal_id
            or not hmac.compare_digest(
                evidence.proposal_digest,
                binding.proposal.proposal_digest,
            )
            or evidence.receipt_id != authorization.receipt.receipt_id
            or not hmac.compare_digest(
                evidence.receipt_digest,
                authorization.receipt.receipt_digest,
            )
        ):
            raise EvidenceBindingError(
                "Execution evidence does not bind the exact durable run/authorization chain"
            )
        state.evidence = evidence

    return state


class SqliteRunExecutionRegistry:
    """M4.3.1 append-only execution chronology layered on the M4.2 registry.

    The M4.2 registry remains authoritative for durable run existence. This class
    uses the same SQLite file and never grants or consumes M2 authority; it only
    records exact objects already produced by the M2 authority/execution spine.
    """

    def __init__(self, lab_registry: SqliteLabRegistry) -> None:
        if not isinstance(lab_registry, SqliteLabRegistry):
            raise TypeError("lab_registry must be a SqliteLabRegistry")
        self._lab_registry = lab_registry
        self._database_path = lab_registry.database_path
        self._initialize()

    @property
    def database_path(self) -> Path:
        return self._database_path

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self._database_path,
            timeout=30.0,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    @contextmanager
    def _sqlite_connection(self):
        try:
            with closing(self._connect()) as connection:
                yield connection
        except sqlite3.Error as exc:
            raise LabPersistenceError("SQLite run execution registry operation failed") from exc

    def _initialize(self) -> None:
        with self._sqlite_connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS lab_run_execution_roots (
                    run_id TEXT PRIMARY KEY,
                    experiment_id TEXT NOT NULL,
                    run_digest TEXT NOT NULL,
                    manifest_digest TEXT NOT NULL,
                    head_sequence INTEGER NOT NULL,
                    head_event_digest TEXT NOT NULL,
                    bound_at TEXT NOT NULL,
                    FOREIGN KEY (run_id) REFERENCES lab_registry_runs(run_id)
                );

                CREATE TABLE IF NOT EXISTS lab_run_execution_events (
                    run_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    event_id TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    previous_event_digest TEXT,
                    event_digest TEXT NOT NULL,
                    PRIMARY KEY (run_id, sequence),
                    FOREIGN KEY (run_id) REFERENCES lab_run_execution_roots(run_id)
                );
                """
            )

    @staticmethod
    def _rollback(connection: sqlite3.Connection) -> None:
        if connection.in_transaction:
            connection.execute("ROLLBACK")

    def _durable_run(self, run_id: str, *, require_open: bool) -> RegisteredRunSnapshot:
        recovery = self._lab_registry.recover_for_run(run_id)
        snapshot = recovery.run(run_id)
        if require_open and snapshot.evidence_sealed:
            raise LabRegistryStateError(
                "Cannot extend run execution chronology after the M4 run is sealed"
            )
        return snapshot

    def register_binding(self, binding: RunExecutionBinding) -> RunExecutionRecovery:
        if not isinstance(binding, RunExecutionBinding):
            raise TypeError("binding must be a RunExecutionBinding")
        with self._sqlite_connection() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                run = self._durable_run(binding.run.run_id, require_open=True)
                if binding.run.to_dict() != run.run.to_dict():
                    raise EvidenceBindingError(
                        "Execution binding does not bind the exact durable M4 run"
                    )
                row = connection.execute(
                    "SELECT run_id FROM lab_run_execution_roots WHERE run_id = ?",
                    (binding.run.run_id,),
                ).fetchone()
                if row is not None:
                    self._load(connection, binding.run.run_id, run=run)
                    raise LabIdentityConflictError(
                        "Run already has a durable execution binding"
                    )
                receipt = RunExecutionEventReceipt.create(
                    run_id=binding.run.run_id,
                    sequence=0,
                    kind=RunExecutionEventKind.BINDING_REGISTERED,
                    payload={"binding": binding.to_dict()},
                    previous_event_digest=None,
                )
                state = _apply_event(
                    None,
                    receipt.kind,
                    receipt.payload,
                    run=run,
                )
                connection.execute(
                    """
                    INSERT INTO lab_run_execution_roots(
                        run_id, experiment_id, run_digest, manifest_digest,
                        head_sequence, head_event_digest, bound_at
                    ) VALUES (?, ?, ?, ?, 0, ?, ?)
                    """,
                    (
                        binding.run.run_id,
                        binding.run.experiment_id,
                        binding.run.run_digest,
                        binding.run.manifest_digest,
                        receipt.event_digest,
                        receipt.created_at,
                    ),
                )
                self._insert_event(connection, receipt)
                connection.execute("COMMIT")
                return self._public(run, state, (receipt,))
            except Exception:
                self._rollback(connection)
                raise

    def register_authorization(
        self,
        authorization: RunExecutionAuthorization,
    ) -> RunExecutionRecovery:
        if not isinstance(authorization, RunExecutionAuthorization):
            raise TypeError("authorization must be a RunExecutionAuthorization")
        return self._append(
            authorization.binding_id,
            RunExecutionEventKind.AUTHORIZATION_REGISTERED,
            {"authorization": authorization.to_dict()},
        )

    def register_evidence(self, evidence: RunExecutionEvidence) -> RunExecutionRecovery:
        if not isinstance(evidence, RunExecutionEvidence):
            raise TypeError("evidence must be a RunExecutionEvidence")
        _validate_uuid(evidence.run_id, "evidence.run_id")
        with self._sqlite_connection() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                run = self._durable_run(evidence.run_id, require_open=True)
                state, events = self._load(connection, evidence.run_id, run=run)
                recovery = self._append_loaded(
                    connection,
                    run=run,
                    state=state,
                    events=events,
                    kind=RunExecutionEventKind.EVIDENCE_REGISTERED,
                    payload={"evidence": evidence.to_dict()},
                )
                connection.execute("COMMIT")
                return recovery
            except Exception:
                self._rollback(connection)
                raise

    def recover(self, run_id: str) -> RunExecutionRecovery:
        _validate_uuid(run_id, "run_id")
        with self._sqlite_connection() as connection:
            try:
                connection.execute("BEGIN")
                run = self._durable_run(run_id, require_open=False)
                state, events = self._load(connection, run_id, run=run)
                connection.execute("COMMIT")
                return self._public(run, state, events)
            except Exception:
                self._rollback(connection)
                raise

    def _append(
        self,
        binding_id: str,
        kind: RunExecutionEventKind,
        payload: Mapping[str, Any],
    ) -> RunExecutionRecovery:
        _validate_uuid(binding_id, "binding_id")
        with self._sqlite_connection() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    """
                    SELECT run_id FROM lab_run_execution_events
                    WHERE sequence = 0 AND kind = ?
                    """,
                    (RunExecutionEventKind.BINDING_REGISTERED.value,),
                ).fetchall()
                matches: list[str] = []
                for candidate in row:
                    run_id = candidate["run_id"]
                    run = self._durable_run(run_id, require_open=True)
                    state, _events = self._load(connection, run_id, run=run)
                    if state.binding.binding_id == binding_id:
                        matches.append(run_id)
                if len(matches) != 1:
                    if matches:
                        raise LabPersistenceIntegrityError(
                            "Execution binding identity is not globally unique"
                        )
                    raise InvalidLabRecordError("Unknown durable run execution binding")
                run_id = matches[0]
                run = self._durable_run(run_id, require_open=True)
                state, events = self._load(connection, run_id, run=run)
                recovery = self._append_loaded(
                    connection,
                    run=run,
                    state=state,
                    events=events,
                    kind=kind,
                    payload=payload,
                )
                connection.execute("COMMIT")
                return recovery
            except Exception:
                self._rollback(connection)
                raise

    def _append_loaded(
        self,
        connection: sqlite3.Connection,
        *,
        run: RegisteredRunSnapshot,
        state: _RunExecutionReplayState,
        events: tuple[RunExecutionEventReceipt, ...],
        kind: RunExecutionEventKind,
        payload: Mapping[str, Any],
    ) -> RunExecutionRecovery:
        previous = events[-1]
        receipt = RunExecutionEventReceipt.create(
            run_id=run.run.run_id,
            sequence=previous.sequence + 1,
            kind=kind,
            payload=payload,
            previous_event_digest=previous.event_digest,
        )
        state = _apply_event(state, receipt.kind, receipt.payload, run=run)
        self._insert_event(connection, receipt)
        cursor = connection.execute(
            """
            UPDATE lab_run_execution_roots
            SET head_sequence = ?, head_event_digest = ?
            WHERE run_id = ? AND head_sequence = ? AND head_event_digest = ?
            """,
            (
                receipt.sequence,
                receipt.event_digest,
                receipt.run_id,
                previous.sequence,
                previous.event_digest,
            ),
        )
        if cursor.rowcount != 1:
            raise LabPersistenceIntegrityError(
                "Run execution root changed during append"
            )
        return self._public(run, state, events + (receipt,))

    @staticmethod
    def _insert_event(
        connection: sqlite3.Connection,
        receipt: RunExecutionEventReceipt,
    ) -> None:
        connection.execute(
            """
            INSERT INTO lab_run_execution_events(
                run_id, sequence, event_id, created_at, kind, payload_json,
                previous_event_digest, event_digest
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                receipt.run_id,
                receipt.sequence,
                receipt.event_id,
                receipt.created_at,
                receipt.kind.value,
                _canonical_json(receipt.payload),
                receipt.previous_event_digest,
                receipt.event_digest,
            ),
        )

    def _load(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        *,
        run: RegisteredRunSnapshot,
    ) -> tuple[_RunExecutionReplayState, tuple[RunExecutionEventReceipt, ...]]:
        root = connection.execute(
            "SELECT * FROM lab_run_execution_roots WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if root is None:
            raise InvalidLabRecordError("Unknown durable run execution binding")
        try:
            root_run_id = _validate_uuid(root["run_id"], "persisted root run_id")
            root_experiment_id = _validate_uuid(
                root["experiment_id"],
                "persisted root experiment_id",
            )
            root_run_digest = _validate_digest(
                root["run_digest"],
                "persisted root run_digest",
            )
            root_manifest_digest = _validate_digest(
                root["manifest_digest"],
                "persisted root manifest_digest",
            )
            head_sequence = root["head_sequence"]
            if type(head_sequence) is not int or head_sequence < 0:
                raise InvalidLabRecordError("persisted root head_sequence is invalid")
            head_digest = _validate_digest(
                root["head_event_digest"],
                "persisted root head_event_digest",
            )
            _validate_timestamp(root["bound_at"], "persisted root bound_at")
        except InvalidLabRecordError as exc:
            raise LabPersistenceIntegrityError(
                "Persisted run execution root is not canonical"
            ) from exc
        if (
            root_run_id != run.run.run_id
            or root_experiment_id != run.run.experiment_id
            or not hmac.compare_digest(root_run_digest, run.run.run_digest)
            or not hmac.compare_digest(root_manifest_digest, run.run.manifest_digest)
        ):
            raise LabPersistenceIntegrityError(
                "Run execution root disagrees with the authoritative M4 run"
            )
        rows = connection.execute(
            """
            SELECT * FROM lab_run_execution_events
            WHERE run_id = ? ORDER BY sequence ASC
            """,
            (run_id,),
        ).fetchall()
        if not rows:
            raise LabPersistenceIntegrityError("Run execution root has no event chronology")
        events: list[RunExecutionEventReceipt] = []
        state: _RunExecutionReplayState | None = None
        previous_digest: str | None = None
        for expected_sequence, row in enumerate(rows):
            try:
                sequence = row["sequence"]
                if type(sequence) is not int or sequence != expected_sequence:
                    raise InvalidLabRecordError("persisted execution sequence is not contiguous")
                event_id = _validate_uuid(row["event_id"], "persisted execution event_id")
                created_at = _validate_timestamp(
                    row["created_at"],
                    "persisted execution created_at",
                )
                kind = _normalize_kind(row["kind"])
                payload = _load_canonical_json(row["payload_json"])
                prior = row["previous_event_digest"]
                if expected_sequence == 0:
                    if prior is not None:
                        raise InvalidLabRecordError(
                            "persisted first execution event has previous digest"
                        )
                else:
                    prior = _validate_digest(
                        prior,
                        "persisted execution previous_event_digest",
                    )
                    if not hmac.compare_digest(prior, previous_digest or ""):
                        raise InvalidLabRecordError(
                            "persisted execution event chain is broken"
                        )
                event_digest = _validate_digest(
                    row["event_digest"],
                    "persisted execution event_digest",
                )
                event = RunExecutionEventReceipt(
                    schema_version=RUN_EXECUTION_EVENT_SCHEMA_VERSION,
                    event_id=event_id,
                    run_id=run_id,
                    sequence=sequence,
                    created_at=created_at,
                    kind=kind,
                    payload=payload,
                    previous_event_digest=prior,
                    event_digest=event_digest,
                )
            except InvalidLabRecordError as exc:
                raise LabPersistenceIntegrityError(
                    "Persisted run execution event is not canonical or valid"
                ) from exc
            state = _apply_event(state, event.kind, event.payload, run=run)
            events.append(event)
            previous_digest = event.event_digest
        if state is None:  # pragma: no cover - rows is non-empty
            raise LabPersistenceIntegrityError("Run execution chronology did not produce state")
        last = events[-1]
        if head_sequence != last.sequence or not hmac.compare_digest(
            head_digest,
            last.event_digest,
        ):
            raise LabPersistenceIntegrityError(
                "Run execution root head disagrees with event chronology"
            )
        return state, tuple(events)

    @staticmethod
    def _public(
        run: RegisteredRunSnapshot,
        state: _RunExecutionReplayState,
        events: tuple[RunExecutionEventReceipt, ...],
    ) -> RunExecutionRecovery:
        return RunExecutionRecovery(
            run=run,
            binding=state.binding,
            authorization=state.authorization,
            evidence=state.evidence,
            events=events,
        )
