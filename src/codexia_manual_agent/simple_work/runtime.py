from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from codexia_manual_agent.domain.models import (
    ProviderConversation,
    ProviderRequest,
    ProviderResponse,
)
from codexia_manual_agent.simple_work.session import (
    SimpleWorkSession,
    SimpleWorkStatus,
    SimpleWorkStore,
)

_TO_HUMAN = "К ПОЛЬЗОВАТЕЛЮ:"
_DONE = "ГОТОВО:"


class _Provider(Protocol):
    def send(self, request: ProviderRequest) -> ProviderResponse: ...


@dataclass(frozen=True, slots=True)
class SimpleWorkRunResult:
    session: SimpleWorkSession
    stop: str


class SimpleWorkRuntime:
    """Minimal persistent Codexia-to-worker loop.

    Ordinary Codexia text is forwarded to the worker as-is.
    Only the two explicit human-readable prefixes have runtime meaning.
    """

    def __init__(self, *, provider: _Provider, store: SimpleWorkStore) -> None:
        self.provider = provider
        self.store = store

    def start(self, user_request: str, *, max_cycles: int = 8) -> SimpleWorkRunResult:
        _validate_cycles(max_cycles)
        session = self.store.save(SimpleWorkSession.create(user_request))
        response = self.provider.send(
            ProviderRequest(prompt=_codexia_bootstrap(session.user_request))
        )
        session = self._accept_codexia_response(session, response)
        self.store.save(session)
        if session.status is not SimpleWorkStatus.READY:
            return SimpleWorkRunResult(session=session, stop=session.status.value)
        return self._drive(session, max_cycles=max_cycles)

    def resume(self, work_id: str, *, max_cycles: int = 8) -> SimpleWorkRunResult:
        _validate_cycles(max_cycles)
        session = self.store.load(work_id)
        if session.status is SimpleWorkStatus.COMPLETED:
            return SimpleWorkRunResult(session=session, stop="completed")
        if session.status is SimpleWorkStatus.WAITING_HUMAN:
            return SimpleWorkRunResult(session=session, stop="waiting_human")
        if session.next_worker_message is None:
            raise RuntimeError("ready simple work has no next worker message")
        return self._drive(session, max_cycles=max_cycles)

    def answer(
        self,
        work_id: str,
        answer: str,
        *,
        max_cycles: int = 8,
    ) -> SimpleWorkRunResult:
        _validate_cycles(max_cycles)
        value = answer.strip()
        if not value:
            raise ValueError("answer must be non-empty")
        session = self.store.load(work_id)
        if session.status is not SimpleWorkStatus.WAITING_HUMAN:
            raise RuntimeError("simple work is not waiting for the human")
        if session.codexia_conversation_id is None:
            raise RuntimeError("waiting simple work lost its Codexia conversation")
        response = self.provider.send(
            ProviderRequest(
                prompt=f"Пользователь ответил:\n\n{value}",
                conversation=ProviderConversation(
                    conversation_id=session.codexia_conversation_id
                ),
            )
        )
        session = session.updated(
            status=SimpleWorkStatus.READY,
            pending_human_question=None,
        )
        session = self._accept_codexia_response(session, response)
        self.store.save(session)
        if session.status is not SimpleWorkStatus.READY:
            return SimpleWorkRunResult(session=session, stop=session.status.value)
        return self._drive(session, max_cycles=max_cycles)

    def status(self, work_id: str) -> SimpleWorkSession:
        return self.store.load(work_id)

    def _drive(
        self,
        session: SimpleWorkSession,
        *,
        max_cycles: int,
    ) -> SimpleWorkRunResult:
        remaining = max_cycles
        while (
            remaining > 0
            and session.status is SimpleWorkStatus.READY
            and session.next_worker_message is not None
        ):
            worker_response = self.provider.send(
                ProviderRequest(
                    prompt=session.next_worker_message,
                    conversation=(
                        ProviderConversation(
                            conversation_id=session.worker_conversation_id
                        )
                        if session.worker_conversation_id is not None
                        else None
                    ),
                )
            )
            worker_conversation_id = _conversation_id(worker_response)
            if session.worker_conversation_id is not None:
                if worker_conversation_id != session.worker_conversation_id:
                    raise RuntimeError("worker conversation identity changed")
            session = session.updated(
                worker_conversation_id=worker_conversation_id,
                last_worker_text=worker_response.text,
                next_worker_message=None,
                worker_turns=session.worker_turns + 1,
            )
            self.store.save(session)
            remaining -= 1

            if session.codexia_conversation_id is None:
                raise RuntimeError("simple work lost its Codexia conversation")
            codexia_response = self.provider.send(
                ProviderRequest(
                    prompt=f"Рабочий чат ответил:\n\n{worker_response.text}",
                    conversation=ProviderConversation(
                        conversation_id=session.codexia_conversation_id
                    ),
                )
            )
            session = self._accept_codexia_response(session, codexia_response)
            self.store.save(session)

            if session.status is SimpleWorkStatus.WAITING_HUMAN:
                return SimpleWorkRunResult(session=session, stop="waiting_human")
            if session.status is SimpleWorkStatus.COMPLETED:
                return SimpleWorkRunResult(session=session, stop="completed")

        return SimpleWorkRunResult(session=session, stop="cycle_limit")

    def _accept_codexia_response(
        self,
        session: SimpleWorkSession,
        response: ProviderResponse,
    ) -> SimpleWorkSession:
        codexia_conversation_id = _conversation_id(response)
        if session.codexia_conversation_id is not None:
            if codexia_conversation_id != session.codexia_conversation_id:
                raise RuntimeError("Codexia conversation identity changed")

        text = response.text.strip()
        if not text:
            raise RuntimeError("Codexia returned empty text")

        if text.startswith(_TO_HUMAN):
            question = text[len(_TO_HUMAN) :].strip()
            if not question:
                raise RuntimeError("Codexia requested the human without a question")
            return session.updated(
                status=SimpleWorkStatus.WAITING_HUMAN,
                codexia_conversation_id=codexia_conversation_id,
                last_codexia_text=text,
                next_worker_message=None,
                pending_human_question=question,
                final_text=None,
            )

        if text.startswith(_DONE):
            final_text = text[len(_DONE) :].strip()
            if not final_text:
                raise RuntimeError("Codexia completed without a final result")
            return session.updated(
                status=SimpleWorkStatus.COMPLETED,
                codexia_conversation_id=codexia_conversation_id,
                last_codexia_text=text,
                next_worker_message=None,
                pending_human_question=None,
                final_text=final_text,
            )

        return session.updated(
            status=SimpleWorkStatus.READY,
            codexia_conversation_id=codexia_conversation_id,
            last_codexia_text=text,
            next_worker_message=text,
            pending_human_question=None,
            final_text=None,
        )


