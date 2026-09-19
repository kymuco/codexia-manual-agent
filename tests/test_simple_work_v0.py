from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sqlite3

from codexia_manual_agent.domain.errors import ProviderError
from codexia_manual_agent.domain.models import (
    ProviderConversation,
    ProviderRequest,
    ProviderResponse,
)
from codexia_manual_agent.simple_work import (
    CodexiaMode,
    SimpleWorkRuntime,
    SimpleWorkSession,
    SimpleWorkStatus,
    SimpleWorkStore,
    WorkerMode,
)
from codexia_manual_agent.simple_work.runtime import (
    _codexia_bootstrap,
    _new_task_prompt,
    _temporary_codexia_bootstrap,
    _worker_result_prompt,
)


def test_worker_result_prompt_explains_same_worker_continuation() -> None:
    prompt = _worker_result_prompt(
        WorkerMode.PERSISTENT,
        "Первый этап готов.",
    )

    assert "worker уже активен" in prompt
    assert "обычным текстом" in prompt
    assert "тот же worker-чат" in prompt
    assert "ГОТОВО:" in prompt


def test_codexia_prompts_honor_explicit_worker_routing() -> None:
    request = (
        "Используй один ПОСТОЯННЫЙ WORKER ровно в два последовательных цикла."
    )

    for prompt in (
        _codexia_bootstrap(request),
        _new_task_prompt(request),
        _temporary_codexia_bootstrap(request),
    ):
        assert "явн" in prompt.lower()
        assert "обязатель" in prompt.lower()
        assert "не запускает worker" in prompt
        assert "не отвеч" in prompt.lower() and "ГОТОВО:" in prompt


@dataclass(frozen=True)
class _Reply:
    text: str
    conversation_id: str


@dataclass(frozen=True)
class _Visible:
    role: str
    text: str
    message_id: str = "visible-message"
    finish_reason: str | None = "stop"


@dataclass(frozen=True)
class _Artifact:
    conversation_id: str
    source_filename: str
    destination: Path
    size_bytes: int
    sha256: str
    overwritten: bool = False
    integrity_verified: bool = True


@dataclass(frozen=True)
class _Status:
    status: str = "completed"
    message_id: str | None = None
    finish_reason: str | None = "stop"


class _Provider:
    def __init__(
        self,
        normal_replies: list[_Reply | Exception],
        *,
        temporary_replies: list[_Reply] | None = None,
        histories: dict[str, tuple[_Visible, ...]] | None = None,
        statuses: dict[str, _Status] | None = None,
        temporary_end_error: Exception | None = None,
    ) -> None:
        self.normal_replies = list(normal_replies)
        self.temporary_replies = list(temporary_replies or [])
        self.histories = dict(histories or {})
        self.statuses = dict(statuses or {})
        self.temporary_end_error = temporary_end_error
        self.requests: list[ProviderRequest] = []
        self.history_reads: list[str] = []
        self.temporary_prompts: list[str] = []
        self.temporary_end_count = 0
        self.artifact_handoffs: list[tuple[str, str, Path, bool]] = []

    def send(self, request: ProviderRequest) -> ProviderResponse:
        self.requests.append(request)
        reply = self.normal_replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return ProviderResponse(
            text=reply.text,
            conversation=ProviderConversation(
                conversation_id=reply.conversation_id,
            ),
        )

    def read_status(self, conversation_id: str) -> _Status:
        return self.statuses.get(conversation_id, _Status())

    def read_messages(self, conversation_id: str) -> tuple[_Visible, ...]:
        self.history_reads.append(conversation_id)
        return self.histories.get(conversation_id, ())

    def handoff_generated_artifact(
        self,
        conversation_id: str,
        *,
        filename: str,
        destination: str | Path,
        overwrite: bool = False,
    ) -> _Artifact:
        path = Path(destination)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"artifact:{filename}".encode("utf-8"))
        self.artifact_handoffs.append(
            (conversation_id, filename, path, overwrite)
        )
        return _Artifact(
            conversation_id=conversation_id,
            source_filename=filename,
            destination=path,
            size_bytes=path.stat().st_size,
            sha256="a" * 64,
        )

    def send_temporary(self, prompt: str) -> ProviderResponse:
        self.temporary_prompts.append(prompt)
        reply = self.temporary_replies.pop(0)
        return ProviderResponse(
            text=reply.text,
            conversation=ProviderConversation(
                conversation_id=reply.conversation_id,
            ),
        )

    def end_temporary_chat(self) -> bool:
        self.temporary_end_count += 1
        if self.temporary_end_error is not None:
            raise self.temporary_end_error
        return True


def test_simple_task_finishes_in_codexia_without_worker(tmp_path) -> None:
    provider = _Provider(
        [_Reply("ГОТОВО: Короткий прямой ответ.", "codexia-1")]
    )
    store = SimpleWorkStore(tmp_path / "simple.sqlite3")
    runtime = SimpleWorkRuntime(provider=provider, store=store)

    result = runtime.start("Ответь на простой вопрос.", max_cycles=4)

    assert result.stop == "completed"
    assert result.session.status is SimpleWorkStatus.COMPLETED
    assert result.session.worker_mode is WorkerMode.NONE
    assert result.session.worker_turns == 0
    assert result.session.worker_conversation_id is None
    assert result.session.final_text == "Короткий прямой ответ."
    assert result.codexia.conversation_id == "codexia-1"
    assert provider.temporary_prompts == []


