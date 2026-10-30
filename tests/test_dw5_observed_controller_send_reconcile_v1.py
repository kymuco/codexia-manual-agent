"""Offline observed-send reconciliation: no model resend and exact audit."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from codexia_manual_agent.workflow_runtime.chat_command_plan_v1 import (
    ChatCommandSource,
    _sha,
)
from tools.dw_codexia_controller_continue_v1 import _prompt
from tools.dw5_observed_controller_send_reconcile_v1 import (
    DEFAULT_REASON,
    TABLE,
    inspect_observed,
    reconcile_observed,
)

PROJECT = "player-intelligence"
WORKER_CONV = "6adb4dd7-9ea0-83ed-bb9f-285ce4706b06"
CONTROLLER_CONV = "6adde286-b844-83ed-b009-0c285194716d"
PREDECESSOR = "938a6213-2a6a-4657-b886-3fee5aba2bea"
OLD_WORKER = "4d2363ac-47f5-4333-b38f-1664bc359262"
WORKER = "a57787c5-91b3-4fa8-814d-c81fda54a8e4"
REQUEST = "30bc9d10-6ea9-47b1-a4d1-f13b6a6d86e4"
RESPONSE = "18fcb124-bec1-473f-b79b-5a7fdcdbf278"
WORKER_TEXT = "No local commands requested. Report the latest admission state."


class ReadOnlyChat:
    def __init__(self, messages):
        self.messages = messages
        self.calls = []

    def get_messages(self, conversation, **kwargs):
        self.calls.append((conversation, kwargs))
        if conversation == WORKER_CONV:
            assert kwargs == {"limit": 1}
            return [self.messages["worker"]]
        assert conversation == CONTROLLER_CONV
        assert kwargs == {"limit": 3, "include_empty": True}
        return [
            self.messages["predecessor"],
            self.messages["request"],
            self.messages["response"],
        ]


@pytest.fixture
def scenario(tmp_path):
    path = tmp_path / "store.sqlite3"
    workspace = str(tmp_path / "workspace")
    source = ChatCommandSource(
        project=PROJECT,
        repo="kymuco/player-intelligence",
        workspace=workspace,
        conversation_id=WORKER_CONV,
        source_message_id=WORKER,
        source_text=WORKER_TEXT,
        source_sha256=_sha(WORKER_TEXT),
    )
    response_text = json.dumps({
        "schema_version": 1,
        "source_sha256": source.source_sha256,
        "decision": "no_commands",
        "steps": [],
        "reason": "Worker requested no local commands",
    })
    messages = {
        "worker": SimpleNamespace(
            role="assistant", message_id=WORKER, text=WORKER_TEXT,
        ),
        "predecessor": SimpleNamespace(
            role="assistant", message_id=PREDECESSOR, text="previous",
        ),
        "request": SimpleNamespace(
            role="user", message_id=REQUEST, text=_prompt(source, DEFAULT_REASON),
        ),
        "response": SimpleNamespace(
            role="assistant", message_id=RESPONSE, text=response_text,
            finish_reason="stop",
        ),
    }
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE dw5_project_binding_v1 (
                project TEXT PRIMARY KEY, phase TEXT,
                worker_conversation TEXT, controller_conversation TEXT,
                repo TEXT, workspace TEXT
            );
            CREATE TABLE dw5_controller_turn_v1 (
                project TEXT, predecessor_message_id TEXT,
                worker_message_id TEXT, worker_sha256 TEXT,
                controller_message_id TEXT, phase TEXT,
                PRIMARY KEY(project, predecessor_message_id)
            );
            CREATE TABLE dw5_controller_presend_recovery_v1 (
                project TEXT, predecessor_message_id TEXT,
                worker_message_id TEXT, worker_sha256 TEXT,
                original_phase TEXT, controller_message_id TEXT,
                reconciliation_digest TEXT
            );
            CREATE TABLE dw5_controller_presend_recovery_v2 (
                project TEXT, predecessor_message_id TEXT,
                worker_message_id TEXT, worker_sha256 TEXT,
                original_phase TEXT, controller_message_id TEXT,
                previous_reconciliation_digest TEXT,
                second_reconciliation_digest TEXT
            );
        """)
        db.execute(
            "INSERT INTO dw5_project_binding_v1 VALUES (?, ?, ?, ?, ?, ?)",
            (PROJECT, "ready", WORKER_CONV, CONTROLLER_CONV,
             source.repo, source.workspace),
        )
        db.execute(
            "INSERT INTO dw5_controller_turn_v1 VALUES (?, ?, ?, ?, ?, ?)",
            (PROJECT, "34214c36-6fa4-46ed-a879-59ab8fc9ba02",
             OLD_WORKER, "a" * 64, PREDECESSOR, "ready"),
        )
        db.execute(
            "INSERT INTO dw5_controller_turn_v1 VALUES (?, ?, ?, ?, ?, ?)",
            (PROJECT, PREDECESSOR, WORKER, source.source_sha256,
             None, "send_admitted"),
        )
        db.execute(
            "INSERT INTO dw5_controller_presend_recovery_v1 VALUES (?, ?, ?, ?, ?, ?, ?)",
            (PROJECT, PREDECESSOR, WORKER, source.source_sha256,
             "send_admitted", None, "1" * 64),
        )
        db.execute(
            "INSERT INTO dw5_controller_presend_recovery_v2 VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (PROJECT, PREDECESSOR, WORKER, source.source_sha256,
             "send_admitted", None, "1" * 64, "2" * 64),
        )

    chat = ReadOnlyChat(messages)
    kwargs = dict(
        registry=path, project=PROJECT, predecessor=PREDECESSOR,
        worker_message_id=WORKER, request_message_id=REQUEST,
        response_message_id=RESPONSE,
        response_sha256_prefix=_sha(response_text)[:16],
        client=chat,
    )
    return path, chat, messages, kwargs


