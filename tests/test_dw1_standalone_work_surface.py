from __future__ import annotations

import ast
import contextlib
import hashlib
import io
import json
import sys
import types
from pathlib import Path

import pytest

from codexia_manual_agent.attention_core import AttentionAdmission, AttentionNeed
from codexia_manual_agent.cli import main
from codexia_manual_agent.completion_core import (
    CompletionClaim,
    CompletionCriterionResult,
)
from codexia_manual_agent.pack_core import (
    PackBinding,
    PackMemberBinding,
    PackMemberKind,
)
from codexia_manual_agent.standalone_work import (
    StandaloneWorkHost,
    StandaloneWorkSelector,
    StandaloneWorkSurface,
)
from codexia_manual_agent.work_core import SqliteWorkStore, WorkState
from codexia_manual_agent.workflow_core import (
    WorkflowBinding,
    project_workflow_runs,
)
from codexia_manual_agent.workflow_orchestration import DurableWorkYieldKind


PROVIDER_REF = "codexia:dw1-test-provider@1.0.0"
WORKFLOW_ID = "codexia:dw1-test"
WORKFLOW_VERSION = "1.0.0"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class _Implementation:
    def __init__(self, binding: WorkflowBinding, mode: str) -> None:
        self.binding = binding
        self.mode = mode
        self.calls = 0

    def propose(self, context):
        self.calls += 1
        if self.mode == "noop":
            return None
        if self.mode != "complete":
            raise AssertionError(self.mode)
        return CompletionClaim.create(
            snapshot=context.work,
            workflow=context.workflow,
            pack_binding=context.pack_binding,
            summary="DW1 exact product Work completed.",
        )


class _Criterion:
    def __init__(self, binding: WorkflowBinding) -> None:
        self.binding = binding
        self.calls = 0

    def evaluate(self, _context):
        self.calls += 1
        return CompletionCriterionResult(
            accepted=True,
            reason="DW1 completion criterion accepted exact product state.",
        )


class _Provider:
    def __init__(self, *, mode: str = "noop") -> None:
        self.workflow = WorkflowBinding.create(
            workflow_id=WORKFLOW_ID,
            version=WORKFLOW_VERSION,
            definition_digest=_sha("dw1-workflow"),
        )
        member = PackMemberBinding.create(
            kind=PackMemberKind.WORKFLOW,
            semantic_id=self.workflow.workflow_id,
            version=self.workflow.version,
            binding_digest=self.workflow.binding_digest,
        )
        self.pack = PackBinding.create(
            pack_id="codexia:dw1-test-pack",
            version="1.0.0",
            definition_digest=_sha("dw1-pack"),
            members=(member,),
        )
        self.implementation = _Implementation(self.workflow, mode)
        self.criterion = _Criterion(self.workflow)
        self.distribution_calls = 0
        self.implementation_calls = 0
        self.criterion_calls = 0

    def codexia_pack_distribution(self):
        self.distribution_calls += 1
        return {
            "schema_version": 1,
            "pack": self.pack.to_dict(),
            "workflows": [self.workflow.to_dict()],
            "roles": [],
            "capabilities": [],
        }

    def codexia_workflow_implementation(self, raw_binding):
        self.implementation_calls += 1
        assert raw_binding == self.workflow.to_dict()
        return self.implementation

    def codexia_completion_criterion(self, raw_binding):
        self.criterion_calls += 1
        assert raw_binding == self.workflow.to_dict()
        return self.criterion


class _PluginService:
    def __init__(self, provider: _Provider) -> None:
        self.provider = provider
        self.calls = 0

    def get(self, plugin_id: str):
        self.calls += 1
        if plugin_id != PROVIDER_REF:
            raise KeyError(plugin_id)
        return self.provider


def _selector() -> StandaloneWorkSelector:
    return StandaloneWorkSelector(
        provider_ref=PROVIDER_REF,
        workflow_id=WORKFLOW_ID,
        workflow_version=WORKFLOW_VERSION,
    )