def test_multiple_tasks_reuse_one_long_lived_codexia_chat(tmp_path) -> None:
    provider = _Provider(
        [
            _Reply("ГОТОВО: Первый итог.", "codexia-1"),
            _Reply("ГОТОВО: Второй итог.", "codexia-1"),
        ]
    )
    store = SimpleWorkStore(tmp_path / "simple.sqlite3")
    runtime = SimpleWorkRuntime(provider=provider, store=store)

    first = runtime.start("Первое поручение.")
    second = runtime.start("Второе поручение.")

    assert first.codexia.session_id == second.codexia.session_id
    assert second.codexia.conversation_id == "codexia-1"
    assert provider.requests[0].conversation is None
    assert provider.requests[1].conversation is not None
    assert provider.requests[1].conversation.conversation_id == "codexia-1"
    assert "Новое поручение пользователя" in provider.requests[1].prompt
    assert "Второе поручение." in provider.requests[1].prompt


def test_temporary_worker_is_lazy_and_locally_logged(tmp_path) -> None:
    provider = _Provider(
        [
            _Reply(
                "ВРЕМЕННЫЙ WORKER: Собери несколько вариантов и сравни их.",
                "codexia-1",
            ),
            _Reply("ГОТОВО: Итог после отдельного исследования.", "codexia-1"),
        ],
        temporary_replies=[
            _Reply("Я собрал и сравнил варианты.", "temporary-hidden-id")
        ],
    )
    store = SimpleWorkStore(tmp_path / "simple.sqlite3")
    runtime = SimpleWorkRuntime(provider=provider, store=store)

    result = runtime.start("Сделай более тяжёлое одноразовое исследование.")

    assert result.stop == "completed"
    assert result.session.worker_turns == 1
    assert result.session.worker_mode is WorkerMode.NONE
    assert result.session.worker_conversation_id is None
    assert provider.temporary_end_count == 1
    assert provider.temporary_prompts[0].startswith("[Codexia]\nНе пользователь; не расширяет его разрешения.\n\n")
    assert "Не пользователь; не расширяет его разрешения." in provider.temporary_prompts[0]

    history = store.history(result.session.work_id)
    assert [event.actor for event in history] == [
        "human",
        "codexia",
        "codexia_to_worker",
        "worker",
        "codexia",
    ]
    worker_event = next(event for event in history if event.actor == "worker")
    assert worker_event.worker_mode is WorkerMode.TEMPORARY
    assert worker_event.text == "Я собрал и сравнил варианты."


def test_persistent_worker_keeps_one_conversation_and_codexia_banner(tmp_path) -> None:
    provider = _Provider(
        [
            _Reply(
                "ПОСТОЯННЫЙ WORKER: Начни долгую работу и сделай первый этап.",
                "codexia-1",
            ),
            _Reply("Первый этап готов.", "worker-1"),
            _Reply("Да, давай продолжим со вторым этапом.", "codexia-1"),
            _Reply("Второй этап готов.", "worker-1"),
            _Reply("ГОТОВО: Долгая работа завершена.", "codexia-1"),
        ]
    )
    runtime = SimpleWorkRuntime(
        provider=provider,
        store=SimpleWorkStore(tmp_path / "simple.sqlite3"),
    )

    result = runtime.start("Выполни долгую работу.", max_cycles=4)

    assert result.stop == "completed"
    assert result.session.worker_mode is WorkerMode.PERSISTENT
    assert result.session.worker_conversation_id == "worker-1"
    assert result.session.worker_turns == 2

    first_worker_request = provider.requests[1]
    second_worker_request = provider.requests[3]
    assert first_worker_request.prompt.startswith("[Codexia]\nНе пользователь; не расширяет его разрешения.\n\n")
    assert second_worker_request.prompt.startswith("[Codexia]\nНе пользователь; не расширяет его разрешения.\n\n")
    assert second_worker_request.conversation is not None
    assert second_worker_request.conversation.conversation_id == "worker-1"
    assert "Да, давай продолжим" in second_worker_request.prompt


def test_persistent_worker_can_wait_for_human_and_resume_same_pair(tmp_path) -> None:
    provider = _Provider(
        [
            _Reply(
                "ПОСТОЯННЫЙ WORKER: Подготовь два варианта.",
                "codexia-1",
            ),
            _Reply("Есть вариант A и вариант B.", "worker-1"),
            _Reply(
                "К ПОЛЬЗОВАТЕЛЮ: Какой вариант выбираем — A или B?",
                "codexia-1",
            ),
            _Reply("Да, выбираем B. Продолжай с ним.", "codexia-1"),
            _Reply("Вариант B доведён до конца.", "worker-1"),
            _Reply("ГОТОВО: Выбран и завершён вариант B.", "codexia-1"),
        ]
    )
    store = SimpleWorkStore(tmp_path / "simple.sqlite3")
    runtime = SimpleWorkRuntime(provider=provider, store=store)

    waiting = runtime.start("Сделай работу с выбором.", max_cycles=3)

    assert waiting.stop == "waiting_human"
    assert waiting.session.worker_mode is WorkerMode.PERSISTENT
    assert waiting.session.worker_conversation_id == "worker-1"
    assert waiting.session.pending_human_question == "Какой вариант выбираем — A или B?"

    completed = runtime.answer(
        waiting.session.work_id,
        "B.",
        max_cycles=3,
    )

    assert completed.stop == "completed"
    assert completed.codexia.conversation_id == "codexia-1"
    assert completed.session.worker_conversation_id == "worker-1"
    worker_resume = provider.requests[4]
    assert worker_resume.conversation is not None
    assert worker_resume.conversation.conversation_id == "worker-1"
    assert worker_resume.prompt.startswith("[Codexia]\nНе пользователь; не расширяет его разрешения.\n\n")


