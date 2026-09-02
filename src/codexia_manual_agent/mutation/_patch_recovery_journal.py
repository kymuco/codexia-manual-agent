from __future__ import annotations

import hmac
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from codexia_manual_agent.authority import ActionLifecycle, AuthorizationReceipt
from codexia_manual_agent.domain.errors import (
    InvalidWorkspaceMutationError,
    WorkspaceMutationBoundaryError,
)
from codexia_manual_agent.mutation.patch_application import PatchApplicationResult, PatchCommitState
from codexia_manual_agent.mutation.patch_execution_plan import PatchExecutionPlan
from codexia_manual_agent.mutation._patch_recovery_parent import (
    PinnedRecoveryJournalParent,
)
from codexia_manual_agent.mutation._patch_recovery_common import (
    MAX_RECOVERY_JOURNAL_BYTES, MAX_RECOVERY_JOURNAL_LINE_BYTES, MAX_RECOVERY_JOURNAL_RECORDS,
    PATCH_RECOVERY_JOURNAL_SCHEMA_VERSION, PatchRecoveryJournalPhase,
    _canonical_json, _digest, _require_digest, _require_timestamp, _require_uuid, _utc_now,
    _validate_executed_binding, _validated_application_result,
)

@dataclass(frozen=True, slots=True)
class PatchRecoveryJournalRecord:
    schema_version: int
    sequence: int
    created_at: str
    phase: PatchRecoveryJournalPhase
    process_id: int
    proposal_id: str
    proposal_digest: str
    authorization_receipt_id: str
    authorization_receipt_digest: str
    execution_id: str
    plan_digest: str
    change_set_digest: str
    application_result: dict[str, Any] | None
    previous_record_digest: str | None
    record_digest: str

    @classmethod
    def create(
        cls,
        *,
        sequence: int,
        phase: PatchRecoveryJournalPhase,
        process_id: int,
        proposal_id: str,
        proposal_digest: str,
        authorization_receipt_id: str,
        authorization_receipt_digest: str,
        execution_id: str,
        plan_digest: str,
        change_set_digest: str,
        application_result: PatchApplicationResult | None,
        previous_record_digest: str | None,
        created_at: str | None = None,
    ) -> "PatchRecoveryJournalRecord":
        created_at = created_at or _utc_now()
        phase = PatchRecoveryJournalPhase(phase)
        result_payload = application_result.to_dict() if application_result is not None else None
        payload = {
            "schema_version": PATCH_RECOVERY_JOURNAL_SCHEMA_VERSION,
            "sequence": sequence,
            "created_at": created_at,
            "phase": phase.value,
            "process_id": process_id,
            "proposal_id": proposal_id,
            "proposal_digest": proposal_digest,
            "authorization_receipt_id": authorization_receipt_id,
            "authorization_receipt_digest": authorization_receipt_digest,
            "execution_id": execution_id,
            "plan_digest": plan_digest,
            "change_set_digest": change_set_digest,
            "application_result": result_payload,
            "previous_record_digest": previous_record_digest,
        }
        return cls(
            schema_version=PATCH_RECOVERY_JOURNAL_SCHEMA_VERSION,
            sequence=sequence,
            created_at=created_at,
            phase=phase,
            process_id=process_id,
            proposal_id=proposal_id,
            proposal_digest=proposal_digest,
            authorization_receipt_id=authorization_receipt_id,
            authorization_receipt_digest=authorization_receipt_digest,
            execution_id=execution_id,
            plan_digest=plan_digest,
            change_set_digest=change_set_digest,
            application_result=result_payload,
            previous_record_digest=previous_record_digest,
            record_digest=_digest(payload),
        )

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "PatchRecoveryJournalRecord":
        if not isinstance(value, dict):
            raise InvalidWorkspaceMutationError("Patch recovery journal record must be an object")
        try:
            return cls(**value)
        except TypeError as exc:
            raise InvalidWorkspaceMutationError(
                "Patch recovery journal record fields are malformed"
            ) from exc

    def __post_init__(self) -> None:
        if self.schema_version != PATCH_RECOVERY_JOURNAL_SCHEMA_VERSION:
            raise InvalidWorkspaceMutationError("Unsupported patch recovery journal schema")
        if type(self.sequence) is not int or self.sequence < 0:
            raise InvalidWorkspaceMutationError("Patch recovery journal sequence is invalid")
        _require_timestamp(self.created_at, "Patch recovery journal created_at")
        object.__setattr__(self, "phase", PatchRecoveryJournalPhase(self.phase))
        if type(self.process_id) is not int or self.process_id <= 0:
            raise InvalidWorkspaceMutationError("Patch recovery process_id must be positive")
        _require_uuid(self.proposal_id, "Patch recovery journal proposal_id")
        _require_digest(self.proposal_digest, "Patch recovery journal proposal digest")
        _require_uuid(
            self.authorization_receipt_id,
            "Patch recovery journal authorization receipt_id",
        )
        _require_digest(
            self.authorization_receipt_digest,
            "Patch recovery journal authorization receipt digest",
        )
        if not isinstance(self.execution_id, str) or not self.execution_id.strip():
            raise InvalidWorkspaceMutationError("Patch recovery journal execution_id is required")
        _require_digest(self.plan_digest, "Patch recovery journal plan digest")
        _require_digest(self.change_set_digest, "Patch recovery journal change-set digest")
        if self.previous_record_digest is not None:
            _require_digest(
                self.previous_record_digest,
                "Patch recovery journal previous record digest",
            )
        if self.phase is PatchRecoveryJournalPhase.TERMINAL:
            if not isinstance(self.application_result, dict):
                raise InvalidWorkspaceMutationError(
                    "Terminal patch recovery journal record requires application result"
                )
            try:
                result = PatchApplicationResult(**self.application_result)
            except Exception as exc:
                raise InvalidWorkspaceMutationError(
                    "Patch recovery terminal application result is malformed"
                ) from exc
            if result.commit_state is PatchCommitState.INDETERMINATE:
                raise InvalidWorkspaceMutationError(
                    "Indeterminate result cannot be a terminal recovery journal record"
                )
        elif self.application_result is not None:
            raise InvalidWorkspaceMutationError(
                "Non-terminal patch recovery journal record cannot carry application result"
            )
        _require_digest(self.record_digest, "Patch recovery journal record digest")
        expected = _digest(self._payload())
        if not hmac.compare_digest(expected, self.record_digest):
            raise InvalidWorkspaceMutationError(
                "Patch recovery journal record digest does not match payload"
            )

    def _payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "sequence": self.sequence,
            "created_at": self.created_at,
            "phase": self.phase.value,
            "process_id": self.process_id,
            "proposal_id": self.proposal_id,
            "proposal_digest": self.proposal_digest,
            "authorization_receipt_id": self.authorization_receipt_id,
            "authorization_receipt_digest": self.authorization_receipt_digest,
            "execution_id": self.execution_id,
            "plan_digest": self.plan_digest,
            "change_set_digest": self.change_set_digest,
            "application_result": self.application_result,
            "previous_record_digest": self.previous_record_digest,
        }

    def to_dict(self) -> dict[str, Any]:
        payload = self._payload()
        payload["record_digest"] = self.record_digest
        return payload


