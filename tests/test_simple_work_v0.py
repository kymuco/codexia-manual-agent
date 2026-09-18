from __future__ import annotations

from dataclasses import dataclass

from codexia_manual_agent.domain.models import (
    ProviderConversation,
    ProviderRequest,
    ProviderResponse,
)
from codexia_manual_agent.simple_work import (
    SimpleWorkRuntime,
    SimpleWorkSession,
    SimpleWorkStatus,
    SimpleWorkStore,
    WorkerMode,
)


@dataclass(frozen=True)
class _Reply:
    text: str
    conversation_id: str


class _Provider:
    def __init__(
        self,
        normal_replies: list[_Reply],
        *,
        temporary_replies: list[_Reply] | None = None,
    ) -> None:
        self.normal_replies = list(normal_replies)
        self.temporary_replies = list(temporary_replies or [])
        self.requests: list[ProviderRequest] = []
        self.temporary_prompts: list[str] = []
        self.temporary_end_count = 0

    def send(self, request: ProviderRequest) -> ProviderResponse:
        self.requests.append(request)
        reply = self.normal_replies.pop(0)
        return ProviderResponse(
            text=reply.text,
            conversation=ProviderConversation(
                conversation_id=reply.conversation_id,
            ),
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
    assert provider.temporary_prompts[0].startswith("[Codexia]\n\n")
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
    assert first_worker_request.prompt.startswith("[Codexia]\n\n")
    assert second_worker_request.prompt.startswith("[Codexia]\n\n")
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
    assert worker_resume.prompt.startswith("[Codexia]\n\n")


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
