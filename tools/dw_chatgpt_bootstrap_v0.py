"""Pilot-only Gen2 Work -> one CWA ChatGPT bootstrap.

The local attempt receipt is transport evidence, NOT Gen2 Work truth.
No retry, continuation, outcome admission, or completion is implemented here.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

MAX_FILES = 12
MAX_TOTAL_BYTES = 6_000_000


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
        entries.append({"name": path.name, "path": str(path), "bytes": size, "sha256": _sha(data)})
        media.append(str(path))
    return entries, media


def _validate_work(status: dict[str, Any]) -> dict[str, Any]:
    work = status["work"]
    if work["state"] != "active" or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", work["work_id"]):
        raise ValueError("bootstrap requires one active Gen2 Work with safe id")
    if not status["workflow"] or any(row.get("pack") is None for row in status["workflow"]):
        raise ValueError("Gen2 Work must have pinned workflow/Pack")
    if status["yield"]["kind"] != "none":
        raise ValueError("Gen2 Work already yielded; do not start another chat")
    if status["unresolved"]["roles_total"] or status["unresolved"]["capabilities_total"]:
        raise ValueError("bootstrap requires no pending role or effect")
    return {"work_id": work["work_id"], "work_digest": work["work_digest"], "revision": work["revision"]}


def _write_receipt(path: Path, record: dict[str, Any], *, exclusive: bool) -> None:
    payload = (json.dumps(record, ensure_ascii=True, sort_keys=True, indent=2) + "\n").encode("ascii")
    if exclusive:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            raise
        if os.name != "nt":
            directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    else:
        # This is an update of the already-claimed transport receipt only.
        temp = path.with_suffix(".tmp")
        with temp.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)


def bootstrap_once(
    *,
    status: dict[str, Any],
    files: list[str],
    receipt_dir: Path,
    runtime: Any | None,
    profile: str = "DEEP",
    commit: bool = False,
) -> dict[str, Any]:
    """Start one external chat, never retry an already claimed external write."""
    if profile not in {"FAST", "BALANCED", "DEEP"}:
        raise ValueError("unsupported CWA model profile")
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
    plan = {"schema": "codexia.dw-chat-bootstrap.v0", "work": work, "files": evidence,
            "profile": profile, "prompt_sha256": _sha(prompt.encode("utf-8"))}
    if not commit:
        return {"status": "DRY_RUN", **plan}
    if runtime is None:
        raise ValueError("explicit CWA runtime required for commit")
    receipt_dir.mkdir(parents=True, exist_ok=True)
    receipt = receipt_dir / f"{work['work_id']}.json"
    # Claim BEFORE any remote call. Existing receipt means UNKNOWN, not retry.
    record = {**plan, "state": "IN_FLIGHT_UNKNOWN"}
    _write_receipt(receipt, record, exclusive=True)
    try:
        response = runtime.send_text_observed(prompt, media=media, model_profile=profile)
        raw = response.response
        conversation = raw.conversation
        conversation_id = conversation.conversation_id
        message_id = conversation.message_id
        answer = raw.text
        if not all(isinstance(x, str) and x for x in (conversation_id, message_id, answer)):
            raise ValueError("CWA response lacks exact message/conversation/text evidence")
        record.update(state="CAPTURED_UNADMITTED", conversation_id=conversation_id,
                      message_id=message_id, response_sha256=_sha(answer.encode("utf-8")))
        _write_receipt(receipt, record, exclusive=False)
        return {"status": record["state"], "receipt": str(receipt),
                "conversation_id": conversation_id, "message_id": message_id,
                "response_sha256": record["response_sha256"]}
    except Exception:
        # Even a local printing/transport failure may follow a successful send.
        # Preserve IN_FLIGHT_UNKNOWN and require read-only reconciliation.
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", default=".codexia/work.sqlite3")
    parser.add_argument("--work-id", required=True)
    parser.add_argument("--file", action="append", required=True, dest="files")
    parser.add_argument("--receipt-dir", default=".codexia/dw-chat-bootstrap")
    parser.add_argument("--auth-file", default="auth_data.json")
    parser.add_argument("--profile", default="DEEP", choices=["FAST", "BALANCED", "DEEP"])
    parser.add_argument("--commit", action="store_true", help="Perform one CWA product write (never retry automatically)")
    args = parser.parse_args()
    from codexia_manual_agent.standalone_work import StandaloneWorkSurface
    from codexia_manual_agent.work_core import SqliteWorkStore
    status = StandaloneWorkSurface(SqliteWorkStore(Path(args.store))).status(args.work_id)
    runtime = None
    if args.commit:
        from chatgpt_web_adapter import assemble_product_runtime
        runtime = assemble_product_runtime(transport="browser-owned", auth_file=args.auth_file)
    result = bootstrap_once(status=status, files=args.files, receipt_dir=Path(args.receipt_dir),
                            runtime=runtime, profile=args.profile, commit=args.commit)
    print(json.dumps(result, ensure_ascii=True, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