@dataclass(frozen=True, slots=True)
class PatchRecoveryJournalRead:
    records: tuple[PatchRecoveryJournalRecord, ...]
    torn_tail: bool


class PatchRecoveryJournal:
    """Durable append-only recovery journal stored outside the patch workspace."""

    def __init__(self, path: Path) -> None:
        path = Path(path)
        if not path.is_absolute():
            raise InvalidWorkspaceMutationError(
                "Patch recovery journal path must be absolute"
            )
        self.path = path.resolve(strict=False)

    def _pin_parent(self, workspace_root: str | Path) -> PinnedRecoveryJournalParent:
        return PinnedRecoveryJournalParent(
            journal_path=self.path,
            workspace_root=workspace_root,
        )

    def assert_fresh(self, *, workspace_root: str | Path) -> None:
        with self._pin_parent(workspace_root) as parent:
            if parent.entry_exists():
                raise WorkspaceMutationBoundaryError(
                    "Patch recovery journal path already exists; explicit recovery "
                    "or a new journal path is required before new authority use"
                )

    def read(self, *, workspace_root: str | Path) -> PatchRecoveryJournalRead:
        with self._pin_parent(workspace_root) as parent:
            try:
                fd = parent.open_existing(writable=False)
            except FileNotFoundError as exc:
                raise WorkspaceMutationBoundaryError(
                    "Patch recovery journal does not exist"
                ) from exc
            try:
                read = self._read_open_fd(fd)
                parent.verify_parent_identity()
                return read
            finally:
                os.close(fd)

    @staticmethod
    def _read_open_fd(fd: int) -> PatchRecoveryJournalRead:
        try:
            before = os.fstat(fd)
            if before.st_size > MAX_RECOVERY_JOURNAL_BYTES:
                raise WorkspaceMutationBoundaryError(
                    "Patch recovery journal exceeds bounded read budget"
                )
            os.lseek(fd, 0, os.SEEK_SET)
            chunks: list[bytes] = []
            remaining = MAX_RECOVERY_JOURNAL_BYTES + 1
            while remaining > 0:
                chunk = os.read(fd, remaining)
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            data = b"".join(chunks)
            after = os.fstat(fd)
        except OSError as exc:
            raise WorkspaceMutationBoundaryError(
                "Patch recovery journal cannot be read securely"
            ) from exc
        if len(data) > MAX_RECOVERY_JOURNAL_BYTES:
            raise WorkspaceMutationBoundaryError(
                "Patch recovery journal exceeds bounded read budget"
            )
        if (
            before.st_dev != after.st_dev
            or before.st_ino != after.st_ino
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
            or len(data) != after.st_size
        ):
            raise WorkspaceMutationBoundaryError(
                "Patch recovery journal changed while it was being read"
            )
        if not data:
            raise WorkspaceMutationBoundaryError("Patch recovery journal is empty")

        torn_tail = not data.endswith(b"\n")
        parts = data.split(b"\n")
        if torn_tail:
            parts = parts[:-1]
        elif parts and parts[-1] == b"":
            parts = parts[:-1]
        if len(parts) > MAX_RECOVERY_JOURNAL_RECORDS:
            raise WorkspaceMutationBoundaryError(
                "Patch recovery journal exceeds bounded record count"
            )

        records: list[PatchRecoveryJournalRecord] = []
        for index, raw in enumerate(parts):
            if not raw:
                raise InvalidWorkspaceMutationError(
                    "Patch recovery journal contains an empty complete record"
                )
            if len(raw) > MAX_RECOVERY_JOURNAL_LINE_BYTES:
                raise WorkspaceMutationBoundaryError(
                    "Patch recovery journal record exceeds line budget"
                )
            try:
                decoded = raw.decode("utf-8")
                value = json.loads(decoded)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise InvalidWorkspaceMutationError(
                    "Patch recovery journal complete record is corrupt"
                ) from exc
            record = PatchRecoveryJournalRecord.from_dict(value)
            expected_previous = records[-1].record_digest if records else None
            if record.sequence != index or record.previous_record_digest != expected_previous:
                raise InvalidWorkspaceMutationError(
                    "Patch recovery journal hash chain or sequence is invalid"
                )
            records.append(record)
        if not records:
            raise WorkspaceMutationBoundaryError(
                "Patch recovery journal has no complete durable records"
            )
        return PatchRecoveryJournalRead(tuple(records), torn_tail)

    @staticmethod
    def _append_open_fd(fd: int, record: PatchRecoveryJournalRecord) -> None:
        line = (_canonical_json(record.to_dict()) + "\n").encode("utf-8")
        if len(line) > MAX_RECOVERY_JOURNAL_LINE_BYTES:
            raise WorkspaceMutationBoundaryError(
                "Patch recovery journal record exceeds line budget"
            )
        try:
            before = os.fstat(fd)
        except OSError as exc:
            raise WorkspaceMutationBoundaryError(
                "Patch recovery journal handle cannot be inspected before append"
            ) from exc
        if before.st_size + len(line) > MAX_RECOVERY_JOURNAL_BYTES:
            raise WorkspaceMutationBoundaryError(
                "Patch recovery journal append would exceed bounded budget"
            )
        try:
            os.lseek(fd, 0, os.SEEK_END)
            view = memoryview(line)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("Patch recovery journal append made no progress")
                view = view[written:]
            os.fsync(fd)
            after = os.fstat(fd)
        except OSError as exc:
            raise WorkspaceMutationBoundaryError(
                "Patch recovery journal append failed"
            ) from exc
        if (
            before.st_dev != after.st_dev
            or before.st_ino != after.st_ino
            or after.st_size != before.st_size + len(line)
        ):
            raise WorkspaceMutationBoundaryError(
                "Patch recovery journal identity changed during append"
            )

    def append_phase(
        self,
        *,
        lifecycle: ActionLifecycle,
        plan: PatchExecutionPlan,
        phase: PatchRecoveryJournalPhase,
        application_result: PatchApplicationResult | None = None,
    ) -> PatchRecoveryJournalRecord:
        receipt = _validate_executed_binding(lifecycle, plan)
        result = None
        if application_result is not None:
            result = _validated_application_result(application_result)
            if result.execution_id != lifecycle.execution_id:
                raise InvalidWorkspaceMutationError(
                    "Patch recovery terminal result execution_id mismatch"
                )
        if phase is PatchRecoveryJournalPhase.TERMINAL and result is None:
            raise InvalidWorkspaceMutationError(
                "Terminal patch recovery journal record requires application result"
            )
        if phase is not PatchRecoveryJournalPhase.TERMINAL and result is not None:
            raise InvalidWorkspaceMutationError(
                "Non-terminal patch recovery journal record cannot carry application result"
            )
        with self._pin_parent(plan.workspace_root) as parent:
            existing: tuple[PatchRecoveryJournalRecord, ...] = ()
            created = False
            try:
                fd = parent.open_existing(writable=True)
            except FileNotFoundError:
                if phase is not PatchRecoveryJournalPhase.EXECUTION_STARTED:
                    raise WorkspaceMutationBoundaryError(
                        "Patch recovery journal must begin with EXECUTION_STARTED"
                    )
                try:
                    fd = parent.create_new()
                except FileExistsError as exc:
                    raise WorkspaceMutationBoundaryError(
                        "Patch recovery journal first-record creation raced with another writer"
                    ) from exc
                created = True
            try:
                if not created:
                    read = self._read_open_fd(fd)
                    if read.torn_tail:
                        raise WorkspaceMutationBoundaryError(
                            "Cannot append to patch recovery journal with a torn tail"
                        )
                    existing = read.records
                if len(existing) >= MAX_RECOVERY_JOURNAL_RECORDS:
                    raise WorkspaceMutationBoundaryError(
                        "Patch recovery journal record budget is exhausted"
                    )
                if existing:
                    _validate_journal_binding(existing, lifecycle, plan, receipt)
                    last = existing[-1]
                    if last.process_id != os.getpid():
                        raise WorkspaceMutationBoundaryError(
                            "A different process cannot append to an existing "
                            "patch recovery journal"
                        )
                    if last.phase is PatchRecoveryJournalPhase.TERMINAL:
                        raise WorkspaceMutationBoundaryError(
                            "Patch recovery journal already has a terminal record"
                        )
                    allowed = {
                        PatchRecoveryJournalPhase.EXECUTION_STARTED:
                        PatchRecoveryJournalPhase.COMMIT_INTENT,
                        PatchRecoveryJournalPhase.COMMIT_INTENT:
                        PatchRecoveryJournalPhase.TERMINAL,
                    }
                    expected = allowed.get(last.phase)
                    if (
                        phase is PatchRecoveryJournalPhase.TERMINAL
                        and application_result is not None
                        and last.phase is PatchRecoveryJournalPhase.EXECUTION_STARTED
                    ):
                        expected = PatchRecoveryJournalPhase.TERMINAL
                    if phase is not expected:
                        raise WorkspaceMutationBoundaryError(
                            "Invalid patch recovery journal transition "
                            f"{last.phase.value} -> {phase.value}"
                        )
                elif phase is not PatchRecoveryJournalPhase.EXECUTION_STARTED:
                    raise WorkspaceMutationBoundaryError(
                        "Patch recovery journal must begin with EXECUTION_STARTED"
                    )
                record = PatchRecoveryJournalRecord.create(
                    sequence=len(existing),
                    phase=phase,
                    process_id=os.getpid(),
                    proposal_id=plan.proposal_id,
                    proposal_digest=plan.proposal_digest,
                    authorization_receipt_id=receipt.receipt_id,
                    authorization_receipt_digest=receipt.receipt_digest,
                    execution_id=lifecycle.execution_id,
                    plan_digest=plan.plan_digest,
                    change_set_digest=plan.change_set_digest,
                    application_result=result,
                    previous_record_digest=(
                        existing[-1].record_digest if existing else None
                    ),
                )
                parent.verify_parent_identity()
                self._append_open_fd(fd, record)
                parent.verify_parent_identity()
                return record
            finally:
                os.close(fd)

