from __future__ import annotations

import os
from hashlib import sha256
from pathlib import Path
from types import TracebackType


class StandaloneProcessRunnerOwnershipError(RuntimeError):
    """Runner ownership could not be acquired or inspected safely."""


def process_attempt_runner_lock_path(
    *,
    journal_path: str | Path,
    attempt_id: str,
) -> Path:
    if not isinstance(attempt_id, str) or not attempt_id:
        raise ValueError("attempt_id must be non-empty text")
    journal = Path(journal_path).resolve()
    lock_dir = journal.parent / f"{journal.name}.runner-locks"
    lock_name = f"{sha256(attempt_id.encode('utf-8')).hexdigest()}.lock"
    return lock_dir / lock_name


class StandaloneProcessRunnerOwnership:
    """Cross-platform OS-released exclusive ownership for one exact attempt.

    The lock is intentionally ephemeral process ownership, not durable attempt
    truth. The filesystem path is deterministic, but the operating system owns
    the actual exclusivity. Process death releases the lock automatically.
    """

    def __init__(
        self,
        *,
        journal_path: str | Path,
        attempt_id: str,
    ) -> None:
        self._path = process_attempt_runner_lock_path(
            journal_path=journal_path,
            attempt_id=attempt_id,
        )
        self._file = None
        self._owned = False

    @property
    def path(self) -> Path:
        return self._path

    @property
    def owned(self) -> bool:
        return self._owned

    def try_acquire(self) -> bool:
        if self._file is not None:
            raise StandaloneProcessRunnerOwnershipError(
                "runner ownership object is already open"
            )

        self._path.parent.mkdir(parents=True, exist_ok=True)
        handle = self._path.open("a+b")
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
                try:
                    os.fsync(handle.fileno())
                except OSError:
                    pass
            handle.seek(0)

            if os.name == "nt":
                import msvcrt

                try:
                    msvcrt.locking(
                        handle.fileno(),
                        msvcrt.LK_NBLCK,
                        1,
                    )
                except OSError:
                    handle.close()
                    return False
            else:
                import fcntl

                try:
                    fcntl.flock(
                        handle.fileno(),
                        fcntl.LOCK_EX | fcntl.LOCK_NB,
                    )
                except (BlockingIOError, OSError):
                    handle.close()
                    return False
        except BaseException:
            if not handle.closed:
                handle.close()
            raise

        self._file = handle
        self._owned = True
        return True

    def release(self) -> None:
        handle = self._file
        if handle is None:
            return

        try:
            if self._owned:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(
                        handle.fileno(),
                        msvcrt.LK_UNLCK,
                        1,
                    )
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._owned = False
            self._file = None
            handle.close()

    def __enter__(self) -> StandaloneProcessRunnerOwnership:
        if not self.try_acquire():
            raise StandaloneProcessRunnerOwnershipError(
                "exact process attempt is already owned by another runner"
            )
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release()


def process_attempt_runner_is_active(
    *,
    journal_path: str | Path,
    attempt_id: str,
) -> bool:
    """Return whether another live process currently owns this exact attempt.

    Failure to inspect the lock is fail-closed: callers receive an exception
    rather than a false claim that the runner is dead.
    """

    ownership = StandaloneProcessRunnerOwnership(
        journal_path=journal_path,
        attempt_id=attempt_id,
    )
    try:
        acquired = ownership.try_acquire()
    except OSError as exc:
        raise StandaloneProcessRunnerOwnershipError(
            "could not inspect runner ownership"
        ) from exc

    if not acquired:
        return True
    ownership.release()
    return False
