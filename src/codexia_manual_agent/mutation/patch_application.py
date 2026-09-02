from __future__ import annotations

import ctypes
import os
from contextlib import ExitStack
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from codexia_manual_agent.authority import (
    ActionLifecycle,
    ActionPhase,
    ActionProposal,
    LocalApprovalAuthority,
)
from codexia_manual_agent.domain.capabilities import Capability
from codexia_manual_agent.domain.errors import (
    InvalidActionTransitionError,
    InvalidWorkspaceMutationError,
    WorkspaceMutationBoundaryError,
    WorkspaceMutationPreimageChangedError,
)
from codexia_manual_agent.mutation.models import MutationOperation, PreimageState
from codexia_manual_agent.mutation.parent_anchor import PinnedMutationTarget, _StagedFile
from codexia_manual_agent.mutation.patch_execution_plan import (
    PatchExecutionPlan,
    PatchExecutionStep,
    preflight_patch_execution_plan,
    revalidate_patch_execution_plan,
    validate_patch_execution_plan_binding,
)
from codexia_manual_agent.mutation.preflight_executor import _is_windows_host
from codexia_manual_agent.mutation.windows_metadata import (
    WindowsReplaceMetadata,
    apply_windows_replace_metadata_fd,
    capture_windows_replace_metadata_fd,
)
from codexia_manual_agent.mutation.windows_txf import (
    PinnedTxFReplaceTarget,
    WindowsTxFTransaction,
    create_metadata_stage,
    create_transaction,
    pin_exact_replace_target,
    require_windows_txf_support,
    move_replace_staged,
)
from codexia_manual_agent.mutation.workspace import _MAX_PREIMAGE_BYTES, _validate_proposal

PATCH_APPLICATION_SCHEMA_VERSION = 1
PATCH_COMMIT_MODEL = "windows.txf.single_transaction.v1"


class PatchCommitState(str, Enum):
    """Filesystem commit outcome known at the M2.4.3 boundary."""

    COMMITTED = "committed"
    ROLLED_BACK = "rolled_back"
    INDETERMINATE = "indeterminate"


class PatchFailureStage(str, Enum):
    """Stage at which a post-consumption application failure stopped progress."""

    STAGING = "staging"
    REVALIDATION = "per_step_revalidation"
    PUBLISH = "publish"
    HANDLE_CLEANUP = "handle_cleanup"
    COMMIT = "commit"
    ROLLBACK = "rollback"
    TRANSACTION_CLEANUP = "transaction_cleanup"


