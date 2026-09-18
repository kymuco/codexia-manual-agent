from __future__ import annotations

from dataclasses import dataclass

from codexia_manual_agent.domain.models import (
    ProviderConversation,
    ProviderRequest,
    ProviderResponse,
)
from codexia_manual_agent.simple_work import (
    SimpleWorkRuntime,
    SimpleWorkStatus,
    SimpleWorkStore,
)


@dataclass(frozen=True)
class _Reply:
    text: str
    conversation_id: str


class _Provider:
    def __init__(self, replies: list[_Reply]) -> None:
        self.replies = list(replies)
        self.requests: list[ProviderRequest] = []

    def send(self, request: ProviderRequest) -> ProviderResponse:
        self.requests.append(request)
        reply = self.replies.pop(0)
        return ProviderResponse(
            text=reply.text,
            conversation=ProviderConversation(
                conversation_id=reply.conversation_id,
            ),
        )


def test_simple_work_uses_persistent_codexia_and_worker_chats(tmp_path) -> None:
    provider = _Provider(
        [
            _Reply("Разберись с задачей и предложи хороший первый результат.", "codexia-1"),
            _Reply("Первый результат.", "worker-1"),
            _Reply("Да, давай продолжим. Проверь ещё один слабый момент.", "codexia-1"),
            _Reply("Уточнённый результат.", "worker-1"),
            _Reply("ГОТОВО: Итог для пользователя.", "codexia-1"),
        ]
    )
    store = SimpleWorkStore(tmp_path / "simple.sqlite3")
    runtime = SimpleWorkRuntime(provider=provider, store=store)

    result = runtime.start("Сделай простое исследование.", max_cycles=4)

    assert result.stop == "completed"
    assert result.session.status is SimpleWorkStatus.COMPLETED
    assert result.session.final_text == "Итог для пользователя."
    assert result.session.worker_turns == 2
    assert result.session.codexia_conversation_id == "codexia-1"
    assert result.session.worker_conversation_id == "worker-1"

    assert provider.requests[0].conversation is None
    assert provider.requests[1].conversation is None
    assert provider.requests[2].conversation is not None
    assert provider.requests[2].conversation.conversation_id == "codexia-1"
    assert provider.requests[3].conversation is not None
    assert provider.requests[3].conversation.conversation_id == "worker-1"
    assert provider.requests[4].conversation is not None
    assert provider.requests[4].conversation.conversation_id == "codexia-1"


def test_ordinary_codexia_text_is_forwarded_without_protocol_wrapper(tmp_path) -> None:
    provider = _Provider(
        [
            _Reply("Да, давай. Проверь два варианта.", "codexia-1"),
            _Reply("Сравнил два варианта.", "worker-1"),
            _Reply("ГОТОВО: Сравнение завершено.", "codexia-1"),
        ]
    )
    runtime = SimpleWorkRuntime(
        provider=provider,
        store=SimpleWorkStore(tmp_path / "simple.sqlite3"),
    )

    runtime.start("Сравни варианты.", max_cycles=2)

    assert provider.requests[1].prompt == "Да, давай. Проверь два варианта."


def test_waiting_human_answer_resumes_same_codexia_and_worker_chats(tmp_path) -> None:
    provider = _Provider(
        [
            _Reply("Сначала собери два разумных варианта.", "codexia-1"),
            _Reply("Есть дешёвый A и более качественный B.", "worker-1"),
            _Reply(
                "К ПОЛЬЗОВАТЕЛЮ: Что важнее: минимальная цена или качество?",
                "codexia-1",
            ),
            _Reply("Выбери качество и доведи сравнение до конца.", "codexia-1"),
            _Reply("Тогда лучший вариант — B.", "worker-1"),
            _Reply("ГОТОВО: Выбран вариант B.", "codexia-1"),
        ]
    )
    store = SimpleWorkStore(tmp_path / "simple.sqlite3")
    runtime = SimpleWorkRuntime(provider=provider, store=store)

    waiting = runtime.start("Подбери вариант.", max_cycles=3)

    assert waiting.stop == "waiting_human"
    assert waiting.session.pending_human_question == (
        "Что важнее: минимальная цена или качество?"
    )

    completed = runtime.answer(
        waiting.session.work_id,
        "Качество.",
        max_cycles=3,
    )

    assert completed.stop == "completed"
    assert completed.session.worker_conversation_id == "worker-1"
    assert completed.session.codexia_conversation_id == "codexia-1"
    assert provider.requests[3].conversation is not None
    assert provider.requests[3].conversation.conversation_id == "codexia-1"
    assert provider.requests[4].conversation is not None
    assert provider.requests[4].conversation.conversation_id == "worker-1"


def test_cycle_limit_preserves_next_worker_message_for_resume(tmp_path) -> None:
    provider = _Provider(
        [
            _Reply("Сделай первый проход.", "codexia-1"),
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
    assert first.session.status is SimpleWorkStatus.READY
    assert first.session.next_worker_message == "Да, теперь сделай второй проход."

    resumed = runtime.resume(first.session.work_id, max_cycles=1)

    assert resumed.stop == "completed"
    assert resumed.session.worker_turns == 2


def test_session_round_trips_through_sqlite(tmp_path) -> None:
    provider = _Provider(
        [
            _Reply("Сделай один проход.", "codexia-1"),
            _Reply("Готово.", "worker-1"),
            _Reply("ГОТОВО: Финал.", "codexia-1"),
        ]
    )
    path = tmp_path / "simple.sqlite3"
    runtime = SimpleWorkRuntime(
        provider=provider,
        store=SimpleWorkStore(path),
    )

    result = runtime.start("Одно поручение.", max_cycles=1)
    recovered = SimpleWorkStore(path).load(result.session.work_id)

    assert recovered == result.session
