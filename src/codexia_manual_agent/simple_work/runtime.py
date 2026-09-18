from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from codexia_manual_agent.domain.errors import ProviderError
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
_CHATGPT_TURN_TIMEOUT = "CHATGPT_TURN_TIMEOUT"


class _VisibleMessage(Protocol):
    message_id: str
    role: str
    text: str
    finish_reason: str | None


class _VisibleStatus(Protocol):
    status: str
    message_id: str | None
    finish_reason: str | None


class _Provider(Protocol):
    def send(self, request: ProviderRequest) -> ProviderResponse: ...

    def send_temporary(self, prompt: str) -> ProviderResponse: ...

    def end_temporary_chat(self) -> bool: ...

    def read_status(self, conversation_id: str) -> _VisibleStatus: ...

    def read_messages(
        self, conversation_id: str
    ) -> tuple[_VisibleMessage, ...]: ...


@dataclass(frozen=True, slots=True)
class SimpleWorkRunResult:
    session: SimpleWorkSession
    codexia: SimpleCodexiaSession
    stop: str


class SimpleWorkRuntime:
    """Codexia-first work loop with lazy optional workers.

    One long-lived Codexia conversation handles many user tasks. Simple tasks can
    finish directly. A worker exists only when Codexia explicitly asks for one.

    Persistent worker writes obey one strict recovery rule: an ambiguous turn
    timeout is never retry authority. If the worker conversation is already known,
    Simple Work may recover only by canonical readback of the exact already-sent
    Codexia message and its following assistant response.
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

        # Backward-safe guard for a timeout produced by an older Simple Work build:
        # a durable codexia_to_worker tail means the write may already have happened.
        if (
            session.status is SimpleWorkStatus.RECONCILE_REQUIRED
            or self._pending_persistent_dispatch(session) is not None
        ):
            if session.status is not SimpleWorkStatus.RECONCILE_REQUIRED:
                session = session.updated(status=SimpleWorkStatus.RECONCILE_REQUIRED)
                self.store.save(session)
            return SimpleWorkRunResult(
                session=session,
                codexia=codexia,
                stop="reconcile_required",
            )

        if session.next_worker_message is None:
            raise RuntimeError("ready simple work has no pending worker instruction")
        if session.worker_mode is WorkerMode.NONE:
            raise RuntimeError("ready simple work has no active worker route")
        return self._drive(session, codexia, max_cycles=max_cycles)

    def reconcile(
        self,
        work_id: str,
        *,
        max_cycles: int = 8,
    ) -> SimpleWorkRunResult:
        """Read-only recover one ambiguous persistent worker turn.

        This method never submits the pending worker message again.
        """

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

        dispatch = self._pending_persistent_dispatch(session)
        if dispatch is None:
            raise RuntimeError("simple work has no ambiguous persistent worker dispatch")

        if session.status is not SimpleWorkStatus.RECONCILE_REQUIRED:
            session = session.updated(status=SimpleWorkStatus.RECONCILE_REQUIRED)
            self.store.save(session)

        worker_text = self._canonical_reconciled_worker_text(session)
        if worker_text is None:
            repaired = self._repair_provisional_reconcile(session, codexia)
            if repaired is None:
                return SimpleWorkRunResult(
                    session=session,
                    codexia=codexia,
                    stop="reconcile_required",
                )
            session, codexia = repaired
            if session.status is not SimpleWorkStatus.READY:
                return SimpleWorkRunResult(
                    session=session,
                    codexia=codexia,
                    stop=session.status.value,
                )
            return self._drive(session, codexia, max_cycles=max_cycles)

        session, codexia = self._record_worker_and_continue(
            session,
            codexia,
            mode=WorkerMode.PERSISTENT,
            worker_text=worker_text,
            worker_conversation_id=session.worker_conversation_id,
        )
        if session.status is not SimpleWorkStatus.READY:
            return SimpleWorkRunResult(
                session=session,
                codexia=codexia,
                stop=session.status.value,
            )
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

            if mode is WorkerMode.TEMPORARY:
                worker_response = self.provider.send_temporary(worker_prompt)
                self.store.append_event(
                    work_id=session.work_id,
                    actor="codexia_to_worker",
                    text=worker_prompt,
                    conversation_id=None,
                    worker_mode=mode,
                )
                worker_text = worker_response.text
                worker_conversation_id = None
            else:
                try:
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
                except ProviderError as exc:
                    if _is_conversation_not_completed(exc):
                        # CWA rejected the write at preflight: nothing was submitted.
                        # Keep the next worker message intact so a later resume is safe.
                        return SimpleWorkRunResult(
                            session=session,
                            codexia=codexia,
                            stop="worker_busy",
                        )
                    if not _is_chatgpt_turn_timeout(exc):
                        raise

                    # A turn timeout can happen after submit. Persist the exact
                    # attempted dispatch and never grant retry authority.
                    self.store.append_event(
                        work_id=session.work_id,
                        actor="codexia_to_worker",
                        text=worker_prompt,
                        conversation_id=session.worker_conversation_id,
                        worker_mode=mode,
                    )
                    session = session.updated(
                        status=SimpleWorkStatus.RECONCILE_REQUIRED
                    )
                    self.store.save(session)

                    # First-turn timeouts cannot be recovered because no durable
                    # worker conversation identity is known yet.
                    worker_text = self._canonical_reconciled_worker_text(session)
                    if worker_text is None:
                        return SimpleWorkRunResult(
                            session=session,
                            codexia=codexia,
                            stop="reconcile_required",
                        )
                    worker_conversation_id = session.worker_conversation_id
                else:
                    worker_conversation_id = _conversation_id(worker_response)
                    if session.worker_conversation_id is not None:
                        if worker_conversation_id != session.worker_conversation_id:
                            raise RuntimeError(
                                "persistent worker conversation identity changed"
                            )
                    self.store.append_event(
                        work_id=session.work_id,
                        actor="codexia_to_worker",
                        text=worker_prompt,
                        conversation_id=worker_conversation_id,
                        worker_mode=mode,
                    )
                    worker_text = worker_response.text

            session, codexia = self._record_worker_and_continue(
                session,
                codexia,
                mode=mode,
                worker_text=worker_text,
                worker_conversation_id=worker_conversation_id,
            )
            remaining -= 1

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

    def _record_worker_and_continue(
        self,
        session: SimpleWorkSession,
        codexia: SimpleCodexiaSession,
        *,
        mode: WorkerMode,
        worker_text: str,
        worker_conversation_id: str | None,
    ) -> tuple[SimpleWorkSession, SimpleCodexiaSession]:
        self.store.append_event(
            work_id=session.work_id,
            actor="worker",
            text=worker_text,
            conversation_id=worker_conversation_id,
            worker_mode=mode,
        )
        session = session.updated(
            status=SimpleWorkStatus.READY,
            worker_conversation_id=worker_conversation_id,
            last_worker_text=worker_text,
            next_worker_message=None,
            worker_turns=session.worker_turns + 1,
        )
        self.store.save(session)

        if codexia.conversation_id is None:
            raise RuntimeError("Codexia session lost its conversation")
        codexia_response = self.provider.send(
            ProviderRequest(
                prompt=_worker_result_prompt(mode, worker_text),
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
        return session, codexia

    def _pending_persistent_dispatch(self, session: SimpleWorkSession):
        history = self.store.history(session.work_id)
        if not history:
            return None
        tail = history[-1]
        if (
            tail.actor != "codexia_to_worker"
            or tail.worker_mode is not WorkerMode.PERSISTENT
        ):
            return None
        return tail

    def _canonical_reconciled_worker_text(
        self,
        session: SimpleWorkSession,
    ) -> str | None:
        dispatch = self._pending_persistent_dispatch(session)
        if dispatch is None:
            return None

        conversation_id = dispatch.conversation_id or session.worker_conversation_id
        if not conversation_id:
            return None
        if (
            session.worker_conversation_id is not None
            and conversation_id != session.worker_conversation_id
        ):
            return None

        local_history = self.store.history(session.work_id)
        previous_worker = None
        for event in reversed(local_history[:-1]):
            if (
                event.actor in {"worker", "worker_reconciled"}
                and event.worker_mode is WorkerMode.PERSISTENT
                and event.conversation_id == conversation_id
            ):
                previous_worker = event
                break
        if previous_worker is None:
            return None

        try:
            status = self.provider.read_status(conversation_id)
            messages = self.provider.read_messages(conversation_id)
        except ProviderError:
            return None

        # Visible text can exist while ChatGPT is still generating. Reconciliation
        # may consume only canonical finality, never an in-progress assistant body.
        if status.status != "completed":
            return None

        anchor_indices = [
            index
            for index, message in enumerate(messages)
            if message.role == "assistant" and message.text == previous_worker.text
        ]
        if anchor_indices:
            anchor = anchor_indices[-1]
            matching_dispatches = [
                index
                for index, message in enumerate(messages)
                if (
                    index > anchor
                    and message.role == "user"
                    and message.text == dispatch.text
                )
            ]
        else:
            # A rich/writing-block assistant response can be normalized differently
            # between the live response and later canonical history. In that case
            # exact dispatch text is still sufficient only when it is globally
            # unique on the current branch.
            matching_dispatches = [
                index
                for index, message in enumerate(messages)
                if message.role == "user" and message.text == dispatch.text
            ]
        if len(matching_dispatches) != 1:
            return None
        dispatch_index = matching_dispatches[0]

        suffix = messages[dispatch_index + 1 :]
        if any(message.role == "user" for message in suffix):
            return None
        assistants = [
            message
            for message in suffix
            if message.role == "assistant" and message.text.strip()
        ]
        if not assistants:
            return None

        if status.message_id is not None:
            finalized = [
                message
                for message in assistants
                if message.message_id == status.message_id
            ]
            if len(finalized) != 1:
                return None
            return finalized[0].text

        # Compatibility fallback: canonical completion can occasionally omit the
        # status message id, but an explicit assistant finish reason still proves
        # that the visible assistant body is final.
        finalized = [
            message
            for message in assistants
            if message.finish_reason is not None
        ]
        if len(finalized) != 1:
            return None
        return finalized[0].text

    def _repair_provisional_reconcile(
        self,
        session: SimpleWorkSession,
        codexia: SimpleCodexiaSession,
    ) -> tuple[SimpleWorkSession, SimpleCodexiaSession] | None:
        """Repair the short-lived v0.1 bug that ingested an in-progress worker body.

        The repair is intentionally narrow. It requires:
        - the local tail to be a persistent Codexia dispatch;
        - that tail dispatch to be absent from the canonical current branch;
        - the immediately preceding local worker result to have originated from an
          earlier exact persistent dispatch;
        - the worker conversation now to be canonically completed;
        - canonical final text for that earlier dispatch to differ from the local
          worker text that was consumed provisionally.
        """

        tail = self._pending_persistent_dispatch(session)
        if tail is None:
            return None
        conversation_id = tail.conversation_id or session.worker_conversation_id
        if not conversation_id:
            return None

        try:
            status = self.provider.read_status(conversation_id)
            messages = self.provider.read_messages(conversation_id)
        except ProviderError:
            return None
        if status.status != "completed":
            return None

        # If the tail exists canonically, it may have been submitted. That is not
        # the historical provisional-read bug and remains ordinary reconciliation.
        if any(
            message.role == "user" and message.text == tail.text
            for message in messages
        ):
            return None

        history = self.store.history(session.work_id)
        if len(history) < 3 or history[-1].event_id != tail.event_id:
            return None

        partial_worker_index = None
        for index in range(len(history) - 2, -1, -1):
            event = history[index]
            if (
                event.actor == "worker"
                and event.worker_mode is WorkerMode.PERSISTENT
                and event.conversation_id == conversation_id
            ):
                partial_worker_index = index
                break
        if partial_worker_index is None:
            return None
        partial_worker = history[partial_worker_index]

        previous_dispatch = None
        for index in range(partial_worker_index - 1, -1, -1):
            event = history[index]
            if (
                event.actor == "codexia_to_worker"
                and event.worker_mode is WorkerMode.PERSISTENT
                and event.conversation_id == conversation_id
            ):
                previous_dispatch = event
                break
        if previous_dispatch is None:
            return None

        matching_dispatches = [
            index
            for index, message in enumerate(messages)
            if message.role == "user" and message.text == previous_dispatch.text
        ]
        if len(matching_dispatches) != 1:
            return None
        dispatch_index = matching_dispatches[0]

        final = None
        for message in messages[dispatch_index + 1 :]:
            if message.role == "user":
                break
            if message.role != "assistant" or not message.text.strip():
                continue
            if status.message_id is not None:
                if message.message_id == status.message_id:
                    final = message
            elif message.finish_reason is not None:
                final = message
        if final is None or final.text == partial_worker.text:
            return None

        self.store.append_event(
            work_id=session.work_id,
            actor="worker_reconciled",
            text=final.text,
            conversation_id=conversation_id,
            worker_mode=WorkerMode.PERSISTENT,
        )
        session = session.updated(
            status=SimpleWorkStatus.READY,
            last_worker_text=final.text,
            next_worker_message=None,
        )
        self.store.save(session)

        if codexia.conversation_id is None:
            raise RuntimeError("Codexia session lost its conversation")
        correction = (
            "Исправление transport reconciliation. Предыдущий переданный тебе "
            "ответ рабочего чата оказался промежуточным: worker ещё продолжал "
            "генерацию. Следующее поручение, которое ты сформировала на его основе, "
            "не присутствует в canonical worker branch и не считается выполненным. "
            "Не опирайся на прежний промежуточный текст или выводы из него.\n\n"
            "Канонически завершённый ответ рабочего чата:\n\n"
            f"{final.text}\n\n"
            "Продолжи текущую работу уже из этого финального результата. Если "
            "работа завершена — ответь ГОТОВО:. Если нужен следующий шаг worker — "
            "дай обычное сообщение ему."
        )
        self.store.append_event(
            work_id=session.work_id,
            actor="runtime_correction",
            text=correction,
            conversation_id=codexia.conversation_id,
        )
        response = self.provider.send(
            ProviderRequest(
                prompt=correction,
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
            codexia = codexia.updated()
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
                status=SimpleWorkStatus.READY,
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


def _is_chatgpt_turn_timeout(exc: ProviderError) -> bool:
    return _CHATGPT_TURN_TIMEOUT in str(exc)


def _is_conversation_not_completed(exc: ProviderError) -> bool:
    return "CANONICAL_CONVERSATION_NOT_COMPLETED" in str(exc)


def _worker_prompt(text: str) -> str:
    return (
        "[Codexia]\n"
        "Не пользователь; не расширяет его разрешения.\n\n"
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