@dataclass(frozen=True, slots=True)
class PatchApplicationResult:
    """Operational M2.4.3 result, deliberately not mutation evidence.

    M2.4.4 owns digest-bound per-file/set observations and the transition from
    ActionPhase.EXECUTED to ActionPhase.OBSERVED. This object only states the
    transaction outcome known to the application boundary.
    """

    schema_version: int
    execution_id: str
    proposal_id: str
    proposal_digest: str
    change_set_digest: str
    plan_digest: str
    commit_model: str
    commit_state: PatchCommitState
    failed_step_index: int | None = None
    failed_target: str | None = None
    failure_stage: PatchFailureStage | None = None
    error: str | None = None
    cleanup_error: str | None = None

    def __post_init__(self) -> None:
        if self.schema_version != PATCH_APPLICATION_SCHEMA_VERSION:
            raise InvalidWorkspaceMutationError("Unsupported patch application result schema")
        if not isinstance(self.execution_id, str) or not self.execution_id.strip():
            raise InvalidWorkspaceMutationError("Patch application execution_id is required")
        try:
            UUID(self.proposal_id)
        except (TypeError, ValueError, AttributeError) as exc:
            raise InvalidWorkspaceMutationError(
                "Patch application proposal_id must be a UUID"
            ) from exc
        for label, value in (
            ("proposal digest", self.proposal_digest),
            ("change-set digest", self.change_set_digest),
            ("plan digest", self.plan_digest),
        ):
            if not isinstance(value, str) or len(value) != 64:
                raise InvalidWorkspaceMutationError(
                    f"Patch application {label} must be a SHA-256 hex digest"
                )
            try:
                int(value, 16)
            except ValueError as exc:
                raise InvalidWorkspaceMutationError(
                    f"Patch application {label} must be a SHA-256 hex digest"
                ) from exc
        if self.commit_model != PATCH_COMMIT_MODEL:
            raise InvalidWorkspaceMutationError("Unsupported patch application commit model")
        try:
            object.__setattr__(self, "commit_state", PatchCommitState(self.commit_state))
        except (TypeError, ValueError) as exc:
            raise InvalidWorkspaceMutationError(
                "Unsupported patch application commit state"
            ) from exc
        if self.failure_stage is not None:
            try:
                object.__setattr__(self, "failure_stage", PatchFailureStage(self.failure_stage))
            except (TypeError, ValueError) as exc:
                raise InvalidWorkspaceMutationError(
                    "Unsupported patch application failure stage"
                ) from exc
        if self.failed_step_index is not None and (
            type(self.failed_step_index) is not int or self.failed_step_index < 0
        ):
            raise InvalidWorkspaceMutationError(
                "Patch application failed_step_index must be non-negative"
            )
        if (self.failed_step_index is None) != (self.failed_target is None):
            raise InvalidWorkspaceMutationError(
                "Patch application failed step index/target must be present together"
            )
        if self.failed_target is not None and (
            not isinstance(self.failed_target, str) or not self.failed_target
        ):
            raise InvalidWorkspaceMutationError(
                "Patch application failed_target must be non-empty text"
            )
        for label, value in (("error", self.error), ("cleanup_error", self.cleanup_error)):
            if value is not None and (not isinstance(value, str) or not value):
                raise InvalidWorkspaceMutationError(
                    f"Patch application {label} must be non-empty text when present"
                )

        if self.commit_state is PatchCommitState.COMMITTED:
            if self.failed_step_index is not None or self.error is not None:
                raise InvalidWorkspaceMutationError(
                    "Committed patch result cannot carry an application failure"
                )
            if self.cleanup_error is None:
                if self.failure_stage is not None:
                    raise InvalidWorkspaceMutationError(
                        "Clean committed patch result cannot carry a failure stage"
                    )
            elif self.failure_stage is not PatchFailureStage.TRANSACTION_CLEANUP:
                raise InvalidWorkspaceMutationError(
                    "Committed cleanup failure must be transaction cleanup"
                )
        else:
            if self.failure_stage is None or self.error is None:
                raise InvalidWorkspaceMutationError(
                    "Non-committed patch result must classify its failure"
                )

    @property
    def committed(self) -> bool:
        return self.commit_state is PatchCommitState.COMMITTED

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "execution_id": self.execution_id,
            "proposal_id": self.proposal_id,
            "proposal_digest": self.proposal_digest,
            "change_set_digest": self.change_set_digest,
            "plan_digest": self.plan_digest,
            "commit_model": self.commit_model,
            "commit_state": self.commit_state.value,
            "failed_step_index": self.failed_step_index,
            "failed_target": self.failed_target,
            "failure_stage": self.failure_stage.value if self.failure_stage else None,
            "error": self.error,
            "cleanup_error": self.cleanup_error,
        }


@dataclass(slots=True)
class _RuntimeStep:
    step: PatchExecutionStep
    m23_plan: Any
    pinned: PinnedMutationTarget | None = None
    replace_target: PinnedTxFReplaceTarget | None = None
    metadata: WindowsReplaceMetadata | None = None
    staged: _StagedFile | None = None


class _PatchStepFailure(Exception):
    def __init__(
        self,
        runtime: _RuntimeStep,
        stage: PatchFailureStage,
        cause: BaseException,
    ) -> None:
        super().__init__(f"{type(cause).__name__}: {cause}")
        self.runtime = runtime
        self.stage = stage
        self.cause = cause


