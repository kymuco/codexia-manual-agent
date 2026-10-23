"""Operator-visible HTTP progress output, independent of Work execution.

Only allow-listed metadata is rendered as progress. Model text is displayed
only with explicit stream_text=True; never write credentials or raw payloads.
"""

from __future__ import annotations

import re
import sys
import time
from typing import TextIO

_STAGE = frozenset(
    {
        "WORK",
        "INPUT",
        "HANDOFF",
        "CLAIM",
        "HTTP_CALL",
        "STREAM_OPEN",
        "FIRST_TOKEN",
        "HTTP_RETURN",
        "MODEL_ROUTE",
        "HTTP_IDENTITY",
        "READBACK",
        "ADMISSION",
        "RESULT",
        "OUTCOME_UNKNOWN",
    }
)
_FIELDS = frozenset(
    {
        "work_id",
        "revision",
        "files",
        "total_bytes",
        "sha256",
        "conversation_id",
        "message_id",
        "model",
        "reasoning_effort",
        "requested_model",
        "requested_reasoning_effort",
        "sent_model",
        "sent_reasoning_effort",
        "observed_model",
        "observed_reasoning_effort",
        "mode",
        "status",
        "error_type",
        "request_stage",
        "response_chars",
        "retry",
    }
)
_SAFE_VALUE = re.compile(r"^[A-Za-z0-9_.:@/-]{1,128}$")


class HttpProgressConsole:
    """Simple sequenced terminal events; no independent runtime authority.

    Stages are observational, not receipts. In particular HTTP_CALL does not
    prove that the remote service accepted any write. A failed call may have
    produced a response even if its local caller never received it.
    """

    def __init__(
        self,
        *,
        stream_text: bool = False,
        progress_output: TextIO | None = None,
        text_output: TextIO | None = None,
    ) -> None:
        self.stream_text = stream_text
        self._progress_output = progress_output or sys.stderr
        self._text_output = text_output or sys.stdout
        self._started = time.monotonic()
        self._sequence = 0
        self._stream_open = False

    def emit(self, stage: str, fields: dict[str, object] | None = None) -> None:
        """Display only known stages and safe metadata, never arbitrary messages."""

        if stage not in _STAGE:
            raise ValueError("unrecognized progress stage")
        metadata = []
        for key, value in (fields or {}).items():
            if key not in _FIELDS:
                continue
            if isinstance(value, bool):
                continue
            if isinstance(value, int) and value >= 0:
                safe = str(value)
            elif isinstance(value, float) and value >= 0:
                safe = f"{value:.3f}"
            elif isinstance(value, str) and _SAFE_VALUE.fullmatch(value):
                safe = value
            else:
                continue
            metadata.append(f"{key}={safe}")
        self._sequence += 1
        elapsed = time.monotonic() - self._started
        suffix = " " + " ".join(metadata) if metadata else ""
        print(
            f"[{self._sequence:02d} +{elapsed:07.2f}s] "
            f"{stage:<14}{suffix}",
            file=self._progress_output,
            flush=True,
        )

    def token(self, text: str) -> None:
        """Show model content on stdout only when the human opts into it."""

        if not self.stream_text or not isinstance(text, str) or not text:
            return
        if not self._stream_open:
            print("\nGPT> ", end="", file=self._text_output, flush=True)
            self._stream_open = True
        print(text, end="", file=self._text_output, flush=True)

    def finish_stream(self) -> None:
        if self._stream_open:
            print(file=self._text_output, flush=True)
            self._stream_open = False
