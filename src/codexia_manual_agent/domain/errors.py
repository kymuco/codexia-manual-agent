from __future__ import annotations


class CodexiaError(Exception):
    """Base class for expected, user-facing Codexia failures."""


class WorkspaceError(CodexiaError):
    """Base class for workspace inspection failures."""


class WorkspaceNotFoundError(WorkspaceError):
    pass


class WorkspaceBoundaryError(WorkspaceError):
    pass


class WorkspacePathNotFoundError(WorkspaceError):
    pass


class WorkspacePathTypeError(WorkspaceError):
    pass


class FileTooLargeError(WorkspaceError):
    pass


class BinaryFileError(WorkspaceError):
    pass


class SearchLimitError(WorkspaceError):
    pass


class GitStatusError(WorkspaceError):
    pass


class UnsupportedToolError(CodexiaError):
    pass


class InvalidToolArgumentsError(CodexiaError):
    pass


class SessionError(CodexiaError):
    pass


class SessionNotFoundError(SessionError):
    pass


class InvalidSessionIdError(SessionError):
    pass


class PromptVersionError(CodexiaError):
    pass


class ProviderError(CodexiaError):
    """A model transport failed before a valid model response was produced."""


class ProviderUnavailableError(ProviderError):
    pass


class ProtocolError(CodexiaError):
    """The model response did not satisfy the Codexia runtime protocol."""


class AgentBudgetError(CodexiaError):
    """A bounded agent run exhausted one of its declared limits."""


class AuthorityError(CodexiaError):
    """Base class for local action-authority failures."""


class ActionIntegrityError(AuthorityError):
    """A proposal or receipt does not match its immutable digest-bound payload."""


class ApprovalRequiredError(AuthorityError):
    """The current policy requires an explicit local human decision."""


class InvalidApprovalDecisionError(AuthorityError):
    """An explicit human approval value is not exactly boolean."""


class AuthorizationDeniedError(AuthorityError):
    """A receipt exists but does not authorize execution."""


class AuthorizationMismatchError(AuthorityError):
    """A receipt is not valid for this proposal, mode, or local policy."""


class AuthorizationConsumedError(AuthorityError):
    """A single-use authorization receipt has already been consumed."""


class InvalidActionTransitionError(AuthorityError):
    """The action lifecycle attempted an invalid state transition."""


class ProcessExecutionError(CodexiaError):
    """Base class for controlled local process execution failures."""


class InvalidProcessSpecError(ProcessExecutionError):
    """A process proposal does not satisfy the M2.1 structured execution schema."""


class ProcessWorkspaceBoundaryError(ProcessExecutionError):
    """A process cwd or workspace identity escapes the canonical workspace."""


class ProcessExecutableNotFoundError(ProcessExecutionError):
    """The requested executable cannot be resolved to a regular file."""


class ProcessExecutableChangedError(ProcessExecutionError):
    """Executable identity changed between proposal and execution."""


class CommandAdmissionError(CodexiaError):
    """A model process request cannot satisfy the local M2.2 admission contract."""


class WorkspaceMutationError(CodexiaError):
    """Base class for M2.3 controlled workspace mutation failures."""


class InvalidWorkspaceMutationError(WorkspaceMutationError):
    """A workspace mutation proposal does not satisfy the M2.3 schema."""


class WorkspaceMutationBoundaryError(WorkspaceMutationError):
    """A mutation target violates the canonical workspace boundary."""


class WorkspaceMutationPreimageChangedError(WorkspaceMutationError):
    """The target preimage changed before authorization could be consumed."""


class WorkspaceMutationTargetExistsError(WorkspaceMutationError):
    """A create operation targeted an existing path."""


class WorkspaceMutationTargetMissingError(WorkspaceMutationError):
    """A replace operation targeted a missing path."""


class GitMutationError(CodexiaError):
    """Base class for M2.5 explicit Git mutation governance failures."""


class InvalidGitMutationError(GitMutationError):
    """A Git commit/push proposal violates the bounded M2.5 schema."""


class GitRepositoryBoundaryError(GitMutationError):
    """The repository or Git metadata escapes the governed workspace boundary."""


class GitMutationPreconditionChangedError(GitMutationError):
    """A bound Git ref, index, executable, identity, or destination changed pre-consumption."""


class GitMutationExecutionError(GitMutationError):
    """A governed Git primitive failed after proposal construction."""
