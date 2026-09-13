from __future__ import annotations

from pathlib import Path
from typing import Iterable

from codexia_manual_agent.providers.chatgpt_web import ChatGPTWebProvider
from codexia_manual_agent.work.chat_peer import ChatGPTPeerLoop
from codexia_manual_agent.work.contracts import (
    WorkActorKind,
    WorkHandoff,
    WorkIntentInterpretation,
    WorkStatement,
)
from codexia_manual_agent.work.pilot_checkpoint import PilotCheckpointSource
from codexia_manual_agent.work.supervisor import (
    BackgroundWorkSupervisor,
    SupervisorStateError,
    SupervisorWorkSnapshot,
)
from codexia_manual_agent.work.supervisor_driver import (
    BackgroundWorkDriver,
    SupervisorDriveResult,
)


def _human_statements(actor: str, texts: Iterable[str]) -> tuple[WorkStatement, ...]:
    return tuple(
        WorkStatement.create(
            author_kind=WorkActorKind.HUMAN,
            actor=actor,
            text=text,
        )
        for text in texts
    )


def start_daily_use_pilot(
    *,
    database_path: str | Path,
    provider: ChatGPTWebProvider,
    conversation_id: str,
    objective: str,
    context: Iterable[str] = (),
    human_constraints: Iterable[str] = (),
    attention_constraints: Iterable[str] = (),
    completion_expectation: str | None = None,
    continuation_scope: str | None = None,
    depth_interpretation: str | None = None,
    human_actor: str = "human",
    codexia_actor: str = "codexia-pilot",
    worker_actor: str = "chatgpt",
) -> SupervisorWorkSnapshot:
    """Register one real ChatGPT conversation as an M6.6 delegated-work pilot."""

    objective_statement = WorkStatement.create(
        author_kind=WorkActorKind.HUMAN,
        actor=human_actor,
        text=objective,
    )
    context_statements = _human_statements(human_actor, context)
    constraint_statements = _human_statements(human_actor, human_constraints)
    attention_statements = _human_statements(human_actor, attention_constraints)
    handoff = WorkHandoff.create(
        objective=objective_statement,
        context=context_statements,
        human_constraints=constraint_statements,
        attention_constraints=attention_statements,
    )
    interpretation_basis = (
        objective_statement,
        *context_statements,
        *constraint_statements,
        *attention_statements,
    )
    interpretation = WorkIntentInterpretation.create(
        handoff=handoff,
        interpreter_kind=WorkActorKind.CODEXIA,
        interpreter=codexia_actor,
        basis_statements=interpretation_basis,
        completion_expectation=(
            completion_expectation
            or "Return a finished result that satisfies the exact delegated objective."
        ),
        continuation_scope=(
            continuation_scope
            or "Stay within the exact delegated objective and HUMAN-authored constraints."
        ),
        depth_interpretation=(
            depth_interpretation
            or "Use enough depth and evidence to produce a dependable finished result."
        ),
    )
    peer_loop = ChatGPTPeerLoop(
        provider,
        codexia_actor=codexia_actor,
        human_actor=human_actor,
        worker_actor=worker_actor,
    )
    cursor = peer_loop.attach(conversation_id)
    supervisor = BackgroundWorkSupervisor(database_path)
    return supervisor.register(
        handoff=handoff,
        interpretation=interpretation,
        cursor=cursor,
    )


def drive_daily_use_pilot(
    *,
    database_path: str | Path,
    provider: ChatGPTWebProvider,
    work_id: str,
    max_steps: int = 32,
    human_actor: str = "human",
    codexia_actor: str = "codexia-pilot",
    worker_actor: str = "chatgpt",
) -> SupervisorDriveResult:
    """Drive one registered pilot until completion or a governed stop boundary."""

    supervisor = BackgroundWorkSupervisor(database_path)
    peer_loop = ChatGPTPeerLoop(
        provider,
        codexia_actor=codexia_actor,
        human_actor=human_actor,
        worker_actor=worker_actor,
    )
    source = PilotCheckpointSource(
        supervisor=supervisor,
        provider=provider,
        actor=codexia_actor,
    )
    return BackgroundWorkDriver(supervisor).drive_chat_until_blocked(
        work_id,
        peer_loop=peer_loop,
        checkpoint_source=source,
        max_steps=max_steps,
    )


def answer_daily_use_pilot(
    *,
    database_path: str | Path,
    work_id: str,
    answer: str,
    human_actor: str = "human",
) -> SupervisorWorkSnapshot:
    """Record an explicit HUMAN answer for exact WAITING_HUMAN pilot work."""

    supervisor = BackgroundWorkSupervisor(database_path)
    recorder = getattr(supervisor, "record_pilot_human_answer", None)
    if not callable(recorder):
        raise SupervisorStateError("Pilot human-answer extension is not installed")
    statement = WorkStatement.create(
        author_kind=WorkActorKind.HUMAN,
        actor=human_actor,
        text=answer,
    )
    return recorder(work_id, answer=statement)


def daily_use_pilot_status(
    *,
    database_path: str | Path,
    work_id: str,
) -> SupervisorWorkSnapshot:
    return BackgroundWorkSupervisor(database_path).recover(work_id)


def pilot_snapshot_summary(snapshot: SupervisorWorkSnapshot) -> dict[str, object]:
    return {
        "work_id": snapshot.work_id,
        "status": snapshot.status.value,
        "conversation_id": snapshot.cursor.conversation_id,
        "cursor_digest": snapshot.cursor.cursor_digest,
        "last_sequence": snapshot.last_sequence,
        "last_event_digest": snapshot.last_event_digest,
        "last_proposal": (
            None if snapshot.last_proposal is None else snapshot.last_proposal.to_dict()
        ),
        "last_admission": (
            None if snapshot.last_admission is None else snapshot.last_admission.to_dict()
        ),
        "last_attention": (
            None if snapshot.last_attention is None else snapshot.last_attention.to_dict()
        ),
        "pending_dispatch": (
            None if snapshot.pending_dispatch is None else snapshot.pending_dispatch.to_dict()
        ),
        "completion": (
            None if snapshot.completion is None else snapshot.completion.to_dict()
        ),
    }


def pilot_drive_summary(result: SupervisorDriveResult) -> dict[str, object]:
    return {
        "work_id": result.work_id,
        "stop": result.stop.value,
        "steps": result.steps,
        "provider_turns": result.provider_turns,
        "snapshot": pilot_snapshot_summary(result.snapshot),
    }
