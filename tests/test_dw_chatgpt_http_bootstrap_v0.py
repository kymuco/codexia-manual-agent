"""Offline exact Work and no-replay tests for observed HTTP bootstrap."""

from __future__ import annotations

from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import pytest

from codexia_manual_agent.providers.chatgpt_http_progress import HttpProgressConsole
from codexia_manual_agent.work_core import SqliteWorkStore, Work, WorkIngressBinding
from tools import dw_chatgpt_http_bootstrap_v0 as runner
from tools.dw_chatgpt_bootstrap_v0 import CLAIM_KIND, CAPTURE_KIND, _claim_once


def case(tmp_path: Path):
    store = SqliteWorkStore(tmp_path / "work.sqlite3")
    work = Work.create(
        objective="A bounded Codexia project review",
        ingress=WorkIngressBinding.create(
            source_namespace="test.http",
            source_id="one-work",
            payload_digest="a" * 64,
        ),
    )
    snapshot = store.create(work)
    status = {
        "work": {
            "state": snapshot.state.value,
            "work_id": work.work_id,
            "work_digest": work.work_digest,
            "revision": snapshot.revision,
            "objective": work.objective,
        },
        "workflow": [{"pack": {"pack_id": "pinned"}}],
        "yield": {"kind": "none"},
        "unresolved": {
            "roles_total": 0,
            "capabilities_total": 0,
            "children_live_total": 0,
        },
    }
    file_path = tmp_path / "handoff.md"
    file_path.write_bytes(b"Initial bounded handoff.\n")
    progress = StringIO()
    reporter = HttpProgressConsole(progress_output=progress, text_output=StringIO())
    return store, status, file_path, reporter, progress


class HTTP:
    provider_id = "chatgpt-http"

    def __init__(
        self, *, error=None, canonical="Right answer", preflight_error=None
    ) -> None:
        self.calls = []
        self.preflight_calls = 0
        self.preflight_error = preflight_error
        self.error = error
        self.canonical = canonical

    def preflight_read_only(self):
        self.preflight_calls += 1
        if self.preflight_error is not None:
            raise self.preflight_error

    def send(self, request, *, media):
        self.calls.append((request, media))
        if self.error is not None:
            raise self.error
        return SimpleNamespace(
            text="Right answer",
            conversation=SimpleNamespace(
                conversation_id="conversation-1", message_id="message-1"
            ),
        )

    def read_messages(self, conversation_id):
        return [
            SimpleNamespace(role="user", message_id="user-1", text="not the answer"),
            SimpleNamespace(
                role="assistant", message_id="message-1", text=self.canonical
            ),
        ]


def test_dry_run_does_not_send_or_modify_work(tmp_path) -> None:
    store, status, file_path, reporter, progress = case(tmp_path)
    http = HTTP()
    result = runner.bootstrap_http_once(
        status=status,
        files=[str(file_path)],
        store=store,
        reporter=reporter,
        client=http,
        commit=False,
    )
    assert result["status"] == "DRY_RUN"
    assert result["transport"] == "chatgpt-http"
    assert result["files"][0]["sha256"]
    assert "WORK" in progress.getvalue()
    assert "INPUT" in progress.getvalue()
    assert "dry_run_no_effect" in progress.getvalue()
    assert http.calls == []
    assert store.events(status["work"]["work_id"]) == ()


def test_gpt6_high_mode_is_pinned_before_http_write(tmp_path) -> None:
    store, status, file_path, reporter, _ = case(tmp_path)
    http = HTTP()
    configured = dict(
        status=status,
        files=[str(file_path)],
        store=store,
        reporter=reporter,
        client=http,
        model="gpt-6",
        reasoning_effort="high",
    )
    preview = runner.bootstrap_http_once(commit=False, **configured)
    assert preview["status"] == "DRY_RUN"
    assert preview["model"] == "gpt-6"
    assert preview["reasoning_effort"] == "high"
    assert http.calls == []
    assert store.events(status["work"]["work_id"]) == ()

    result = runner.bootstrap_http_once(commit=True, **configured)
    assert result["status"] == "CAPTURED_UNADMITTED"
    events = store.events(status["work"]["work_id"])
    assert events[0].payload["plan"]["model"] == "gpt-6"
    assert events[0].payload["plan"]["reasoning_effort"] == "high"
    assert len(http.calls) == 1


def test_invalid_effort_never_claims_or_sends(tmp_path) -> None:
    store, status, file_path, reporter, _ = case(tmp_path)
    http = HTTP()
    with pytest.raises(ValueError, match="reasoning effort"):
        runner.bootstrap_http_once(
            status=status,
            files=[str(file_path)],
            store=store,
            reporter=reporter,
            client=http,
            commit=True,
            reasoning_effort="low",
        )
    assert store.events(status["work"]["work_id"]) == ()
    assert not http.calls


