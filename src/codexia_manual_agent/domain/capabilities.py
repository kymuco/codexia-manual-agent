from __future__ import annotations

from enum import StrEnum


class Capability(StrEnum):
    """Independent authorities granted to a Codexia session."""

    READ_WORKSPACE = "read_workspace"
    WRITE_WORKSPACE = "write_workspace"
    EXECUTE_PROCESS = "execute_process"
    NETWORK_ACCESS = "network_access"
    GIT_COMMIT = "git_commit"
    GIT_PUSH = "git_push"
    DELETE_FILES = "delete_files"
    OUTSIDE_WORKSPACE = "outside_workspace"


READ_ONLY_CAPABILITIES: tuple[Capability, ...] = (Capability.READ_WORKSPACE,)
