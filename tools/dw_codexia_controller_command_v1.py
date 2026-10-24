"""Capture command decisions from an EXISTING saved Codexia controller chat.

The controller thinks in its usual conversation; this tool makes NO model call.
It never launches shells, sends results, or claims local execution authority.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from types import SimpleNamespace

from codexia_manual_agent.standalone_work import (
    StandaloneWorkHost,
    StandaloneWorkSelector,
    StandaloneWorkSurface,
)
from codexia_manual_agent.work_core import SqliteWorkStore
from codexia_manual_agent.workflow_runtime.chat_command_plan_v1 import (
    ChatCommandSource,
    _sha,
)
from codexia_manual_agent.workflow_runtime.controller_command_v1 import (
    ControllerCommandDecision,
    ControllerCommandReviewWorkflow,
    pack_binding,
    workflow_binding,
)

_PROVIDER = "codexia:saved-controller-command-review-provider@0.1.0"


class _Plugin:
    def codexia_pack_distribution(self):
        return {
            "schema_version": 1,
            "pack": pack_binding().to_dict(),
            "workflows": [workflow_binding().to_dict()],
            "roles": [], "capabilities": [],
        }

    def codexia_workflow_implementation(self, binding):
        if binding != workflow_binding().to_dict():
            raise ValueError("controller workflow binding changed")
        return ControllerCommandReviewWorkflow()

    def codexia_completion_criterion(self, binding):
        raise ValueError("controller review cannot claim completion")


class _Plugins:
    def get(self, plugin_id):
        if plugin_id != _PROVIDER:
            raise KeyError("unsupported controller decision provider")
        return _Plugin()


def _selector():
    return StandaloneWorkSelector(
        provider_ref=_PROVIDER,
        workflow_id=workflow_binding().workflow_id,
        workflow_version=workflow_binding().version,
    )


def _host():
    return StandaloneWorkHost(plugin_service=_Plugins())


def _client(auth_file):
    from chatgpt_web_adapter.browserless_http import BrowserlessHTTPClient

    return BrowserlessHTTPClient(auth_file=auth_file)


def _latest(client, conversation_id):
    messages = client.get_messages(conversation_id, limit=1)
    if (
        len(messages) != 1
        or messages[0].role != "assistant"
        or not messages[0].message_id
    ):
        raise ValueError("conversation has no completed latest assistant reply")
    return messages[0]


def _capture(args, client, store):
    worker = _latest(client, args.worker_conversation)
    controller = _latest(client, args.codexia_conversation)
    if (
        args.worker_message_id
        and args.worker_message_id != worker.message_id
    ):
        raise ValueError("worker message ID changed since selection")
    if (
        args.codexia_message_id
        and args.codexia_message_id != controller.message_id
    ):
        raise ValueError("controller message ID changed since selection")
    source = ChatCommandSource(
        project=args.project,
        repo=args.repo,
        conversation_id=args.worker_conversation,
        source_message_id=worker.message_id,
        source_text=worker.text,
        source_sha256=_sha(worker.text),
        workspace=str(Path(args.workspace).resolve()),
    )
    decision = ControllerCommandDecision.from_messages(
        source=source,
        controller_conversation_id=args.codexia_conversation,
        controller_message_id=controller.message_id,
        controller_text=controller.text,
    )
    # Canonical deterministic source ID provides idempotent ingress.
    result = StandaloneWorkSurface(store).start(
        objective=decision.to_json(), selector=_selector(),
        host_factory=_host,
        source_id="controller:" + args.codexia_conversation + ":"
        + controller.message_id,
    )
    work_id = result["work"]["work_id"]
    return {
        "status": "CODEXIA_DECISION_CAPTURED_NO_MODEL_CALL",
        "work_id": work_id,
        "project": source.project,
        "repo": source.repo,
        "worker_conversation": source.conversation_id,
        "controller_conversation": decision.controller_conversation_id,
        "plan_digest": decision.plan.digest,
        "decision": decision.plan.decision,
        "steps": len(decision.plan.steps),
        "effects": "NONE",
    }


def _status(store, work_id):
    snapshot = store.snapshot(work_id)
    decision = ControllerCommandDecision.parse(snapshot.work.objective)
    events = store.events(work_id)
    surface = StandaloneWorkSurface(store).status(work_id)
    return {
        "work_id": work_id,
        "project": decision.source.project,
        "repo": decision.source.repo,
        "workspace": decision.source.workspace,
        "worker_conversation": decision.source.conversation_id,
        "controller_conversation": decision.controller_conversation_id,
        "worker_message_id": decision.source.source_message_id,
        "controller_message_id": decision.controller_message_id,
        "plan_digest": decision.plan.digest,
        "plan": decision.plan.to_dict(),
        "revision": snapshot.revision,
        "yield": surface["yield"],
        "review_events": [
            e.kind for e in events
            if e.kind.startswith("attention.")
        ],
        "effects": "NONE",
    }


def _review(store, work_id):
    # Workflow emits a durable AttentionNeed (not an effect permission).
    result = StandaloneWorkSurface(store).advance(
        work_id, selector=_selector(),
        host_factory=_host, max_steps=3,
    )
    return {
        "work_id": work_id,
        "progression": result["progression"]["status"],
        "status": _status(store, work_id),
    }


def _answer(store, work_id, action):
    if action not in {"approve", "reject"}:
        raise ValueError("invalid review action")
    status = _status(store, work_id)
    if status["yield"]["kind"] != "attention":
        raise ValueError("no current exact review AttentionNeed")
    digest = status["plan_digest"]
    keyword = "APPROVE" if action == "approve" else "REJECT"
    if input(f"Type {keyword} {digest[:12]} to record review: ").strip() != (
        f"{keyword} {digest[:12]}"
    ):
        raise ValueError("local human review not confirmed")
    StandaloneWorkSurface(store).answer(
        work_id, response_text=f"{keyword} {digest}"
    )
    return _review(store, work_id)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", default=".codexia/controller-command-work.sqlite3")
    sub = parser.add_subparsers(dest="action", required=True)
    capture = sub.add_parser("capture")
    capture.add_argument("--project", required=True)
    capture.add_argument("--repo", required=True)
    capture.add_argument("--workspace", required=True)
    capture.add_argument("--worker-conversation", required=True)
    capture.add_argument("--codexia-conversation", required=True)
    capture.add_argument("--worker-message-id")
    capture.add_argument("--codexia-message-id")
    capture.add_argument("--auth-file", required=True)
    review = sub.add_parser("review")
    review.add_argument("work_id")
    status = sub.add_parser("status")
    status.add_argument("work_id")
    answer = sub.add_parser("answer")
    answer.add_argument("work_id")
    answer.add_argument("action", choices=("approve", "reject"))
    args = parser.parse_args(argv)
    try:
        store_path = Path(args.store)
        if args.action == "capture":
            store_path.parent.mkdir(parents=True, exist_ok=True)
        store = SqliteWorkStore(store_path)
        if args.action == "capture":
            result = _capture(args, _client(args.auth_file), store)
        elif args.action == "review":
            result = _review(store, args.work_id)
        elif args.action == "answer":
            result = _answer(store, args.work_id, args.action)
        else:
            result = _status(store, args.work_id)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (KeyboardInterrupt, EOFError):
        print("INTERRUPTED: inspect Work before taking another action", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"STOPPED ({type(exc).__name__}): {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