def _validate_journal_binding(
    records: tuple[PatchRecoveryJournalRecord, ...],
    lifecycle: ActionLifecycle,
    plan: PatchExecutionPlan,
    receipt: AuthorizationReceipt,
) -> None:
    for record in records:
        record.__post_init__()
        if (
            record.proposal_id != plan.proposal_id
            or record.proposal_digest != plan.proposal_digest
            or record.authorization_receipt_id != receipt.receipt_id
            or record.authorization_receipt_digest != receipt.receipt_digest
            or record.execution_id != lifecycle.execution_id
            or record.plan_digest != plan.plan_digest
            or record.change_set_digest != plan.change_set_digest
        ):
            raise InvalidWorkspaceMutationError(
                "Patch recovery journal is not bound to this lifecycle and execution plan"
            )
    process_ids = {record.process_id for record in records}
    if len(process_ids) != 1:
        raise InvalidWorkspaceMutationError(
            "Patch recovery journal records must belong to one execution process"
        )
    phases = tuple(record.phase for record in records)
    if not phases or phases[0] is not PatchRecoveryJournalPhase.EXECUTION_STARTED:
        raise InvalidWorkspaceMutationError(
            "Patch recovery journal must begin with EXECUTION_STARTED"
        )
    if len(phases) > 3:
        raise InvalidWorkspaceMutationError("Patch recovery journal has too many state records")
    valid = {
        (PatchRecoveryJournalPhase.EXECUTION_STARTED,),
        (
            PatchRecoveryJournalPhase.EXECUTION_STARTED,
            PatchRecoveryJournalPhase.COMMIT_INTENT,
        ),
        (
            PatchRecoveryJournalPhase.EXECUTION_STARTED,
            PatchRecoveryJournalPhase.TERMINAL,
        ),
        (
            PatchRecoveryJournalPhase.EXECUTION_STARTED,
            PatchRecoveryJournalPhase.COMMIT_INTENT,
            PatchRecoveryJournalPhase.TERMINAL,
        ),
    }
    if phases not in valid:
        raise InvalidWorkspaceMutationError(
            "Patch recovery journal phase sequence is invalid"
        )
