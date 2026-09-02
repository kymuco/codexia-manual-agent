from __future__ import annotations

from hashlib import sha256
from typing import BinaryIO

from codexia_manual_agent.domain.errors import InvalidWorkspaceMutationError


_READ_CHUNK_BYTES = 1024 * 1024


def hash_bounded_stream(
    handle: BinaryIO,
    *,
    max_bytes: int,
    label: str = "Mutation preimage",
) -> tuple[int, str]:
    """Hash at most ``max_bytes`` and fail immediately on growth past the budget."""

    if type(max_bytes) is not int or max_bytes < 0:
        raise ValueError("max_bytes must be a non-negative integer")

    digest = sha256()
    total = 0
    while True:
        remaining = max_bytes - total
        # Read one byte beyond the remaining budget so a concurrently growing
        # file cannot keep this loop alive indefinitely past the declared cap.
        chunk = handle.read(min(_READ_CHUNK_BYTES, remaining + 1))
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise InvalidWorkspaceMutationError(
                f"{label} exceeds hashing budget ({total} > {max_bytes})"
            )
        digest.update(chunk)
    return total, digest.hexdigest()
