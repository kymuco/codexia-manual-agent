from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest

from codexia_manual_agent.domain.errors import ProviderError
from codexia_manual_agent.domain.models import (
    ProviderConversation,
    ProviderRequest,
    ProviderResponse,
)
from codexia_manual_agent.pack_core import (
    PackAdmission,
    PackBinding,
    PackMemberBinding,
    PackMemberKind,
    PackWorkflowBinding,
)
from codexia_manual_agent.providers.chatgpt_web import ChatGPTWebProvider
from codexia_manual_agent.providers.model_provider_cognition import (
    ModelProviderCognitionPort,
)
from codexia_manual_agent.role_core import (
    CognitionRequest,
    CognitionTransportBridge,
    CognitionTransportPortError,
    ContextProjection,
    RoleAdmission,
    RoleBinding,
    RoleRun,
    RoleRunState,
    project_cognition_handoff,
)
from codexia_manual_agent.work_core import SqliteWorkStore, Work, WorkIngressBinding
from codexia_manual_agent.workflow_core import (
    WorkflowAdmission,
    WorkflowBinding,
    WorkflowRun,
)

INSTRUCTIONS = "Review the supplied evidence and return one bounded conclusion."
CONTEXT = "artifact=A; evidence=B"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _workflow_binding() -> WorkflowBinding:
    return WorkflowBinding.create(
        workflow_id="codexia:g2.14-cognition",
        version="1.0.0",
        definition_digest=_sha("g2.14-workflow"),
    )


def _role_binding() -> RoleBinding:
    return RoleBinding.create(
        role_id="codexia:g2.14-reviewer",
        version="1.0.0",
        instructions_digest=_sha(INSTRUCTIONS),
    )


def _pack(
    workflow: WorkflowBinding,
    role: RoleBinding,
) -> PackBinding:
    return PackBinding.create(
        pack_id="codexia:g2.14-pack",
        version="1.0.0",
        definition_digest=_sha("g2.14-pack"),
        members=(
            PackMemberBinding.create(
                kind=PackMemberKind.WORKFLOW,
                semantic_id=workflow.workflow_id,
                version=workflow.version,
                binding_digest=workflow.binding_digest,
            ),
            PackMemberBinding.create(
                kind=PackMemberKind.ROLE,
                semantic_id=role.role_id,
                version=role.version,
                binding_digest=role.binding_digest,
            ),
        ),
    )


def _requested(
    store: SqliteWorkStore,
    *,
    context: str = CONTEXT,
    source_id: str = "g2.14",
):
    workflow_binding = _workflow_binding()
    role_binding = _role_binding()
    work = Work.create(
        objective="Exercise ModelProvider cognition adapter",
        ingress=WorkIngressBinding.create(
            source_namespace="standalone.api",
            source_id=source_id,
            payload_digest=_sha(f"payload:{source_id}:{context}"),
        ),
    )
    initial = store.create(work)
    workflow_run = WorkflowRun.create(
        snapshot=initial,
        binding=workflow_binding,
    )
    workflow = WorkflowAdmission(store).admit_start(workflow_run)
    PackAdmission(store).admit_workflow_binding(
        PackWorkflowBinding.create(
            workflow=workflow,
            snapshot=store.snapshot(work.work_id),
            pack=_pack(workflow_binding, role_binding),
        )
    )
    role_run = RoleRun.create(
        workflow=workflow,
        snapshot=store.snapshot(work.work_id),
        binding=role_binding,
        context=ContextProjection.create(content_digest=_sha(context)),
    )
    role = RoleAdmission(store).admit_start(role_run)
    request = CognitionRequest.create(
        role=role,
        snapshot=store.snapshot(work.work_id),
        instructions=INSTRUCTIONS,
        context=context,
    )
    requested = RoleAdmission(store).admit_request(request)
    return work, requested, request


class FakeProvider:
    provider_id = "fake-model"

    def __init__(self, response: ProviderResponse | None = None) -> None:
        self.calls: list[ProviderRequest] = []
        self.response = response or ProviderResponse(text="bounded provider answer")

    def send(self, request: ProviderRequest) -> ProviderResponse:
        self.calls.append(request)
        return self.response


