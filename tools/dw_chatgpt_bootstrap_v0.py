"""Pilot-only Gen2 Work -> one CWA ChatGPT bootstrap.

A CAS-claimed WorkEvent is the durable no-replay fence. External CWA output is
transport evidence, not admitted RoleRun, execution authority or WorkCompletion.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from codexia_manual_agent.work_core import WorkSnapshot, WorkStore

MAX_FILES = 12
MAX_TOTAL_BYTES = 6_000_000
CLAIM_KIND = "codexia.chat.bootstrap.claimed.v0"
CAPTURE_KIND = "codexia.chat.bootstrap.captured.v0"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_evidence(paths: list[str]) -> tuple[list[dict[str, Any]], list[str]]:
    if not paths or len(paths) > MAX_FILES:
        raise ValueError(f"supply 1..{MAX_FILES} explicit context files")
    entries: list[dict[str, Any]] = []
    media: list[str] = []
    seen: set[str] = set()
    remaining = MAX_TOTAL_BYTES
    for raw in paths:
        source = Path(raw).expanduser()
        if source.is_symlink() or not source.is_file():
            raise ValueError("context must be a regular, non-symlink file")
        path = source.resolve(strict=True)
        if str(path) in seen:
            raise ValueError("duplicate context file")
        seen.add(str(path))
        size = path.stat().st_size
        if size <= 0 or size > remaining:
            raise ValueError("context file is empty or total byte budget exceeded")
        data = path.read_bytes()
        if len(data) != size:
            raise ValueError("context changed during read")
        remaining -= size
        entries.append(
            {"name": path.name, "path": str(path), "bytes": size, "sha256": _sha(data)}
        )
        media.append(str(path))
    return entries, media


def _validate_work(status: dict[str, Any]) -> dict[str, Any]:
    work = status["work"]
    if work["state"] != "active" or not re.fullmatch(
        r"[a-f0-9-]{36}", work["work_id"]
    ):
        raise ValueError("bootstrap requires an active Gen2 Work with UUID id")
    if not status["workflow"] or any(row.get("pack") is None for row in status["workflow"]):
        raise ValueError("Gen2 Work must have pinned workflow/Pack")
    if status["yield"]["kind"] != "none":
        raise ValueError("Gen2 Work already yielded; do not start another chat")
    unresolved = status["unresolved"]
    if (
        unresolved["roles_total"]
        or unresolved["capabilities_total"]
        or unresolved["children_live_total"]
    ):
        raise ValueError("bootstrap requires no active role, effect or delegated child")
    return {
        "work_id": work["work_id"],
        "work_digest": work["work_digest"],
        "revision": work["revision"],
    }


def _claim_once(
    *,
    store: WorkStore,
    work: dict[str, Any],
    plan: dict[str, Any],
) -> WorkSnapshot:
    """Atomically commit dispatch intent against the exact validated Gen2 head.

    An earlier attempt, even one which may have failed before the actual send,
    permanently blocks another bootstrap for this Work. Recovery is read-only.
    """
    work_id = work["work_id"]
    events = store.events(work_id)
    if any(event.kind in {CLAIM_KIND, CAPTURE_KIND} for event in events):
        raise ValueError("bootstrap was previously claimed for this Work; no retry")
    snapshot = store.snapshot(work_id)
    if (
        snapshot.work.work_digest != work["work_digest"]
        or snapshot.revision != work["revision"]
        or snapshot.state.value != "active"
        or len(events) != snapshot.revision
        or (events[-1].event_digest if events else None) != snapshot.last_event_digest
    ):
        raise ValueError("Gen2 Work frontier changed before bootstrap claim")
    candidate = snapshot.next_event(
        kind=CLAIM_KIND,
        payload={"schema": "codexia.chat.bootstrap.claim.v0", "plan": plan},
    )
    # BEGIN IMMEDIATE + expected_revision protects the race between this
    # preflight and the actual write; it is the *only* dispatch gate.
    return store.append(
        work_id, expected_revision=snapshot.revision, event=candidate
    )


def _capture_response(
    *, store: WorkStore, claimed: WorkSnapshot, execution: Any
) -> dict[str, Any]:
    if getattr(execution, "transport", None) != "browser-owned":
        raise ValueError("CWA returned unexpected transport: preserve claimed UNKNOWN")
    raw = execution.response
    conversation = raw.conversation
    conversation_id = conversation.conversation_id
    message_id = conversation.message_id
    answer = raw.text
    if not all(
        isinstance(value, str) and value
        for value in (conversation_id, message_id, answer)
    ):
        raise ValueError("CWA response lacks exact message/conversation/text evidence")
    response_sha = _sha(answer.encode("utf-8"))
    event = claimed.next_event(
        kind=CAPTURE_KIND,
        payload={
            "schema": "codexia.chat.bootstrap.capture.v0",
            "conversation_id": conversation_id,
            "message_id": message_id,
            "response_sha256": response_sha,
            "transport": "browser-owned",
        },
    )
    store.append(
        claimed.work.work_id, expected_revision=claimed.revision, event=event
    )
    return {
        "status": "CAPTURED_UNADMITTED",
        "work_id": claimed.work.work_id,
        "conversation_id": conversation_id,
        "message_id": message_id,
        "response_sha256": response_sha,
    }


def bootstrap_once(
    *,
    status: dict[str, Any],
    files: list[str],
    store: WorkStore,
    runtime: Any | None,
    commit: bool = False,
) -> dict[str, Any]:
    """Start one external chat. Never replay a persisted attempt."""
    work = _validate_work(status)
    evidence, media = _file_evidence(files)
    prompt = (
        "[Codexia delegated Work — transport role is not human authorship]\n"
        f"Work: {work['work_id']}\n"
        f"Objective: {status['work']['objective']}\n\n"
        "Read the attached project handoff/decision/plan files. Restore context, "
        "identify the next narrow step and its validation. Explain what is established "
        "versus assumed. Do not treat this message as permission for repository, "
        "process, network or filesystem effects. Do not claim completion from an answer."
    )
    plan = {
        "schema": "codexia.dw-chat-bootstrap.v0",
        "work": work,
        "files": evidence,
        "model_profile": None,  # rich attachments do not compose with explicit mode
        "prompt_sha256": _sha(prompt.encode("utf-8")),
    }
    if not commit:
        return {"status": "DRY_RUN", **plan}
    if runtime is None:
        raise ValueError("explicit CWA runtime required for commit")

    # This SQLite CAS is deliberately after CWA initialization, but before the
    # remote effect. A failed or ambiguous send must not be replayed.
    claimed = _claim_once(store=store, work=work, plan=plan)
    execution = runtime.send_text_observed(prompt, media=media)
    # A failed capture still leaves CLAIM_KIND durably visible. An already
    # completed response may be recovered later, but never resent here.
    return _capture_response(store=store, claimed=claimed, execution=execution)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", default=".codexia/work.sqlite3")
    parser.add_argument("--work-id", required=True)
    parser.add_argument("--file", action="append", required=True, dest="files")
    parser.add_argument("--auth-file", default="auth_data.json")
    parser.add_argument(
        "--commit",
        action="store_true",
        help="Record atomic Gen2 claim then perform exactly one CWA product write",
    )
    args = parser.parse_args()
    from codexia_manual_agent.standalone_work import StandaloneWorkSurface
    from codexia_manual_agent.work_core import SqliteWorkStore

    store = SqliteWorkStore(Path(args.store))
    status = StandaloneWorkSurface(store).status(args.work_id)
    runtime = None
    if args.commit:
        from chatgpt_web_adapter import assemble_product_runtime

        runtime = assemble_product_runtime(
            transport="browser-owned", auth_file=args.auth_file
        )
    result = bootstrap_once(
        status=status,
        files=args.files,
        store=store,
        runtime=runtime,
        commit=args.commit,
    )
    print(json.dumps(result, ensure_ascii=True, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