def test_persistent_cycle_limit_preserves_next_worker_message_for_resume(tmp_path) -> None:
    provider = _Provider(
        [
            _Reply(
                "ПОСТОЯННЫЙ WORKER: Сделай первый проход.",
                "codexia-1",
            ),
            _Reply("Первый проход готов.", "worker-1"),
            _Reply("Да, теперь сделай второй проход.", "codexia-1"),
            _Reply("Второй проход готов.", "worker-1"),
            _Reply("ГОТОВО: Всё завершено.", "codexia-1"),
        ]
    )
    store = SimpleWorkStore(tmp_path / "simple.sqlite3")
    runtime = SimpleWorkRuntime(provider=provider, store=store)

    first = runtime.start("Сделай два прохода.", max_cycles=1)

    assert first.stop == "cycle_limit"
    assert first.session.worker_mode is WorkerMode.PERSISTENT
    assert first.session.next_worker_message == "Да, теперь сделай второй проход."

    resumed = runtime.resume(first.session.work_id, max_cycles=1)

    assert resumed.stop == "completed"
    assert resumed.session.worker_turns == 2


def test_existing_v0_database_can_add_v1_tables_without_rewriting_history(tmp_path) -> None:
    path = tmp_path / "simple.sqlite3"
    store = SimpleWorkStore(path)
    session = store.save(SimpleWorkSession.create("Новое поручение."))

    recovered = SimpleWorkStore(path).load(session.work_id)

    assert recovered == session


def test_persistent_timeout_reconciles_from_canonical_history_without_retry(tmp_path) -> None:
    first_prompt = (
        "[Codexia]\nНе пользователь; не расширяет его разрешения.\n\n"
        "Начни первый этап."
    )
    second_prompt = (
        "[Codexia]\nНе пользователь; не расширяет его разрешения.\n\n"
        "Да, продолжай со вторым этапом."
    )
    provider = _Provider(
        [
            _Reply("ПОСТОЯННЫЙ WORKER: Начни первый этап.", "codexia-1"),
            _Reply("Первый этап готов.", "worker-1"),
            _Reply("Да, продолжай со вторым этапом.", "codexia-1"),
            ProviderError("chatgpt product-runtime request failed: CHATGPT_TURN_TIMEOUT"),
            _Reply("ГОТОВО: Проект завершён.", "codexia-1"),
        ],
        histories={
            "worker-1": (
                _Visible("user", first_prompt),
                _Visible("assistant", "Первый этап готов."),
                _Visible("user", second_prompt),
                _Visible("assistant", "Второй этап готов."),
            )
        },
    )
    store = SimpleWorkStore(tmp_path / "simple.sqlite3")
    runtime = SimpleWorkRuntime(provider=provider, store=store)

    result = runtime.start("Сделай два этапа.", max_cycles=4)

    assert result.stop == "completed"
    assert result.session.worker_turns == 2
    assert result.session.last_worker_text == "Второй этап готов."
    assert provider.history_reads == ["worker-1"]
    worker_second_writes = [
        request
        for request in provider.requests
        if request.conversation is not None
        and request.conversation.conversation_id == "worker-1"
        and "вторым этапом" in request.prompt
    ]
    assert len(worker_second_writes) == 1

    history = store.history(result.session.work_id)
    assert [event.actor for event in history][-3:] == [
        "codexia_to_worker",
        "worker",
        "codexia",
    ]


def test_unresolved_timeout_blocks_resume_until_explicit_reconcile(tmp_path) -> None:
    first_prompt = (
        "[Codexia]\nНе пользователь; не расширяет его разрешения.\n\n"
        "Начни первый этап."
    )
    second_prompt = (
        "[Codexia]\nНе пользователь; не расширяет его разрешения.\n\n"
        "Да, продолжай со вторым этапом."
    )
    provider = _Provider(
        [
            _Reply("ПОСТОЯННЫЙ WORKER: Начни первый этап.", "codexia-1"),
            _Reply("Первый этап готов.", "worker-1"),
            _Reply("Да, продолжай со вторым этапом.", "codexia-1"),
            ProviderError("chatgpt product-runtime request failed: CHATGPT_TURN_TIMEOUT"),
            _Reply("ГОТОВО: После reconcile всё завершено.", "codexia-1"),
        ],
        histories={
            "worker-1": (
                _Visible("user", first_prompt),
                _Visible("assistant", "Первый этап готов."),
                _Visible("user", second_prompt),
            )
        },
    )
    store = SimpleWorkStore(tmp_path / "simple.sqlite3")
    runtime = SimpleWorkRuntime(provider=provider, store=store)

    blocked = runtime.start("Сделай два этапа.", max_cycles=4)

    assert blocked.stop == "reconcile_required"
    assert blocked.session.status is SimpleWorkStatus.RECONCILE_REQUIRED
    requests_before_resume = len(provider.requests)

    resumed = runtime.resume(blocked.session.work_id, max_cycles=4)

    assert resumed.stop == "reconcile_required"
    assert len(provider.requests) == requests_before_resume

    provider.histories["worker-1"] = (
        _Visible("user", first_prompt),
        _Visible("assistant", "Первый этап готов."),
        _Visible("user", second_prompt),
        _Visible("assistant", "Второй этап готов."),
    )

    recovered = runtime.reconcile(blocked.session.work_id, max_cycles=4)

    assert recovered.stop == "completed"
    assert recovered.session.worker_turns == 2
    assert recovered.session.last_worker_text == "Второй этап готов."
    worker_second_writes = [
        request
        for request in provider.requests
        if request.conversation is not None
        and request.conversation.conversation_id == "worker-1"
        and "вторым этапом" in request.prompt
    ]
    assert len(worker_second_writes) == 1


