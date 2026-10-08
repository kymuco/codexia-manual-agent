from __future__ import annotations

"""Reconcile a completed DW3 synthesis without a new cognition dispatch.

Default is read-only dry run. The only WorkStore write is a human-authorized
CognitionTransportBridge.record_outcome() after exact CWA/Work proof.
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path
from urllib.parse import urlparse
from uuid import UUID

from codexia_manual_agent.research_work import (
    ResearchContextMaterialPort,
    ResearchInstructionsMaterialPort,
)
from codexia_manual_agent.role_core import (
    CognitionOutcome,
    RoleRunState,
    project_role_runs,
)
from codexia_manual_agent.role_core.transport_bridge import CognitionTransportBridge
from codexia_manual_agent.role_core.transport_projection import (
    project_cognition_handoffs,
)
from codexia_manual_agent.work_core import SqliteWorkStore, WorkState
from codexia_manual_agent.workflow_orchestration.role_cognition import (
    RoleCognitionMaterializationService,
)
from codexia_manual_agent.workflow_runtime.research_v1 import (
    RESEARCH_CONTROL_END,
    RESEARCH_CONTROL_START,
    REVISER_ROLE_ID,
    STAGE_SYNTHESIS,
    SYNTHESIZER_ROLE_ID,
    ResearchRoleOutput,
)
from examples.dw3_research_live_host import _hot_research_continuation_prompt

PORT_ID = "model-provider:cwa-subprocess"
_CONTROL_FIELDS = frozenset(
    {
        "schema_version",
        "objective_coverage_complete",
        "evidence_sufficient",
        "material_uncertainty_resolved",
    }
)


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _unique_json_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate control-trailer key")
        result[key] = value
    return result


def canonicalize_synthesis(text: str) -> tuple[str, str]:
    """Unwrap only the literal CWA escaped FINAL trailer, preserving its values.

    Raw CWA response bytes remain separately verified and SHA-bound. The
    returned plaintext is the exact output to admit to Research Pack 1.1.
    """
    stripped = text.strip()
    if (
        stripped.count(RESEARCH_CONTROL_START) > 1
        or stripped.count(RESEARCH_CONTROL_END) > 1
    ):
        raise ValueError("synthesis requires exactly one final control trailer")
    if stripped.endswith(RESEARCH_CONTROL_END):
        return text, "verbatim"

    if not stripped.endswith("</escape>"):
        return text, "verbatim"

    wrapped = stripped[: -len("</escape>")].rstrip()
    if not wrapped.endswith(RESEARCH_CONTROL_END):
        raise ValueError("CWA escape wrapper has an invalid final control end")

    start = wrapped.rfind(RESEARCH_CONTROL_START)
    if start < 0:
        raise ValueError("CWA escape wrapper has no control start")

    leading = wrapped[:start]
    opening = re.search(
        r"(?:\r?\n)[ \t]*<escape>[ \t]*\r?\n[ \t]*$",
        leading,
    )
    if opening is None:
        raise ValueError("CWA escape wrapper is not a standalone trailer wrapper")

    body = leading[: opening.start()].rstrip()
    if not body:
        raise ValueError("CWA escape wrapper has no synthesis body")

    canonical = body + "\n\n" + wrapped[start:]
    return canonical, "cwa-final-control-escape-unwrapped-v1"


def validate_synthesis(text: str) -> ResearchRoleOutput:
    canonical, _normalization = canonicalize_synthesis(text)
    stripped = canonical.strip()
    start_count = stripped.count(RESEARCH_CONTROL_START)
    end_count = stripped.count(RESEARCH_CONTROL_END)
    terminal_end = stripped.endswith(RESEARCH_CONTROL_END)
    if start_count != 1 or end_count != 1 or not terminal_end:
        raise ValueError(
            "synthesis requires exactly one final control trailer "
            f"(starts={start_count}, ends={end_count}, "
            f"terminal_end={terminal_end})"
        )
    prefix, raw = stripped.rsplit(RESEARCH_CONTROL_START, 1)
    if not prefix.strip():
        raise ValueError("synthesis content is empty")
    raw = raw[: -len(RESEARCH_CONTROL_END)].strip()
    trailer = json.loads(raw, object_pairs_hook=_unique_json_pairs)
    if not isinstance(trailer, dict):
        raise TypeError("synthesis trailer must be an object")
    if set(trailer) != _CONTROL_FIELDS:
        raise ValueError("synthesis trailer does not have exact v1 fields")
    if type(trailer["schema_version"]) is not int or trailer["schema_version"] != 1:
        raise ValueError("unsupported synthesis control version")
    for field in _CONTROL_FIELDS - {"schema_version"}:
        if type(trailer[field]) is not bool:
            raise ValueError(f"control {field} must be boolean")

    parsed = ResearchRoleOutput.parse(canonical, expected_stage=STAGE_SYNTHESIS)
    if (
        parsed.content != prefix.rstrip()
        or parsed.objective_coverage != trailer["objective_coverage_complete"]
        or parsed.evidence_sufficiency != trailer["evidence_sufficient"]
        or parsed.material_unresolved_uncertainty
        == trailer["material_uncertainty_resolved"]
    ):
        raise ValueError("synthesis controls do not round-trip through Research Pack")
    return parsed


def _cwa_conversation_id(value: str) -> str:
    parsed = urlparse(value)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "chatgpt.com"
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("conversation must use a canonical chatgpt.com URL")
    sections = parsed.path.strip("/").split("/")
    if len(sections) != 2 or sections[0] != "c":
        raise ValueError("conversation URL must identify one existing ChatGPT chat")
    candidate = sections[1]
    if str(UUID(candidate)) != candidate:
        raise ValueError("conversation URL must contain canonical UUID")
    return candidate


def _read_cwa(
    *,
    cwa_python: str,
    cwa_source_root: str | None,
    conversation: str,
    auth_file: str,
    request_message_id: str,
    response_message_id: str,
) -> dict:
    helper = Path(__file__).with_name("dw3_cwa_recovery_read.py")
    command = [
        cwa_python,
        str(helper),
        "--conversation",
        conversation,
        "--auth-file",
        auth_file,
        "--request-message-id",
        request_message_id,
        "--response-message-id",
        response_message_id,
    ]
    child_env = os.environ.copy()
    if cwa_source_root is not None:
        source_root = Path(cwa_source_root).expanduser().resolve()
        if not (source_root / "chatgpt_web_adapter" / "__init__.py").is_file():
            raise ValueError(
                "CWA source root must contain chatgpt_web_adapter/__init__.py"
            )
        prior_path = child_env.get("PYTHONPATH", "")
        child_env["PYTHONPATH"] = os.pathsep.join(
            part for part in (str(source_root), prior_path) if part
        )
    child_env["PYTHONIOENCODING"] = "utf-8"
    child_env["PYTHONUTF8"] = "1"
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=180,
        env=child_env,
    )
    if completed.returncode != 0:
        raise ValueError(
            "read-only CWA proof failed: " + completed.stderr.strip()[-1000:]
        )
    result = json.loads(completed.stdout)
    if not isinstance(result, dict):
        raise TypeError("CWA proof must be a JSON object")
    if set(result) != {"request", "response", "prior_revision"}:
        raise ValueError("CWA proof has an unexpected payload")
    return result


def verify_recovery(
    *,
    store: SqliteWorkStore,
    work_id: str,
    expected_revision: int,
    expected_head_digest: str,
    expected_handoff_id: str,
    expected_sha256: str,
    recovered_file: Path,
    turn: dict,
    request_message_id: str,
    response_message_id: str,
) -> tuple[CognitionOutcome, dict]:
    snapshot = store.snapshot(work_id)
    events = store.events(work_id)
    if (
        snapshot.state is not WorkState.ACTIVE
        or snapshot.revision != expected_revision
        or snapshot.last_event_digest != expected_head_digest
        or len(events) != expected_revision
        or events[-1].event_digest != expected_head_digest
    ):
        raise ValueError("Work head changed; reconciliation is not authorized")

    roles = project_role_runs(events)
    synthesizers = [
        item for item in roles if item.run.binding.role_id == SYNTHESIZER_ROLE_ID
    ]
    revisers = [item for item in roles if item.run.binding.role_id == REVISER_ROLE_ID]
    if len(synthesizers) != 1 or len(revisers) != 1 or len(roles) != 4:
        raise ValueError("expected exact four-role DW3 Research Work")
    synth = synthesizers[0]
    reviser = revisers[0]
    if (
        synth.state is not RoleRunState.REQUESTED
        or reviser.state is not RoleRunState.COMPLETED
        or reviser.output_text is None
    ):
        raise ValueError("DW3 reviser/synthesizer role frontier is not recoverable")
    if events[-1].kind != "role.cognition-handoff-admitted":
        raise ValueError("current Work head must be synthesis handoff")

    handoffs = [
        item
        for item in project_cognition_handoffs(events)
        if item.request_id == synth.request_id
    ]
    if (
        len(handoffs) != 1
        or handoffs[0].handoff_id != expected_handoff_id
        or events[-1].event_id != expected_handoff_id
        or handoffs[0].port_id != PORT_ID
    ):
        raise ValueError("exact synthesis handoff identity/port not proven")

    materialized = RoleCognitionMaterializationService(
        store=store,
        instructions=ResearchInstructionsMaterialPort(),
        context=ResearchContextMaterialPort(store=store, work_id=work_id),
    ).rematerialize_request(
        work_id=work_id,
        role_run_id=synth.run.role_run_id,
    )
    request = materialized.request
    if (
        request.request_id != handoffs[0].request_id
        or request.request_digest != handoffs[0].request_digest
        or request.role_run_id != handoffs[0].role_run_id
    ):
        raise ValueError("recovered request differs from admitted handoff")

    delta = _hot_research_continuation_prompt(request.context)
    if delta is None or json.loads(delta)["stage"] != STAGE_SYNTHESIS:
        raise ValueError("Research context cannot form the expected hot delta")
    expected_prompt = (
        "[Codexia product-runtime system context]\n"
        f"{request.instructions.strip()}\n\n"
        "[Codexia product-runtime request]\n"
        f"{delta}"
    )
    user = turn["request"]
    answer = turn["response"]
    revision = turn["prior_revision"]
    if (
        user["message_id"] != request_message_id
        or user["text"] != expected_prompt
        or answer["message_id"] != response_message_id
        or answer["finish_reason"] != "stop"
        or revision["finish_reason"] != "stop"
        or revision["text"] != reviser.output_text
    ):
        raise ValueError("CWA prompt/response/reviser history differs from Work")

    raw = recovered_file.read_bytes()
    file_sha = _sha256(raw)
    if file_sha != expected_sha256:
        raise ValueError("recovered file SHA-256 mismatch")
    content = raw.decode("utf-8", errors="strict")
    if content != answer["text"]:
        raise ValueError("recovered file differs from canonical CWA answer")
    canonical, normalization = canonicalize_synthesis(content)
    parsed = validate_synthesis(canonical)
    outcome = CognitionOutcome.succeeded(request, output_text=canonical)
    result = {
        "verified": True,
        "mode": "dry-run",
        "work_id": work_id,
        "work_revision": snapshot.revision,
        "handoff_id": handoffs[0].handoff_id,
        "port_id": handoffs[0].port_id,
        "request_id": request.request_id,
        "request_digest": request.request_digest,
        "cwa_request_message_id": request_message_id,
        "cwa_response_message_id": response_message_id,
        "cwa_previous_revision_id": revision["message_id"],
        "response_sha256": file_sha,
        "response_chars": len(content),
        "admitted_output_sha256": _sha256(canonical.encode("utf-8")),
        "admitted_output_chars": len(canonical),
        "normalization": normalization,
        "objective_coverage_complete": parsed.objective_coverage,
        "evidence_sufficient": parsed.evidence_sufficiency,
        "material_uncertainty_resolved": not parsed.material_unresolved_uncertainty,
        "workstore_modified": False,
    }
    return outcome, result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-id", required=True)
    parser.add_argument("--store", required=True)
    parser.add_argument("--expected-revision", required=True, type=int)
    parser.add_argument("--expected-head-digest", required=True)
    parser.add_argument("--handoff-id", required=True)
    parser.add_argument("--conversation", required=True)
    parser.add_argument("--request-message-id", required=True)
    parser.add_argument("--response-message-id", required=True)
    parser.add_argument("--recovered-file", required=True, type=Path)
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument("--cwa-python", required=True)
    parser.add_argument("--cwa-source-root")
    parser.add_argument("--auth-file", required=True)
    parser.add_argument("--commit", action="store_true")
    parser.add_argument("--approve-sha256")
    args = parser.parse_args()

    if args.commit:
        if args.approve_sha256 != args.expected_sha256:
            raise ValueError("commit requires exact --approve-sha256 acknowledgement")
    elif args.approve_sha256 is not None:
        raise ValueError("approval is only accepted together with --commit")

    conversation_id = _cwa_conversation_id(args.conversation)
    if not Path(args.store).is_file():
        raise ValueError("existing WorkStore is required; never create recovery store")
    store = SqliteWorkStore(args.store)
    initial_revision = store.snapshot(args.work_id).revision
    turn = _read_cwa(
        cwa_python=args.cwa_python,
        cwa_source_root=args.cwa_source_root,
        conversation=args.conversation,
        auth_file=args.auth_file,
        request_message_id=args.request_message_id,
        response_message_id=args.response_message_id,
    )
    outcome, report = verify_recovery(
        store=store,
        work_id=args.work_id,
        expected_revision=args.expected_revision,
        expected_head_digest=args.expected_head_digest,
        expected_handoff_id=args.handoff_id,
        expected_sha256=args.expected_sha256,
        recovered_file=args.recovered_file,
        turn=turn,
        request_message_id=args.request_message_id,
        response_message_id=args.response_message_id,
    )
    if initial_revision != args.expected_revision:
        raise ValueError("Work head changed while CWA proof was being read")
    report["cwa_conversation_id"] = conversation_id

    if args.commit:
        completed = CognitionTransportBridge(store).record_outcome(
            outcome, port_id=PORT_ID
        )
        if completed.state is not RoleRunState.COMPLETED:
            raise RuntimeError("reconciled role was not completed")
        report["mode"] = "committed"
        report["workstore_modified"] = True
        report["new_work_revision"] = store.snapshot(args.work_id).revision

    print(json.dumps(report, sort_keys=True, indent=2, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
