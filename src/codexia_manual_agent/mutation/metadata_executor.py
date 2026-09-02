from __future__ import annotations

import os
from uuid import uuid4

from codexia_manual_agent.authority import ActionLifecycle, ActionPhase, LocalApprovalAuthority
from codexia_manual_agent.domain.errors import (
    InvalidActionTransitionError,
    InvalidWorkspaceMutationError,
    WorkspaceMutationBoundaryError,
    WorkspaceMutationPreimageChangedError,
)
from codexia_manual_agent.mutation.models import (
    MutationOperation,
    MutationTerminationReason,
    PreimageState,
    WorkspaceMutationObservation,
)
from codexia_manual_agent.mutation.parent_anchor import PinnedMutationTarget, _StagedFile
from codexia_manual_agent.mutation.windows_metadata import (
    WindowsReplaceMetadata,
    apply_windows_replace_metadata_fd,
    capture_windows_replace_binding,
    capture_windows_replace_metadata_fd,
    validate_windows_relative_target,
)
from codexia_manual_agent.mutation.windows_txf import (
    PinnedTxFReplaceTarget,
    WindowsTxFTransaction,
    create_metadata_stage as _write_metadata_stage,
    create_transaction as _create_txf_transaction,
    move_replace_staged as _win_txf_replace_staged,
    pin_exact_replace_target as _win_pin_exact_replace_target,
)
from codexia_manual_agent.mutation.workspace import (
    _MAX_PREIMAGE_BYTES,
    _PendingOutcome,
    _append_error,
    _failure_reason,
    _record,
    _staging_mode,
    _validate_proposal,
)


def _append_cleanup_error(existing: str | None, label: str, exc: BaseException) -> str:
    return _append_error(existing, f"{label}: {type(exc).__name__}: {exc}")


def _close_stage(
    pinned: PinnedMutationTarget,
    staged: _StagedFile | None,
    cleanup_error: str | None,
) -> tuple[_StagedFile | None, str | None]:
    if staged is None:
        return None, cleanup_error
    try:
        pinned.close_staged(staged)
    except OSError as exc:
        cleanup_error = _append_cleanup_error(
            cleanup_error,
            "transacted staging handle cleanup failed",
            exc,
        )
        return staged, cleanup_error
    return None, cleanup_error


def _close_target(
    replace_target: PinnedTxFReplaceTarget | None,
    cleanup_error: str | None,
) -> tuple[PinnedTxFReplaceTarget | None, str | None]:
    if replace_target is None:
        return None, cleanup_error
    try:
        replace_target.close()
    except OSError as exc:
        cleanup_error = _append_cleanup_error(
            cleanup_error,
            "transacted replace target pin cleanup failed",
            exc,
        )
        return replace_target, cleanup_error
    return None, cleanup_error


def _finish_transaction(
    transaction: WindowsTxFTransaction | None,
    *,
    committed: bool,
    cleanup_error: str | None,
) -> tuple[WindowsTxFTransaction | None, str | None]:
    if transaction is None:
        return None, cleanup_error
    if not committed:
        try:
            transaction.rollback()
        except OSError as exc:
            cleanup_error = _append_cleanup_error(
                cleanup_error,
                "Windows TxF rollback cleanup failed",
                exc,
            )

    try:
        transaction.close()
    except OSError as exc:
        cleanup_error = _append_cleanup_error(
            cleanup_error,
            "Windows TxF transaction handle cleanup failed",
            exc,
        )
        try:
            transaction.close()
        except OSError as retry_exc:
            cleanup_error = _append_cleanup_error(
                cleanup_error,
                "Windows TxF transaction handle cleanup retry failed",
                retry_exc,
            )
            return transaction, cleanup_error

    return None, cleanup_error