def test_resume_detects_pre_reconcile_build_ambiguous_dispatch_tail(tmp_path) -> None:
    provider = _Provider([])
    store = SimpleWorkStore(tmp_path / "simple.sqlite3")
    runtime = SimpleWorkRuntime(provider=provider, store=store)
    session = SimpleWorkSession.create("Продолжи проект.").updated(
        worker_mode=WorkerMode.PERSISTENT,
        worker_conversation_id="worker-1",
        last_worker_text="Первый этап готов.",
        next_worker_message="Да, продолжай.",
    )
    store.save(session)
    store.append_event(
        work_id=session.work_id,
        actor="worker",
        text="Первый этап готов.",
        conversation_id="worker-1",
        worker_mode=WorkerMode.PERSISTENT,
    )
    store.append_event(
        work_id=session.work_id,
        actor="codexia_to_worker",
        text=(
            "[Codexia]\nНе пользователь; не расширяет его разрешения.\n\n"
            "Да, продолжай."
        ),
        conversation_id="worker-1",
        worker_mode=WorkerMode.PERSISTENT,
    )

    guarded = runtime.resume(session.work_id)

    assert guarded.stop == "reconcile_required"
    assert guarded.session.status is SimpleWorkStatus.RECONCILE_REQUIRED
    assert provider.requests == []


def test_reconcile_falls_back_to_globally_unique_exact_dispatch_when_anchor_differs(tmp_path) -> None:
    second_prompt = (
        "[Codexia]\nНе пользователь; не расширяет его разрешения.\n\n"
        "Спроектируй R0 transfer battery."
    )
    provider = _Provider(
        [
            _Reply("ПОСТОЯННЫЙ WORKER: Начни исследование.", "codexia-1"),
            _Reply("Локальный writing-block текст.", "worker-1"),
            _Reply("Спроектируй R0 transfer battery.", "codexia-1"),
            ProviderError("chatgpt product-runtime request failed: CHATGPT_TURN_TIMEOUT"),
            _Reply("ГОТОВО: R0 восстановлен.", "codexia-1"),
        ],
        histories={
            "worker-1": (
                _Visible("user", "[Codexia]\nНе пользователь; не расширяет его разрешения.\n\nНачни исследование."),
                _Visible("assistant", "Канонически нормализованный writing-block текст."),
                _Visible("user", second_prompt),
                _Visible("assistant", "Готовая спецификация R0."),
            )
        },
    )
    runtime = SimpleWorkRuntime(
        provider=provider,
        store=SimpleWorkStore(tmp_path / "simple.sqlite3"),
    )

    result = runtime.start("Проведи исследование.", max_cycles=4)

    assert result.stop == "completed"
    assert result.session.worker_turns == 2
    assert result.session.last_worker_text == "Готовая спецификация R0."


def test_reconcile_does_not_consume_visible_text_before_canonical_completion(tmp_path) -> None:
    first_prompt = (
        "[Codexia]\nНе пользователь; не расширяет его разрешения.\n\n"
        "Начни первый этап."
    )
    second_prompt = (
        "[Codexia]\nНе пользователь; не расширяет его разрешения.\n\n"
        "Сделай второй этап."
    )
    provider = _Provider(
        [
            _Reply("ПОСТОЯННЫЙ WORKER: Начни первый этап.", "codexia-1"),
            _Reply("Первый этап готов.", "worker-1"),
            _Reply("Сделай второй этап.", "codexia-1"),
            ProviderError("chatgpt product-runtime request failed: CHATGPT_TURN_TIMEOUT"),
        ],
        histories={
            "worker-1": (
                _Visible("user", first_prompt, "u1", None),
                _Visible("assistant", "Первый этап готов.", "a1", "stop"),
                _Visible("user", second_prompt, "u2", None),
                _Visible("assistant", "Промежуточный текст...", "a2", None),
            )
        },
        statuses={
            "worker-1": _Status(
                status="in_progress",
                message_id="a2",
                finish_reason=None,
            )
        },
    )
    store = SimpleWorkStore(tmp_path / "simple.sqlite3")
    runtime = SimpleWorkRuntime(provider=provider, store=store)

    blocked = runtime.start("Сделай два этапа.", max_cycles=4)

    assert blocked.stop == "reconcile_required"
    assert blocked.session.worker_turns == 1
    assert blocked.session.last_worker_text == "Первый этап готов."
    history = store.history(blocked.session.work_id)
    assert [event.text for event in history if event.actor == "worker"] == [
        "Первый этап готов."
    ]


