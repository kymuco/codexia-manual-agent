"""One explicitly approved Codexia Work -> HTTP ChatGPT bootstrap.

Default is an offline dry-run. --commit asks the human to type SEND before
durably claiming the existing Gen2 Work and starting *one* external HTTP turn.

Bootstrap capture is transport evidence, NOT cognition admission or WorkCompletion.
Never rerun a previously claimed Work, including after process interruption.
Run as: python -m tools.dw_chatgpt_http_bootstrap_v0 [...]
"""

from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import sys
from pathlib import Path
from typing import Any

from codexia_manual_agent.domain.models import ProviderRequest
from codexia_manual_agent.providers.chatgpt_http import ChatGPTHttpProvider
from codexia_manual_agent.providers.chatgpt_http_progress import HttpProgressConsole
from codexia_manual_agent.standalone_work import StandaloneWorkSurface
from codexia_manual_agent.work_core import SqliteWorkStore, WorkSnapshot, WorkStore
from tools.dw_chatgpt_bootstrap_v0 import (
    CAPTURE_KIND,
    _claim_once,
    _file_evidence,
    _sha,
    _validate_work,
)


def _freeze_files(evidence: list[dict[str, Any]]) -> list[tuple[bytes, str]]:
    """Freeze exact bytes before any durable external-dispatch claim."""

    frozen = []
    filenames = set()
    for item in evidence:
        source = Path(item["path"])
        if source.is_symlink() or not source.is_file():
            raise ValueError("context file disappeared before snapshot")
        data = source.read_bytes()
        if len(data) != item["bytes"] or _sha(data) != item["sha256"]:
            raise ValueError("context file changed after preflight; no claim")
        filename = item["name"]
        if filename in filenames:
            raise ValueError("different paths share upload filename; no claim")
        filenames.add(filename)
        frozen.append((data, filename))
    return frozen


def _verify_canonical(provider: ChatGPTHttpProvider, response: Any) -> None:
    """Observe one exact response via read-only canonical history."""

    conversation = response.conversation
    if conversation is None:
        raise ValueError("HTTP response omitted conversation identity")
    cid, mid = conversation.conversation_id, conversation.message_id
    if not cid or not mid or not response.text.strip():
        raise ValueError("HTTP response omitted canonical identity or text")
    messages = provider.read_messages(cid)
    matching = [
        message
        for message in messages
        if message.role == "assistant" and message.message_id == mid
    ]
    if len(matching) != 1 or matching[0].text != response.text:
        raise ValueError("read-only ChatGPT history did not prove exact assistant reply")


def _capture(
    store: WorkStore, claimed: WorkSnapshot, response: Any
) -> dict[str, str]:
    """Commit transport-only result, never a Gen2 role or Work outcome."""

    conversation = response.conversation
    response_sha = hashlib.sha256(response.text.encode("utf-8")).hexdigest()
    event = claimed.next_event(
        kind=CAPTURE_KIND,
        payload={
            "schema": "codexia.chat.bootstrap.capture.v0",
            "conversation_id": conversation.conversation_id,
            "message_id": conversation.message_id,
            "response_sha256": response_sha,
            "transport": "chatgpt-http",
            "readback": "exact_assistant_id_and_text",
        },
    )
    store.append(
        claimed.work.work_id,
        expected_revision=claimed.revision,
        event=event,
    )
    return {
        "status": "CAPTURED_UNADMITTED",
        "work_id": claimed.work.work_id,
        "conversation_id": conversation.conversation_id,
        "message_id": conversation.message_id,
        "response_sha256": response_sha,
    }


def _assert_cwa_source(cwa_root: Path) -> None:
    """Reject accidentally importing the normal browser-backed CWA checkout."""

    from chatgpt_web_adapter.browserless_http import BrowserlessHTTPClient

    expected = (
        cwa_root.expanduser().resolve()
        / "src"
        / "chatgpt_web_adapter"
        / "browserless_http.py"
    ).resolve()
    actual = Path(inspect.getfile(BrowserlessHTTPClient)).resolve()
    if actual != expected:
        raise RuntimeError(
            "imported ChatGPT HTTP implementation differs from --cwa-root; "
            "fix PYTHONPATH before running --commit"
        )


