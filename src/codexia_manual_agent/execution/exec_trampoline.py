from __future__ import annotations

import fcntl
import json
import os
import sys


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        view = view[written:]


def _write_error(fd: int, exc: BaseException) -> None:
    payload = {
        "error": f"{type(exc).__name__}: {exc}",
        "errno": getattr(exc, "errno", None),
    }
    _write_all(
        fd,
        b"ERROR "
        + json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        + b"\n",
    )


def main() -> int:
    if len(sys.argv) < 3:
        return 125
    try:
        status_fd = int(sys.argv[1])
    except ValueError:
        return 125

    executable = sys.argv[2]
    argv = [executable, *sys.argv[3:]]
    try:
        _write_all(status_fd, b"READY\n")
        flags = fcntl.fcntl(status_fd, fcntl.F_GETFD)
        fcntl.fcntl(status_fd, fcntl.F_SETFD, flags | fcntl.FD_CLOEXEC)
        os.execve(executable, argv, dict(os.environ))
    except BaseException as exc:
        try:
            _write_error(status_fd, exc)
        finally:
            try:
                os.close(status_fd)
            except OSError:
                pass
        return 126


if __name__ == "__main__":
    raise SystemExit(main())