def _host_factory(provider: _Provider):
    service = _PluginService(provider)

    def factory() -> StandaloneWorkHost:
        return StandaloneWorkHost(plugin_service=service)

    factory.service = service
    return factory


def _forbidden_host() -> StandaloneWorkHost:
    raise AssertionError("host must not be loaded for durable no-op state")


def test_start_is_exactly_activated_and_restart_idempotent(tmp_path) -> None:
    path = tmp_path / "work.sqlite"
    provider = _Provider()
    factory = _host_factory(provider)
    surface = StandaloneWorkSurface(SqliteWorkStore(path))

    first = surface.start(
        objective="Prove standalone Gen2 product activation.",
        selector=_selector(),
        host_factory=factory,
        source_id="dw1-start",
    )

    assert first["work"]["state"] == "active"
    assert first["work"]["revision"] == 2
    assert first["event_count"] == 2
    assert first["workflow"][0]["binding"]["workflow_id"] == WORKFLOW_ID
    assert first["workflow"][0]["pack"]["pack_id"] == "codexia:dw1-test-pack"
    assert provider.implementation_calls == 0

    restarted = StandaloneWorkSurface(SqliteWorkStore(path))
    second = restarted.start(
        objective="Prove standalone Gen2 product activation.",
        selector=_selector(),
        host_factory=_forbidden_host,
        source_id="dw1-start",
    )

    assert second["work"]["work_id"] == first["work"]["work_id"]
    assert second["work"]["revision"] == 2
    assert second["latest_event"] == first["latest_event"]


def test_concurrent_exact_start_cannot_cross_activation_boundary(
    tmp_path,
) -> None:
    path = tmp_path / "concurrent-start.sqlite"
    main_provider = _Provider(mode="complete")
    peer_provider = _Provider(mode="complete")
    selector = _selector()
    fired = False

    def racing_factory() -> StandaloneWorkHost:
        nonlocal fired
        if not fired:
            fired = True
            peer = StandaloneWorkSurface(SqliteWorkStore(path))
            peer.start(
                objective="Concurrent exact standalone start.",
                selector=selector,
                host_factory=_host_factory(peer_provider),
                source_id="dw1-concurrent-start",
            )
        return StandaloneWorkHost(
            plugin_service=_PluginService(main_provider)
        )

    started = StandaloneWorkSurface(SqliteWorkStore(path)).start(
        objective="Concurrent exact standalone start.",
        selector=selector,
        host_factory=racing_factory,
        source_id="dw1-concurrent-start",
    )

    assert fired is True
    assert started["work"]["revision"] == 2
    assert started["event_count"] == 2
    assert main_provider.implementation.calls == 0
    assert peer_provider.implementation.calls == 0
    assert started["yield"]["kind"] == "none"


def test_status_and_inspect_are_read_only_and_advance_is_finite(tmp_path) -> None:
    path = tmp_path / "read.sqlite"
    provider = _Provider(mode="noop")
    factory = _host_factory(provider)
    surface = StandaloneWorkSurface(SqliteWorkStore(path))
    started = surface.start(
        objective="Exercise finite standalone progression.",
        selector=_selector(),
        host_factory=factory,
        source_id="dw1-read",
    )
    work_id = started["work"]["work_id"]

    before = surface.status(work_id)
    inspection = surface.inspect(work_id, offset=0, limit=1)
    after = surface.status(work_id)

    assert after == before
    assert inspection["chronology"]["total_events"] == 2
    assert inspection["chronology"]["has_more"] is True
    assert len(inspection["chronology"]["events"]) == 1

    progressed = surface.advance(
        work_id,
        selector=_selector(),
        host_factory=factory,
        max_steps=1,
    )
    assert progressed["progression"]["status"] == "quiescent"
    assert progressed["progression"]["steps_used"] == 1
    assert provider.implementation.calls == 1
    assert progressed["status"]["work"]["revision"] == 2


