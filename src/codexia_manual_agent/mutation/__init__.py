"""Controlled workspace mutation contracts for Codexia."""

import os

from codexia_manual_agent.domain.errors import WorkspaceMutationBoundaryError

from .hardened_workspace import prepare_create_proposal, prepare_replace_proposal
from .models import (
    MutationOperation,
    MutationTerminationReason,
    PreimageSnapshot,
    PreimageState,
    WorkspaceMutationObservation,
)
from .patches import (
    MAX_PATCH_FILES,
    MAX_PATCH_FILE_BYTES,
    MAX_PATCH_PREVIEW_BYTES,
    MAX_PATCH_TOTAL_CONTENT_BYTES,
    PATCH_ACTION,
    PatchApprovalPreview,
    PatchChangeSet,
    PatchFileChange,
    PatchFilePreview,
    PatchFileRequest,
)
from .patch_preview_budget_repairs import (
    build_patch_approval_preview,
    parse_patch_proposal,
    prepare_patch_proposal,
)
from .model_patch import (
    MODEL_PATCH_PREPARATION_SCHEMA_VERSION,
    ModelPatchApprovalPreview,
    ModelPatchPreparation,
    prepare_model_patch_proposal,
)
from . import patch_windows_case_binding_repairs as _patch_windows_case_binding_repairs
from . import patch_windows_namespace_guard as _patch_windows_namespace_guard
from .parent_anchor import PinnedMutationTarget as _PinnedMutationTarget
from .preflight_executor import WorkspaceMutationExecutor
from .patch_execution_plan import (
    PATCH_EXECUTION_BACKEND,
    PATCH_EXECUTION_PLAN_SCHEMA_VERSION,
    PATCH_EXECUTION_PLATFORM,
    PatchExecutionPlan,
    PatchExecutionStep,
    build_patch_execution_plan,
    preflight_patch_execution_plan,
    revalidate_patch_execution_plan,
    validate_patch_execution_plan_binding,
)
from .patch_application import (
    PATCH_APPLICATION_SCHEMA_VERSION,
    PATCH_COMMIT_MODEL,
    PatchApplicationExecutor,
    PatchApplicationResult,
    PatchCommitState,
    PatchFailureStage,
)
from .patch_observation import (
    PATCH_MUTATION_OBSERVATION_SCHEMA_VERSION,
    PATCH_MUTATION_RECEIPT_SCHEMA_VERSION,
    PatchExpectedFileState,
    PatchFileMutationObservation,
    PatchFileObservationStatus,
    PatchMutationObserver,
    PatchMutationReceipt,
    PatchTerminalExpectation,
    PatchVerificationOutcome,
    validate_patch_mutation_receipt_binding,
)
from .patch_recovery import (
    MAX_RECOVERY_JOURNAL_BYTES,
    MAX_RECOVERY_JOURNAL_LINE_BYTES,
    MAX_RECOVERY_JOURNAL_RECORDS,
    PATCH_CRASH_ASSESSMENT_SCHEMA_VERSION,
    PATCH_RECOVERY_FILE_OBSERVATION_SCHEMA_VERSION,
    PATCH_RECOVERY_JOURNAL_SCHEMA_VERSION,
    PATCH_RECOVERY_RECEIPT_SCHEMA_VERSION,
    PatchCrashFilesystemClassification,
    PatchCrashRecoveryAssessment,
    PatchKtmOutcome,
    PatchRecoveryFileObservation,
    PatchRecoveryJournal,
    PatchRecoveryJournalPhase,
    PatchRecoveryJournalRead,
    PatchRecoveryJournalRecord,
    PatchRecoveryManager,
    PatchRecoveryPersistenceError,
    PatchRecoveryReceipt,
    PatchRecoverySource,
    PatchRecoveryVerificationOutcome,
    RecoverablePatchApplicationExecutor,
    query_transaction_outcome,
    validate_patch_crash_recovery_assessment_binding,
    validate_patch_recovery_receipt_binding,
)
from .workspace import CREATE_ACTION, MAX_POSTIMAGE_BYTES, REPLACE_ACTION
from .windows_rename import rename_staged_fd as _abi_rename_staged_fd
from .windows_rename import strict_replace_staged_fd as _abi_strict_replace_staged_fd
from . import parent_anchor as _parent_anchor
from . import secure_executor as _secure_executor
from . import workspace as _workspace

# Keep the support boundary identical for package-level and direct-module imports.
_workspace.prepare_create_proposal = prepare_create_proposal
_workspace.prepare_replace_proposal = prepare_replace_proposal
_workspace.WorkspaceMutationExecutor = WorkspaceMutationExecutor
_secure_executor.WorkspaceMutationExecutor = WorkspaceMutationExecutor

