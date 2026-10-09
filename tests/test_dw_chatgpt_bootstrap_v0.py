from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from codexia_manual_agent.work_core import (
    SqliteWorkStore,
    Work,
    WorkIngressBinding,
)

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "dw_chatgpt_bootstrap_v0", ROOT / "tools" / "dw_chatgpt_bootstrap_v0.py"
)
assert spec and spec.loader
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def work_and_status(tmp_path):
    store = SqliteWorkStore(tmp_path / "work.sqlite3")
    work = Work.create(
        objective="Continue project",
        ingress=WorkIngressBinding.create(
            source_namespace="test.dw.bootstrap",
            source_id="case-1",
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
        "workflow": [{"pack": {"pack_id": "test-only-pinned"}}],
        "yield": {"kind": "none"},
        "unresolved": {
            "roles_total": 0,
            "capabilities_total": 0,
            "children_live_total": 0,
        },
    }
    return store, status


class Runtime:
    def __init__(self, *, fail=False, transport="browser-owned", ready=True, missing=None):
        self.calls = []
        self.fail = fail
        self.transport = transport
        self.ready = ready
        self.missing = missing

    def health(self):
        return SimpleNamespace(ready=self.ready)

    def capabilities(self):
        return self

    def state(self, name):
        return SimpleNamespace(value="UNKNOWN" if name == self.missing else "AVAILABLE")

    def send_text_observed(self, prompt, **kwargs):
        self.calls.append((prompt, kwargs))
        if self.fail:
            raise TimeoutError("ambiguous CWA write")
        return SimpleNamespace(
            transport=self.transport,
            response=SimpleNamespace(
                text="Propose a narrow PR",
                conversation=SimpleNamespace(
                    conversation_id="chat-1", message_id="msg-1"
                ),
            ),
        )


def case(tmp_path, *, runtime=None):
    store, status = work_and_status(tmp_path)
    file = tmp_path / "handoff.md"
    file.write_text("handoff", encoding="utf-8")
    runtime = runtime or Runtime()
    kwargs = dict(
        status=status, files=[str(file)], store=store, runtime=runtime, commit=True
    )
    return store, status, file, runtime, kwargs


def test_dry_run_no_side_effect(tmp_path):
    store, status, file, runtime, kwargs = case(tmp_path)
    result = mod.bootstrap_once(**{**kwargs, "commit": False})
    assert result["status"] == "DRY_RUN"
    assert len(result["files"]) == 1
    assert not runtime.calls
    assert store.events(status["work"]["work_id"]) == ()


def test_exactly_one_send_with_attachment_and_gen2_cas_receipt(tmp_path):
    store, status, file, runtime, kwargs = case(tmp_path)
    result = mod.bootstrap_once(**kwargs)
    assert result["status"] == "CAPTURED_UNADMITTED"
    assert len(runtime.calls) == 1
    assert runtime.calls[0][1]["media"] == [str(file.resolve())]
    assert "model_profile" not in runtime.calls[0][1]
    events = store.events(status["work"]["work_id"])
    assert [event.kind for event in events] == [
        mod.CLAIM_KIND,
        mod.CAPTURE_KIND,
    ]
    assert events[0].payload["plan"]["work"]["work_digest"] == status["work"]["work_digest"]
    assert events[1].payload["conversation_id"] == "chat-1"
    with pytest.raises(ValueError, match="previously claimed|frontier changed"):
        mod.bootstrap_once(**kwargs)
    assert len(runtime.calls) == 1
    assert len(SqliteWorkStore(store.path).events(status["work"]["work_id"])) == 2


def test_ambiguous_send_has_durable_no_replay_claim(tmp_path):
    store, status, file, runtime, kwargs = case(tmp_path, runtime=Runtime(fail=True))
    with pytest.raises(TimeoutError):
        mod.bootstrap_once(**kwargs)
    assert [e.kind for e in store.events(status["work"]["work_id"])] == [
        mod.CLAIM_KIND
    ]
    with pytest.raises(ValueError):
        mod.bootstrap_once(
            **{**kwargs, "store": SqliteWorkStore(store.path)}
        )
    assert len(runtime.calls) == 1


def test_stale_work_revision_never_sends(tmp_path):
    store, status, file, runtime, kwargs = case(tmp_path)
    snapshot = store.snapshot(status["work"]["work_id"])
    store.append(
        snapshot.work.work_id,
        expected_revision=snapshot.revision,
        event=snapshot.next_event(kind="test.external.change", payload={}),
    )
    with pytest.raises(ValueError, match="frontier changed"):
        mod.bootstrap_once(**kwargs)
    assert not runtime.calls


def test_live_child_and_unexpected_transport_rejected(tmp_path):
    store, status, file, runtime, kwargs = case(tmp_path, runtime=Runtime(transport="experimental"))
    invalid = {
        **status,
        "unresolved": {**status["unresolved"], "children_live_total": 1},
    }
    with pytest.raises(ValueError, match="delegated child"):
        mod.bootstrap_once(**{**kwargs, "status": invalid})
    assert not runtime.calls
    with pytest.raises(ValueError, match="unexpected transport"):
        mod.bootstrap_once(**kwargs)
    assert [event.kind for event in store.events(status["work"]["work_id"])] == [
        mod.CLAIM_KIND
    ]


def test_reject_duplicate_file_before_claim(tmp_path):
    store, status, file, runtime, kwargs = case(tmp_path)
    with pytest.raises(ValueError, match="duplicate context file"):
        mod.bootstrap_once(**{**kwargs, "files": [str(file), str(file)]})
    assert not store.events(status["work"]["work_id"])
    assert not runtime.calls



def test_cwa_readiness_fail_closed_before_claim(tmp_path):
    for runtime in (Runtime(ready=False), Runtime(missing="files")):
        case_dir = tmp_path / ("unready" if not runtime.ready else "no-files")
        case_dir.mkdir()
        store, status, file, _, kwargs = case(case_dir, runtime=runtime)
        with pytest.raises(ValueError, match="not ready|capability files"):
            mod.bootstrap_once(**kwargs)
        assert store.events(status["work"]["work_id"]) == ()
        assert not runtime.calls


def test_image_media_is_rejected_before_no_replay_claim(tmp_path):
    store, status, file, runtime, kwargs = case(tmp_path)
    image = tmp_path / "screenshot.png"
    image.write_bytes(b"not-an-image-but-also-not-a-handoff")
    with pytest.raises(ValueError, match="only \\.md/\\.txt handoff files"):
        mod.bootstrap_once(**{**kwargs, "files": [str(image)]})
    assert store.events(status["work"]["work_id"]) == ()
    assert runtime.calls == []