def test_busy_worker_preflight_is_not_recorded_as_submitted_dispatch(tmp_path) -> None:
    provider = _Provider(
        [
            _Reply("ПОСТОЯННЫЙ WORKER: Первый этап.", "codexia-1"),
            _Reply("Первый этап готов.", "worker-1"),
            _Reply("Продолжай.", "codexia-1"),
            ProviderError(
                "chatgpt product-runtime request failed: "
                "browser-owned write preflight failed: "
                "CANONICAL_CONVERSATION_NOT_COMPLETED"
            ),
        ]
    )
    store = SimpleWorkStore(tmp_path / "simple.sqlite3")
    runtime = SimpleWorkRuntime(provider=provider, store=store)

    busy = runtime.start("Сделай проект.", max_cycles=4)

    assert busy.stop == "worker_busy"
    assert busy.session.status is SimpleWorkStatus.READY
    assert busy.session.next_worker_message == "Продолжай."
    history = store.history(busy.session.work_id)
    dispatches = [
        event for event in history if event.actor == "codexia_to_worker"
    ]
    assert len(dispatches) == 1
    assert "Первый этап." in dispatches[0].text


def test_reconcile_repairs_historical_provisional_worker_ingest(tmp_path) -> None:
    store = SimpleWorkStore(tmp_path / "simple.sqlite3")
    codexia = store.codexia().updated(conversation_id="codexia-1")
    store.save_codexia(codexia)

    previous_prompt = (
        "[Codexia]\nНе пользователь; не расширяет его разрешения.\n\n"
        "Спроектируй R0."
    )
    stale_next_prompt = (
        "[Codexia]\nНе пользователь; не расширяет его разрешения.\n\n"
        "Исправь findings из промежуточного ревью."
    )
    session = SimpleWorkSession.create("Исследовательский проект.").updated(
        worker_mode=WorkerMode.PERSISTENT,
        worker_conversation_id="worker-1",
        last_worker_text="Промежуточный worker текст.",
        next_worker_message="Исправь findings из промежуточного ревью.",
        worker_turns=2,
    )
    store.save(session)
    store.append_event(
        work_id=session.work_id,
        actor="codexia_to_worker",
        text=previous_prompt,
        conversation_id="worker-1",
        worker_mode=WorkerMode.PERSISTENT,
    )
    store.append_event(
        work_id=session.work_id,
        actor="worker",
        text="Промежуточный worker текст.",
        conversation_id="worker-1",
        worker_mode=WorkerMode.PERSISTENT,
    )
    store.append_event(
        work_id=session.work_id,
        actor="codexia",
        text="Исправь findings из промежуточного ревью.",
        conversation_id="codexia-1",
    )
    store.append_event(
        work_id=session.work_id,
        actor="codexia_to_worker",
        text=stale_next_prompt,
        conversation_id="worker-1",
        worker_mode=WorkerMode.PERSISTENT,
    )

    provider = _Provider(
        [_Reply("ГОТОВО: Финальный worker ответ перечитан корректно.", "codexia-1")],
        histories={
            "worker-1": (
                _Visible("user", previous_prompt, "u-r0", None),
                _Visible(
                    "assistant",
                    "Полный канонический worker ответ.",
                    "a-final",
                    "stop",
                ),
            )
        },
        statuses={
            "worker-1": _Status(
                status="completed",
                message_id="a-final",
                finish_reason="stop",
            )
        },
    )
    runtime = SimpleWorkRuntime(provider=provider, store=store)

    repaired = runtime.reconcile(session.work_id, max_cycles=2)

    assert repaired.stop == "completed"
    assert repaired.session.worker_turns == 2
    assert repaired.session.last_worker_text == "Полный канонический worker ответ."
    assert repaired.session.final_text == "Финальный worker ответ перечитан корректно."
    history = store.history(session.work_id)
    assert any(event.actor == "worker_reconciled" for event in history)
    assert any(event.actor == "runtime_correction" for event in history)
    assert len(provider.requests) == 1
    assert provider.requests[0].conversation is not None
    assert provider.requests[0].conversation.conversation_id == "codexia-1"
    assert "промежуточным" in provider.requests[0].prompt
    assert "Полный канонический worker ответ." in provider.requests[0].prompt


def test_persistent_worker_artifacts_are_materialized_before_codexia_review(tmp_path) -> None:
    worker_text = (
        "Готово. "
        "[R0 package](sandbox:/mnt/data/ear_r0_source.zip) "
        "[Frozen spec](sandbox:/mnt/data/ear_r0/R0_SPEC_FROZEN.md)"
    )
    provider = _Provider(
        [
            _Reply("ПОСТОЯННЫЙ WORKER: Собери R0 пакет.", "codexia-1"),
            _Reply(worker_text, "worker-1"),
            _Reply("ГОТОВО: R0 готов локально.", "codexia-1"),
        ]
    )
    store = SimpleWorkStore(tmp_path / ".codexia" / "simple.sqlite3")
    runtime = SimpleWorkRuntime(provider=provider, store=store)

    result = runtime.start("Собери R0.", max_cycles=2)

    assert result.stop == "completed"
    assert [item[1] for item in provider.artifact_handoffs] == [
        "ear_r0_source.zip",
        "R0_SPEC_FROZEN.md",
    ]
    artifacts = store.artifacts(result.session.work_id)
    assert [artifact.source_filename for artifact in artifacts] == [
        "ear_r0_source.zip",
        "R0_SPEC_FROZEN.md",
    ]
    assert all(Path(artifact.local_path).is_file() for artifact in artifacts)
    assert all(artifact.sha256 == "a" * 64 for artifact in artifacts)

    codexia_review = provider.requests[2].prompt
    assert "Артефакты из этого ответа уже материализованы локально" in codexia_review
    assert "ear_r0_source.zip" in codexia_review
    assert "R0_SPEC_FROZEN.md" in codexia_review
    assert str(Path(artifacts[0].local_path)) in codexia_review


