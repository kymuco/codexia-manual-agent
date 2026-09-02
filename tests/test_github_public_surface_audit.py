from __future__ import annotations

import importlib.util
from pathlib import Path


def load_surface_module():
    script = Path(__file__).resolve().parents[1] / "scripts" / "github_public_surface_audit.py"
    spec = importlib.util.spec_from_file_location("github_public_surface_audit", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    import sys

    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def empty_snapshot():
    return {
        "repository": {"has_wiki": False, "has_discussions": False, "has_pages": False},
        "issues": [],
        "pull_requests": [],
        "pull_request_reviews": [],
        "issue_comments": [],
        "review_comments": [],
        "commit_comments": [],
        "releases": [],
        "workflow_runs": [],
        "artifacts": [],
    }


def test_pr_and_comment_metadata_are_scanned_for_credentials():
    audit = load_surface_module()
    secret = "sk-" + "githubsurfacevalue123456789"
    snapshot = empty_snapshot()
    snapshot["pull_requests"] = [{"number": 7, "title": "safe", "body": f"debug {secret}"}]
    snapshot["issue_comments"] = [{"id": 12, "body": "safe"}]

    state = audit.audit_snapshot(snapshot)

    assert state.totals["HIGH"] == 1
    assert state.findings[0].kind == "openai-style-token"
    assert secret not in state.findings[0].location


def test_review_submission_body_is_scanned():
    audit = load_surface_module()
    secret = "sk-" + "reviewsubmissionvalue123456789"
    snapshot = empty_snapshot()
    snapshot["pull_request_reviews"] = [{"id": 4, "body": f"finding {secret}"}]

    state = audit.audit_snapshot(snapshot)

    assert state.totals["HIGH"] == 1
    assert state.findings[0].kind == "openai-style-token"


def test_commit_comment_metadata_is_scanned_for_credentials():
    audit = load_surface_module()
    secret = "sk-" + "commitcommentvalue123456789"
    snapshot = empty_snapshot()
    snapshot["commit_comments"] = [{"id": 9, "body": f"debug {secret}"}]

    state = audit.audit_snapshot(snapshot)

    assert state.totals["HIGH"] == 1
    assert state.findings[0].kind == "openai-style-token"


def test_verified_synthetic_fixture_quoted_in_review_is_not_high():
    audit = load_surface_module()
    synthetic = "sk-" + "proj-" + "THIS_IS_A_TEST_SECRET_123456789"
    snapshot = empty_snapshot()
    snapshot["review_comments"] = [
        {
            "id": 33,
            "body": f"Known intentionally synthetic test fixture `{synthetic}` retained for regression evidence.",
        }
    ]

    state = audit.audit_snapshot(snapshot)

    assert state.totals["HIGH"] == 0


def test_exact_historical_private_key_example_is_verified_by_body_digest_only():
    audit = load_surface_module()
    header = "-----BEGIN " + "PRIVATE KEY-----"
    body = (
        "**<sub><sub>![P1 Badge](https://img.shields.io/badge/P1-orange?style=flat)</sub></sub>  "
        "Do not emit lines adjacent to detected secrets**\n\n"
        "When a private-key header is detected, the next line normally contains the key material, "
        "but this context construction includes that line and the later redaction only replaces recognized "
        f"signatures. For example, scanning a PEM containing `{header}` prints the following base64 line "
        "verbatim; an AWS access-key finding similarly prints an adjacent unmatched `AWS_SECRET_ACCESS_KEY`. "
        "Thus running or sharing this security audit can disclose the secrets it finds, so secret findings "
        "should omit surrounding lines or redact the complete credential structure.\n\n"
        "Useful? React with 👍\u00a0/ 👎."
    )
    snapshot = empty_snapshot()
    snapshot["review_comments"] = [{"id": 3897428879, "body": body}]

    state = audit.audit_snapshot(snapshot)

    assert state.totals["HIGH"] == 0
    assert state.synthetic_fixture_matches == 1

    snapshot["review_comments"][0]["body"] = body + " changed"
    changed = audit.audit_snapshot(snapshot)
    assert changed.totals["HIGH"] == 1


def test_exact_immutable_diff_hunk_synthetic_values_are_verified_by_comment_identity():
    audit = load_surface_module()
    empty_name = "sk-" + "empty-tree-name-secret-123456789"
    gitlink_name = "sk-" + "gitlink-name-secret-123456789"
    hunk = (
        "@@ -1,2 +1,2 @@\n"
        f'+empty_name = b"{empty_name}"\n'
        f'+gitlink_name = b"{gitlink_name}"\n'
    )
    base = {
        "path": "tests/test_public_release_audit.py",
        "commit_id": "03c1b3178357923cc80b9a8efa6f5c927b77d720",
        "diff_hunk": hunk,
    }
    snapshot = empty_snapshot()
    snapshot["review_comments"] = [
        {"id": 3901451178, **base},
        {"id": 3901476992, **base},
    ]

    state = audit.audit_snapshot(snapshot)

    assert state.totals["HIGH"] == 0
    assert state.synthetic_fixture_matches == 4

    snapshot["review_comments"].append({"id": 999999, **base})
    unverified = audit.audit_snapshot(snapshot)
    assert unverified.totals["HIGH"] == 2


def test_workflow_metadata_deduplicates_identical_text_but_not_unique_values():
    audit = load_surface_module()
    private_a = "person" + "@" + "private-domain.org"
    private_b = "other" + "@" + "private-domain.org"
    snapshot = empty_snapshot()
    snapshot["workflow_runs"] = [
        {"id": 1, "head_commit": {"author": {"email": private_a}}},
        {"id": 2, "head_commit": {"author": {"email": private_a}}},
        {"id": 3, "head_commit": {"author": {"email": private_b}}},
    ]

    state = audit.audit_snapshot(snapshot)

    email_findings = [finding for finding in state.findings if finding.kind == "email-address"]
    assert len(email_findings) == 2
    assert state.totals["REVIEW"] == 3  # two unique emails + Actions-log review


def test_existing_pull_request_history_requires_content_review():
    audit = load_surface_module()
    snapshot = empty_snapshot()
    snapshot["pull_requests"] = [{"number": 1, "title": "safe", "body": "safe"}]

    state = audit.audit_snapshot(snapshot)

    assert state.totals["REVIEW"] == 1
    assert state.findings[0].kind == "pull-request-history-requires-content-audit"


def test_uninspected_actions_history_requires_review():
    audit = load_surface_module()
    snapshot = empty_snapshot()
    snapshot["workflow_runs"] = [{"id": 123, "name": "CI", "status": "completed"}]

    state = audit.audit_snapshot(snapshot)

    assert state.totals["HIGH"] == 0
    assert state.totals["REVIEW"] == 1
    assert state.findings[0].kind == "actions-history-requires-log-audit"


def test_artifact_metadata_requires_explicit_content_audit():
    audit = load_surface_module()
    snapshot = empty_snapshot()
    snapshot["artifacts"] = [{"id": 99, "name": "private-build-output", "expired": False}]

    state = audit.audit_snapshot(snapshot)

    assert state.totals["REVIEW"] == 1
    assert state.findings[0].kind == "actions-artifact-requires-content-audit"


def test_wiki_pages_or_discussions_require_separate_audit():
    audit = load_surface_module()
    snapshot = empty_snapshot()
    snapshot["repository"] = {"has_wiki": True, "has_pages": True, "has_discussions": True}

    state = audit.audit_snapshot(snapshot)

    assert {finding.kind for finding in state.findings} == {
        "wiki-requires-content-audit",
        "pages-require-content-audit",
        "discussions-require-content-audit",
    }


def test_clean_github_metadata_without_secondary_surfaces_can_pass():
    audit = load_surface_module()
    snapshot = empty_snapshot()

    state = audit.audit_snapshot(snapshot)

    assert state.totals == {"HIGH": 0, "REVIEW": 0}