class RaisingProvider:
    provider_id = "raising-model"

    def __init__(self) -> None:
        self.calls: list[ProviderRequest] = []

    def send(self, request: ProviderRequest) -> ProviderResponse:
        self.calls.append(request)
        raise ProviderError("provider transport became ambiguous")


class FakeChatGPTRuntime:
    def __init__(self) -> None:
        self.calls = []

    def send_text_observed(self, text, **kwargs):
        self.calls.append((text, kwargs))
        return SimpleNamespace(
            transport="browser-owned",
            response=SimpleNamespace(
                text="chatgpt bounded answer",
                conversation=SimpleNamespace(
                    conversation_id="conversation-1",
                    message_id="message-1",
                    parent_message_id="message-1",
                    finish_reason="stop",
                ),
                request=None,
                metrics=None,
            ),
        )


def test_model_provider_adapter_maps_exact_role_request_without_provider_state(
    tmp_path,
) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    work, _, request = _requested(store)
    provider = FakeProvider(
        ProviderResponse(
            text="bounded provider answer",
            conversation=ProviderConversation(
                conversation_id="provider-conversation",
                message_id="provider-message",
            ),
            model="provider-model",
            reasoning_effort="high",
            metrics={"total": 1.25},
        )
    )
    port = ModelProviderCognitionPort(provider)

    completed = CognitionTransportBridge(store).dispatch_once(request, port)

    assert completed.state is RoleRunState.COMPLETED
    assert completed.output_text == "bounded provider answer"
    assert provider.calls == [
        ProviderRequest(
            prompt=CONTEXT,
            system=INSTRUCTIONS,
            conversation=None,
        )
    ]
    handoff = project_cognition_handoff(
        store.events(work.work_id),
        request.request_id,
    )
    assert handoff.port_id == "model-provider:fake-model"
    assert port.provider_id == "fake-model"


def test_empty_context_becomes_nonempty_visible_provider_turn(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    _, _, request = _requested(store, context="", source_id="empty-context")
    provider = FakeProvider()
    port = ModelProviderCognitionPort(provider)

    completed = CognitionTransportBridge(store).dispatch_once(request, port)

    assert completed.state is RoleRunState.COMPLETED
    assert provider.calls == [
        ProviderRequest(
            prompt=INSTRUCTIONS,
            system=None,
            conversation=None,
        )
    ]


def test_provider_exception_preserves_handoff_and_never_redispatches(tmp_path) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    work, _, request = _requested(store, source_id="provider-error")
    first_provider = RaisingProvider()
    bridge = CognitionTransportBridge(store)

    with pytest.raises(CognitionTransportPortError):
        bridge.dispatch_once(
            request,
            ModelProviderCognitionPort(first_provider),
        )

    assert len(first_provider.calls) == 1
    handoff = project_cognition_handoff(
        store.events(work.work_id),
        request.request_id,
    )
    assert handoff.port_id == "model-provider:raising-model"

    retry_provider = RaisingProvider()
    recovered = bridge.dispatch_once(
        request,
        ModelProviderCognitionPort(retry_provider),
    )

    assert recovered.state is RoleRunState.REQUESTED
    assert retry_provider.calls == []


def test_existing_chatgpt_web_provider_is_a_concrete_gen2_cognition_port(
    tmp_path,
) -> None:
    store = SqliteWorkStore(tmp_path / "work.sqlite")
    _, _, request = _requested(store, source_id="chatgpt-web")
    runtime = FakeChatGPTRuntime()
    provider = ChatGPTWebProvider(runtime=runtime)
    port = ModelProviderCognitionPort(provider)

    completed = CognitionTransportBridge(store).dispatch_once(request, port)

    assert completed.state is RoleRunState.COMPLETED
    assert completed.output_text == "chatgpt bounded answer"
    assert port.port_id == "model-provider:chatgpt-web"
    assert len(runtime.calls) == 1
    sent_text, kwargs = runtime.calls[0]
    assert sent_text == (
        "[Codexia product-runtime system context]\n"
        f"{INSTRUCTIONS}\n\n"
        "[Codexia product-runtime request]\n"
        f"{CONTEXT}"
    )
    assert kwargs["conversation"] is None
