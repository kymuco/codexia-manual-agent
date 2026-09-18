from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from codexia_manual_agent.domain.models import (
    ProviderConversation,
    ProviderRequest,
    ProviderResponse,
)
from codexia_manual_agent.simple_work.session import (
    SimpleCodexiaSession,
    SimpleWorkSession,
    SimpleWorkStatus,
    SimpleWorkStore,
    WorkerMode,
)

_TO_HUMAN = "К ПОЛЬЗОВАТЕЛЮ:"
_DONE = "ГОТОВО:"
_TEMP_WORKER = "ВРЕМЕННЫЙ WORKER:"
_PERSISTENT_WORKER = "ПОСТОЯННЫЙ WORKER:"


class _Provider(Protocol):
    def send(self, request: ProviderRequest) -> ProviderResponse: ...

    def send_temporary(self, prompt: str) -> ProviderResponse: ...

    def end_temporary_chat(self) -> bool: ...


@dataclass(frozen=True, slots=True)
class SimpleWorkRunResult:
    session: SimpleWorkSession
    codexia: SimpleCodexiaSession
    stop: str


class SimpleWorkRuntime:
    """Codexia-first work loop with lazy optional workers.

    One long-lived Codexia conversation handles many user tasks. Simple tasks can
    finish directly. A worker exists only when Codexia explicitly asks for one.
    Persistent workers are reserved for work whose separate history should survive.
    """

    def __init__(self, *, provider: _Provider, store: SimpleWorkStore) -> None:
        self.provider = provider
        self.store = store

    def start(self, user_request: str, *, max_cycles: int = 8) -> SimpleWorkRunResult:
        _validate_cycles(max_cycles)
        session = self.store.save(SimpleWorkSession.create(user_request))
        codexia = self.store.codexia()
        self.store.append_event(
            work_id=session.work_id,
            actor="human",
            text=session.user_request,
        )

        if codexia.conversation_id is None:
            request = ProviderRequest(prompt=_codexia_bootstrap(session.user_request))
        else:
            request = ProviderRequest(
                prompt=_new_task_prompt(session.user_request),
                conversation=ProviderConversation(
                    conversation_id=codexia.conversation_id
                ),
            )
        response = self.provider.send(request)
        session, codexia = self._accept_codexia_response(
            session,
            codexia,
            response,
        )
        self.store.save(session)
        self.store.save_codexia(codexia)

        if session.status is not SimpleWorkStatus.READY:
            return SimpleWorkRunResult(
                session=session,
                codexia=codexia,
                stop=session.status.value,
            )
        return self._drive(session, codexia, max_cycles=max_cycles)

    def resume(self, work_id: str, *, max_cycles: int = 8) -> SimpleWorkRunResult:
        _validate_cycles(max_cycles)
        session = self.store.load(work_id)
        codexia = self.store.codexia()
        if session.status is SimpleWorkStatus.COMPLETED:
            return SimpleWorkRunResult(session=session, codexia=codexia, stop="completed")
        if session.status is SimpleWorkStatus.WAITING_HUMAN:
            return SimpleWorkRunResult(
                session=session,
                codexia=codexia,
                stop="waiting_human",
            )
        if session.next_worker_message is None:
            raise RuntimeError("ready simple work has no pending worker instruction")
        if session.worker_mode is WorkerMode.NONE:
            raise RuntimeError("ready simple work has no active worker route")
        return self._drive(session, codexia, max_cycles=max_cycles)

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
        codexia = self.store.codexia()
        if session.status is not SimpleWorkStatus.WAITING_HUMAN:
            raise RuntimeError("simple work is not waiting for the human")
        if codexia.conversation_id is None:
            raise RuntimeError("Codexia session has no conversation")

        self.store.append_event(
            work_id=session.work_id,
            actor="human",
            text=value,
        )
        response = self.provider.send(
            ProviderRequest(
                prompt=f"Пользователь ответил на твой вопрос:\n\n{value}",
                conversation=ProviderConversation(
                    conversation_id=codexia.conversation_id
                ),
            )
        )
        session = session.updated(
            status=SimpleWorkStatus.READY,
            pending_human_question=None,
        )
        session, codexia = self._accept_codexia_response(
            session,
            codexia,
            response,
        )
        self.store.save(session)
        self.store.save_codexia(codexia)
        if session.status is not SimpleWorkStatus.READY:
            return SimpleWorkRunResult(
                session=session,
                codexia=codexia,
                stop=session.status.value,
            )
        return self._drive(session, codexia, max_cycles=max_cycles)

    def status(self, work_id: str) -> SimpleWorkSession:
        return self.store.load(work_id)

    def _drive(
        self,
        session: SimpleWorkSession,
        codexia: SimpleCodexiaSession,
        *,
        max_cycles: int,
    ) -> SimpleWorkRunResult:
        remaining = max_cycles
        while (
            remaining > 0
            and session.status is SimpleWorkStatus.READY
            and session.next_worker_message is not None
        ):
            mode = session.worker_mode
            if mode is WorkerMode.NONE:
                raise RuntimeError("worker instruction exists without a worker mode")

            worker_prompt = _worker_prompt(session.next_worker_message)
            self.store.append_event(
                work_id=session.work_id,
                actor="codexia_to_worker",
                text=worker_prompt,
                conversation_id=session.worker_conversation_id,
                worker_mode=mode,
            )

            if mode is WorkerMode.TEMPORARY:
                worker_response = self.provider.send_temporary(worker_prompt)
                worker_conversation_id = None
            else:
                worker_response = self.provider.send(
                    ProviderRequest(
                        prompt=worker_prompt,
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
                        raise RuntimeError("persistent worker conversation identity changed")

            self.store.append_event(
                work_id=session.work_id,
                actor="worker",
                text=worker_response.text,
                conversation_id=worker_conversation_id,
                worker_mode=mode,
            )
            session = session.updated(
                worker_conversation_id=worker_conversation_id,
                last_worker_text=worker_response.text,
                next_worker_message=None,
                worker_turns=session.worker_turns + 1,
            )
            self.store.save(session)
            remaining -= 1

            if codexia.conversation_id is None:
                raise RuntimeError("Codexia session lost its conversation")
            codexia_response = self.provider.send(
                ProviderRequest(
                    prompt=_worker_result_prompt(mode, worker_response.text),
                    conversation=ProviderConversation(
                        conversation_id=codexia.conversation_id
                    ),
                )
            )
            session, codexia = self._accept_codexia_response(
                session,
                codexia,
                codexia_response,
            )
            self.store.save(session)
            self.store.save_codexia(codexia)

            if session.status is SimpleWorkStatus.WAITING_HUMAN:
                if mode is WorkerMode.TEMPORARY:
                    session = self._close_temporary_worker(session)
                return SimpleWorkRunResult(
                    session=session,
                    codexia=codexia,
                    stop="waiting_human",
                )
            if session.status is SimpleWorkStatus.COMPLETED:
                if mode is WorkerMode.TEMPORARY:
                    session = self._close_temporary_worker(session)
                return SimpleWorkRunResult(
                    session=session,
                    codexia=codexia,
                    stop="completed",
                )

        if (
            session.status is SimpleWorkStatus.READY
            and session.worker_mode is WorkerMode.TEMPORARY
            and session.next_worker_message is not None
        ):
            session, codexia = self._pause_temporary_at_cycle_limit(session, codexia)
            if session.status is SimpleWorkStatus.WAITING_HUMAN:
                return SimpleWorkRunResult(
                    session=session,
                    codexia=codexia,
                    stop="waiting_human",
                )
            if session.status is SimpleWorkStatus.COMPLETED:
                return SimpleWorkRunResult(
                    session=session,
                    codexia=codexia,
                    stop="completed",
                )

        return SimpleWorkRunResult(
            session=session,
            codexia=codexia,
            stop="cycle_limit",
        )

    def _pause_temporary_at_cycle_limit(
        self,
        session: SimpleWorkSession,
        codexia: SimpleCodexiaSession,
    ) -> tuple[SimpleWorkSession, SimpleCodexiaSession]:
        session = self._close_temporary_worker(session)
        if codexia.conversation_id is None:
            raise RuntimeError("Codexia session lost its conversation")
        response = self.provider.send(
            ProviderRequest(
                prompt=(
                    "Simple Work закрыл временный рабочий чат из-за лимита циклов "
                    "текущего запуска. Контекст его ответов уже есть в этом чате и "
                    "сохранён локально. Если работа ещё нужна, дай новое самодостаточное "
                    "поручение через ВРЕМЕННЫЙ WORKER: или ПОСТОЯННЫЙ WORKER:. "
                    "Если работа уже завершена, ответь ГОТОВО:. Если нужен пользователь, "
                    "ответь К ПОЛЬЗОВАТЕЛЮ:."
                ),
                conversation=ProviderConversation(
                    conversation_id=codexia.conversation_id
                ),
            )
        )
        session, codexia = self._accept_codexia_response(
            session,
            codexia,
            response,
        )
        self.store.save(session)
        self.store.save_codexia(codexia)
        return session, codexia

    def _close_temporary_worker(self, session: SimpleWorkSession) -> SimpleWorkSession:
        self.provider.end_temporary_chat()
        closed = session.updated(
            worker_mode=WorkerMode.NONE,
            worker_conversation_id=None,
            next_worker_message=None,
        )
        self.store.save(closed)
        return closed

    def _accept_codexia_response(
        self,
        session: SimpleWorkSession,
        codexia: SimpleCodexiaSession,
        response: ProviderResponse,
    ) -> tuple[SimpleWorkSession, SimpleCodexiaSession]:
        codexia_conversation_id = _conversation_id(response)
        if codexia.conversation_id is not None:
            if codexia_conversation_id != codexia.conversation_id:
                raise RuntimeError("Codexia conversation identity changed")
        else:
            codexia = codexia.updated(conversation_id=codexia_conversation_id)

        text = response.text.strip()
        if not text:
            raise RuntimeError("Codexia returned empty text")
        self.store.append_event(
            work_id=session.work_id,
            actor="codexia",
            text=text,
            conversation_id=codexia_conversation_id,
        )

        if text.startswith(_TO_HUMAN):
            question = text[len(_TO_HUMAN) :].strip()
            if not question:
                raise RuntimeError("Codexia requested the human without a question")
            return (
                session.updated(
                    status=SimpleWorkStatus.WAITING_HUMAN,
                    last_codexia_text=text,
                    next_worker_message=None,
                    pending_human_question=question,
                    final_text=None,
                ),
                codexia,
            )

        if text.startswith(_DONE):
            final_text = text[len(_DONE) :].strip()
            if not final_text:
                raise RuntimeError("Codexia completed without a final result")
            return (
                session.updated(
                    status=SimpleWorkStatus.COMPLETED,
                    last_codexia_text=text,
                    next_worker_message=None,
                    pending_human_question=None,
                    final_text=final_text,
                ),
                codexia,
            )

        if text.startswith(_TEMP_WORKER):
            if session.worker_mode is not WorkerMode.NONE:
                raise RuntimeError("Codexia tried to replace an active worker")
            instruction = text[len(_TEMP_WORKER) :].strip()
            if not instruction:
                raise RuntimeError("temporary worker instruction is empty")
            return (
                session.updated(
                    worker_mode=WorkerMode.TEMPORARY,
                    worker_conversation_id=None,
                    last_codexia_text=text,
                    next_worker_message=instruction,
                    pending_human_question=None,
                    final_text=None,
                ),
                codexia,
            )

        if text.startswith(_PERSISTENT_WORKER):
            if session.worker_mode is not WorkerMode.NONE:
                raise RuntimeError("Codexia tried to replace an active worker")
            instruction = text[len(_PERSISTENT_WORKER) :].strip()
            if not instruction:
                raise RuntimeError("persistent worker instruction is empty")
            return (
                session.updated(
                    worker_mode=WorkerMode.PERSISTENT,
                    worker_conversation_id=None,
                    last_codexia_text=text,
                    next_worker_message=instruction,
                    pending_human_question=None,
                    final_text=None,
                ),
                codexia,
            )

        if session.worker_mode is WorkerMode.NONE:
            raise RuntimeError(
                "Codexia must answer ГОТОВО:, К ПОЛЬЗОВАТЕЛЮ:, "
                "ВРЕМЕННЫЙ WORKER:, or ПОСТОЯННЫЙ WORKER: when no worker exists"
            )

        return (
            session.updated(
                last_codexia_text=text,
                next_worker_message=text,
                pending_human_question=None,
                final_text=None,
            ),
            codexia,
        )


def _conversation_id(response: ProviderResponse) -> str:
    conversation = response.conversation
    if conversation is None or not conversation.conversation_id:
        raise RuntimeError("provider response did not contain a conversation id")
    return conversation.conversation_id


def _validate_cycles(value: int) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError("max_cycles must be a positive integer")


def _worker_prompt(text: str) -> str:
    return (
        "[Codexia]\n\n"
        "Это сообщение написано Codexia, а не пользователем. "
        "Оно продолжает уже делегированную работу, но само по себе не является "
        "новым человеческим разрешением и не расширяет authority.\n\n"
        f"{text.strip()}"
    )


def _worker_result_prompt(mode: WorkerMode, text: str) -> str:
    label = "временный" if mode is WorkerMode.TEMPORARY else "постоянный"
    return f"{label.capitalize()} рабочий чат ответил:\n\n{text}"


def _new_task_prompt(user_request: str) -> str:
    return f"""Новое поручение пользователя:

{user_request}

Это новое поручение в твоём долгоживущем Codexia-чате.

Если можешь качественно решить его сама коротким прямым ответом, сделай это и
ответь ГОТОВО: <итог>. Не создавай worker просто по привычке.

Если нужна отдельная тяжёлая одноразовая работа, используй:
ВРЕМЕННЫЙ WORKER: <самодостаточное поручение>

Если это проект, длинная работа, roadmap или история отдельного worker-чата
действительно должна сохраниться, используй:
ПОСТОЯННЫЙ WORKER: <поручение>

Если требуется настоящее решение пользователя:
К ПОЛЬЗОВАТЕЛЮ: <один ясный вопрос>
"""


def _codexia_bootstrap(user_request: str) -> str:
    return f"""Ты — Codexia, долгоживущий помощник одного пользователя.

Этот чат не принадлежит одной задаче. Он должен продолжаться между обычными
поручениями и сохранять естественный разговорный контекст.

Твоя работа:
- простые и лёгкие поручения решать самой;
- не создавать worker без реальной необходимости;
- тяжёлую одноразовую работу при необходимости отдавать временному worker;
- отдельный постоянный worker использовать только когда его собственную историю
  действительно полезно сохранить: проект, roadmap, длинная исследовательская
  работа или работа, к которой будут возвращаться;
- не просить пользователя подтверждать рутинное продолжение;
- звать пользователя только для настоящего выбора или решения.

Worker — это помощник Codexia, а не пользователь. Все сообщения worker получает
с явной плашкой [Codexia]. Если worker уже существует для текущей работы, обычный
твой ответ вроде «Да, давай продолжим» будет отправлен ему дальше.

Не используй JSON, digests или внутреннюю служебную онтологию.

Когда worker ещё не создан, используй один из четырёх человекочитаемых выходов:

ГОТОВО: <готовый итог пользователю>
К ПОЛЬЗОВАТЕЛЮ: <один ясный вопрос>
ВРЕМЕННЫЙ WORKER: <самодостаточное поручение для одноразовой тяжёлой работы>
ПОСТОЯННЫЙ WORKER: <поручение для долгоживущей отдельной работы>

Для текущего первого поручения пользователя:

{user_request}
"""