def _runtime_step(plan: PatchExecutionPlan, step: PatchExecutionStep) -> _RuntimeStep:
    """Re-parse the exact step through the accepted M2.3 structural validator."""

    step.validate_primitive_digest(workspace_root=plan.workspace_root)
    structural = ActionProposal.create(
        capability=Capability.WRITE_WORKSPACE,
        action=step.action,
        workspace_root=plan.workspace_root,
        parameters=step.m23_parameters(),
        summary=f"M2.4.3 internal structural step {step.index}; not an authority proposal.",
    )
    return _RuntimeStep(step=step, m23_plan=_validate_proposal(structural))


def _move_create_staged(
    transaction: WindowsTxFTransaction,
    staged: _StagedFile,
    target: Path,
) -> None:
    """Publish one transacted CREATE without replace semantics.

    This is the multi-file counterpart of the accepted M2.3 no-clobber CREATE:
    the final move omits MOVEFILE_REPLACE_EXISTING, so a target that appears after
    authority consumption fails the whole transaction instead of being clobbered.
    """

    if os.name != "nt":
        raise WorkspaceMutationBoundaryError(
            "M2.4.3 transacted create publish is available only on Windows"
        )
    if staged.fd < 0 or staged.token is None:
        raise WorkspaceMutationBoundaryError("Transacted staged CREATE is not publishable")

    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    move = kernel32.MoveFileTransactedW
    move.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    move.restype = wintypes.BOOL
    MOVEFILE_WRITE_THROUGH = 0x00000008
    if not move(
        staged.token,
        str(target),
        None,
        None,
        MOVEFILE_WRITE_THROUGH,
        wintypes.HANDLE(transaction.handle),
    ):
        error = ctypes.get_last_error()
        if error in {80, 183}:
            raise FileExistsError(error, "Patch CREATE target appeared before commit", str(target))
        raise WorkspaceMutationBoundaryError(
            f"Windows TxF create move failed closed (winerror={error})"
        )
    staged.token = None


def _close_metadata(runtime: _RuntimeStep) -> str | None:
    if runtime.metadata is None:
        return None
    try:
        runtime.metadata.close()
    except OSError as exc:
        return f"metadata cleanup failed: {type(exc).__name__}: {exc}"
    runtime.metadata = None
    return None


def _close_replace_target(runtime: _RuntimeStep) -> str | None:
    if runtime.replace_target is None:
        return None
    try:
        runtime.replace_target.close()
    except OSError as exc:
        return f"replace target pin cleanup failed: {type(exc).__name__}: {exc}"
    runtime.replace_target = None
    return None


def _close_staged(runtime: _RuntimeStep) -> str | None:
    if runtime.staged is None or runtime.pinned is None:
        return None
    try:
        runtime.pinned.close_staged(runtime.staged)
    except OSError as exc:
        return f"staging handle cleanup failed: {type(exc).__name__}: {exc}"
    runtime.staged = None
    return None


def _join_errors(*errors: str | None) -> str | None:
    items = [item for item in errors if item]
    return "; ".join(items) if items else None


@dataclass(frozen=True, slots=True)
class _RetainedTransactionBinding:
    transaction: WindowsTxFTransaction
    execution_id: str
    proposal_id: str
    proposal_digest: str
    plan_digest: str
    change_set_digest: str

    def matches(
        self,
        *,
        transaction: WindowsTxFTransaction,
        execution_id: str,
        plan: PatchExecutionPlan,
    ) -> bool:
        return (
            self.transaction is transaction
            and self.execution_id == execution_id
            and self.proposal_id == plan.proposal_id
            and self.proposal_digest == plan.proposal_digest
            and self.plan_digest == plan.plan_digest
            and self.change_set_digest == plan.change_set_digest
        )