def _state(path: Path):
    with sqlite3.connect(path) as db:
        return db.execute(
            "SELECT phase, controller_message_id FROM dw5_controller_turn_v1 "
            "WHERE project=? ORDER BY rowid DESC LIMIT 1", (PROJECT,),
        ).fetchone()


def _approval(prompt):
    return prompt.split("Type ", 1)[1].split(": ", 1)[0]


def test_inspect_is_read_only_and_binds_exact_request_response(scenario):
    path, chat, _, kwargs = scenario
    before = path.read_bytes()
    result = inspect_observed(**kwargs)
    assert path.read_bytes() == before
    assert result["status"] == "COMPLETED_CONTROLLER_SEND_OBSERVED_READ_ONLY"
    assert result["decision"] == "no_commands"
    assert result["steps"] == 0
    assert result["response_finish_reason"] == "stop"
    assert result["v1_archive_digest"] == "1" * 64
    assert result["v2_archive_digest"] == "2" * 64
    assert result["effects"] == "NONE"
    assert len(chat.calls) == 2
    assert _state(path) == ("send_admitted", None)


def test_reconciliation_is_transactional_and_never_sends(scenario):
    path, chat, _, kwargs = scenario
    result = reconcile_observed(**kwargs, confirm=_approval)
    assert result["status"] == "OBSERVED_CONTROLLER_SEND_RECONCILED_NO_SEND"
    assert result["effects"] == "SQLITE_AUDIT_AND_READY_ONLY"
    assert result["controller_message_id"] == RESPONSE
    assert _state(path) == ("ready", RESPONSE)
    assert len(chat.calls) == 4

    with sqlite3.connect(path) as db:
        row = db.execute(
            f"SELECT response_message_id, response_sha256, reconciliation_digest "
            f"FROM {TABLE}"
        ).fetchone()
        assert row == (
            RESPONSE, kwargs["response_sha256_prefix"].ljust(64, "0")
            if False else _sha(chat.messages["response"].text),
            result["reconciliation_digest"],
        )
        assert db.execute(
            "SELECT second_reconciliation_digest "
            "FROM dw5_controller_presend_recovery_v2"
        ).fetchone()[0] == "2" * 64
    with pytest.raises(ValueError, match="exact ambiguous"):
        inspect_observed(**kwargs)


@pytest.mark.parametrize("tamper", [
    "request_text", "request_id", "response_finish", "response_id",
    "invalid_json", "source_hash", "different_head",
])
def test_tampered_canonical_branch_never_mutates(scenario, tamper):
    path, _, messages, kwargs = scenario
    if tamper == "request_text":
        messages["request"].text += " not submitted originally"
    elif tamper == "request_id":
        messages["request"].message_id = PREDECESSOR
    elif tamper == "response_finish":
        messages["response"].finish_reason = None
    elif tamper == "response_id":
        messages["response"].message_id = "c3e4e557-3d6e-45ad-81a4-9de8247ee312"
    elif tamper == "invalid_json":
        messages["response"].text = "{bad-json"
        kwargs["response_sha256_prefix"] = _sha(messages["response"].text)[:16]
    elif tamper == "source_hash":
        messages["worker"].text += " changed"
    elif tamper == "different_head":
        messages["predecessor"].message_id = "c3e4e557-3d6e-45ad-81a4-9de8247ee312"

    original = path.read_bytes()
    with pytest.raises(ValueError):
        reconcile_observed(**kwargs, confirm=_approval)
    assert path.read_bytes() == original
    assert _state(path) == ("send_admitted", None)


def test_denied_approval_preserves_fence(scenario):
    path, _, _, kwargs = scenario
    original = path.read_bytes()
    with pytest.raises(ValueError, match="human declined"):
        reconcile_observed(**kwargs, confirm=lambda _: "DENIED")
    assert path.read_bytes() == original


def test_prior_archive_chain_mismatch_is_rejected(scenario):
    path, _, _, kwargs = scenario
    with sqlite3.connect(path) as db:
        db.execute(
            "UPDATE dw5_controller_presend_recovery_v2 "
            "SET previous_reconciliation_digest=?",
            ("broken",),
        )
    with pytest.raises(ValueError, match="prior audit chain"):
        inspect_observed(**kwargs)
    assert _state(path) == ("send_admitted", None)