def _conversation_id(response: ProviderResponse) -> str:
    conversation = response.conversation
    if conversation is None or not conversation.conversation_id:
        raise RuntimeError("provider response did not contain a conversation id")
    return conversation.conversation_id


def _validate_cycles(value: int) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError("max_cycles must be a positive integer")


def _codexia_bootstrap(user_request: str) -> str:
    return f"""Ты — Codexia для одной делегированной работы.

Пользователь дал короткое поручение:

{user_request}

У тебя есть отдельный постоянный рабочий чат, который выполняет работу.
Твоя задача — вести эту работу дальше так же естественно, как это делал бы
пользователь: смотреть на ответ рабочего чата и решать, что ему написать дальше.

Обычный твой ответ будет отправлен в рабочий чат ровно как написан.
Поэтому пиши нормальные человекочитаемые сообщения. Можно отвечать очень коротко,
например: «Да, давай продолжим», «Да, сделай следующий шаг» или
«Хорошо, теперь проверь это ещё раз». Если задача сложная, можешь дать более
подробное следующее поручение.

Не проси пользователя подтверждать рутинное продолжение работы.
Не используй JSON, идентификаторы, digests или внутреннюю служебную разметку.

Только два случая имеют специальный формат:

К ПОЛЬЗОВАТЕЛЮ: <один ясный вопрос>
— используй только когда действительно нужно решение пользователя.

ГОТОВО: <готовый итог для пользователя>
— используй только когда поручение действительно доведено до результата.

Первым ответом напиши сообщение, которое нужно отправить рабочему чату.
"""