def test_attention_yields_without_host_and_answer_resumes_same_work(
    tmp_path,
) -> None:
    path = tmp_path / "attention.sqlite"
    provider = _Provider()
    surface = StandaloneWorkSurface(SqliteWorkStore(path))
    started = surface.start(
        objective="Reach and answer a genuine human judgment boundary.",
        selector=_selector(),
        host_factory=_host_factory(provider),
        source_id="dw1-attention",
    )
    work_id = started["work"]["work_id"]
    store = SqliteWorkStore(path)
    workflow = project_workflow_runs(store.events(work_id))[0]
    need = AttentionAdmission(store).admit_need(
        AttentionNeed.create(
            workflow=workflow,
            snapshot=store.snapshot(work_id),
            question="Which exact branch should continue?",
            reason="Only the human can choose the intended branch.",
        )
    )

    yielded = StandaloneWorkSurface(store).advance(
        work_id,
        selector=_selector(),
        host_factory=_forbidden_host,
        max_steps=8,
    )
    assert yielded["progression"]["status"] == "yielded"
    assert yielded["progression"]["steps_used"] == 0
    assert yielded["progression"]["yield"]["kind"] == "attention"

    answered = StandaloneWorkSurface(store).answer(
        work_id,
        response_text="Continue with branch B.",
    )
    assert answered["response"]["attention_id"] == need.attention_id
    assert answered["status"]["work"]["work_id"] == work_id
    assert answered["status"]["yield"]["kind"] == "none"
    assert answered["status"]["attention_response_count"] == 1

    retry = StandaloneWorkSurface(store).answer(
        work_id,
        response_text="Continue with branch B.",
    )
    assert retry["idempotent"] is True
    assert retry["response"]["response_id"] == answered["response"]["response_id"]


def test_invalid_budget_is_rejected_before_zero_step_yield_shortcut(
    tmp_path,
) -> None:
    path = tmp_path / "invalid-budget.sqlite"
    provider = _Provider()
    surface = StandaloneWorkSurface(SqliteWorkStore(path))
    started = surface.start(
        objective="Validate finite budget before current AttentionNeed.",
        selector=_selector(),
        host_factory=_host_factory(provider),
        source_id="dw1-invalid-budget",
    )
    work_id = started["work"]["work_id"]
    store = SqliteWorkStore(path)
    workflow = project_workflow_runs(store.events(work_id))[0]
    AttentionAdmission(store).admit_need(
        AttentionNeed.create(
            workflow=workflow,
            snapshot=store.snapshot(work_id),
            question="Need exact user choice?",
            reason="Budget validation must precede the yielded shortcut.",
        )
    )

    with pytest.raises(ValueError, match="max_steps"):
        StandaloneWorkSurface(store).advance(
            work_id,
            selector=_selector(),
            host_factory=_forbidden_host,
            max_steps=0,
        )


def test_completion_is_terminal_and_does_not_reload_host(tmp_path) -> None:
    path = tmp_path / "completion.sqlite"
    provider = _Provider(mode="complete")
    factory = _host_factory(provider)
    surface = StandaloneWorkSurface(SqliteWorkStore(path))
    started = surface.start(
        objective="Complete one exact standalone Gen2 Work.",
        selector=_selector(),
        host_factory=factory,
        source_id="dw1-completion",
    )
    work_id = started["work"]["work_id"]

    completed = surface.advance(
        work_id,
        selector=_selector(),
        host_factory=factory,
        max_steps=2,
    )
    assert completed["progression"]["status"] == "yielded"
    assert completed["progression"]["yield"]["kind"] == "completion"
    assert completed["status"]["work"]["state"] == WorkState.COMPLETED.value

    restarted = StandaloneWorkSurface(SqliteWorkStore(path))
    no_op = restarted.advance(
        work_id,
        selector=_selector(),
        host_factory=_forbidden_host,
        max_steps=8,
    )
    assert no_op["progression"]["status"] == "yielded"
    assert no_op["progression"]["steps_used"] == 0
    assert no_op["status"]["yield"]["kind"] == DurableWorkYieldKind.COMPLETION.value


