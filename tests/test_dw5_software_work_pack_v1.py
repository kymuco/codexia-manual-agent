"""DW5.1 software patch proposal: actual Gen2 Work/Role/Attention, zero effects."""

from __future__ import annotations

import hashlib
import json

import pytest

from codexia_manual_agent.pack_core import PackMemberBinding, PackMemberKind
from codexia_manual_agent.role_core import (
    CognitionOutcome,
    RoleRunState,
    project_role_runs,
)
from codexia_manual_agent.standalone_work import (
    StandaloneWorkHost,
    StandaloneWorkSelector,
    StandaloneWorkSurface,
)
from codexia_manual_agent.work_core import SqliteWorkStore
from codexia_manual_agent.workflow_runtime.software_v1 import (
    SoftwareContextMaterialPort,
    SoftwareInstructionsMaterialPort,
    SoftwarePatchProposal,
    SoftwareProposalWorkflowV1,
    SoftwareSnapshot,
    software_pack_binding,
    software_role_binding,
    software_role_instructions,
    software_workflow_binding,
)

PROVIDER_REF = "codexia:dw5-software-proposal-provider@0.1.0"


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _source() -> SoftwareSnapshot:
    return SoftwareSnapshot(
        repo="example/repository",
        base_head="a" * 40,
        path="src/hello.py",
        original_text="def greet():\n    return 'hello'\n",
        preimage_sha256=_hash("def greet():\n    return 'hello'\n"),
        task="Make the greeting more descriptive.",
    )


def _proposal(source: SoftwareSnapshot, *, replacement=None) -> SoftwarePatchProposal:
    return SoftwarePatchProposal(
        repo=source.repo,
        base_head=source.base_head,
        path=source.path,
        preimage_sha256=source.preimage_sha256,
        replacement_text=(
            replacement
            if replacement is not None
            else "def greet():\n    return 'hello world'\n"
        ),
        summary="Clarify the greeting output.",
        verification_plan="Run existing unit test under host-approved argv.",
        risks="Downstream tests may assert old greeting.",
    )


def _encoded(value: SoftwarePatchProposal) -> str:
    return json.dumps(value.to_dict(), ensure_ascii=False)


class _Plugin:
    def codexia_pack_distribution(self):
        return {
            "schema_version": 1,
            "pack": software_pack_binding().to_dict(),
            "workflows": [software_workflow_binding().to_dict()],
            "roles": [software_role_binding().to_dict()],
            "capabilities": [],
        }

    def codexia_workflow_implementation(self, binding):
        assert binding == software_workflow_binding().to_dict()
        return SoftwareProposalWorkflowV1()

    def codexia_completion_criterion(self, binding):
        raise AssertionError("DW5.1 cannot claim Work completion")


class _PluginService:
    def get(self, plugin_id):
        assert plugin_id == PROVIDER_REF
        return _Plugin()


class _Cognition:
    port_id = "dw5-scripted-proposal"

    def __init__(self, answer: str):
        self.answer = answer
        self.calls = 0

    def complete(self, request):
        self.calls += 1
        return CognitionOutcome.succeeded(
            request.request, output_text=self.answer
        )


def _selector() -> StandaloneWorkSelector:
    return StandaloneWorkSelector(
        provider_ref=PROVIDER_REF,
        workflow_id=software_workflow_binding().workflow_id,
        workflow_version=software_workflow_binding().version,
    )


def _host(store, work_id=None, port=None):
    return StandaloneWorkHost(
        plugin_service=_PluginService(),
        cognition_port=port,
        instructions=SoftwareInstructionsMaterialPort() if work_id else None,
        context=(
            SoftwareContextMaterialPort(store=store, work_id=work_id)
            if work_id else None
        ),
    )


def _start(store, objective=None):
    source = objective or _source()
    surface = StandaloneWorkSurface(store)
    result = surface.start(
        objective=source.to_json(), selector=_selector(),
        host_factory=lambda: _host(store),
    )
    return result["work"]["work_id"]


def _advance(store, work_id, port, steps=5):
    return StandaloneWorkSurface(store).advance(
        work_id,
        selector=_selector(),
        host_factory=lambda: _host(store, work_id, port=port),
        max_steps=steps,
    )


def test_software_review_uses_pinned_role_attention_no_effects(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "software.sqlite")
    source = _source()
    candidate = _proposal(source)
    work_id = _start(store, source)
    port = _Cognition(_encoded(candidate))

    result = _advance(store, work_id, port)
    assert result["status"]["yield"]["kind"] == "attention"
    assert port.calls == 1
    assert result["status"]["work"]["state"] == "active"
    question = result["status"]["yield"]["attention"]["question"]
    assert "a/src/hello.py" in question
    assert "b/src/hello.py" in question
    assert "hello world" in question
    assert candidate.digest in question
    assert source.base_head in question
    assert source.preimage_sha256 in question

    events = store.events(work_id)
    assert [item.kind for item in events].count("role.completed") == 1
    assert [item.kind for item in events].count("attention.need-declared") == 1
    assert not any(
        item.kind.startswith("capability.")
        or item.kind in {"work.completed", "completion.claim-admitted"}
        for item in events
    )
    roles = project_role_runs(events)
    assert len(roles) == 1
    assert roles[0].run.binding == software_role_binding()
    assert roles[0].state is RoleRunState.COMPLETED
    assert roles[0].output_text == _encoded(candidate)

    before = store.events(work_id)
    restarted = SqliteWorkStore(tmp_path / "software.sqlite")
    resumed = _advance(restarted, work_id, port)
    assert resumed["status"]["yield"]["kind"] == "attention"
    assert port.calls == 1
    assert restarted.events(work_id) == before

    # Human acknowledgement is recorded, never permission to touch repo.
    StandaloneWorkSurface(restarted).answer(
        work_id,
        response_text="APPROVE " + candidate.digest,
    )
    reviewed = _advance(restarted, work_id, port)
    assert reviewed["status"]["work"]["state"] == "active"
    assert reviewed["status"]["yield"]["kind"] == "none"
    assert port.calls == 1
    assert not any(
        item.kind.startswith("capability.") or item.kind == "work.completed"
        for item in restarted.events(work_id)
    )


