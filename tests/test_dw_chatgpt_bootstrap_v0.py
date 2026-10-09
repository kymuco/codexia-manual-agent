from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("dw_chatgpt_bootstrap_v0", ROOT / "tools" / "dw_chatgpt_bootstrap_v0.py")
assert spec and spec.loader
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def status():
    return {
        "work": {"state": "active", "work_id": "w-1", "work_digest": "digest", "revision": 3, "objective": "Continue project"},
        "workflow": [{"pack": {"pack_id": "x"}}], "yield": {"kind": "none"},
        "unresolved": {"roles_total": 0, "capabilities_total": 0},
    }


class Runtime:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def send_text_observed(self, prompt, **kwargs):
        self.calls.append((prompt, kwargs))
        if self.fail:
            raise TimeoutError("ambiguous CWA write")
        return SimpleNamespace(response=SimpleNamespace(text="Propose a narrow PR", conversation=SimpleNamespace(conversation_id="chat-1", message_id="msg-1")))


def test_dry_run_no_side_effect(tmp_path):
    file = tmp_path / "decisions.md"
    file.write_text("decisions", encoding="utf-8")
    runtime = Runtime()
    result = mod.bootstrap_once(status=status(), files=[str(file)], receipt_dir=tmp_path / "attempts", runtime=runtime)
    assert result["status"] == "DRY_RUN"
    assert len(result["files"]) == 1
    assert not runtime.calls
    assert not (tmp_path / "attempts").exists()


def test_exactly_one_send_with_attachment_and_receipt(tmp_path):
    file = tmp_path / "handoff.md"
    file.write_text("handoff", encoding="utf-8")
    runtime = Runtime()
    kwargs = dict(status=status(), files=[str(file)], receipt_dir=tmp_path / "attempts", runtime=runtime, commit=True)
    result = mod.bootstrap_once(**kwargs)
    assert result["status"] == "CAPTURED_UNADMITTED"
    assert len(runtime.calls) == 1
    assert runtime.calls[0][1]["media"] == [str(file.resolve())]
    record = json.loads((tmp_path / "attempts/w-1.json").read_text())
    assert record["conversation_id"] == "chat-1"
    with pytest.raises(FileExistsError):
        mod.bootstrap_once(**kwargs)
    assert len(runtime.calls) == 1


def test_unknown_never_retried(tmp_path):
    file = tmp_path / "project_map.md"
    file.write_text("map", encoding="utf-8")
    runtime = Runtime(fail=True)
    kwargs = dict(status=status(), files=[str(file)], receipt_dir=tmp_path / "attempts", runtime=runtime, commit=True)
    with pytest.raises(TimeoutError):
        mod.bootstrap_once(**kwargs)
    record = json.loads((tmp_path / "attempts/w-1.json").read_text())
    assert record["state"] == "IN_FLIGHT_UNKNOWN"
    with pytest.raises(FileExistsError):
        mod.bootstrap_once(**kwargs)
    assert len(runtime.calls) == 1


def test_reject_invalid_work_or_file_before_send(tmp_path):
    runtime = Runtime()
    context = tmp_path / "file.md"
    context.write_text("x")
    invalid = status()
    invalid["yield"] = {"kind": "attention"}
    with pytest.raises(ValueError):
        mod.bootstrap_once(status=invalid, files=[str(context)], receipt_dir=tmp_path / "receipts", runtime=runtime, commit=True)
    with pytest.raises(ValueError):
        mod.bootstrap_once(status=status(), files=[str(context), str(context)], receipt_dir=tmp_path / "receipts", runtime=runtime, commit=True)
    assert not runtime.calls