# CREATE and sealed legacy helpers retain the centralized ABI-correct rename
# serializer. Active REPLACE no longer uses path-based FileRenameInfoEx; it is
# handled transactionally by the metadata executor's TxF backend.
_parent_anchor._win_rename_staged_fd = _abi_rename_staged_fd
_secure_executor._win_strict_replace_staged_fd = _abi_strict_replace_staged_fd

# Seal the lower-level legacy commit methods as well. Windows create remains a
# safe no-clobber primitive. Non-Windows create is disabled because a held dirfd
# does not prove the parent inode stayed inside the workspace. Legacy replace is
# disabled on every platform because strict replace requires the TxF executor's
# exact destination transaction and metadata-preserving stage.
_legacy_commit_create = _PinnedMutationTarget.commit_create


def _guarded_commit_create(self, staged):
    if os.name != "nt":
        raise WorkspaceMutationBoundaryError(
            "M2.3 direct create commit is disabled outside Windows; "
            "use the constrained platform executor"
        )
    return _legacy_commit_create(self, staged)


def _guarded_commit_replace(self, staged):
    raise WorkspaceMutationBoundaryError(
        "M2.3 direct replace commit is disabled; strict replace requires the "
        "metadata-preserving TxF executor"
    )


_PinnedMutationTarget.commit_create = _guarded_commit_create
_PinnedMutationTarget.commit_replace = _guarded_commit_replace

__all__ = [
    "CREATE_ACTION",
    "MAX_PATCH_FILES",
    "MAX_PATCH_FILE_BYTES",
    "MAX_PATCH_PREVIEW_BYTES",
    "MAX_PATCH_TOTAL_CONTENT_BYTES",
    "MAX_POSTIMAGE_BYTES",
    "MAX_RECOVERY_JOURNAL_BYTES",
    "MAX_RECOVERY_JOURNAL_LINE_BYTES",
    "MAX_RECOVERY_JOURNAL_RECORDS",
    "MODEL_PATCH_PREPARATION_SCHEMA_VERSION",
    "ModelPatchApprovalPreview",
    "ModelPatchPreparation",
    "MutationOperation",
    "MutationTerminationReason",
    "PATCH_ACTION",
    "PATCH_APPLICATION_SCHEMA_VERSION",
    "PATCH_COMMIT_MODEL",
    "PATCH_EXECUTION_BACKEND",
    "PATCH_EXECUTION_PLAN_SCHEMA_VERSION",
    "PATCH_EXECUTION_PLATFORM",
    "PATCH_MUTATION_OBSERVATION_SCHEMA_VERSION",
    "PATCH_MUTATION_RECEIPT_SCHEMA_VERSION",
    "PATCH_CRASH_ASSESSMENT_SCHEMA_VERSION",
    "PATCH_RECOVERY_FILE_OBSERVATION_SCHEMA_VERSION",
    "PATCH_RECOVERY_JOURNAL_SCHEMA_VERSION",
    "PATCH_RECOVERY_RECEIPT_SCHEMA_VERSION",
    "PatchApplicationExecutor",
    "PatchApplicationResult",
    "PatchApprovalPreview",
    "PatchChangeSet",
    "PatchCommitState",
    "PatchCrashFilesystemClassification",
    "PatchCrashRecoveryAssessment",
    "PatchExecutionPlan",
    "PatchExecutionStep",
    "PatchExpectedFileState",
    "PatchFailureStage",
    "PatchKtmOutcome",
    "build_patch_execution_plan",
    "PatchFileChange",
    "PatchFileMutationObservation",
    "PatchFileObservationStatus",
    "PatchFilePreview",
    "PatchFileRequest",
    "PatchMutationObserver",
    "PatchMutationReceipt",
    "PatchRecoveryFileObservation",
    "PatchRecoveryJournal",
    "PatchRecoveryJournalPhase",
    "PatchRecoveryJournalRead",
    "PatchRecoveryJournalRecord",
    "PatchRecoveryManager",
    "PatchRecoveryPersistenceError",
    "PatchRecoveryReceipt",
    "PatchRecoverySource",
    "PatchRecoveryVerificationOutcome",
    "RecoverablePatchApplicationExecutor",
    "PatchTerminalExpectation",
    "PatchVerificationOutcome",
    "PreimageSnapshot",
    "PreimageState",
    "REPLACE_ACTION",
    "WorkspaceMutationExecutor",
    "WorkspaceMutationObservation",
    "build_patch_approval_preview",
    "parse_patch_proposal",
    "prepare_create_proposal",
    "prepare_model_patch_proposal",
    "prepare_patch_proposal",
    "prepare_replace_proposal",
    "preflight_patch_execution_plan",
    "query_transaction_outcome",
    "revalidate_patch_execution_plan",
    "validate_patch_execution_plan_binding",
    "validate_patch_crash_recovery_assessment_binding",
    "validate_patch_mutation_receipt_binding",
    "validate_patch_recovery_receipt_binding",
]
