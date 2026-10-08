from __future__ import annotations

import hashlib
from dataclasses import replace

import pytest

from codexia_manual_agent.reconciliation import (
    PendingCognition,
    ReadbackObservation,
    ReadbackState,
    ReconciliationVerdict,
    verify_readback,
)

_REQUEST_ID = "e1713744-c9fe-47d0-b04c-810c29466b49"
_HANDOFF_ID = "1cba7f2d-c8c8-44ef-9dac-13980bbef253"
_RAW = "# Recovered analysis\n\nUncertainty remains.\n"


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@pytest.fixture
def scope() -> PendingCognition:
    return PendingCognition(
        work_id="work-id",
        workflow_run_id="workflow-id",
        role_run_id="role-id",
        request_id=_REQUEST_ID,
        request_digest="1" * 64,
        handoff_id=_HANDOFF_ID,
        handoff_digest="2" * 64,
        port_id="model-provider:cwa-subprocess",
        provider_namespace="tenant-A:account-1",
        expected_wire_sha256="3" * 64,
        expected_work_revision=17,
        expected_head_digest="4" * 64,
    )


@pytest.fixture
def observation() -> ReadbackObservation:
    return ReadbackObservation(
        state=ReadbackState.COMPLETED,
        port_id="model-provider:cwa-subprocess",
        provider_namespace="tenant-A:account-1",
        request_id=_REQUEST_ID,
        observed_wire_sha256="3" * 64,
        request_message_id="msg-user",
        response_message_id="msg-assistant",
        response_candidates=1,
        branch_verified=True,
        request_precedes_response=True,
        intervening_user_turns=0,
        finish_reason="stop",
        response_text=_RAW,
        reported_response_sha256=_sha(_RAW),
    )


def test_unique_context_match_is_not_automatic_provider_proof(
    scope, observation
) -> None:
    first = verify_readback(scope, observation)
    second = verify_readback(scope, observation)

    assert first == second
    assert first.verdict is ReconciliationVerdict.CONTEXT_CORROBORATED
    assert first.reason == "unique_context_match"
    assert first.eligible_for_automatic_admission is False
    assert first.request_id == _REQUEST_ID
    assert first.handoff_id == _HANDOFF_ID
    assert first.response_sha256 == _sha(_RAW)
    assert len(first.report_digest) == 64
    assert not hasattr(first, "output_text")
    assert not hasattr(first, "outcome")


def test_no_observation_fails_closed(scope) -> None:
    report = verify_readback(scope, None)
    assert report.verdict is ReconciliationVerdict.UNVERIFIABLE
    assert report.reason == "no_observation"
    assert report.response_sha256 is None


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (ReadbackState.PENDING, "provider_pending"),
        (ReadbackState.NOT_FOUND, "provider_not_found"),
        (ReadbackState.READ_UNAVAILABLE, "provider_read_unavailable"),
        (ReadbackState.UNSUPPORTED, "provider_unsupported"),
    ],
)
def test_no_completed_response_must_not_be_inferred(
    scope, observation, state, expected
) -> None:
    candidate = replace(observation, state=state)
    report = verify_readback(scope, candidate)
    assert report.verdict is ReconciliationVerdict.UNVERIFIABLE
    assert report.reason == expected
    assert report.eligible_for_automatic_admission is False


@pytest.mark.parametrize(
    ("field", "bad_value", "reason"),
    [
        ("port_id", "another-port", "provider_binding_mismatch"),
        ("provider_namespace", "tenant-B:account-2", "provider_binding_mismatch"),
        ("request_id", "wrong-request", "request_identity_mismatch"),
        ("observed_wire_sha256", "5" * 64, "wire_input_mismatch"),
        ("response_candidates", 2, "multiple_response_candidates"),
        ("request_message_id", "msg-assistant", "message_identity_collision"),
        ("request_precedes_response", False, "response_order_conflict"),
        ("intervening_user_turns", 1, "intervening_user_turn"),
        ("branch_verified", False, "branch_conflict"),
        ("reported_response_sha256", "0" * 64, "response_digest_mismatch"),
    ],
)
def test_mismatches_reject_without_an_outcome(
    scope, observation, field, bad_value, reason
) -> None:
    report = verify_readback(scope, replace(observation, **{field: bad_value}))
    assert report.verdict is ReconciliationVerdict.CONFLICT
    assert report.reason == reason
    assert report.eligible_for_automatic_admission is False