class PatchApplicationExecutor:
    """Apply one bounded patch as one Windows TxF transaction.

    The executor consumes exactly the patch proposal receipt. It never fabricates
    or consumes per-file authority receipts. Every file is nevertheless re-parsed
    through the accepted M2.3 structural schema and retains M2.3 parent/preimage,
    no-clobber, exact-replace, staging-identity and metadata-preservation checks.
    """

    def __init__(self) -> None:
        self._retained_transactions: list[WindowsTxFTransaction] = []
        self._retained_transaction_bindings: dict[
            int, _RetainedTransactionBinding
        ] = {}

    def _bind_transaction_to_execution(
        self,
        transaction: WindowsTxFTransaction,
        *,
        execution_id: str,
        plan: PatchExecutionPlan,
    ) -> None:
        self._retained_transaction_bindings[id(transaction)] = (
            _RetainedTransactionBinding(
                transaction=transaction,
                execution_id=execution_id,
                proposal_id=plan.proposal_id,
                proposal_digest=plan.proposal_digest,
                plan_digest=plan.plan_digest,
                change_set_digest=plan.change_set_digest,
            )
        )

    def _retained_transaction_binding(
        self,
        transaction: WindowsTxFTransaction,
    ) -> _RetainedTransactionBinding | None:
        binding = self._retained_transaction_bindings.get(id(transaction))
        if binding is None or binding.transaction is not transaction:
            return None
        return binding

    def _forget_transaction_binding(
        self,
        transaction: WindowsTxFTransaction,
    ) -> None:
        binding = self._retained_transaction_bindings.get(id(transaction))
        if binding is not None and binding.transaction is transaction:
            self._retained_transaction_bindings.pop(id(transaction), None)

    def _retry_retained_transaction_cleanup(self) -> None:
        if not self._retained_transactions:
            return
        retained = self._retained_transactions
        self._retained_transactions = []
        errors: list[str] = []
        for transaction in retained:
            if not transaction.finished:
                # An unfinished retained transaction represents an unresolved
                # mutation outcome. M2.4.5 must reconcile it; do not silently
                # roll it back while admitting a new patch.
                self._retained_transactions.append(transaction)
                errors.append("unfinished retained TxF transaction requires recovery")
                continue
            try:
                transaction.close()
            except OSError as exc:
                self._retained_transactions.append(transaction)
                errors.append(f"transaction handle cleanup failed: {type(exc).__name__}: {exc}")
            else:
                self._forget_transaction_binding(transaction)
        if self._retained_transactions:
            raise WorkspaceMutationBoundaryError(
                "Previous M2.4.3 transaction cleanup/recovery remains unresolved; "
                "authorization was not consumed. " + "; ".join(errors)
            )

    def _prepare_runtime_steps(self, plan: PatchExecutionPlan) -> list[_RuntimeStep]:
        return [_runtime_step(plan, step) for step in plan.steps]

    def _require_atomic_commit_support(self, runtimes: list[_RuntimeStep]) -> None:
        # M2.4.2 proves the existing primitive support boundary (including TxF
        # for REPLACE). M2.4.3 strengthens the chosen commit model: CREATE also
        # participates in the same TxF transaction, so every existing parent
        # must live on a writable local NTFS volume with transactions enabled.
        # Repeated targets under one parent share the same bounded support probe.
        checked: set[str] = set()
        for runtime in runtimes:
            parent = runtime.m23_plan.parent
            key = os.path.normcase(os.path.abspath(str(parent)))
            if key in checked:
                continue
            require_windows_txf_support(parent)
            checked.add(key)

    def _admit_transaction_targets(
        self,
        *,
        transaction: WindowsTxFTransaction,
        runtimes: list[_RuntimeStep],
        stack: ExitStack,
    ) -> None:
        for runtime in runtimes:
            m23 = runtime.m23_plan
            pinned = stack.enter_context(
                PinnedMutationTarget(
                    root=m23.root,
                    parent=m23.parent,
                    target_name=m23.target_path.name,
                )
            )
            runtime.pinned = pinned
            pinned.verify_parent_identity()

            if runtime.step.operation is MutationOperation.CREATE:
                observed = pinned.capture_preimage(max_bytes=_MAX_PREIMAGE_BYTES)
                if observed.state is not PreimageState.ABSENT or observed != m23.expected_preimage:
                    raise WorkspaceMutationPreimageChangedError(
                        f"Patch CREATE target changed before authorization consumption: {m23.target}"
                    )
                continue

            replace_target = pin_exact_replace_target(
                transaction,
                m23.target_path,
                max_bytes=_MAX_PREIMAGE_BYTES,
            )
            if replace_target is None:
                raise WorkspaceMutationPreimageChangedError(
                    f"Patch REPLACE target disappeared before authorization consumption: {m23.target}"
                )
            runtime.replace_target = replace_target
            if replace_target.snapshot != m23.expected_preimage:
                raise WorkspaceMutationPreimageChangedError(
                    f"Patch REPLACE target changed before authorization consumption: {m23.target}"
                )
            runtime.metadata = capture_windows_replace_metadata_fd(
                replace_target.fd,
                expected_path=m23.target_path,
            )
            pinned.verify_parent_identity()

        # Final parent/absence check immediately before authority burn. Exact
        # REPLACE objects remain pinned transactionally; CREATE absence can still
        # race after this point, but the no-clobber transactional publish converts
        # that race into whole-transaction rollback.
        for runtime in runtimes:
            assert runtime.pinned is not None
            runtime.pinned.verify_parent_identity()
            if runtime.step.operation is MutationOperation.CREATE:
                observed = runtime.pinned.capture_preimage(max_bytes=_MAX_PREIMAGE_BYTES)
                if observed != runtime.m23_plan.expected_preimage:
                    raise WorkspaceMutationPreimageChangedError(
                        f"Patch CREATE target changed at final pre-consumption pin: "
                        f"{runtime.m23_plan.target}"
                    )

    def _stage_and_publish(
        self,
        *,
        transaction: WindowsTxFTransaction,
        runtimes: list[_RuntimeStep],
    ) -> None:
        for runtime in runtimes:
            m23 = runtime.m23_plan
            pinned = runtime.pinned
            assert pinned is not None
            try:
                runtime.staged = create_metadata_stage(
                    transaction,
                    pinned,
                    m23.postimage,
                    mode=(m23.expected_preimage.mode or 0o644),
                )
                pinned.verify_parent_identity()
                pinned.verify_staged_identity(runtime.staged)
            except Exception as exc:
                raise _PatchStepFailure(runtime, PatchFailureStage.STAGING, exc) from exc

            if runtime.step.operation is MutationOperation.REPLACE:
                try:
                    assert runtime.replace_target is not None
                    assert runtime.metadata is not None
                    current = capture_windows_replace_metadata_fd(
                        runtime.replace_target.fd,
                        expected_path=m23.target_path,
                    )
                    try:
                        if current.binding != runtime.metadata.binding:
                            raise WorkspaceMutationPreimageChangedError(
                                "Replace target security metadata changed before patch commit"
                            )
                    finally:
                        current.close()
                    apply_windows_replace_metadata_fd(runtime.staged.fd, runtime.metadata)
                    pinned.verify_parent_identity()
                    pinned.verify_staged_identity(runtime.staged)
                except Exception as exc:
                    raise _PatchStepFailure(
                        runtime,
                        PatchFailureStage.REVALIDATION,
                        exc,
                    ) from exc

            try:
                if runtime.step.operation is MutationOperation.CREATE:
                    _move_create_staged(transaction, runtime.staged, m23.target_path)
                else:
                    move_replace_staged(transaction, runtime.staged, m23.target_path)
            except Exception as exc:
                raise _PatchStepFailure(runtime, PatchFailureStage.PUBLISH, exc) from exc

            cleanup = _join_errors(
                _close_staged(runtime),
                _close_replace_target(runtime),
                _close_metadata(runtime),
            )
            if cleanup:
                raise _PatchStepFailure(
                    runtime,
                    PatchFailureStage.HANDLE_CLEANUP,
                    OSError(cleanup),
                )

    def _rollback_after_failure(
        self,
        transaction: WindowsTxFTransaction,
    ) -> tuple[PatchCommitState, PatchFailureStage | None, str | None]:
        try:
            transaction.rollback()
        except Exception as exc:
            # Keep the active transaction handle alive and block future execution
            # until M2.4.5 recovery/reconciliation exists.
            self._retained_transactions.append(transaction)
            return (
                PatchCommitState.INDETERMINATE,
                PatchFailureStage.ROLLBACK,
                f"rollback failed: {type(exc).__name__}: {exc}",
            )

        try:
            transaction.close()
        except OSError as exc:
            self._retained_transactions.append(transaction)
            return (
                PatchCommitState.ROLLED_BACK,
                PatchFailureStage.TRANSACTION_CLEANUP,
                f"rolled-back transaction handle cleanup failed: {type(exc).__name__}: {exc}",
            )
        self._forget_transaction_binding(transaction)
        return PatchCommitState.ROLLED_BACK, None, None

    def execute(
        self,
        lifecycle: ActionLifecycle,
        plan: PatchExecutionPlan,
        *,
        authority: LocalApprovalAuthority,
    ) -> PatchApplicationResult:
        if lifecycle.phase is not ActionPhase.AUTHORIZED:
            raise InvalidActionTransitionError("Patch application requires AUTHORIZED lifecycle")
        if lifecycle.authorization is None:
            raise InvalidActionTransitionError("Authorized patch application has no receipt")
        if not _is_windows_host():
            raise WorkspaceMutationBoundaryError(
                "M2.4.3 patch application remains disabled outside Windows"
            )

        validate_patch_execution_plan_binding(lifecycle.proposal, plan)
        self._retry_retained_transaction_cleanup()
        runtimes = self._prepare_runtime_steps(plan)
        preflight_patch_execution_plan(lifecycle.proposal, plan)
        self._require_atomic_commit_support(runtimes)
        revalidate_patch_execution_plan(lifecycle.proposal, plan)

        transaction: WindowsTxFTransaction | None = None
        with ExitStack() as stack:
            try:
                transaction = create_transaction()
                self._admit_transaction_targets(
                    transaction=transaction,
                    runtimes=runtimes,
                    stack=stack,
                )
            except BaseException:
                # Everything above is pre-authority. Close any exact-target and
                # metadata handles, then roll back/close the inspection-only
                # transaction before the failure escapes. The patch receipt must
                # remain untouched.
                for runtime in runtimes:
                    _close_staged(runtime)
                    _close_replace_target(runtime)
                    _close_metadata(runtime)
                if transaction is not None:
                    state, _, _ = self._rollback_after_failure(transaction)
                    if state is PatchCommitState.INDETERMINATE:
                        raise WorkspaceMutationBoundaryError(
                            "Pre-authority TxF rollback became indeterminate; "
                            "authorization was not consumed and recovery is required"
                        )
                raise

            execution_id = uuid4().hex
            try:
                lifecycle.consume_authorization(authority=authority)
            except BaseException as exc:
                # Receipt consumption is process-wide and atomic; a replay/race can
                # still reject at this exact point even after all read-only admission
                # succeeded. Tear down the inspection-only transaction before the
                # authority failure escapes.
                cleanup_errors: list[str] = []
                for runtime in runtimes:
                    cleanup = _join_errors(
                        _close_staged(runtime),
                        _close_replace_target(runtime),
                        _close_metadata(runtime),
                    )
                    if cleanup:
                        cleanup_errors.append(cleanup)
                state, _, rollback_error = self._rollback_after_failure(transaction)
                transaction = None
                if state is PatchCommitState.INDETERMINATE:
                    detail = _join_errors(*cleanup_errors, rollback_error)
                    raise WorkspaceMutationBoundaryError(
                        "Patch authority consumption failed and the inspection-only "
                        "transaction could not be proven rolled back; recovery is required. "
                        f"{detail or ''}"
                    ) from exc
                raise

            # This transition is deterministic after successful consumption: the
            # lifecycle proposal/mode are immutable and execution_id is locally
            # generated. If an invariant violation still occurs, no filesystem
            # commit is allowed to follow it.
            try:
                lifecycle.record_executed(execution_id)
            except BaseException:
                for runtime in runtimes:
                    _close_staged(runtime)
                    _close_replace_target(runtime)
                    _close_metadata(runtime)
                self._rollback_after_failure(transaction)
                transaction = None
                raise

            self._bind_transaction_to_execution(
                transaction,
                execution_id=execution_id,
                plan=plan,
            )

            try:
                self._stage_and_publish(transaction=transaction, runtimes=runtimes)
            except _PatchStepFailure as failure:
                # Stop immediately on the first classified mutation failure. No
                # best-effort continuation to later files is permitted.
                for runtime in runtimes:
                    _close_staged(runtime)
                    _close_replace_target(runtime)
                    _close_metadata(runtime)
                state, rollback_stage, cleanup = self._rollback_after_failure(transaction)
                transaction = None
                stage = (
                    rollback_stage
                    if state is PatchCommitState.INDETERMINATE
                    else failure.stage
                )
                return PatchApplicationResult(
                    schema_version=PATCH_APPLICATION_SCHEMA_VERSION,
                    execution_id=execution_id,
                    proposal_id=plan.proposal_id,
                    proposal_digest=plan.proposal_digest,
                    change_set_digest=plan.change_set_digest,
                    plan_digest=plan.plan_digest,
                    commit_model=PATCH_COMMIT_MODEL,
                    commit_state=state,
                    failed_step_index=failure.runtime.step.index,
                    failed_target=failure.runtime.step.target,
                    failure_stage=stage,
                    error=f"{type(failure.cause).__name__}: {failure.cause}",
                    cleanup_error=cleanup,
                )
            except BaseException:
                # Unknown/interrupt failures must never fall through into commit.
                for runtime in runtimes:
                    _close_staged(runtime)
                    _close_replace_target(runtime)
                    _close_metadata(runtime)
                if transaction is not None:
                    self._rollback_after_failure(transaction)
                    transaction = None
                raise

            # All namespace transitions are now pending in one TxF transaction.
            # No target is externally committed until this single commit point.
            try:
                transaction.commit()
            except Exception as exc:
                state, rollback_stage, cleanup = self._rollback_after_failure(transaction)
                transaction = None
                return PatchApplicationResult(
                    schema_version=PATCH_APPLICATION_SCHEMA_VERSION,
                    execution_id=execution_id,
                    proposal_id=plan.proposal_id,
                    proposal_digest=plan.proposal_digest,
                    change_set_digest=plan.change_set_digest,
                    plan_digest=plan.plan_digest,
                    commit_model=PATCH_COMMIT_MODEL,
                    commit_state=state,
                    failure_stage=(
                        rollback_stage
                        if state is PatchCommitState.INDETERMINATE
                        else PatchFailureStage.COMMIT
                    ),
                    error=f"{type(exc).__name__}: {exc}",
                    cleanup_error=cleanup,
                )
            except BaseException:
                # Interrupt-like failures must not leave a transaction eligible for
                # accidental fall-through or later reuse. Attempt rollback, retain an
                # unresolved handle if rollback cannot be proved, then preserve the
                # original interrupt.
                self._rollback_after_failure(transaction)
                transaction = None
                raise

            cleanup_error: str | None = None
            try:
                transaction.close()
            except OSError as exc:
                cleanup_error = f"committed transaction handle cleanup failed: {type(exc).__name__}: {exc}"
                self._retained_transactions.append(transaction)
            else:
                self._forget_transaction_binding(transaction)
            finally:
                transaction = None

            return PatchApplicationResult(
                schema_version=PATCH_APPLICATION_SCHEMA_VERSION,
                execution_id=execution_id,
                proposal_id=plan.proposal_id,
                proposal_digest=plan.proposal_digest,
                change_set_digest=plan.change_set_digest,
                plan_digest=plan.plan_digest,
                commit_model=PATCH_COMMIT_MODEL,
                commit_state=PatchCommitState.COMMITTED,
                failure_stage=(
                    PatchFailureStage.TRANSACTION_CLEANUP if cleanup_error else None
                ),
                cleanup_error=cleanup_error,
            )


__all__ = [
    "PATCH_APPLICATION_SCHEMA_VERSION",
    "PATCH_COMMIT_MODEL",
    "PatchApplicationExecutor",
    "PatchApplicationResult",
    "PatchCommitState",
    "PatchFailureStage",
]