def _invoke_cli(argv: list[str]) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = main(argv)
    return code, stdout.getvalue(), stderr.getvalue()


def test_cli_exposes_start_status_advance_inspect_and_answer(
    tmp_path,
    monkeypatch,
) -> None:
    provider = _Provider(mode="noop")
    module = types.ModuleType("dw1_test_host")
    module.make_host = lambda: StandaloneWorkHost(
        plugin_service=_PluginService(provider)
    )
    monkeypatch.setitem(sys.modules, "dw1_test_host", module)
    store_path = tmp_path / "cli.sqlite"

    runtime_args = [
        "--host-factory",
        "dw1_test_host:make_host",
        "--provider-ref",
        PROVIDER_REF,
        "--workflow-id",
        WORKFLOW_ID,
        "--workflow-version",
        WORKFLOW_VERSION,
    ]
    code, stdout, stderr = _invoke_cli(
        [
            "work",
            "start",
            "Use the DW1 CLI.",
            "--store",
            str(store_path),
            "--source-id",
            "dw1-cli",
            *runtime_args,
            "--json",
        ]
    )
    assert code == 0, stderr
    started = json.loads(stdout)
    work_id = started["work"]["work_id"]

    code, stdout, stderr = _invoke_cli(
        ["work", "status", work_id, "--store", str(store_path), "--json"]
    )
    assert code == 0, stderr
    assert json.loads(stdout)["work"]["revision"] == 2

    code, stdout, stderr = _invoke_cli(
        [
            "work",
            "advance",
            work_id,
            "--store",
            str(store_path),
            "--max-steps",
            "1",
            *runtime_args,
            "--json",
        ]
    )
    assert code == 0, stderr
    assert json.loads(stdout)["progression"]["status"] == "quiescent"

    code, stdout, stderr = _invoke_cli(
        [
            "work",
            "inspect",
            work_id,
            "--store",
            str(store_path),
            "--limit",
            "1",
            "--json",
        ]
    )
    assert code == 0, stderr
    assert json.loads(stdout)["chronology"]["total_events"] == 2

    store = SqliteWorkStore(store_path)
    workflow = project_workflow_runs(store.events(work_id))[0]
    need = AttentionAdmission(store).admit_need(
        AttentionNeed.create(
            workflow=workflow,
            snapshot=store.snapshot(work_id),
            question="CLI human choice?",
            reason="DW1 CLI proof requires an exact human response.",
        )
    )
    code, stdout, stderr = _invoke_cli(
        [
            "work",
            "answer",
            work_id,
            "Choose the explicit CLI branch.",
            "--store",
            str(store_path),
            "--attention-id",
            need.attention_id,
            "--json",
        ]
    )
    assert code == 0, stderr
    answered = json.loads(stdout)
    assert answered["response"]["attention_id"] == need.attention_id
    assert answered["status"]["work"]["work_id"] == work_id


def test_product_surface_adds_no_scheduler_or_plugin_discovery() -> None:
    root = Path(__file__).resolve().parents[1]
    source = (
        root
        / "src"
        / "codexia_manual_agent"
        / "standalone_work"
        / "surface.py"
    ).read_text(encoding="utf-8")
    cli = (root / "src" / "codexia_manual_agent" / "cli.py").read_text(
        encoding="utf-8"
    )

    tree = ast.parse(source)
    assert not any(isinstance(node, ast.While) for node in ast.walk(tree))
    assert "Scheduler" not in source
    assert "pkgutil" not in cli
    assert "entry_points" not in cli
    assert "intent_resolution_runtime" not in source
    assert "hde_" not in source