@pytest.mark.parametrize(
    ("field", "bad_value", "reason"),
    [
        ("request_id", None, "missing_request_binding"),
        ("observed_wire_sha256", None, "missing_request_binding"),
        ("response_candidates", None, "candidate_count_unknown"),
        ("response_candidates", 0, "no_response_candidate"),
        ("request_message_id", None, "missing_message_identity"),
        ("response_message_id", None, "missing_message_identity"),
        ("request_precedes_response", None, "response_order_unknown"),
        ("intervening_user_turns", None, "intervening_turns_unknown"),
        ("branch_verified", None, "branch_lineage_unknown"),
        ("finish_reason", "length", "not_final_response"),
        ("finish_reason", None, "not_final_response"),
        ("response_text", "", "no_response_text"),
        ("response_text", None, "no_response_text"),
        ("reported_response_sha256", None, "missing_response_digest"),
    ],
)
def test_missing_or_incomplete_evidence_never_grants_admission(
    scope, observation, field, bad_value, reason
) -> None:
    report = verify_readback(scope, replace(observation, **{field: bad_value}))
    assert report.verdict is ReconciliationVerdict.UNVERIFIABLE
    assert report.reason == reason


def test_response_over_gen2_limit_is_rejected(scope, observation) -> None:
    oversize = "a" * (131_072 + 1)
    candidate = replace(
        observation,
        response_text=oversize,
        reported_response_sha256=_sha(oversize),
    )
    report = verify_readback(scope, candidate)
    assert report.verdict is ReconciliationVerdict.CONFLICT
    assert report.reason == "response_out_of_bounds"


def test_invalid_utf8_text_is_rejected_before_any_admission(scope, observation) -> None:
    invalid_unicode = "prefix-\ud800"
    report = verify_readback(
        scope,
        replace(observation, response_text=invalid_unicode),
    )
    assert report.verdict is ReconciliationVerdict.CONFLICT
    assert report.reason == "response_not_utf8"
    assert report.eligible_for_automatic_admission is False


def test_missing_or_forged_request_cannot_be_recovered_by_text_match(
    scope, observation
) -> None:
    same_content_different_request = replace(
        observation, request_id="someone-elses-request"
    )
    assert (
        verify_readback(scope, same_content_different_request).verdict
        is ReconciliationVerdict.CONFLICT
    )


def test_report_hash_changes_with_source_evidence(scope, observation) -> None:
    good = verify_readback(scope, observation)
    altered = replace(
        observation,
        response_text="Different result.",
        reported_response_sha256=_sha("Different result."),
    )
    changed = verify_readback(scope, altered)
    assert changed.verdict is ReconciliationVerdict.CONTEXT_CORROBORATED
    assert changed.report_digest != good.report_digest
    assert changed.response_sha256 != good.response_sha256


def test_report_preserves_raw_uncertainty_not_semantic_completion(
    scope, observation
) -> None:
    raw = (
        "# Recommendation\n\nStill uncertain\n\n"
        "<<<CODEXIA_CONTROL_V1>>>\n"
        '{"schema_version":1,"objective_coverage_complete":true,'
        '"evidence_sufficient":true,"material_uncertainty_resolved":false}\n'
        "<<<END_CODEXIA_CONTROL_V1>>>"
    )
    report = verify_readback(
        scope,
        replace(
            observation,
            response_text=raw,
            reported_response_sha256=_sha(raw),
        ),
    )
    assert report.verdict is ReconciliationVerdict.CONTEXT_CORROBORATED
    assert report.eligible_for_automatic_admission is False
    assert report.response_sha256 == _sha(raw)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("expected_work_revision", 0),
        ("expected_work_revision", True),
        ("request_digest", "xyz"),
        ("expected_head_digest", "ABC" * 21 + "A"),
        ("port_id", " bad-port "),
        ("provider_namespace", ""),
    ],
)
def test_malformed_scope_is_rejected(scope, field, value) -> None:
    with pytest.raises((TypeError, ValueError)):
        replace(scope, **{field: value})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("response_candidates", -1),
        ("response_candidates", True),
        ("intervening_user_turns", -1),
        ("branch_verified", 1),
        ("request_precedes_response", "yes"),
        ("response_text", b"bytes"),
    ],
)
def test_malformed_observation_is_rejected(observation, field, value) -> None:
    with pytest.raises((TypeError, ValueError)):
        replace(observation, **{field: value})


def test_verifier_does_not_change_input_snapshots(scope, observation) -> None:
    before_scope = repr(scope)
    before_observation = repr(observation)
    for _ in range(3):
        verify_readback(scope, observation)
    assert repr(scope) == before_scope
    assert repr(observation) == before_observation
    assert scope.expected_work_revision == 17


def test_wrong_types_are_rejected(scope, observation) -> None:
    with pytest.raises(TypeError):
        verify_readback("not-scope", observation)
    with pytest.raises(TypeError):
        verify_readback(scope, {"state": "completed"})
