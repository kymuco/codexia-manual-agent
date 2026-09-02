from __future__ import annotations

import os
import stat
from pathlib import Path
from uuid import uuid4

from codexia_manual_agent.domain.errors import WorkspaceMutationBoundaryError
from codexia_manual_agent.mutation.parent_anchor import (
    _win_close_handle,
    _win_open_directory,
    _win_verify_directory_handle,
)
from codexia_manual_agent.mutation.windows_txf import (
    WindowsTxFTransaction,
    create_transaction,
    require_windows_txf_support,
)
from codexia_manual_agent.mutation._patch_recovery_windows_namespace import (
    _rollback_and_close_namespace_transaction,
    _win_create_transacted_namespace_marker,
    _win_open_journal_file,
    _win_verify_journal_fd_path,
)


class PinnedRecoveryJournalParent:
    """Pin the recovery-journal parent namespace across one complete I/O operation.

    On Windows, a short-lived TxF transaction creates an invisible marker in the
    journal parent. TxF pins every directory component of a modified file against
    rename until the transaction ends. The marker's resolved handle path is
    verified before any non-transacted journal I/O is allowed. The marker
    transaction is always rolled back; it is namespace admission, not journal
    persistence.
    """

    def __init__(self, *, journal_path: Path, workspace_root: str | Path) -> None:
        self.path = Path(journal_path)
        self.workspace_root = Path(workspace_root).resolve(strict=True)
        self.parent = self.path.parent
        self.name = self.path.name
        self._dir_fd: int | None = None
        self._windows_transaction: WindowsTxFTransaction | None = None
        self._windows_marker_fd: int | None = None
        self._windows_marker_path: Path | None = None
        self._windows_handles: list[int] = []
        self._windows_paths: tuple[Path, ...] = ()

    def __enter__(self) -> "PinnedRecoveryJournalParent":
        self._validate_location()
        try:
            if os.name == "nt":
                self._pin_windows_namespace()
            else:
                self._pin_posix_parent()
            self.verify_parent_identity()
            return self
        except BaseException:
            self.close()
            raise

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def close(self) -> None:
        if self._dir_fd is not None:
            try:
                os.close(self._dir_fd)
            finally:
                self._dir_fd = None

        marker_fd = self._windows_marker_fd
        self._windows_marker_fd = None
        self._windows_marker_path = None
        if marker_fd is not None:
            try:
                os.close(marker_fd)
            except OSError:
                pass

        transaction = self._windows_transaction
        self._windows_transaction = None
        cleanup_error: BaseException | None = None
        if transaction is not None:
            try:
                _rollback_and_close_namespace_transaction(transaction)
            except BaseException as exc:
                cleanup_error = exc

        handles = self._windows_handles
        self._windows_handles = []
        self._windows_paths = ()
        for handle in reversed(handles):
            try:
                _win_close_handle(handle)
            except BaseException as exc:
                if cleanup_error is None:
                    cleanup_error = exc
        if cleanup_error is not None:
            raise cleanup_error

    def _validate_location(self) -> None:
        try:
            self.path.relative_to(self.workspace_root)
        except ValueError:
            pass
        else:
            raise WorkspaceMutationBoundaryError(
                "Patch recovery journal must live outside the patch workspace"
            )
        if not self.parent.exists() or not self.parent.is_dir():
            raise WorkspaceMutationBoundaryError(
                "Patch recovery journal parent directory must already exist"
            )

    def _windows_chain(self) -> tuple[Path, ...]:
        anchor_text = self.parent.anchor
        if not anchor_text:
            raise WorkspaceMutationBoundaryError(
                "Patch recovery journal parent has no Windows volume/share anchor"
            )
        anchor = Path(anchor_text)
        try:
            relative = self.parent.relative_to(anchor)
        except ValueError as exc:
            raise WorkspaceMutationBoundaryError(
                "Patch recovery journal parent cannot be anchored"
            ) from exc
        paths = [anchor]
        current = anchor
        for part in relative.parts:
            current = current / part
            paths.append(current)
        return tuple(paths)

    def _open_windows_chain(self) -> None:
        paths = self._windows_chain()
        handles: list[int] = []
        try:
            for path in paths:
                handle = _win_open_directory(path)
                _win_verify_directory_handle(handle, path)
                handles.append(handle)
        except BaseException:
            for handle in reversed(handles):
                _win_close_handle(handle)
            raise
        self._windows_paths = paths
        self._windows_handles = handles

    def _verify_windows_chain(self) -> None:
        if not self._windows_handles:
            raise WorkspaceMutationBoundaryError(
                "Patch recovery journal directory chain is not pinned"
            )
        for handle, expected in zip(
            self._windows_handles,
            self._windows_paths,
            strict=True,
        ):
            _win_verify_directory_handle(handle, expected)

    def _pin_windows_namespace(self) -> None:
        self._open_windows_chain()
        require_windows_txf_support(self.parent)
        transaction = create_transaction()
        marker_fd: int | None = None
        marker_path = self.parent / f".codexia-recovery-pin-{uuid4().hex}"
        try:
            marker_fd = _win_create_transacted_namespace_marker(transaction, marker_path)
            _win_verify_journal_fd_path(
                marker_fd,
                expected=marker_path,
                workspace_root=self.workspace_root,
            )
            self._verify_windows_chain()
        except BaseException:
            if marker_fd is not None:
                try:
                    os.close(marker_fd)
                except OSError:
                    pass
            _rollback_and_close_namespace_transaction(transaction)
            raise
        self._windows_transaction = transaction
        self._windows_marker_fd = marker_fd
        self._windows_marker_path = marker_path

    def _pin_posix_parent(self) -> None:
        flags = os.O_RDONLY
        flags |= getattr(os, "O_DIRECTORY", 0)
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(self.parent, flags)
            info = os.fstat(fd)
        except OSError as exc:
            raise WorkspaceMutationBoundaryError(
                "Patch recovery journal parent cannot be pinned"
            ) from exc
        if not stat.S_ISDIR(info.st_mode):
            os.close(fd)
            raise WorkspaceMutationBoundaryError(
                "Patch recovery journal parent is not a directory"
            )
        self._dir_fd = fd

    def verify_parent_identity(self) -> None:
        if os.name == "nt":
            if (
                self._windows_transaction is None
                or self._windows_marker_fd is None
                or self._windows_marker_path is None
            ):
                raise WorkspaceMutationBoundaryError(
                    "Patch recovery journal parent is not transactionally pinned"
                )
            self._verify_windows_chain()
            _win_verify_journal_fd_path(
                self._windows_marker_fd,
                expected=self._windows_marker_path,
                workspace_root=self.workspace_root,
            )
            return

        if self._dir_fd is None:
            raise WorkspaceMutationBoundaryError(
                "Patch recovery journal parent is not pinned"
            )
        try:
            held = os.fstat(self._dir_fd)
            current = os.stat(self.parent, follow_symlinks=False)
        except OSError as exc:
            raise WorkspaceMutationBoundaryError(
                "Patch recovery journal parent identity cannot be revalidated"
            ) from exc
        if (
            not stat.S_ISDIR(current.st_mode)
            or held.st_dev != current.st_dev
            or held.st_ino != current.st_ino
        ):
            raise WorkspaceMutationBoundaryError(
                "Patch recovery journal parent identity changed"
            )

    def entry_exists(self) -> bool:
        self.verify_parent_identity()
        if os.name == "nt":
            exists = os.path.lexists(self.path)
        else:
            if self._dir_fd is None:
                raise WorkspaceMutationBoundaryError(
                    "Patch recovery journal parent is not pinned"
                )
            try:
                os.stat(self.name, dir_fd=self._dir_fd, follow_symlinks=False)
            except FileNotFoundError:
                exists = False
            except OSError as exc:
                raise WorkspaceMutationBoundaryError(
                    "Patch recovery journal entry cannot be inspected"
                ) from exc
            else:
                exists = True
        self.verify_parent_identity()
        return exists

    def open_existing(self, *, writable: bool) -> int:
        self.verify_parent_identity()
        if os.name == "nt":
            fd = _win_open_journal_file(self.path, create=False, writable=writable)
        else:
            if self._dir_fd is None:
                raise WorkspaceMutationBoundaryError(
                    "Patch recovery journal parent is not pinned"
                )
            flags = os.O_RDWR if writable else os.O_RDONLY
            flags |= getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(self.name, flags, dir_fd=self._dir_fd)
        try:
            self._verify_regular_fd(fd)
            if os.name == "nt":
                _win_verify_journal_fd_path(
                    fd,
                    expected=self.path,
                    workspace_root=self.workspace_root,
                )
            self.verify_parent_identity()
            return fd
        except BaseException:
            os.close(fd)
            raise

    def create_new(self) -> int:
        self.verify_parent_identity()
        if os.name == "nt":
            fd = _win_open_journal_file(self.path, create=True, writable=True)
        else:
            if self._dir_fd is None:
                raise WorkspaceMutationBoundaryError(
                    "Patch recovery journal parent is not pinned"
                )
            flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
            flags |= getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(self.name, flags, 0o600, dir_fd=self._dir_fd)
        try:
            self._verify_regular_fd(fd)
            if os.name == "nt":
                _win_verify_journal_fd_path(
                    fd,
                    expected=self.path,
                    workspace_root=self.workspace_root,
                )
            self.verify_parent_identity()
            return fd
        except BaseException:
            os.close(fd)
            raise

    @staticmethod
    def _verify_regular_fd(fd: int) -> None:
        try:
            info = os.fstat(fd)
        except OSError as exc:
            raise WorkspaceMutationBoundaryError(
                "Patch recovery journal handle cannot be inspected"
            ) from exc
        if not stat.S_ISREG(info.st_mode):
            raise WorkspaceMutationBoundaryError(
                "Patch recovery journal must remain a regular file"
            )
