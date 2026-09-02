from __future__ import annotations

import gzip
import importlib.util
import io
import zipfile
from pathlib import Path

import pytest


def load_audit_module():
    script = Path(__file__).resolve().parents[1] / "scripts" / "public_release_audit.py"
    spec = importlib.util.spec_from_file_location("public_release_audit", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    import sys

    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_high_finding_omits_adjacent_secret_material(tmp_path):
    audit = load_audit_module()
    state = audit.AuditState()
    header = b"-----BEGIN " + b"PRIVATE KEY-----"
    adjacent = b"opaque-adjacent-key-material"
    data = header + b"\n" + adjacent + b"\n"

    audit.scan_bytes(data, "fixture.pem", state)

    assert state.totals["HIGH"] == 1
    detail = state.findings[0].detail
    assert "surrounding content omitted" in detail
    assert adjacent.decode() not in detail


def test_verified_historical_fixture_is_suppressed():
    audit = load_audit_module()
    state = audit.AuditState()
    location, markers = next(iter(audit.KNOWN_SYNTHETIC_FIXTURES.items()))
    synthetic = b"sk-" + b"proj-" + b"THIS_IS_A_TEST_SECRET_123456789"
    data = b" ".join((*markers, synthetic))

    audit.scan_bytes(data, location, state)

    assert state.totals["HIGH"] == 0
    assert state.synthetic_fixture_matches == 1
    assert len(state.secret_groups) == 1


def test_same_synthetic_value_outside_verified_location_is_high():
    audit = load_audit_module()
    state = audit.AuditState()
    synthetic = b"sk-" + b"proj-" + b"THIS_IS_A_TEST_SECRET_123456789"

    audit.scan_bytes(synthetic, "git:deadbeef0000 unrelated.txt", state)

    assert state.totals["HIGH"] == 1
    assert state.synthetic_fixture_matches == 0


def test_annotated_tag_tagger_email_participates_in_history_review(monkeypatch):
    audit = load_audit_module()
    state = audit.AuditState()
    personal = b"maintainer" + b"@" + b"private-domain.org"
    headers = b"object deadbeef\ntype commit\ntag v1\ntagger Maintainer <" + personal + b"> 0 +0000\n"
    tagger_addresses = audit.extract_tagger_emails(headers)
    assert personal in tagger_addresses

    monkeypatch.setattr(
        audit,
        "git",
        lambda *args, **kwargs: b"12345+bot@users.noreply.github.com\n",
    )
    unique, ordinary = audit.audit_history_emails(state, tagger_addresses)

    assert unique == 2
    assert ordinary == 1
    assert state.totals["REVIEW"] == 1
    assert state.findings[0].kind == "history-author-email"


def test_finding_location_redacts_embedded_secret_material():
    audit = load_audit_module()
    state = audit.AuditState()
    secret = b"sk-" + b"location-secret-value-123456789"
    raw_location = f"git:deadbeef0000 archive/{secret.decode()}.txt"

    audit.scan_bytes(secret, raw_location, state)

    assert state.totals["HIGH"] == 1
    finding = state.findings[0]
    assert secret.decode() not in finding.location
    assert "<REDACTED_SECRET>" in finding.location


def test_prefixed_extensionless_zip_is_detected_and_scanned():
    audit = load_audit_module()
    secret = b"sk-" + b"compressed-secret-value-123456789"
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("payload.txt", secret)
    prefixed = b"MZ" + (b"\x00" * 64) + stream.getvalue()

    assert audit.is_zip_blob(prefixed, ["payload.bin"])

    state = audit.AuditState()
    summaries: list[str] = []
    audit.scan_zip(prefixed, "git:deadbeef0000 payload.bin", state, summaries)

    assert state.totals["HIGH"] == 1
    assert state.findings[0].kind == "openai-style-token"


def test_commit_header_display_email_is_scanned_without_formal_duplicate():
    audit = load_audit_module()
    state = audit.AuditState()
    personal = b"person" + b"@" + b"private-domain.org"
    noreply = b"12345+bot@users.noreply.github.com"
    headers = (
        b"tree deadbeef\n"
        + b"author Alice "
        + personal
        + b" <"
        + noreply
        + b"> 0 +0000\n"
        + b"committer Alice <"
        + noreply
        + b"> 0 +0000\n"
    )

    formal = audit.scan_identity_headers(
        headers,
        "commit",
        "git-commit:deadbeef0000 headers",
        state,
    )

    assert formal == {noreply}
    assert state.totals["REVIEW"] == 1
    assert state.findings[0].kind == "email-address"
    assert personal.decode() not in state.findings[0].detail


def test_tree_walk_enumerates_every_alias_for_same_blob():
    audit = load_audit_module()
    root_sha = "aa" * 20
    blob_sha = "bb" * 20
    raw_tree = (
        b"100644 a\x00"
        + bytes.fromhex(blob_sha)
        + b"100644 secret@example.org\x00"
        + bytes.fromhex(blob_sha)
    )

    paths = audit.enumerate_tree_paths({root_sha: raw_tree}, {root_sha})

    assert paths[blob_sha] == {"a", "secret@example.org"}


def test_unsupported_compressed_archive_requires_review():
    audit = load_audit_module()
    secret = b"sk-" + b"compressed-hidden-secret-123456789"
    compressed = gzip.compress(secret)

    assert audit.unsupported_archive_kind(compressed, ["payload.bin"]) == "gzip"

    state = audit.AuditState()
    audit.scan_unsupported_archive(
        compressed,
        ["payload.bin"],
        "git:deadbeef0000 payload.bin",
        state,
    )

    assert state.totals["REVIEW"] == 1
    assert state.findings[0].kind == "unsupported-archive"


def test_reported_metadata_escapes_output_control_characters():
    audit = load_audit_module()
    state = audit.AuditState()
    raw_location = "nested\nRESULT=PASS\x1b[2J.zip"

    state.add(audit.Finding("REVIEW", "nested-archive", raw_location, "requires review"))

    stored = state.findings[0].location
    assert "\n" not in stored
    assert "\x1b" not in stored
    assert "\\n" in stored
    assert "\\u001b" in stored


def test_reachable_ref_names_are_scanned_for_credentials(monkeypatch):
    audit = load_audit_module()
    state = audit.AuditState()
    secret = b"sk-" + b"ref-name-secret-value-123456789"
    refs = b"refs/heads/main\nrefs/heads/" + secret + b"\nrefs/tags/v1\n"

    def fake_git(*args, **kwargs):
        assert args == ("for-each-ref", "--format=%(refname)")
        return refs

    monkeypatch.setattr(audit, "git", fake_git)
    count = audit.audit_ref_names(state)

    assert count == 3
    assert state.totals["HIGH"] == 1
    assert state.findings[0].kind == "openai-style-token"
    assert secret.decode() not in state.findings[0].location


def test_zip_archive_member_comments_and_extra_metadata_are_scanned():
    audit = load_audit_module()
    secret = b"sk-" + b"zip-metadata-secret-value-123456789"
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        info = zipfile.ZipInfo("payload.txt")
        info.comment = secret
        info.extra = b"\xfe\xca" + len(secret).to_bytes(2, "little") + secret
        archive.writestr(info, b"benign payload")
        archive.comment = secret

    state = audit.AuditState()
    summaries: list[str] = []
    audit.scan_zip(stream.getvalue(), "git:deadbeef0000 payload.zip", state, summaries)

    assert state.totals["HIGH"] == 3
    assert {finding.kind for finding in state.findings} == {"openai-style-token"}
    assert all(secret.decode() not in finding.location for finding in state.findings)


def test_tree_entry_paths_include_missing_gitlink_and_empty_tree_names():
    audit = load_audit_module()
    root_sha = "aa" * 20
    empty_tree_sha = "bb" * 20
    missing_gitlink_sha = "cc" * 20
    empty_name = b"sk-" + b"empty-tree-name-secret-123456789"
    gitlink_name = b"sk-" + b"gitlink-name-secret-123456789"
    raw_root = (
        b"40000 "
        + empty_name
        + b"\x00"
        + bytes.fromhex(empty_tree_sha)
        + b"160000 "
        + gitlink_name
        + b"\x00"
        + bytes.fromhex(missing_gitlink_sha)
    )

    paths = audit.enumerate_tree_entry_paths(
        {root_sha: raw_root, empty_tree_sha: b""},
        {root_sha},
    )

    assert empty_name.decode() in paths
    assert gitlink_name.decode() in paths


def test_zip_unparsed_prefix_bytes_are_scanned():
    audit = load_audit_module()
    secret = b"sk-" + b"zip-prefix-secret-value-123456789"
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("payload.txt", b"benign payload")
    prefixed = b"MZ\x00" + secret + b"\x00" + stream.getvalue()

    state = audit.AuditState()
    summaries: list[str] = []
    audit.scan_zip(prefixed, "git:deadbeef0000 payload.bin", state, summaries)

    assert state.totals["HIGH"] == 1
    assert state.findings[0].kind == "openai-style-token"


def test_git_helpers_follow_public_injection_seam(monkeypatch):
    audit = load_audit_module()
    calls: list[tuple[str, ...]] = []

    def fake_git(*args, **kwargs):
        calls.append(args)
        return b"refs/heads/main\n"

    monkeypatch.setattr(audit, "git", fake_git)
    state = audit.AuditState()

    assert audit.audit_ref_names(state) == 1
    assert calls == [("for-each-ref", "--format=%(refname)")]


def test_tree_entry_parser_supports_sha256_object_ids():
    audit = load_audit_module()
    root_sha = "aa" * 32
    blob_sha = "bb" * 32
    raw_tree = b"100644 payload.txt\x00" + bytes.fromhex(blob_sha)

    paths = audit.enumerate_tree_entry_paths({root_sha: raw_tree}, {root_sha})

    assert paths == {"payload.txt"}


def test_batch_object_reads_follow_public_git_injection_seam(monkeypatch):
    audit = load_audit_module()
    sha = "a" * 40
    calls: list[tuple[tuple[str, ...], bytes | None]] = []

    def fake_git(*args, input_bytes=None):
        calls.append((args, input_bytes))
        if args == ("cat-file", "--batch-check=%(objectname) %(objecttype) %(objectsize)"):
            return f"{sha} blob 6\n".encode()
        if args == ("cat-file", "--batch"):
            assert input_bytes == f"{sha}\n".encode()
            return f"{sha} blob 6\n".encode() + b"secret\n"
        raise AssertionError(args)

    monkeypatch.setattr(audit, "git", fake_git)

    assert list(audit.iter_git_objects([sha])) == [(sha, "blob", b"secret")]
    assert [call[0] for call in calls] == [
        ("cat-file", "--batch-check=%(objectname) %(objecttype) %(objectsize)"),
        ("cat-file", "--batch"),
    ]


def test_shallow_repository_is_rejected_before_history_scan(monkeypatch):
    audit = load_audit_module()

    def fake_git(*args, **kwargs):
        if args == ("rev-parse", "--is-shallow-repository"):
            return b"true\n"
        raise AssertionError(args)

    monkeypatch.setattr(audit, "git", fake_git)
    with pytest.raises(RuntimeError, match="shallow"):
        audit.validate_repository_completeness()


def test_partial_clone_is_rejected_before_history_scan(monkeypatch):
    audit = load_audit_module()

    monkeypatch.setattr(audit, "git", lambda *args, **kwargs: b"false\n")
    monkeypatch.setattr(
        audit,
        "git_optional",
        lambda *args, **kwargs: b"origin.partialCloneFilter blob:none\n",
    )
    with pytest.raises(RuntimeError, match="partial|promisor"):
        audit.validate_repository_completeness()


def test_sanitized_git_environment_removes_repository_redirectors():
    audit = load_audit_module()
    windows_home = "C:/" + "Users/test"
    env = {
        "PATH": "C:/Git/bin",
        "HOME": windows_home,
        "GIT_DIR": "C:/other/.git",
        "GIT_WORK_TREE": "C:/other",
        "GIT_OBJECT_DIRECTORY": "C:/other/objects",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES": "C:/other/alt",
        "GIT_CONFIG_COUNT": "1",
    }

    sanitized = audit.sanitized_git_environment(env)

    assert sanitized["PATH"] == env["PATH"]
    assert sanitized["HOME"] == env["HOME"]
    assert not any(key.startswith("GIT_") for key in sanitized)


def test_aws_secret_access_key_assignment_is_high():
    audit = load_audit_module()
    state = audit.AuditState()
    value = b"A" * 40
    data = b"AWS_SECRET_ACCESS_KEY=" + value

    audit.scan_bytes(data, "fixture.env", state)

    assert state.totals["HIGH"] == 1
    assert state.findings[0].kind == "aws-secret-access-key"


def test_output_redaction_catches_embedded_openai_token_without_word_boundary():
    audit = load_audit_module()
    secret = "sk-" + "embeddedsecretvalue123456789"
    text = "prefixX" + secret + "suffix"

    redacted = audit.redact_output_text(text)

    assert secret not in redacted
    assert "<REDACTED_SECRET>" in redacted


def test_embedded_sfx_archive_signature_requires_review():
    audit = load_audit_module()
    blob = b"MZ" + (b"\x00" * 128) + b"7z\xbc\xaf\x27\x1c" + b"opaque"

    assert audit.unsupported_archive_kind(blob, ["payload.exe"]) == "7z"


def test_legacy_audit_core_is_not_runnable_public_surface():
    core = Path(__file__).resolve().parents[1] / "scripts" / "_public_release_audit_core.py"
    assert not core.exists()