def bootstrap_http_once(
    *,
    status: dict[str, Any],
    files: list[str],
    store: WorkStore,
    reporter: HttpProgressConsole,
    client: ChatGPTHttpProvider | None = None,
    commit: bool = False,
    model: str = "gpt-6-instant",
    reasoning_effort: str | None = None,
) -> dict[str, Any]:
    """Safe offline preflight or exactly one CAS-fenced HTTP bootstrap."""

    if reasoning_effort not in {
        None,
        "instant",
        "medium",
        "high",
        "standard",
        "extended",
    }:
        raise ValueError("unsupported reasoning effort before Work claim")
    work = _validate_work(status)
    reporter.emit("WORK", {"work_id": work["work_id"], "revision": work["revision"]})
    evidence, _ = _file_evidence(files)
    frozen = _freeze_files(evidence)
    manifest = json.dumps(evidence, sort_keys=True, ensure_ascii=True)
    reporter.emit(
        "INPUT",
        {
            "files": len(evidence),
            "total_bytes": sum(item["bytes"] for item in evidence),
            "sha256": _sha(manifest.encode("utf-8")),
        },
    )

    prompt = (
        "[Codexia delegated Work - transport role is not human authorship]\n"
        f"Work: {work['work_id']}\n"
        f"Objective: {status['work']['objective']}\n\n"
        "Read the attached project handoff/decision/plan files. Restore context, "
        "identify the next narrow step and its validation. Explain what is "
        "established versus assumed. Do not treat this message as permission for "
        "repository, process, network or filesystem effects. Do not claim "
        "completion from an answer."
    )
    plan = {
        "schema": "codexia.dw-chat-bootstrap-http.v0",
        "transport": "chatgpt-http",
        "work": work,
        "files": evidence,
        "model": model,
        "reasoning_effort": reasoning_effort,
        "prompt_sha256": _sha(prompt.encode("utf-8")),
    }
    if not commit:
        reporter.emit("RESULT", {"status": "dry_run_no_effect"})
        return {"status": "DRY_RUN", **plan}
    if client is None or client.provider_id != "chatgpt-http":
        raise ValueError("explicit HTTP-only provider required before claim")

    # Exactly the same durable CLAIM_KIND as the original browser pilot:
    # never re-dispatch an existing claimed or uncertain Work by switching transport.
    claimed = _claim_once(store=store, work=work, plan=plan)
    reporter.emit(
        "CLAIM",
        {"work_id": claimed.work.work_id, "revision": claimed.revision,
         "status": "claim_committed"},
    )
    try:
        response = client.send(ProviderRequest(prompt=prompt), media=frozen)
        reporter.finish_stream()
        _verify_canonical(client, response)
        reporter.emit(
            "READBACK",
            {
                "conversation_id": response.conversation.conversation_id,
                "message_id": response.conversation.message_id,
                "status": "exact_reply_verified",
            },
        )
        result = _capture(store, claimed, response)
    except BaseException:
        reporter.finish_stream()
        reporter.emit("OUTCOME_UNKNOWN", {"retry": "forbidden"})
        reporter.emit("RESULT", {"status": "claim_present_reconcile_only"})
        raise

    reporter.emit("ADMISSION", {"status": "not_admitted"})
    reporter.emit("RESULT", {"status": "captured_unadmitted"})
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", default=".codexia/work.sqlite3")
    parser.add_argument("--work-id", required=True)
    parser.add_argument("--file", action="append", required=True, dest="files")
    parser.add_argument("--auth-file", default="auth_data.json")
    parser.add_argument("--model", default="gpt-6-instant")
    parser.add_argument(
        "--reasoning-effort",
        choices=("instant", "medium", "high", "standard", "extended"),
        default=None,
        help="ChatGPT web thinking effort; high is sent as extended",
    )
    parser.add_argument("--cwa-root", type=Path)
    parser.add_argument("--stream", action="store_true")
    parser.add_argument("--commit", action="store_true")
    args = parser.parse_args(argv)

    reporter = HttpProgressConsole(stream_text=args.stream)
    try:
        store = SqliteWorkStore(Path(args.store))
        status = StandaloneWorkSurface(store).status(args.work_id)
        if not args.commit:
            result = bootstrap_http_once(
                status=status, files=args.files, store=store,
                reporter=reporter, model=args.model,
                reasoning_effort=args.reasoning_effort,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0

        if args.cwa_root is None:
            parser.error("--cwa-root is required for --commit")
        _assert_cwa_source(args.cwa_root)
        if not Path(args.auth_file).is_file():
            raise ValueError("auth file is missing; no Work claim was created")
        # Client construction must be completed before claim.
        provider = ChatGPTHttpProvider(
            auth_file=args.auth_file,
            model=args.model,
            reasoning_effort=args.reasoning_effort,
            progress=reporter.emit,
            on_token=reporter.token if args.stream else None,
        )
        # This preflight deliberately performs no HTTP write and no Work mutation.
        work = _validate_work(status)
        evidence, _ = _file_evidence(args.files)
        _freeze_files(evidence)
        print(
            f"Work={work['work_id']} rev={work['revision']} "
            f"files={len(evidence)} model={args.model} "
            f"reasoning_effort={args.reasoning_effort or 'default'} "
            "transport=HTTP",
            file=sys.stderr,
            flush=True,
        )
        print(
            "This will permanently claim the Work and attempt ONE ChatGPT HTTP "
            "turn. A failure after claim cannot be retried with the same Work.",
            file=sys.stderr,
            flush=True,
        )
        try:
            confirmation = input("Type SEND to commit, anything else to cancel: ")
        except (KeyboardInterrupt, EOFError):
            reporter.emit("RESULT", {"status": "cancelled_before_claim"})
            return 130
        if confirmation.strip() != "SEND":
            reporter.emit("RESULT", {"status": "cancelled_before_claim"})
            return 1
        result = bootstrap_http_once(
            status=status, files=args.files, store=store, reporter=reporter,
            client=provider, commit=True, model=args.model,
            reasoning_effort=args.reasoning_effort,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (KeyboardInterrupt, EOFError):
        reporter.finish_stream()
        print(
            "Interrupted. Inspect durable WorkStore claim before any further action; "
            "never resend a claimed attempt.",
            file=sys.stderr,
        )
        return 130
    except Exception as exc:
        reporter.finish_stream()
        # Do not render the exception message: it may contain product payloads.
        reporter.emit(
            "RESULT",
            {"status": "stopped_inspect_workstore", "error_type": type(exc).__name__},
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