def test_manual_artifact_intake_uses_existing_last_persistent_worker_result(tmp_path) -> None:
    provider = _Provider([])
    store = SimpleWorkStore(tmp_path / ".codexia" / "simple.sqlite3")
    session = SimpleWorkSession.create("Долгая работа.").updated(
        worker_mode=WorkerMode.PERSISTENT,
        worker_conversation_id="worker-1",
        last_worker_text=(
            "Архив: [download](sandbox:/mnt/data/ear_r0_source.zip)"
        ),
        worker_turns=3,
        status=SimpleWorkStatus.WAITING_HUMAN,
        pending_human_question="Запусти familiarization.",
    )
    store.save(session)
    runtime = SimpleWorkRuntime(provider=provider, store=store)

    artifacts = runtime.intake_artifacts(session.work_id)

    assert len(artifacts) == 1
    artifact = artifacts[0]
    assert artifact.worker_turn == 3
    assert artifact.source_filename == "ear_r0_source.zip"
    assert artifact.source_conversation_id == "worker-1"
    assert Path(artifact.local_path).is_file()
    assert provider.requests == []


def test_manual_artifact_intake_is_idempotent_for_materialized_turn(tmp_path) -> None:
    provider = _Provider([])
    store = SimpleWorkStore(tmp_path / ".codexia" / "simple.sqlite3")
    session = SimpleWorkSession.create("Долгая работа.").updated(
        worker_mode=WorkerMode.PERSISTENT,
        worker_conversation_id="worker-1",
        last_worker_text="[file](sandbox:/mnt/data/result.zip)",
        worker_turns=1,
    )
    store.save(session)
    runtime = SimpleWorkRuntime(provider=provider, store=store)

    first = runtime.intake_artifacts(session.work_id)
    second = runtime.intake_artifacts(session.work_id)

    assert first == second
    assert len(provider.artifact_handoffs) == 1


def test_saved_codexia_chat_can_be_selected_per_new_work(tmp_path) -> None:
    provider = _Provider(
        [_Reply("ГОТОВО: Ответ из Voice Engine контекста.", "codexia-voice")]
    )
    store = SimpleWorkStore(tmp_path / "simple.sqlite3")
    general = store.codexia()
    voice = store.add_codexia("voice-engine", "codexia-voice")
    runtime = SimpleWorkRuntime(provider=provider, store=store)

    result = runtime.start(
        "Продолжи мысль в этом контексте.",
        codexia_alias="voice-engine",
    )

    assert result.stop == "completed"
    assert result.codexia.alias == "voice-engine"
    assert result.codexia.session_id == voice.session_id
    assert result.session.codexia_alias == "voice-engine"
    assert provider.requests[0].conversation is not None
    assert provider.requests[0].conversation.conversation_id == "codexia-voice"
    assert store.codexia("general").session_id == general.session_id


def test_multiple_saved_codexia_chats_keep_independent_conversation_identity(tmp_path) -> None:
    provider = _Provider(
        [
            _Reply("ГОТОВО: Voice one.", "voice-chat"),
            _Reply("ГОТОВО: Guitar one.", "guitar-chat"),
            _Reply("ГОТОВО: Voice two.", "voice-chat"),
        ]
    )
    store = SimpleWorkStore(tmp_path / "simple.sqlite3")
    store.add_codexia("voice-engine", "voice-chat")
    store.add_codexia("guitar", "guitar-chat")
    runtime = SimpleWorkRuntime(provider=provider, store=store)

    voice_one = runtime.start("Voice task 1.", codexia_alias="voice-engine")
    guitar = runtime.start("Guitar task.", codexia_alias="guitar")
    voice_two = runtime.start("Voice task 2.", codexia_alias="voice-engine")

    assert voice_one.codexia.session_id == voice_two.codexia.session_id
    assert guitar.codexia.session_id != voice_one.codexia.session_id
    assert [
        request.conversation.conversation_id
        for request in provider.requests
        if request.conversation is not None
    ] == ["voice-chat", "guitar-chat", "voice-chat"]


def test_answer_uses_codexia_chat_bound_to_original_work(tmp_path) -> None:
    provider = _Provider(
        [
            _Reply("К ПОЛЬЗОВАТЕЛЮ: Выбери A или B.", "voice-chat"),
            _Reply("ГОТОВО: Выбран A.", "voice-chat"),
        ]
    )
    store = SimpleWorkStore(tmp_path / "simple.sqlite3")
    store.add_codexia("voice-engine", "voice-chat")
    runtime = SimpleWorkRuntime(provider=provider, store=store)

    waiting = runtime.start("Нужен выбор.", codexia_alias="voice-engine")
    answered = runtime.answer(waiting.session.work_id, "A")

    assert waiting.session.codexia_alias == "voice-engine"
    assert answered.codexia.alias == "voice-engine"
    assert answered.stop == "completed"
    assert provider.requests[1].conversation is not None
    assert provider.requests[1].conversation.conversation_id == "voice-chat"


