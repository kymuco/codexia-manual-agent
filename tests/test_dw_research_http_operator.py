"""Offline daily-use Research HTTP operator tests with real Gen2 SQLite."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from codexia_manual_agent.providers.chatgpt_http import ChatGPTHttpProvider
from codexia_manual_agent.providers.model_provider_cognition import (
    ModelProviderCognitionPort,
)
from codexia_manual_agent.standalone_work import StandaloneWorkSurface
from codexia_manual_agent.work_core import SqliteWorkStore
from codexia_manual_agent.workflow_runtime.research_v1 import (
    STAGE_CRITIQUE,
    STAGE_INITIAL,
    STAGE_REVISION,
    STAGE_SYNTHESIS,
    ResearchRoleOutput,
)
from tools import dw_research_http as operator


def _output(
    stage,
    content,
    *,
    human=False,
    synthesis=False,
):
    return ResearchRoleOutput(
        schema_version=1,
        stage=stage,
        content=content,
        needs_human=human,
        human_question="Which design should be preferred?" if human else None,
        human_reason="User tradeoff is not specified." if human else None,
        objective_coverage=True if synthesis else None,
        evidence_sufficiency=True if synthesis else None,
        material_unresolved_uncertainty=False if synthesis else None,
    ).to_json()


class FakeHTTP:
    def __init__(self, *, human=False):
        self.turns = [
            _output(STAGE_INITIAL, "Initial analysis", human=human),
            _output(STAGE_CRITIQUE, "Critique"),
            _output(STAGE_REVISION, "Revised analysis"),
            _output(STAGE_SYNTHESIS, "# Final useful report", synthesis=True),
        ]
        self.calls = 0
        self.auth_reads = 0
        self.reads = 0
        self.changed_readback = False

    def get_messages(self, conversation_id, **kwargs):
        self.reads += 1
        index = int(conversation_id.split("-")[-1])
        return [
            SimpleNamespace(
                node_id=f"n-{index}",
                message_id=f"m-{index}",
                role="assistant",
                text=(
                    "TAMPERED"
                    if self.changed_readback
                    else self.turns[index]
                ),
                create_time=None,
                recipient="all",
                model="gpt-6",
                finish_reason="stop",
            )
        ]

    def refresh_auth(self, *, persist):
        assert persist is False
        self.auth_reads += 1
        return SimpleNamespace(
            status_code=200, access_token_present=True, persisted=False
        )

    def send(self, prompt, **kwargs):
        index = self.calls
        self.calls += 1
        assert kwargs["model"] == "gpt-6"
        assert kwargs["reasoning_effort"] == "high"
        return SimpleNamespace(
            text=self.turns[index],
            conversation=SimpleNamespace(
                conversation_id=f"c-{index}",
                message_id=f"m-{index}",
                parent_message_id=f"m-{index}",
                finish_reason="stop",
            ),
            request=None,
            metrics=None,
        )


def _start(store):
    return StandaloneWorkSurface(store).start(
        objective="Compare A and B and prepare a bounded summary",
        selector=operator._selector(),
        host_factory=lambda: operator._host(store, None),
    )["work"]["work_id"]


def _port(http):
    return ModelProviderCognitionPort(
        ChatGPTHttpProvider(
            client=http, model="gpt-6", reasoning_effort="high"
        )
    )


def test_research_operator_four_roles_and_export(tmp_path: Path) -> None:
    path = tmp_path / "work.sqlite"
    store = SqliteWorkStore(path)
    work_id = _start(store)
    http = FakeHTTP()
    destination = tmp_path / "result.md"

    result = operator._execute(store, work_id, _port(http), out=destination)

    assert http.calls == 4
    assert http.auth_reads == 4
    assert result["work_id"] == work_id
    assert result["yield"] == "completion"
    assert result["output"] == str(destination)
    assert destination.read_text(encoding="utf-8") == "# Final useful report"
    assert result["export_sha256"]

    # Completing the same Work again must not create a fifth HTTP turn.
    reopened = SqliteWorkStore(path)
    before = reopened.events(work_id)
    again = operator._execute(reopened, work_id, _port(http))
    assert again["yield"] == "completion"
    assert http.calls == 4
    assert reopened.events(work_id) == before

    with pytest.raises(FileExistsError):
        operator._export(reopened, work_id, destination)


def test_human_attention_stops_and_resumes_without_replay(tmp_path: Path) -> None:
    store = SqliteWorkStore(tmp_path / "attention.sqlite")
    work_id = _start(store)
    http = FakeHTTP(human=True)
    port = _port(http)

    first = operator._execute(store, work_id, port)
    assert first["yield"] == "attention"
    assert first["human_question"] == "Which design should be preferred?"
    assert http.calls == 1
    assert http.auth_reads == 1

    again = operator._execute(store, work_id, port)
    assert again["yield"] == "attention"
    assert http.calls == 1

    StandaloneWorkSurface(store).answer(
        work_id, response_text="Prefer B for fault isolation."
    )
    final = operator._execute(store, work_id, port, out=tmp_path / "answer.md")
    assert final["yield"] == "completion"
    assert http.calls == 4
    assert http.auth_reads == 4


def test_verified_http_adapter_requires_exact_readback() -> None:
    from codexia_manual_agent.domain.models import ProviderRequest

    http = FakeHTTP()
    provider = operator._VerifiedHTTPProvider(
        client=http, model="gpt-6", reasoning_effort="high"
    )
    result = provider.send(ProviderRequest(prompt="Research one step"))
    assert result.text == http.turns[0]
    assert http.calls == 1
    assert http.reads == 1

    http.changed_readback = True
    with pytest.raises(ValueError, match="read-only ChatGPT history"):
        provider.send(ProviderRequest(prompt="Different new step"))
    assert http.calls == 2
    assert http.reads == 2


def test_objective_snapshots_only_named_text_files(tmp_path: Path) -> None:
    reference = tmp_path / "handoff.md"
    reference.write_text("Decision: retain explicit human approval", encoding="utf-8")

    objective = operator._objective("Review decision", [str(reference)])
    assert "Decision: retain explicit human approval" in objective
    assert "[Untrusted reference document: handoff.md" in objective

    with pytest.raises(ValueError, match="unique"):
        operator._objective("Task", [str(reference), str(reference)])
    with pytest.raises(ValueError, match="budget"):
        operator._objective("x" * 17_000, [])
    with pytest.raises(ValueError, match="Markdown/TXT"):
        invalid = tmp_path / "file.pdf"
        invalid.write_bytes(b"Not a supported file")
        operator._objective("Task", [str(invalid)])


def test_run_without_send_only_creates_local_work(tmp_path: Path, capsys) -> None:
    store = tmp_path / "local.sqlite"
    exit_code = operator.main([
        "--store", str(store),
        "run", "Prepare a bounded local task",
    ])
    assert exit_code == 0
    output = capsys.readouterr().out
    assert "WORK_ID=" in output
    assert '"revision": 2' in output
    assert '"yield": "none"' in output


def test_resume_without_send_is_read_only(tmp_path: Path, capsys) -> None:
    path = tmp_path / "work.sqlite"
    store = SqliteWorkStore(path)
    work_id = _start(store)
    previous = store.events(work_id)
    exit_code = operator.main([
        "--store", str(path), "resume", work_id
    ])
    assert exit_code == 0
    assert '"NO_SEND"' in capsys.readouterr().out
    assert store.events(work_id) == previous
