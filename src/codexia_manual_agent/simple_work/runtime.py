from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import re
from typing import Protocol
from urllib.parse import unquote

from codexia_manual_agent.domain.errors import ProviderError
from codexia_manual_agent.domain.models import (
    ProviderConversation,
    ProviderRequest,
    ProviderResponse,
)
from codexia_manual_agent.simple_work.session import (
    CodexiaMode,
    SimpleCodexiaSession,
    SimpleWorkArtifact,
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
_SANDBOX_ARTIFACT_RE = re.compile(r"sandbox:/mnt/data/([^\s)]+)")


class _VisibleMessage(Protocol):
    message_id: str
    role: str
    text: str
    finish_reason: str | None


class _VisibleStatus(Protocol):
    status: str
    message_id: str | None
    finish_reason: str | None


class _ArtifactHandoff(Protocol):
    conversation_id: str
    source_filename: str
    destination: Path
    size_bytes: int
    sha256: str
    overwritten: bool
    integrity_verified: bool


class _Provider(Protocol):
    def send(self, request: ProviderRequest) -> ProviderResponse: ...

    def send_temporary(self, prompt: str) -> ProviderResponse: ...

    def end_temporary_chat(self) -> bool: ...

    def read_status(self, conversation_id: str) -> _VisibleStatus: ...

    def read_messages(
        self, conversation_id: str
    ) -> tuple[_VisibleMessage, ...]: ...

    def handoff_generated_artifact(
        self,
        conversation_id: str,
        *,
        filename: str,
        destination: str | Path,
        overwrite: bool = False,
    ) -> _ArtifactHandoff: ...


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

    def start(
        self,
        user_request: str,
        *,
        codexia_alias: str = "general",
        temporary_codexia: bool = False,
        max_cycles: int = 8,
    ) -> SimpleWorkRunResult:
        _validate_cycles(max_cycles)
        if temporary_codexia and codexia_alias != "general":
            raise ValueError(
                "temporary Codexia cannot be combined with a saved Codexia alias"
            )

        if temporary_codexia:
            codexia = SimpleCodexiaSession.create(alias="temporary")
            codexia_mode = CodexiaMode.TEMPORARY
        else:
            codexia = self.store.codexia(codexia_alias)
            codexia_mode = CodexiaMode.SAVED

        session = self.store.save(
            SimpleWorkSession.create(
                user_request,
                codexia_alias=codexia.alias,
                codexia_mode=codexia_mode,
            )
        )
        self.store.append_event(
            work_id=session.work_id,
            actor="human",
            text=session.user_request,
        )

        temporary_live = False
        try:
            if session.codexia_mode is CodexiaMode.TEMPORARY:
                response = self.provider.send_temporary(
                    _temporary_codexia_bootstrap(session.user_request)
                )
                temporary_live = True
            elif codexia.conversation_id is None:
                response = self.provider.send(
                    ProviderRequest(prompt=_codexia_bootstrap(session.user_request))
                )
            else:
                response = self.provider.send(
                    ProviderRequest(
                        prompt=_new_task_prompt(session.user_request),
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
            self._save_codexia_if_saved(session, codexia)

            if session.status is not SimpleWorkStatus.READY:
                result = SimpleWorkRunResult(
                    session=session,
                    codexia=codexia,
                    stop=session.status.value,
                )
            else:
                result = self._drive(session, codexia, max_cycles=max_cycles)

            if session.codexia_mode is CodexiaMode.TEMPORARY:
                result = self._close_temporary_codexia_result(result)
                temporary_live = False
            return result
        except Exception:
            if session.codexia_mode is CodexiaMode.TEMPORARY:
                if temporary_live:
                    try:
                        self.provider.end_temporary_chat()
                    except Exception:
                        pass
                session = session.updated(status=SimpleWorkStatus.TEMPORARY_CLOSED)
                self.store.save(session)
                self.store.append_event(
                    work_id=session.work_id,
                    actor="codexia_temporary_closed",
                    text="stop=exception",
                    conversation_id=codexia.conversation_id,
                )
            raise

    def resume(self, work_id: str, *, max_cycles: int = 8) -> SimpleWorkRunResult:
        _validate_cycles(max_cycles)
        session = self.store.load(work_id)
        if session.codexia_mode is CodexiaMode.TEMPORARY:
            raise RuntimeError(
                "temporary Codexia work cannot cross a process boundary; "
                "start a new temporary or saved Codexia work instead"
            )
        codexia = self.store.codexia(session.codexia_alias)
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
        if session.codexia_mode is CodexiaMode.TEMPORARY:
            raise RuntimeError(
                "temporary Codexia work cannot cross a process boundary; "
                "start a new temporary or saved Codexia work instead"
            )
        codexia = self.store.codexia(session.codexia_alias)

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
        if session.codexia_mode is CodexiaMode.TEMPORARY:
            raise RuntimeError(
                "temporary Codexia work cannot cross a process boundary; "
                "start a new temporary or saved Codexia work instead"
            )
        codexia = self.store.codexia(session.codexia_alias)
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

    def intake_artifacts(self, work_id: str) -> tuple[SimpleWorkArtifact, ...]:
        """Materialize explicit sandbox artifacts from the latest persistent worker result."""

        session = self.store.load(work_id)
        if session.worker_mode is not WorkerMode.PERSISTENT:
            raise RuntimeError("artifact intake requires a persistent worker")
        if session.worker_conversation_id is None:
            raise RuntimeError("persistent worker has no conversation identity")
        if not session.last_worker_text:
            raise RuntimeError("persistent worker has no local result to inspect")
        turn = session.worker_turns
        if turn <= 0:
            raise RuntimeError("persistent worker has no completed turn")
        artifacts, _failures = self._materialize_worker_artifacts(
            session,
            worker_text=session.last_worker_text,
            worker_conversation_id=session.worker_conversation_id,
            worker_turn=turn,
        )
        return artifacts

    def _send_codexia_turn(
        self,
        session: SimpleWorkSession,
        codexia: SimpleCodexiaSession,
        prompt: str,
    ) -> ProviderResponse:
        if session.codexia_mode is CodexiaMode.TEMPORARY:
            return self.provider.send_temporary(prompt)
        if codexia.conversation_id is None:
            raise RuntimeError("Codexia session lost its conversation")
        return self.provider.send(
            ProviderRequest(
                prompt=prompt,
                conversation=ProviderConversation(
                    conversation_id=codexia.conversation_id
                ),
            )
        )

    def _save_codexia_if_saved(
        self,
        session: SimpleWorkSession,
        codexia: SimpleCodexiaSession,
    ) -> None:
        if session.codexia_mode is CodexiaMode.SAVED:
            self.store.save_codexia(codexia)

    def _close_temporary_codexia_result(
        self,
        result: SimpleWorkRunResult,
    ) -> SimpleWorkRunResult:
        if result.session.codexia_mode is not CodexiaMode.TEMPORARY:
            return result

        session = result.session
        cleanup_error: str | None = None
        try:
            closed = self.provider.end_temporary_chat()
        except Exception as exc:
            closed = False
            cleanup_error = str(exc) or exc.__class__.__name__

        if closed is not True:
            cleanup_error = cleanup_error or "temporary lifecycle close returned false"
            self.store.append_event(
                work_id=session.work_id,
                actor="codexia_temporary_close_unproven",
                text=cleanup_error,
                conversation_id=result.codexia.conversation_id,
            )
            if session.status is not SimpleWorkStatus.COMPLETED:
                session = session.updated(status=SimpleWorkStatus.TEMPORARY_CLOSED)
                self.store.save(session)
                return SimpleWorkRunResult(
                    session=session,
                    codexia=result.codexia,
                    stop="temporary_close_unproven",
                )
            return SimpleWorkRunResult(
                session=session,
                codexia=result.codexia,
                stop="completed_cleanup_unproven",
            )

        self.store.append_event(
            work_id=session.work_id,
            actor="codexia_temporary_closed",
            text=f"stop={result.stop}",
            conversation_id=result.codexia.conversation_id,
        )
        if session.status is not SimpleWorkStatus.COMPLETED:
            session = session.updated(status=SimpleWorkStatus.TEMPORARY_CLOSED)
            self.store.save(session)
            return SimpleWorkRunResult(
                session=session,
                codexia=result.codexia,
                stop=SimpleWorkStatus.TEMPORARY_CLOSED.value,
            )
        return result

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
        next_worker_turn = session.worker_turns + 1
        self.store.append_event(
            work_id=session.work_id,
            actor="worker",
            text=worker_text,
            conversation_id=worker_conversation_id,
            worker_mode=mode,
        )

        artifacts: tuple[SimpleWorkArtifact, ...] = ()
        artifact_failures: tuple[str, ...] = ()
        if (
            mode is WorkerMode.PERSISTENT
            and worker_conversation_id is not None
        ):
            artifacts, artifact_failures = self._materialize_worker_artifacts(
                session,
                worker_text=worker_text,
                worker_conversation_id=worker_conversation_id,
                worker_turn=next_worker_turn,
            )

        session = session.updated(
            status=SimpleWorkStatus.READY,
            worker_conversation_id=worker_conversation_id,
            last_worker_text=worker_text,
            next_worker_message=None,
            worker_turns=next_worker_turn,
        )
        self.store.save(session)

        codexia_response = self._send_codexia_turn(
            session,
            codexia,
            _worker_result_prompt(
                mode,
                worker_text,
                artifacts=artifacts,
                artifact_failures=artifact_failures,
            ),
        )
        session, codexia = self._accept_codexia_response(
            session,
            codexia,
            codexia_response,
        )
        self.store.save(session)
        self._save_codexia_if_saved(session, codexia)
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
        repaired_turn = max(1, session.worker_turns)
        artifacts, artifact_failures = self._materialize_worker_artifacts(
            session,
            worker_text=final.text,
            worker_conversation_id=conversation_id,
            worker_turn=repaired_turn,
        )
        session = session.updated(
            status=SimpleWorkStatus.READY,
            last_worker_text=final.text,
            next_worker_message=None,
        )
        self.store.save(session)

        correction = (
            "Исправление transport reconciliation. Предыдущий переданный тебе "
            "ответ рабочего чата оказался промежуточным: worker ещё продолжал "
            "генерацию. Следующее поручение, которое ты сформировала на его основе, "
            "не присутствует в canonical worker branch и не считается выполненным. "
            "Не опирайся на прежний промежуточный текст или выводы из него.\n\n"
            "Канонически завершённый ответ рабочего чата:\n\n"
            f"{final.text}\n\n"
            f"{_artifact_context(artifacts, artifact_failures)}"
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
        response = self._send_codexia_turn(
            session,
            codexia,
            correction,
        )
        session, codexia = self._accept_codexia_response(
            session,
            codexia,
            response,
        )
        self.store.save(session)
        self._save_codexia_if_saved(session, codexia)
        return session, codexia

    def _materialize_worker_artifacts(
        self,
        session: SimpleWorkSession,
        *,
        worker_text: str,
        worker_conversation_id: str,
        worker_turn: int,
    ) -> tuple[tuple[SimpleWorkArtifact, ...], tuple[str, ...]]:
        filenames = _sandbox_artifact_filenames(worker_text)
        if not filenames:
            return (), ()

        existing = {
            (artifact.worker_turn, artifact.source_filename): artifact
            for artifact in self.store.artifacts(session.work_id)
        }
        turn_dir = (
            self.store.path.parent
            / "artifacts"
            / session.work_id
            / f"worker-{worker_turn:04d}"
        )
        turn_dir.mkdir(parents=True, exist_ok=True)

        materialized: list[SimpleWorkArtifact] = []
        failures: list[str] = []
        for filename in filenames:
            prior = existing.get((worker_turn, filename))
            if prior is not None and Path(prior.local_path).is_file():
                materialized.append(prior)
                continue

            destination = (turn_dir / filename).absolute()
            try:
                handoff = self.provider.handoff_generated_artifact(
                    worker_conversation_id,
                    filename=filename,
                    destination=destination,
                    overwrite=False,
                )
            except (ProviderError, OSError, ValueError) as exc:
                message = f"{filename}: {exc}"
                failures.append(message)
                self.store.append_event(
                    work_id=session.work_id,
                    actor="artifact_intake_failed",
                    text=message,
                    conversation_id=worker_conversation_id,
                    worker_mode=WorkerMode.PERSISTENT,
                )
                continue

            if handoff.conversation_id != worker_conversation_id:
                raise RuntimeError("artifact handoff conversation identity changed")
            if handoff.source_filename != filename:
                raise RuntimeError("artifact handoff filename identity changed")
            if handoff.integrity_verified is not True:
                raise RuntimeError("artifact handoff did not verify integrity")

            artifact = self.store.save_artifact(
                work_id=session.work_id,
                worker_turn=worker_turn,
                source_filename=filename,
                local_path=str(handoff.destination),
                size_bytes=handoff.size_bytes,
                sha256=handoff.sha256,
                source_conversation_id=worker_conversation_id,
            )
            materialized.append(artifact)
            self.store.append_event(
                work_id=session.work_id,
                actor="artifact_materialized",
                text=(
                    f"filename={artifact.source_filename}\n"
                    f"local_path={artifact.local_path}\n"
                    f"size_bytes={artifact.size_bytes}\n"
                    f"sha256={artifact.sha256}"
                ),
                conversation_id=worker_conversation_id,
                worker_mode=WorkerMode.PERSISTENT,
            )

        return tuple(materialized), tuple(failures)

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
            if session.codexia_mode is CodexiaMode.TEMPORARY:
                raise RuntimeError(
                    "temporary Codexia cannot create a temporary worker because "
                    "CWA owns one live Temporary lifecycle; use a persistent worker"
                )
            instruction = text[len(_TEMP_WORKER) :].strip()
            if not instruction:
                raise RuntimeError("temporary worker instruction is empty")
            if session.worker_mode is WorkerMode.PERSISTENT:
                raise RuntimeError("Codexia tried to replace an active worker")
            if session.worker_mode is WorkerMode.TEMPORARY:
                return (
                    session.updated(
                        status=SimpleWorkStatus.READY,
                        last_codexia_text=text,
                        next_worker_message=instruction,
                        pending_human_question=None,
                        final_text=None,
                    ),
                    codexia,
                )
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
            instruction = text[len(_PERSISTENT_WORKER) :].strip()
            if not instruction:
                raise RuntimeError("persistent worker instruction is empty")
            if session.worker_mode is WorkerMode.TEMPORARY:
                raise RuntimeError("Codexia tried to replace an active worker")
            if session.worker_mode is WorkerMode.PERSISTENT:
                return (
                    session.updated(
                        status=SimpleWorkStatus.READY,
                        last_codexia_text=text,
                        next_worker_message=instruction,
                        pending_human_question=None,
                        final_text=None,
                    ),
                    codexia,
                )
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


def _worker_result_prompt(
    mode: WorkerMode,
    text: str,
    *,
    artifacts: tuple[SimpleWorkArtifact, ...] = (),
    artifact_failures: tuple[str, ...] = (),
) -> str:
    label = "временный" if mode is WorkerMode.TEMPORARY else "постоянный"
    prompt = f"{label.capitalize()} рабочий чат ответил:\n\n{text}"
    prompt += _artifact_context(artifacts, artifact_failures)
    prompt += (
        "\nЭтот worker уже активен для текущей работы. Если ему нужен следующий "
        "шаг, ответь обычным текстом следующего поручения — Simple Work отправит "
        "его в тот же worker-чат. Не создавай worker заново. Если работа полностью "
        "завершена, ответь ГОТОВО: <итог>. Если без решения пользователя продолжать "
        "нельзя, ответь К ПОЛЬЗОВАТЕЛЮ: <вопрос>."
    )
    return prompt


def _artifact_context(
    artifacts: tuple[SimpleWorkArtifact, ...],
    artifact_failures: tuple[str, ...],
) -> str:
    lines: list[str] = []
    if artifacts:
        lines.extend(
            [
                "",
                "Артефакты из этого ответа уже материализованы локально:",
            ]
        )
        for artifact in artifacts:
            lines.append(
                f"- {artifact.source_filename} -> {artifact.local_path} "
                f"(sha256={artifact.sha256})"
            )
    if artifact_failures:
        lines.extend(
            [
                "",
                "Некоторые явно указанные артефакты не удалось материализовать:",
            ]
        )
        lines.extend(f"- {failure}" for failure in artifact_failures)
    if not lines:
        return ""
    return "\n" + "\n".join(lines) + "\n\n"


def _sandbox_artifact_filenames(text: str) -> tuple[str, ...]:
    filenames: list[str] = []
    seen: set[str] = set()
    for match in _SANDBOX_ARTIFACT_RE.finditer(text):
        candidate = unquote(match.group(1)).strip()
        if not candidate or "\\" in candidate:
            continue
        path = PurePosixPath(candidate)
        if any(part in {"", ".", ".."} for part in path.parts):
            continue
        filename = path.name
        if not filename or filename in {".", ".."}:
            continue
        if filename not in seen:
            seen.add(filename)
            filenames.append(filename)
    return tuple(filenames)


def _new_task_prompt(user_request: str) -> str:
    return f"""Новое поручение пользователя:

{user_request}

Это новое поручение в твоём долгоживущем Codexia-чате.

Явные указания пользователя о способе выполнения — например использовать worker,
сохранить отдельный worker-чат или сделать несколько worker-циклов — обязательны.
Не заменяй явно запрошенный worker собственной прямой работой.

Если выбираешь worker route, служебный префикс должен стоять в начале твоего
видимого финального ответа текущего turn. Упоминание префикса только в рассуждении
не запускает worker. Не отвечай ГОТОВО:, пока явно запрошенные worker-циклы не
выполнены.

Если можешь качественно решить поручение сама и пользователь не задал иной способ
выполнения, сделай это и ответь ГОТОВО: <итог>. Не создавай worker просто по привычке.

Если нужна отдельная тяжёлая одноразовая работа, используй:
ВРЕМЕННЫЙ WORKER: <самодостаточное поручение>

Если это проект, длинная работа, roadmap или история отдельного worker-чата
действительно должна сохраниться, используй:
ПОСТОЯННЫЙ WORKER: <поручение>

Если требуется настоящее решение пользователя:
К ПОЛЬЗОВАТЕЛЮ: <один ясный вопрос>
"""


def _temporary_codexia_bootstrap(user_request: str) -> str:
    return f"""Ты — Codexia в одноразовом Temporary Chat.

Этот чат существует только в текущем процессе Simple Work и не станет selectable
saved Codexia chat. Локальный transcript работы сохранится, но после завершения
этого запуска продолжить именно этот Temporary Chat будет нельзя.

Решай простые и достаточно сложные одноразовые задачи сама. Но явные указания
пользователя о способе выполнения — например использовать persistent worker или
сделать несколько worker-циклов — обязательны. Не заменяй явно запрошенный worker
собственной прямой работой.

Если выбираешь worker route, служебный префикс должен стоять в начале твоего
видимого финального ответа текущего turn. Упоминание префикса только в рассуждении
не запускает worker. Не отвечай ГОТОВО:, пока явно запрошенные worker-циклы не
выполнены.

Не создавай worker просто по привычке. Если действительно нужна отдельная длинная
ветка работы или пользователь явно потребовал persistent worker, можно использовать
только:

ПОСТОЯННЫЙ WORKER: <поручение>

ВРЕМЕННЫЙ WORKER в этом режиме запрещён: CWA поддерживает один live Temporary
lifecycle, и он уже принадлежит этому Codexia-чату.

Если задача завершена:
ГОТОВО: <готовый итог>

Если без решения пользователя продолжать нельзя:
К ПОЛЬЗОВАТЕЛЮ: <один ясный вопрос>

Помни: human boundary завершит этот Temporary Chat; следующего turn в нём уже не
будет. Поэтому задавай вопрос только когда без него действительно нельзя.

Worker — помощник Codexia, а не пользователь. Не используй JSON, digests или
внутреннюю служебную онтологию.

Текущее поручение пользователя:

{user_request}
"""


def _codexia_bootstrap(user_request: str) -> str:
    return f"""Ты — Codexia, долгоживущий помощник одного пользователя.

Этот чат не принадлежит одной задаче. Он должен продолжаться между обычными
поручениями и сохранять естественный разговорный контекст.

Твоя работа:
- простые и лёгкие поручения решать самой;
- явные указания пользователя о способе выполнения считать обязательными: если
  пользователь просит использовать worker, сохранить worker-чат или выполнить
  несколько worker-циклов, не подменяй это собственной прямой работой;
- если выбираешь worker route, ставить соответствующий служебный префикс в начало
  видимого финального ответа текущего turn; упоминание префикса только в рассуждении
  не запускает worker;
- не отвечать ГОТОВО:, пока явно запрошенные worker-циклы не выполнены;
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