def test_codexia_registry_lists_general_first_and_rejects_duplicate_conversation(tmp_path) -> None:
    store = SimpleWorkStore(tmp_path / "simple.sqlite3")
    general = store.codexia()
    guitar = store.add_codexia("guitar", "guitar-chat")
    voice = store.add_codexia("voice-engine", "voice-chat")

    chats = store.codexia_chats()

    assert [chat.alias for chat in chats] == [
        "general",
        "guitar",
        "voice-engine",
    ]
    assert chats[0].session_id == general.session_id
    assert chats[1].session_id == guitar.session_id
    assert chats[2].session_id == voice.session_id

    try:
        store.add_codexia("other", "voice-chat")
    except ValueError as exc:
        assert "already registered" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("duplicate conversation registration must fail")


def test_existing_singleton_and_work_rows_migrate_to_general_codexia(tmp_path) -> None:
    database = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE simple_codexia_v1 (
                slot INTEGER PRIMARY KEY CHECK(slot = 1),
                session_id TEXT NOT NULL,
                conversation_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO simple_codexia_v1 (
                slot, session_id, conversation_id, created_at, updated_at
            ) VALUES (1, 'legacy-session', 'legacy-chat', 't0', 't1')
            """
        )
        connection.execute(
            """
            CREATE TABLE simple_work_v1 (
                work_id TEXT PRIMARY KEY,
                user_request TEXT NOT NULL,
                status TEXT NOT NULL,
                worker_mode TEXT NOT NULL,
                worker_conversation_id TEXT,
                last_codexia_text TEXT,
                last_worker_text TEXT,
                next_worker_message TEXT,
                pending_human_question TEXT,
                final_text TEXT,
                worker_turns INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO simple_work_v1 (
                work_id, user_request, status, worker_mode,
                worker_conversation_id, last_codexia_text, last_worker_text,
                next_worker_message, pending_human_question, final_text,
                worker_turns, created_at, updated_at
            ) VALUES (
                'legacy-work', 'old task', 'completed', 'none',
                NULL, NULL, NULL, NULL, NULL, 'done', 0, 't0', 't1'
            )
            """
        )

    store = SimpleWorkStore(database)

    general = store.codexia("general")
    old_work = store.load("legacy-work")

    assert general.alias == "general"
    assert general.session_id == "legacy-session"
    assert general.conversation_id == "legacy-chat"
    assert old_work.codexia_alias == "general"
    assert old_work.codexia_mode is CodexiaMode.SAVED


def test_temporary_codexia_direct_task_completes_and_is_not_registered(tmp_path) -> None:
    provider = _Provider(
        [],
        temporary_replies=[
            _Reply("ГОТОВО: Одноразовый ответ.", "temporary-codexia-1")
        ],
    )
    store = SimpleWorkStore(tmp_path / "simple.sqlite3")
    runtime = SimpleWorkRuntime(provider=provider, store=store)

    result = runtime.start(
        "Одноразовая задача.",
        temporary_codexia=True,
    )

    assert result.stop == "completed"
    assert result.session.status is SimpleWorkStatus.COMPLETED
    assert result.session.codexia_mode is CodexiaMode.TEMPORARY
    assert result.session.codexia_alias == "temporary"
    assert result.codexia.alias == "temporary"
    assert result.codexia.conversation_id == "temporary-codexia-1"
    assert result.session.final_text == "Одноразовый ответ."
    assert provider.temporary_end_count == 1
    assert provider.requests == []

    chats = store.codexia_chats()
    assert [chat.alias for chat in chats] == ["general"]
    history = store.history(result.session.work_id)
    assert [event.actor for event in history] == [
        "human",
        "codexia",
        "codexia_temporary_closed",
    ]


def test_temporary_codexia_can_supervise_persistent_worker_in_same_run(tmp_path) -> None:
    provider = _Provider(
        [_Reply("Worker завершил исследование.", "worker-saved-1")],
        temporary_replies=[
            _Reply(
                "ПОСТОЯННЫЙ WORKER: Проведи отдельное исследование.",
                "temporary-codexia-1",
            ),
            _Reply(
                "ГОТОВО: Итог после persistent worker.",
                "temporary-codexia-1",
            ),
        ],
    )
    store = SimpleWorkStore(tmp_path / "simple.sqlite3")
    runtime = SimpleWorkRuntime(provider=provider, store=store)

    result = runtime.start(
        "Сделай одноразовую работу с отдельным исследованием.",
        temporary_codexia=True,
        max_cycles=3,
    )

    assert result.stop == "completed"
    assert result.session.codexia_mode is CodexiaMode.TEMPORARY
    assert result.session.worker_mode is WorkerMode.PERSISTENT
    assert result.session.worker_conversation_id == "worker-saved-1"
    assert result.session.worker_turns == 1
    assert result.session.final_text == "Итог после persistent worker."
    assert len(provider.temporary_prompts) == 2
    assert len(provider.requests) == 1
    assert provider.requests[0].conversation is None
    assert provider.temporary_end_count == 1


def test_temporary_codexia_stays_live_across_two_persistent_worker_cycles(
    tmp_path,
) -> None:
    provider = _Provider(
        [
            _Reply("Первый worker-этап готов.", "worker-saved-1"),
            _Reply("Второй worker-этап готов.", "worker-saved-1"),
        ],
        temporary_replies=[
            _Reply(
                "ПОСТОЯННЫЙ WORKER: Сделай первый этап.",
                "temporary-codexia-1",
            ),
            _Reply(
                "ПОСТОЯННЫЙ WORKER: Теперь сделай второй этап.",
                "temporary-codexia-1",
            ),
            _Reply(
                "ГОТОВО: Оба этапа завершены.",
                "temporary-codexia-1",
            ),
        ],
    )
    store = SimpleWorkStore(tmp_path / "simple.sqlite3")
    runtime = SimpleWorkRuntime(provider=provider, store=store)

    result = runtime.start(
        "Сделай два последовательных этапа через одного persistent worker.",
        temporary_codexia=True,
        max_cycles=4,
    )

    assert result.stop == "completed"
    assert result.session.codexia_mode is CodexiaMode.TEMPORARY
    assert result.codexia.conversation_id == "temporary-codexia-1"
    assert result.session.worker_mode is WorkerMode.PERSISTENT
    assert result.session.worker_conversation_id == "worker-saved-1"
    assert result.session.worker_turns == 2
    assert result.session.final_text == "Оба этапа завершены."

    assert len(provider.temporary_prompts) == 3
    assert len(provider.requests) == 2
    assert provider.requests[0].conversation is None
    assert provider.requests[1].conversation is not None
    assert (
        provider.requests[1].conversation.conversation_id
        == "worker-saved-1"
    )
    assert provider.temporary_end_count == 1

    history = store.history(result.session.work_id)
    assert [event.actor for event in history] == [
        "human",
        "codexia",
        "codexia_to_worker",
        "worker",
        "codexia",
        "codexia_to_worker",
        "worker",
        "codexia",
        "codexia_temporary_closed",
    ]


def test_temporary_codexia_human_boundary_closes_and_cannot_resume(tmp_path) -> None:
    provider = _Provider(
        [],
        temporary_replies=[
            _Reply(
                "К ПОЛЬЗОВАТЕЛЮ: Какой вариант выбрать?",
                "temporary-codexia-1",
            )
        ],
    )
    store = SimpleWorkStore(tmp_path / "simple.sqlite3")
    runtime = SimpleWorkRuntime(provider=provider, store=store)

    result = runtime.start(
        "Если без выбора нельзя — спроси.",
        temporary_codexia=True,
    )

    assert result.stop == "temporary_closed"
    assert result.session.status is SimpleWorkStatus.TEMPORARY_CLOSED
    assert result.session.pending_human_question == "Какой вариант выбрать?"
    assert provider.temporary_end_count == 1

    try:
        runtime.answer(result.session.work_id, "A")
    except RuntimeError as exc:
        assert "cannot cross a process boundary" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("closed Temporary Codexia must not be resumable")


def test_temporary_codexia_rejects_temporary_worker_and_closes_lifecycle(tmp_path) -> None:
    provider = _Provider(
        [],
        temporary_replies=[
            _Reply(
                "ВРЕМЕННЫЙ WORKER: Сделай отдельную работу.",
                "temporary-codexia-1",
            )
        ],
    )
    runtime = SimpleWorkRuntime(
        provider=provider,
        store=SimpleWorkStore(tmp_path / "simple.sqlite3"),
    )

    try:
        runtime.start("Попробуй временного worker.", temporary_codexia=True)
    except RuntimeError as exc:
        assert "cannot create a temporary worker" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("Temporary Codexia must not own two Temporary lifecycles")

    assert provider.temporary_end_count == 1


def test_saved_registry_reserves_temporary_alias(tmp_path) -> None:
    store = SimpleWorkStore(tmp_path / "simple.sqlite3")

    try:
        store.add_codexia("temporary", "saved-chat")
    except ValueError as exc:
        assert "reserved" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("temporary alias must be reserved")


def test_temporary_codexia_preserves_completed_result_when_cleanup_is_unproven(
    tmp_path,
) -> None:
    provider = _Provider(
        [],
        temporary_replies=[
            _Reply("ГОТОВО: Результат уже готов.", "temporary-codexia-1")
        ],
        temporary_end_error=ProviderError(
            "failed to end chatgpt temporary lifecycle: "
            "PR8_13_TEMPORARY_LIFECYCLE_END_NOT_PROVEN"
        ),
    )
    store = SimpleWorkStore(tmp_path / "simple.sqlite3")
    runtime = SimpleWorkRuntime(provider=provider, store=store)

    result = runtime.start("Одноразовая задача.", temporary_codexia=True)

    assert result.stop == "completed_cleanup_unproven"
    assert result.session.status is SimpleWorkStatus.COMPLETED
    assert result.session.final_text == "Результат уже готов."
    assert provider.temporary_end_count == 1
    history = store.history(result.session.work_id)
    cleanup_events = [
        event
        for event in history
        if event.actor == "codexia_temporary_close_unproven"
    ]
    assert len(cleanup_events) == 1
    assert "PR8_13_TEMPORARY_LIFECYCLE_END_NOT_PROVEN" in cleanup_events[0].text


def test_temporary_codexia_human_boundary_remains_terminal_when_cleanup_is_unproven(
    tmp_path,
) -> None:
    provider = _Provider(
        [],
        temporary_replies=[
            _Reply("К ПОЛЬЗОВАТЕЛЮ: Нужен выбор.", "temporary-codexia-1")
        ],
        temporary_end_error=ProviderError(
            "failed to end chatgpt temporary lifecycle"
        ),
    )
    store = SimpleWorkStore(tmp_path / "simple.sqlite3")
    runtime = SimpleWorkRuntime(provider=provider, store=store)

    result = runtime.start("Спроси только если нужно.", temporary_codexia=True)

    assert result.stop == "temporary_close_unproven"
    assert result.session.status is SimpleWorkStatus.TEMPORARY_CLOSED
    assert result.session.pending_human_question == "Нужен выбор."
    assert provider.temporary_end_count == 1