def test_reject_is_also_not_effect_authority(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "rejection.sqlite")
    source = _source()
    candidate = _proposal(source)
    work_id = _start(store)
    port = _Cognition(_encoded(candidate))
    _advance(store, work_id, port)
    StandaloneWorkSurface(store).answer(
        work_id, response_text="REJECT " + candidate.digest
    )
    result = _advance(store, work_id, port)
    assert result["status"]["work"]["state"] == "active"
    assert port.calls == 1
    assert not any(
        event.kind.startswith("capability.")
        for event in store.events(work_id)
    )


def test_invalid_human_review_cannot_change_proposal(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "stale-review.sqlite")
    source = _source()
    candidate = _proposal(source)
    work_id = _start(store)
    _advance(store, work_id, _Cognition(_encoded(candidate)))
    StandaloneWorkSurface(store).answer(
        work_id, response_text="APPROVE " + "f" * 64
    )
    with pytest.raises(Exception, match="response does not bind exact proposal"):
        _advance(store, work_id, _Cognition(_encoded(candidate)))
    assert not any(
        event.kind.startswith("capability.")
        for event in store.events(work_id)
    )


@pytest.mark.parametrize(
    "mutation",
    [
        lambda s, p: {"repo": "other/repo"},
        lambda s, p: {"base_head": "b" * 40},
        lambda s, p: {"path": "src/other.py"},
        lambda s, p: {"preimage_sha256": "b" * 64},
        lambda s, p: {"replacement_text": s.original_text},
        lambda s, p: {"replacement_text": "changed without trailing LF"},
        lambda s, p: {"replacement_text": "x\n" * 210},
    ],
)
def test_exact_source_preconditions_and_patch_limits(mutation) -> None:
    source = _source()
    raw = _proposal(source).to_dict()
    raw.update(mutation(source, raw))
    with pytest.raises(ValueError):
        candidate = SoftwarePatchProposal.parse(json.dumps(raw))
        candidate.review_diff(source)


@pytest.mark.parametrize(
    "path",
    [
        "../etc/passwd", "/tmp/file.py", "src/../secret.py", ".git/config",
        ".github/workflows/ci.yml", "src\\bad.py", "src/bad.sh",
        "src/./bad.py",
    ],
)
def test_unsafe_snapshot_paths_fail(path) -> None:
    raw = _source().to_dict()
    raw["path"] = path
    with pytest.raises(ValueError):
        SoftwareSnapshot.parse(json.dumps(raw))


def test_snapshot_digest_and_exact_schemas() -> None:
    source = _source()
    assert SoftwareSnapshot.parse(source.to_json()) == source
    candidate = _proposal(source)
    assert SoftwarePatchProposal.parse(_encoded(candidate)) == candidate
    assert candidate.digest == _hash(
        json.dumps(
            candidate.to_dict(), ensure_ascii=False, sort_keys=True,
            separators=(",", ":"),
        )
    )
    assert software_pack_binding().contains(
        PackMemberBinding.create(
            kind=PackMemberKind.ROLE,
            semantic_id=software_role_binding().role_id,
            version=software_role_binding().version,
            binding_digest=software_role_binding().binding_digest,
        )
    )
    assert software_role_instructions(software_role_binding())
    raw = source.to_dict()
    raw["preimage_sha256"] = "b" * 64
    with pytest.raises(ValueError, match="does not match"):
        SoftwareSnapshot.parse(json.dumps(raw))
    candidate_json = candidate.to_dict()
    candidate_json["another_candidate"] = {}
    with pytest.raises(ValueError, match="not exact"):
        SoftwarePatchProposal.parse(json.dumps(candidate_json))


def test_wrong_candidate_is_not_human_approvable(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "bad-proposal.sqlite")
    work_id = _start(store)
    candidate = _proposal(_source())
    altered = candidate.to_dict()
    altered["base_head"] = "b" * 40
    port = _Cognition(json.dumps(altered))
    with pytest.raises(Exception, match="invalid bounded Software proposal"):
        _advance(store, work_id, port)
    assert port.calls == 1
    assert [event.kind for event in store.events(work_id)][-1] == "role.completed"
    assert not any(
        event.kind.startswith("attention.") or event.kind.startswith("capability.")
        for event in store.events(work_id)
    )