class WindowsMetadataReplaceExecutor:
    """TxF strict replace with pre-consumption exact pin and metadata preservation."""

    def __init__(self) -> None:
        self._retained_transactions: list[WindowsTxFTransaction] = []

    def _retry_retained_transaction_cleanup(self) -> None:
        if not self._retained_transactions:
            return

        retained = self._retained_transactions
        self._retained_transactions = []
        retry_errors: list[str] = []

        for transaction in retained:
            pending, cleanup_error = _finish_transaction(
                transaction,
                committed=transaction.finished,
                cleanup_error=None,
            )
            if pending is not None:
                self._retained_transactions.append(pending)
            if cleanup_error:
                retry_errors.append(cleanup_error)

        if self._retained_transactions:
            detail = "; ".join(retry_errors) or "transaction cleanup retry failed"
            raise WorkspaceMutationBoundaryError(
                "Previous Windows TxF transaction cleanup remains unresolved; "
                f"authorization was not consumed. {detail}"
            )

    def execute(
        self,
        lifecycle: ActionLifecycle,
        *,
        authority: LocalApprovalAuthority,
    ) -> WorkspaceMutationObservation:
        if lifecycle.phase is not ActionPhase.AUTHORIZED:
            raise InvalidActionTransitionError("Workspace mutation requires AUTHORIZED lifecycle")
        if lifecycle.authorization is None:
            raise InvalidActionTransitionError("Authorized workspace mutation has no receipt")
        if os.name != "nt":
            raise WorkspaceMutationBoundaryError(
                "M2.3 metadata-preserving strict replace is supported only on Windows"
            )

        plan = _validate_proposal(lifecycle.proposal)
        if plan.operation is not MutationOperation.REPLACE:
            raise InvalidWorkspaceMutationError(
                "Metadata replace executor accepts only workspace.replace_file.v1"
            )
        validate_windows_relative_target(plan.target)
        self._retry_retained_transaction_cleanup()
        receipt = lifecycle.authorization

        with PinnedMutationTarget(
            root=plan.root,
            parent=plan.parent,
            target_name=plan.target_path.name,
        ) as pinned:
            transaction: WindowsTxFTransaction | None = _create_txf_transaction()
            replace_target: PinnedTxFReplaceTarget | None = None
            metadata: WindowsReplaceMetadata | None = None

            # Pre-consumption failures must leave the receipt untouched and roll back
            # the empty/inspection-only transaction before escaping.
            try:
                replace_target = _win_pin_exact_replace_target(
                    transaction,
                    plan.target_path,
                    max_bytes=_MAX_PREIMAGE_BYTES,
                )
                if replace_target is None:
                    raise WorkspaceMutationPreimageChangedError(
                        "Replace target disappeared before authorization consumption"
                    )
                if replace_target.snapshot != plan.expected_preimage:
                    raise WorkspaceMutationPreimageChangedError(
                        "Workspace mutation preimage changed before authorization consumption"
                    )
                metadata = capture_windows_replace_metadata_fd(
                    replace_target.fd,
                    expected_path=plan.target_path,
                )
            except BaseException:
                try:
                    if metadata is not None:
                        metadata.close()
                finally:
                    try:
                        if replace_target is not None:
                            replace_target.close()
                    finally:
                        pending_transaction, _ = _finish_transaction(
                            transaction,
                            committed=False,
                            cleanup_error=None,
                        )
                        if pending_transaction is not None:
                            self._retained_transactions.append(pending_transaction)
                raise

            # Only after the exact transacted object, canonical final path, data,
            # stream policy and preservable metadata are bound may authority burn.
            metadata_binding = dict(metadata.binding)
            observed = replace_target.snapshot
            mutation_id = uuid4().hex
            lifecycle.consume_authorization(authority=authority)
            lifecycle.record_executed(mutation_id)

            staged: _StagedFile | None = None
            committed = False
            cleanup_error: str | None = None
            pending: _PendingOutcome | None = None

            try:
                staged = _write_metadata_stage(
                    transaction,
                    pinned,
                    plan.postimage,
                    mode=_staging_mode(plan),
                )
                pinned.verify_parent_identity()
                pinned.verify_staged_identity(staged)

                current_metadata = capture_windows_replace_metadata_fd(
                    replace_target.fd,
                    expected_path=plan.target_path,
                )
                try:
                    if current_metadata.binding != metadata.binding:
                        pending = _PendingOutcome(
                            observed_preimage=observed,
                            reason=MutationTerminationReason.PREIMAGE_CHANGED,
                            error="Replace target security metadata changed before commit",
                        )
                finally:
                    current_metadata.close()

                if pending is None:
                    apply_windows_replace_metadata_fd(staged.fd, metadata)
                    pinned.verify_parent_identity()
                    pinned.verify_staged_identity(staged)

                    # The exact target remains share=0 until the transacted namespace
                    # transition succeeds. TxF then owns the pending target name;
                    # close all transacted file handles before ending the transaction.
                    _win_txf_replace_staged(transaction, staged, plan.target_path)
                    staged, cleanup_error = _close_stage(pinned, staged, cleanup_error)
                    replace_target, cleanup_error = _close_target(
                        replace_target,
                        cleanup_error,
                    )
                    if cleanup_error is not None:
                        pending = _PendingOutcome(
                            observed_preimage=observed,
                            reason=MutationTerminationReason.WRITE_ERROR,
                            error=cleanup_error,
                        )
                    else:
                        transaction.commit()
                        committed = True

            except (
                InvalidWorkspaceMutationError,
                WorkspaceMutationBoundaryError,
                WorkspaceMutationPreimageChangedError,
                OSError,
            ) as exc:
                if committed:
                    cleanup_error = _append_cleanup_error(
                        cleanup_error,
                        "post-commit housekeeping failed",
                        exc,
                    )
                elif pending is None:
                    pending = _PendingOutcome(
                        observed_preimage=observed,
                        reason=_failure_reason(exc),
                        error=f"{type(exc).__name__}: {exc}",
                    )
            finally:
                staged, cleanup_error = _close_stage(pinned, staged, cleanup_error)
                replace_target, cleanup_error = _close_target(
                    replace_target,
                    cleanup_error,
                )
                transaction, cleanup_error = _finish_transaction(
                    transaction,
                    committed=committed,
                    cleanup_error=cleanup_error,
                )
                if transaction is not None:
                    self._retained_transactions.append(transaction)
                if metadata is not None:
                    try:
                        metadata.close()
                    except OSError as exc:
                        cleanup_error = _append_cleanup_error(
                            cleanup_error,
                            "Windows replace metadata cleanup failed",
                            exc,
                        )
                    finally:
                        metadata = None

            if pending is not None:
                error = pending.error
                if cleanup_error and cleanup_error != error:
                    error = _append_error(error, cleanup_error) if error else cleanup_error
                return _record(
                    lifecycle,
                    mutation_id=mutation_id,
                    plan=plan,
                    observed_preimage=pending.observed_preimage,
                    applied=False,
                    reason=pending.reason,
                    receipt=receipt,
                    error=error,
                )

            if not committed:
                return _record(
                    lifecycle,
                    mutation_id=mutation_id,
                    plan=plan,
                    observed_preimage=observed,
                    applied=False,
                    reason=MutationTerminationReason.WRITE_ERROR,
                    receipt=receipt,
                    error=_append_error(
                        cleanup_error,
                        "Mutation ended without a committed TxF transaction",
                    ),
                )

            try:
                post = pinned.capture_preimage(max_bytes=_MAX_PREIMAGE_BYTES)
                post_metadata = capture_windows_replace_binding(plan.target_path)
            except (
                InvalidWorkspaceMutationError,
                WorkspaceMutationBoundaryError,
                WorkspaceMutationPreimageChangedError,
                OSError,
            ) as exc:
                return _record(
                    lifecycle,
                    mutation_id=mutation_id,
                    plan=plan,
                    observed_preimage=observed,
                    applied=True,
                    reason=MutationTerminationReason.POSTIMAGE_MISMATCH,
                    receipt=receipt,
                    error=_append_error(
                        cleanup_error,
                        f"post-commit verification failed: {type(exc).__name__}: {exc}",
                    ),
                )

            if (
                post.state is not PreimageState.PRESENT
                or post.size_bytes != len(plan.postimage)
                or post.sha256 != plan.postimage_sha256
                or post_metadata != metadata_binding
            ):
                return _record(
                    lifecycle,
                    mutation_id=mutation_id,
                    plan=plan,
                    observed_preimage=observed,
                    applied=True,
                    reason=MutationTerminationReason.POSTIMAGE_MISMATCH,
                    receipt=receipt,
                    postimage_size_bytes=post.size_bytes,
                    postimage_sha256=post.sha256,
                    error=_append_error(
                        cleanup_error,
                        "Committed postimage or preserved Windows metadata does not match",
                    ),
                )

            return _record(
                lifecycle,
                mutation_id=mutation_id,
                plan=plan,
                observed_preimage=observed,
                applied=True,
                reason=MutationTerminationReason.APPLIED,
                receipt=receipt,
                postimage_size_bytes=post.size_bytes,
                postimage_sha256=post.sha256,
                error=cleanup_error,
            )