def test_exact_frozen_file_bytes_and_capture_with_readback(tmp_path) -> None:
    store, status, file_path, reporter, progress = case(tmp_path)
    http = HTTP()
    result = runner.bootstrap_http_once(
        status=status,
        files=[str(file_path)],
        store=store,
        reporter=reporter,
        client=http,
        commit=True,
    )
    assert result["status"] == "CAPTURED_UNADMITTED"
    assert result["conversation_id"] == "conversation-1"
    assert http.preflight_calls == 1
    assert len(http.calls) == 1
    sent_request, media = http.calls[0]
    assert "transport role is not human authorship" in sent_request.prompt
    assert media == [(b"Initial bounded handoff.\n", "handoff.md")]
    events = store.events(status["work"]["work_id"])
    assert [event.kind for event in events] == [CLAIM_KIND, CAPTURE_KIND]
    assert events[0].payload["plan"]["transport"] == "chatgpt-http"
    assert events[1].payload["transport"] == "chatgpt-http"
    assert events[1].payload["readback"] == "exact_assistant_id_and_text"
    assert "CLAIM" in progress.getvalue()
    assert "READBACK" in progress.getvalue()
    assert "ADMISSION" in progress.getvalue()
    assert "not_admitted" in progress.getvalue()

    # The result cannot be dispatched again, even across a fresh store instance.
    with pytest.raises(ValueError, match="previously claimed|frontier changed"):
        runner.bootstrap_http_once(
            status=status, files=[str(file_path)],
            store=SqliteWorkStore(store.path), reporter=reporter,
            client=http, commit=True,
        )
    assert len(http.calls) == 1



@pytest.mark.parametrize(
    "error",
    [
        TimeoutError("HTTP session timed out: token=PRIVATE"),
        RuntimeError("401 token_expired: token=PRIVATE"),
        OSError("HTTP session worker unavailable"),
    ],
)
def test_http_preflight_failure_before_claim_is_not_a_send(tmp_path, error) -> None:
    store, status, file_path, reporter, progress = case(tmp_path)
    http = HTTP(preflight_error=error)
    with pytest.raises(type(error)):
        runner.bootstrap_http_once(
            status=status, files=[str(file_path)], store=store,
            reporter=reporter, client=http, commit=True,
        )
    assert http.preflight_calls == 1
    assert http.calls == []
    assert store.events(status["work"]["work_id"]) == ()
    assert "http_preflight_failed_no_claim" in progress.getvalue()
    assert "PRIVATE" not in progress.getvalue()


def test_read_only_preflight_is_before_claim_not_before_dry_run(tmp_path) -> None:
    store, status, file_path, reporter, _ = case(tmp_path)
    http = HTTP()
    runner.bootstrap_http_once(
        status=status, files=[str(file_path)], store=store,
        reporter=reporter, client=http, commit=False,
    )
    assert http.preflight_calls == 0
    assert store.events(status["work"]["work_id"]) == ()
    runner.bootstrap_http_once(
        status=status, files=[str(file_path)], store=store,
        reporter=reporter, client=http, commit=True,
    )
    assert http.preflight_calls == 1
    assert len(http.calls) == 1


def test_unknown_http_result_keeps_claim_and_forbids_retry(tmp_path) -> None:
    store, status, file_path, reporter, progress = case(tmp_path)
    http = HTTP(error=TimeoutError("a remote reply could have happened"))
    with pytest.raises(TimeoutError):
        runner.bootstrap_http_once(
            status=status, files=[str(file_path)], store=store, reporter=reporter,
            client=http, commit=True,
        )
    assert [e.kind for e in store.events(status["work"]["work_id"])] == [
        CLAIM_KIND
    ]
    assert "OUTCOME_UNKNOWN" in progress.getvalue()
    assert "retry=forbidden" in progress.getvalue()
    assert len(http.calls) == 1


def test_canonical_mismatch_holds_durable_claim_and_no_capture(tmp_path) -> None:
    store, status, file_path, reporter, progress = case(tmp_path)
    http = HTTP(canonical="different answer")
    with pytest.raises(ValueError, match="did not prove exact"):
        runner.bootstrap_http_once(
            status=status, files=[str(file_path)], store=store, reporter=reporter,
            client=http, commit=True,
        )
    assert len(http.calls) == 1
    assert [e.kind for e in store.events(status["work"]["work_id"])] == [
        CLAIM_KIND
    ]
    assert "claim_present_reconcile_only" in progress.getvalue()


def test_file_change_or_duplicate_name_rejected_before_claim(tmp_path) -> None:
    store, status, file_path, reporter, progress = case(tmp_path)
    from tools.dw_chatgpt_bootstrap_v0 import _file_evidence

    evidence, _ = _file_evidence([str(file_path)])
    file_path.write_text("Changed", encoding="utf-8")
    with pytest.raises(ValueError, match="changed"):
        runner._freeze_files(evidence)

    first = tmp_path / "a"
    second = tmp_path / "b"
    first.mkdir()
    second.mkdir()
    (first / "notes.md").write_text("alpha", encoding="utf-8")
    (second / "notes.md").write_text("beta", encoding="utf-8")
    evidence, _ = _file_evidence(
        [str(first / "notes.md"), str(second / "notes.md")]
    )
    with pytest.raises(ValueError, match="share upload filename"):
        runner._freeze_files(evidence)
    assert store.events(status["work"]["work_id"]) == ()


def test_previous_browser_claim_cannot_be_redispatched_over_http(tmp_path) -> None:
    store, status, file_path, reporter, progress = case(tmp_path)
    _claim_once(
        store=store,
        work={
            "work_id": status["work"]["work_id"],
            "work_digest": status["work"]["work_digest"],
            "revision": status["work"]["revision"],
        },
        plan={"schema": "historical-browser-claim"},
    )
    http = HTTP()
    with pytest.raises(ValueError, match="previously claimed"):
        runner.bootstrap_http_once(
            status=status, files=[str(file_path)], store=store, reporter=reporter,
            client=http, commit=True,
        )
    assert http.calls == []
