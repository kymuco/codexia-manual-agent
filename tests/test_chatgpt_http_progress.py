"""Offline contract tests for human-visible Codexia HTTP progress."""

from __future__ import annotations

from io import StringIO
from types import SimpleNamespace

import pytest

from codexia_manual_agent.domain.errors import ProviderError
from codexia_manual_agent.domain.models import ProviderRequest
from codexia_manual_agent.providers.chatgpt_http import ChatGPTHttpProvider
from codexia_manual_agent.providers.chatgpt_http_progress import HttpProgressConsole


class StreamingHTTP:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls = 0

    def send(self, prompt, **kwargs):
        self.calls += 1
        if callback := kwargs.get("on_event"):
            callback({"type": "stream_started", "auth": "SHOULD_NOT_BE_LOGGED"})
        if callback := kwargs.get("on_token"):
            callback("Hello")
            callback(" world")
        if self.error:
            raise self.error
        return SimpleNamespace(
            text="Hello world",
            conversation=SimpleNamespace(
                conversation_id="conversation-1",
                message_id="message-2",
                parent_message_id="message-2",
                finish_reason="stop",
            ),
            request=SimpleNamespace(
                observed_model="gpt-6",
                sent_model="gpt-6",
                observed_reasoning_effort="extended",
                sent_reasoning_effort="extended",
            ),
            metrics=None,
        )


def test_operator_sees_ordered_stages_and_opt_in_text() -> None:
    progress, tokens = StringIO(), StringIO()
    reporter = HttpProgressConsole(
        stream_text=True, progress_output=progress, text_output=tokens
    )
    reporter.emit("WORK", {"work_id": "known-work", "revision": 4})
    reporter.emit("HANDOFF", {"status": "committed", "revision": 5})
    http = StreamingHTTP()
    provider = ChatGPTHttpProvider(
        client=http,
        model="gpt-6",
        reasoning_effort="high",
        progress=reporter.emit,
        on_token=reporter.token,
    )

    result = provider.send(ProviderRequest(prompt="PRIVATE PROMPT"))
    reporter.finish_stream()
    reporter.emit(
        "RESULT",
        {"status": "captured", "message_id": result.conversation.message_id},
    )

    out = progress.getvalue()
    assert http.calls == 1
    assert result.text == "Hello world"
    assert "WORK" in out and "HANDOFF" in out
    assert "HTTP_CALL" in out and "FIRST_TOKEN" in out
    assert "HTTP_RETURN" in out and "HTTP_IDENTITY" in out
    assert "MODEL_ROUTE" in out
    assert "requested_model=gpt-6" in out
    assert "requested_reasoning_effort=high" in out
    assert "sent_reasoning_effort=extended" in out
    assert "observed_reasoning_effort=extended" in out
    assert "message_id=message-2" in out and "RESULT" in out
    assert "attempt_started_not_receipt" in out
    assert "PRIVATE PROMPT" not in out
    assert "SHOULD_NOT_BE_LOGGED" not in out
    assert "Hello world" not in out
    assert "Hello world" in tokens.getvalue()
    numbers = [int(line.split("]")[0].split("[")[1].split()[0]) for line in out.splitlines()]
    assert numbers == list(range(1, len(numbers) + 1))


def test_progress_default_hides_model_text_and_private_metadata() -> None:
    progress, tokens = StringIO(), StringIO()
    reporter = HttpProgressConsole(progress_output=progress, text_output=tokens)
    reporter.emit(
        "INPUT",
        {
            "files": 3,
            "total_bytes": 1200,
            "raw_context": "PRIVATE CONTEXT",
            "token": "SECRET",
            "status": "ready",
            "model": "gpt-6-instant",
            "sha256": "d" * 64,
        },
    )
    reporter.token("PRIVATE ANSWER")
    reporter.finish_stream()
    assert "files=3" in progress.getvalue()
    assert "sha256=" + "d" * 64 in progress.getvalue()
    assert "token=" not in progress.getvalue()
    assert "raw_context=" not in progress.getvalue()
    assert "PRIVATE" not in progress.getvalue()
    assert tokens.getvalue() == ""


def test_untrusted_progress_values_and_stage_rejected_or_elided() -> None:
    progress = StringIO()
    reporter = HttpProgressConsole(progress_output=progress)
    with pytest.raises(ValueError, match="unrecognized"):
        reporter.emit("RAW_NETWORK_PAYLOAD", {"status": "unused"})
    reporter.emit(
        "RESULT",
        {
            "status": "contains space or\nnewline",
            "conversation_id": "Bearer secret",
            "error_type": "TimeoutError",
            "retry": "forbidden",
        },
    )
    out = progress.getvalue()
    assert "contains space" not in out
    assert "Bearer" not in out
    assert "error_type=TimeoutError" in out
    assert "retry=forbidden" in out


def test_error_shows_unknown_without_secret_or_automatic_replay() -> None:
    progress = StringIO()
    reporter = HttpProgressConsole(progress_output=progress)
    http = StreamingHTTP(TimeoutError("token=private-secret"))
    provider = ChatGPTHttpProvider(client=http, progress=reporter.emit)
    with pytest.raises(ProviderError, match="do not retry") as error:
        provider.send(ProviderRequest(prompt="PRIVATE PROMPT"))
    assert http.calls == 1
    out = progress.getvalue()
    assert "HTTP_CALL" in out
    assert "OUTCOME_UNKNOWN" in out
    assert "retry=forbidden" in out
    assert "TimeoutError" in out
    assert "private-secret" not in out
    assert "PRIVATE PROMPT" not in out
    assert "private-secret" not in str(error.value)


def test_progress_writer_exception_does_not_abort_sent_request() -> None:
    http = StreamingHTTP()
    errors = []

    def failing_logger(stage, fields):
        errors.append(stage)
        raise OSError("broken progress console")

    def failing_token(text):
        raise OSError("broken token console")

    provider = ChatGPTHttpProvider(
        client=http,
        progress=failing_logger,
        on_token=failing_token,
    )
    assert provider.send(ProviderRequest(prompt="once")).text == "Hello world"
    assert http.calls == 1
    assert "HTTP_CALL" in errors
    assert "HTTP_IDENTITY" in errors


def test_keyboard_interrupt_is_observed_as_ambiguous_without_retry() -> None:
    progress = StringIO()
    reporter = HttpProgressConsole(progress_output=progress)

    class InterruptedHTTP:
        def __init__(self):
            self.calls = 0

        def send(self, prompt, **kwargs):
            self.calls += 1
            raise KeyboardInterrupt()

    http = InterruptedHTTP()
    provider = ChatGPTHttpProvider(client=http, progress=reporter.emit)
    with pytest.raises(KeyboardInterrupt):
        provider.send(ProviderRequest(prompt="One attempt"))
    assert http.calls == 1
    assert "OUTCOME_UNKNOWN" in progress.getvalue()
    assert "retry=forbidden" in progress.getvalue()
