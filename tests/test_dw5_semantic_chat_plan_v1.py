"""Offline DW5 semantic interpreter tests: durable Gen2, no shell effects."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from codexia_manual_agent.role_core import CognitionOutcome, project_role_runs
from codexia_manual_agent.standalone_work import StandaloneWorkSurface
from codexia_manual_agent.work_core import SqliteWorkStore
from codexia_manual_agent.workflow_runtime.chat_command_plan_v1 import (
    INSTRUCTIONS,
    ChatCommandSource,
    CommandPlan,
    PlannedStep,
    _sha,
)
from tools import dw_chat_plan_v1 as operator


def _reply():
    tick = chr(96) * 3
    return (
        "Для проверки истории сначала выполни в PowerShell:\n\n"
        f"{tick}powershell\n"
        "@'\nfrom pathlib import Path\nPath('pi-15.md').write_text('ok')\n"
        "'@ | python -\n"
        f"{tick}\n\n"
        "Этот блок создаёт файл в текущем каталоге. "
        "А вот просто пример команды для других ситуаций, НЕ запускай:\n\n"
        f"{tick}bash\nrm -rf build\n{tick}\n"
    )


def _source(index=1, root="W:\\dev\\project"):
    text = _reply()
    return ChatCommandSource(
        project=f"project-{index}",
        repo=f"person/project-{index}",
        conversation_id=f"00000000-0000-4000-8000-{index:012d}",
        source_message_id=f"11111111-1111-4111-8111-{index:012d}",
        source_text=text, source_sha256=_sha(text), workspace=root,
    )


def _plan(source, *, decision="commands", command=None):
    command = command or (
        "@'\nfrom pathlib import Path\nPath('pi-15.md').write_text('ok')\n"
        "'@ | python -"
    )
    steps = (
        [PlannedStep(
            step=1, shell="powershell", command=command,
            cwd=".", purpose="Create pi-15.md in the project workspace.",
        ).to_dict()]
        if decision == "commands" else []
    )
    return {
        "schema_version": 1,
        "source_sha256": source.source_sha256,
        "decision": decision, "steps": steps,
        "reason": "Only the first command is requested for execution."
        if decision == "commands" else "Nothing actionable in this reply.",
    }


def _started(store, source):
    return StandaloneWorkSurface(store).start(
        objective=source.to_json(), selector=operator._selector(),
        host_factory=lambda: operator._host(store),
        source_id=source.conversation_id + ":" + source.source_message_id,
    )["work"]["work_id"]


class _Port:
    port_id = "dw5-semantic-fake"

    def __init__(self, text: str):
        self.text = text
        self.calls = 0
        self.materialized_context = None
        self.materialized_instructions = None

    def complete(self, request):
        self.calls += 1
        self.materialized_context = request.request.context
        self.materialized_instructions = request.request.instructions
        return CognitionOutcome.succeeded(
            request.request, output_text=self.text
        )


class _Reader:
    def __init__(self, text, source):
        self.text, self.source = text, source

    def get_messages(self, conversation_id, *, limit):
        assert conversation_id == self.source.conversation_id
        assert limit == 1
        return [SimpleNamespace(
            role="assistant", message_id=self.source.source_message_id, text=self.text
        )]


def test_normal_reply_here_string_survives_role_and_plan(tmp_path) -> None:
    source = _source(root=str(tmp_path))
    store = SqliteWorkStore(tmp_path / "interpret.sqlite")
    work_id = _started(store, source)
    port = _Port(json.dumps(_plan(source)))
    result = operator._advance(store, work_id, port)
    assert result["status"] == "PLAN_READY_UNEXECUTED"
    assert result["command_effects"] == "NONE"
    assert result["plan"]["steps"][0]["command"].startswith("@'\n")
    assert "rm -rf" not in result["plan"]["steps"][0]["command"]
    assert result["plan"]["steps"][0]["cwd"] == "."
    assert port.calls == 1
    assert port.materialized_instructions == INSTRUCTIONS
    materialized = ChatCommandSource.parse(port.materialized_context)
    assert materialized == source
    assert "НЕ запускай" in port.materialized_context
    assert len(project_role_runs(store.events(work_id))) == 1
    events = store.events(work_id)
    kinds = [event.kind for event in events]
    assert kinds.count("role.cognition-handoff-admitted") == 1
    assert kinds.count("role.completed") == 1
    assert not any(x.startswith("capability.") for x in kinds)

    restarted = SqliteWorkStore(tmp_path / "interpret.sqlite")
    before = restarted.events(work_id)
    recovered = operator._advance(restarted, work_id, port)
    assert recovered["status"] == "PLAN_READY_UNEXECUTED"
    assert restarted.events(work_id) == before
    assert port.calls == 1


@pytest.mark.parametrize("decision", ["no_commands", "uncertain"])
def test_no_commands_and_uncertainty_are_valid_without_effects(tmp_path, decision):
    source = _source(root=str(tmp_path))
    store = SqliteWorkStore(tmp_path / (decision + ".sqlite"))
    work_id = _started(store, source)
    port = _Port(json.dumps(_plan(source, decision=decision)))
    result = operator._advance(store, work_id, port)
    assert result["status"] == "PLAN_READY_UNEXECUTED"
    assert result["plan"]["steps"] == []
    assert result["plan"]["decision"] == decision
    assert port.calls == 1


def test_malformed_role_stored_exact_no_dispatch_after_restart(tmp_path):
    source = _source(root=str(tmp_path))
    store = SqliteWorkStore(tmp_path / "malformed.sqlite")
    work_id = _started(store, source)
    port = _Port("The commands look fine; just run them.")
    result = operator._advance(store, work_id, port)
    assert result["status"] == "INVALID_PLAN_PRESERVED"
    assert result["recovery"] == "export-raw"
    assert port.calls == 1
    original = project_role_runs(store.events(work_id))[0].output_text
    assert original == port.text

    output = tmp_path / "raw.txt"
    assert operator._export(store, work_id, output, raw=True)["status"] == "RAW_ROLE_EXPORT_ONLY"
    assert output.read_text(encoding="utf-8").strip() == port.text
    with pytest.raises(FileExistsError):
        operator._export(store, work_id, output, raw=True)
    assert operator._advance(store, work_id, port)["status"] == "INVALID_PLAN_PRESERVED"
    assert port.calls == 1
    assert project_role_runs(store.events(work_id))[0].output_text == original


def test_five_projects_have_independent_work_and_source_bindings(tmp_path):
    store = SqliteWorkStore(tmp_path / "shared.sqlite")
    ids = []
    for i in range(1, 6):
        source = _source(i, root=str(tmp_path))
        work_id = _started(store, source)
        ids.append(work_id)
        port = _Port(json.dumps(_plan(source)))
        result = operator._advance(store, work_id, port)
        assert result["project"] == f"project-{i}"
        assert result["repo"] == f"person/project-{i}"
        assert result["conversation_id"] == source.conversation_id
        assert result["source_message_id"] == source.source_message_id
        assert result["status"] == "PLAN_READY_UNEXECUTED"
    assert len(set(ids)) == 5
    assert len({
        project_role_runs[0].run.work_id
        for work_id in ids
        if (project_role_runs := project_role_runs_for(store, work_id))
    }) == 5


def project_role_runs_for(store, work_id):
    return project_role_runs(store.events(work_id))


@pytest.mark.parametrize("mutation", [
    lambda data: data.update({"decision": "commands", "steps": []}),
    lambda data: data.update({"decision": "no_commands"}),
    lambda data: data.update({"source_sha256": "0" * 64}),
    lambda data: data["steps"][0].update({"step": 2}),
    lambda data: data["steps"][0].update({"cwd": "../private"}),
    lambda data: data["steps"][0].update({"cwd": "C:/Users/admin"}),
    lambda data: data["steps"][0].update({"command": "echo ok\x00"}),
    lambda data: data.update({"unexpected": "shell"}),
])
def test_strict_plan_rejects_mutations(mutation):
    source = _source()
    data = _plan(source)
    mutation(data)
    with pytest.raises((ValueError, TypeError)):
        CommandPlan.parse(json.dumps(data), source=source)


def test_model_output_escape_wrapper_and_digest():
    source = _source()
    original = _plan(source)
    wrapped = "<escape>" + json.dumps(original) + "</escape>"
    result = CommandPlan.parse(wrapped, source=source)
    assert result.to_dict() == original
    assert len(result.digest) == 64
    assert "\n" in result.steps[0].command


def test_command_source_version_hash_and_length_fail_closed():
    source = _source()
    assert ChatCommandSource.parse(source.to_json()) == source
    data = source.to_dict()
    data["schema_version"] = 2
    with pytest.raises(ValueError, match="schema"):
        ChatCommandSource.parse(json.dumps(data))
    data = source.to_dict()
    data["source_sha256"] = "f" * 64
    with pytest.raises(ValueError, match="SHA mismatch"):
        ChatCommandSource.parse(json.dumps(data))


def test_latest_assistant_read_only_no_command_regex(tmp_path):
    source = _source(root=str(tmp_path))
    client = _Reader(source.source_text, source)
    args = SimpleNamespace(
        project=source.project, repo=source.repo,
        conversation=source.conversation_id,
        message_id=source.source_message_id,
        workspace=str(tmp_path),
    )
    captured = operator._source_from_message(args, client)
    assert captured == source
    assert "rm -rf build" in captured.source_text
    args.message_id = "22222222-2222-4222-8222-222222222222"
    with pytest.raises(ValueError, match="current chat head"):
        operator._source_from_message(args, client)
