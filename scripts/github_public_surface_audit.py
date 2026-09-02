from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping

_LOCAL_AUDIT_PATH = Path(__file__).with_name("public_release_audit.py")
_LOCAL_AUDIT_SPEC = importlib.util.spec_from_file_location("_public_release_audit_v8", _LOCAL_AUDIT_PATH)
if _LOCAL_AUDIT_SPEC is None or _LOCAL_AUDIT_SPEC.loader is None:
    raise RuntimeError("unable to load local public-release audit helpers")
_local = importlib.util.module_from_spec(_LOCAL_AUDIT_SPEC)
sys.modules[_LOCAL_AUDIT_SPEC.name] = _local
_LOCAL_AUDIT_SPEC.loader.exec_module(_local)

AuditState = _local.AuditState
Finding = _local.Finding
MAX_FINDINGS = _local.MAX_FINDINGS
redact_output_text = _local.redact_output_text
scan_bytes = _local.scan_bytes

# GitHub review-comment diff hunks are immutable publication surfaces. These
# three historical comments contain only already-verified diagnostic/test
# signatures, not credentials. Keep the exemption stable-ID and exact-match
# bound so an identical secret-shaped value anywhere else remains HIGH.
_VERIFIED_REVIEW_COMMENT_FIXTURES: dict[int, dict[str, Any]] = {
    3897428879: {
        "field": "body",
        "text_sha256": "98f22c50a917ba927a95fb4796e313a1c2df1350abe35f5cca78f49d71a5ec16",
        "allowed": {
            "private-key": {
                "3021d90eb9437b2d8f30e8363695c4418b5e5f1870801b5c317e9398ee0f572d"
            }
        },
    },
    3901451178: {
        "field": "diff_hunk",
        "path": "tests/test_public_release_audit.py",
        "commit_id": "03c1b3178357923cc80b9a8efa6f5c927b77d720",
        "allowed": {
            "openai-style-token": {
                "d255ee609659d95ee1ee78db7bd23b9074171c8023b0ffaea01e03b14d37560d",
                "2778fc586bc5b5f9602db1c5e0d80bf132580e1a51a165fba544a1b9a1fd8b2b",
            }
        },
    },
    3901476992: {
        "field": "diff_hunk",
        "path": "tests/test_public_release_audit.py",
        "commit_id": "03c1b3178357923cc80b9a8efa6f5c927b77d720",
        "allowed": {
            "openai-style-token": {
                "d255ee609659d95ee1ee78db7bd23b9074171c8023b0ffaea01e03b14d37560d",
                "2778fc586bc5b5f9602db1c5e0d80bf132580e1a51a165fba544a1b9a1fd8b2b",
            }
        },
    },
}


def _iter_text(value: Any, path: str = "root") -> Iterable[tuple[str, str]]:
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, Mapping):
        for key, item in value.items():
            yield from _iter_text(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _iter_text(item, f"{path}[{index}]")


def _mask_verified_synthetic_fixture(data: bytes) -> bytes:
    lowered = data.lower()
    if b"synthetic" not in lowered or b"test" not in lowered:
        return data
    openai_pattern = next(
        pattern for label, pattern in _local.SECRET_PATTERNS if label == "openai-style-token"
    )
    pieces: list[bytes] = []
    cursor = 0
    changed = False
    for match in openai_pattern.finditer(data):
        value = match.group(0)
        if hashlib.sha256(value).hexdigest() != _local.KNOWN_SYNTHETIC_SECRET_SHA256:
            continue
        pieces.append(data[cursor : match.start()])
        pieces.append(b"<VERIFIED_SYNTHETIC_FIXTURE>")
        cursor = match.end()
        changed = True
    if not changed:
        return data
    pieces.append(data[cursor:])
    return b"".join(pieces)


def _mask_exact_secret_digests(
    data: bytes,
    allowed: Mapping[str, set[str]],
) -> tuple[bytes, int]:
    ranges: list[tuple[int, int]] = []
    for label, pattern in _local.SECRET_PATTERNS:
        allowed_digests = allowed.get(label)
        if not allowed_digests:
            continue
        for match in pattern.finditer(data):
            if hashlib.sha256(match.group(0)).hexdigest() in allowed_digests:
                ranges.append((match.start(), match.end()))
    if not ranges:
        return data, 0
    ordered = sorted(set(ranges))
    pieces: list[bytes] = []
    cursor = 0
    count = 0
    for start, end in ordered:
        if start < cursor:
            continue
        pieces.append(data[cursor:start])
        pieces.append(b"<VERIFIED_HISTORICAL_FIXTURE>")
        cursor = end
        count += 1
    pieces.append(data[cursor:])
    return b"".join(pieces), count


def _mask_verified_review_comment_fixture(
    item: Mapping[str, Any],
    field: str,
    data: bytes,
) -> tuple[bytes, int]:
    comment_id = item.get("id")
    if type(comment_id) is not int:
        return data, 0
    rule = _VERIFIED_REVIEW_COMMENT_FIXTURES.get(comment_id)
    if rule is None or rule.get("field") != field:
        return data, 0
    expected_path = rule.get("path")
    if expected_path is not None and item.get("path") != expected_path:
        return data, 0
    expected_commit = rule.get("commit_id")
    if expected_commit is not None and item.get("commit_id") != expected_commit:
        return data, 0
    expected_text_digest = rule.get("text_sha256")
    if expected_text_digest is not None and hashlib.sha256(data).hexdigest() != expected_text_digest:
        return data, 0
    allowed = rule.get("allowed")
    if not isinstance(allowed, Mapping):
        return data, 0
    normalized: dict[str, set[str]] = {}
    for label, digests in allowed.items():
        if isinstance(label, str) and isinstance(digests, set):
            normalized[label] = set(digests)
    return _mask_exact_secret_digests(data, normalized)


def _scan_section(
    state: AuditState,
    section: str,
    value: Any,
    *,
    dedupe_identical_text: bool = False,
) -> None:
    seen_text: set[bytes] = set()
    if isinstance(value, list):
        for index, item in enumerate(value):
            if not isinstance(item, Mapping):
                for path, text in _iter_text(item, f"{section}[{index}]"):
                    data = text.encode("utf-8", "surrogateescape")
                    if dedupe_identical_text and data in seen_text:
                        continue
                    seen_text.add(data)
                    data = _mask_verified_synthetic_fixture(data)
                    scan_bytes(data, f"github:{path}", state)
                continue
            prefix = f"{section}[{index}]"
            for path, text in _iter_text(item, prefix):
                data = text.encode("utf-8", "surrogateescape")
                if dedupe_identical_text and data in seen_text:
                    continue
                seen_text.add(data)
                data = _mask_verified_synthetic_fixture(data)
                field = path.rsplit(".", 1)[-1]
                if section == "review_comments":
                    data, masked = _mask_verified_review_comment_fixture(item, field, data)
                    state.synthetic_fixture_matches += masked
                scan_bytes(data, f"github:{path}", state)
        return

    for path, text in _iter_text(value, section):
        data = text.encode("utf-8", "surrogateescape")
        if dedupe_identical_text and data in seen_text:
            continue
        seen_text.add(data)
        data = _mask_verified_synthetic_fixture(data)
        scan_bytes(data, f"github:{path}", state)


def audit_snapshot(snapshot: Mapping[str, Any]) -> AuditState:
    """Audit GitHub-side metadata and fail closed on uninspected content surfaces."""
    state = AuditState()
    for section in (
        "repository",
        "issues",
        "pull_requests",
        "pull_request_reviews",
        "issue_comments",
        "review_comments",
        "commit_comments",
        "releases",
    ):
        _scan_section(state, section, snapshot.get(section, []))

    repository = snapshot.get("repository", {})
    if isinstance(repository, Mapping):
        if repository.get("has_wiki"):
            state.add(
                Finding(
                    "REVIEW",
                    "wiki-requires-content-audit",
                    "github:wiki",
                    "repository wiki is enabled and is a separate publication surface",
                )
            )
        if repository.get("has_pages"):
            state.add(
                Finding(
                    "REVIEW",
                    "pages-require-content-audit",
                    "github:pages",
                    "GitHub Pages is enabled and its published content requires separate audit",
                )
            )
        if repository.get("has_discussions"):
            state.add(
                Finding(
                    "REVIEW",
                    "discussions-require-content-audit",
                    "github:discussions",
                    "GitHub Discussions are enabled and require separate content audit",
                )
            )

    pull_requests = list(snapshot.get("pull_requests", []))
    if pull_requests:
        state.add(
            Finding(
                "REVIEW",
                "pull-request-history-requires-content-audit",
                "github:pull-requests/commit-history",
                (
                    f"pull_requests={len(pull_requests)}; PR-only commits/refs may remain publicly "
                    "accessible even when feature branches are deleted and require explicit content audit"
                ),
            )
        )

    workflow_runs = list(snapshot.get("workflow_runs", []))
    if workflow_runs:
        # The Actions API repeats the same head-commit strings in many runs.
        # Scan each unique text value once: this preserves detection of every
        # distinct credential/privacy value without turning one repeated value
        # into thousands of identical findings.
        _scan_section(state, "workflow_runs", workflow_runs, dedupe_identical_text=True)
        state.add(
            Finding(
                "REVIEW",
                "actions-history-requires-log-audit",
                "github:actions/runs",
                (
                    f"workflow_runs={len(workflow_runs)}; run/job logs are a separate publication "
                    "surface and require explicit audit before visibility changes"
                ),
            )
        )

    artifacts = list(snapshot.get("artifacts", []))
    if artifacts:
        _scan_section(state, "artifacts", artifacts)
        for index, _artifact in enumerate(artifacts):
            state.add(
                Finding(
                    "REVIEW",
                    "actions-artifact-requires-content-audit",
                    f"github:actions/artifacts[{index}]",
                    "artifact metadata was scanned; artifact payload bytes require explicit audit",
                )
            )

    releases = list(snapshot.get("releases", []))
    release_assets = 0
    for release in releases:
        if isinstance(release, Mapping):
            assets = release.get("assets", [])
            if isinstance(assets, list):
                release_assets += len(assets)
    if release_assets:
        state.add(
            Finding(
                "REVIEW",
                "release-assets-require-content-audit",
                "github:releases/assets",
                f"release_assets={release_assets}; asset payload bytes require explicit audit",
            )
        )
    return state


def _canonical(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def resolve_trusted_gh_executable(
    *,
    path_value: str | None = None,
    audit_root: Path | None = None,
) -> Path:
    root = _canonical(audit_root or Path.cwd())
    raw_path = os.environ.get("PATH", "") if path_value is None else path_value
    candidate_names = ("gh.exe",) if os.name == "nt" else ("gh",)
    for entry in raw_path.split(os.pathsep):
        if not entry:
            continue
        directory = _canonical(Path(entry))
        for name in candidate_names:
            candidate = directory / name
            if not candidate.is_file():
                continue
            resolved = _canonical(candidate)
            if _is_within(resolved, root):
                continue
            if os.name != "nt" and not os.access(resolved, os.X_OK):
                continue
            return resolved
    raise RuntimeError("trusted GitHub CLI executable was not found outside the audited workspace")


def _gh_call(gh: Path, *args: str) -> bytes:
    completed = subprocess.run(
        [str(gh), "api", *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        message = completed.stderr.decode("utf-8", "replace").strip()
        raise RuntimeError(f"gh api failed: {message}")
    return completed.stdout


def _gh_json(gh: Path, endpoint: str) -> Any:
    output = _gh_call(gh, "--paginate", "--slurp", endpoint)
    try:
        return json.loads(output.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"gh api returned invalid JSON for {endpoint}") from exc


def _gh_object(gh: Path, endpoint: str) -> Mapping[str, Any]:
    output = _gh_call(gh, endpoint)
    try:
        value = json.loads(output.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"gh api returned invalid JSON for {endpoint}") from exc
    if not isinstance(value, Mapping):
        raise RuntimeError(f"gh api expected object for {endpoint}")
    return value


def _flatten_pages(value: Any, *, object_key: str | None = None) -> list[Any]:
    if not isinstance(value, list):
        raise RuntimeError("gh --slurp response must be a list of pages")
    result: list[Any] = []
    for page in value:
        if object_key is not None:
            if not isinstance(page, Mapping):
                raise RuntimeError("gh page must be an object")
            items = page.get(object_key, [])
        else:
            items = page
        if not isinstance(items, list):
            raise RuntimeError("gh page collection must be a list")
        result.extend(items)
    return result


def fetch_snapshot(repo: str) -> dict[str, Any]:
    if not repo or "/" not in repo:
        raise RuntimeError("repository must be owner/name")
    gh = resolve_trusted_gh_executable(audit_root=Path.cwd())
    prefix = f"repos/{repo}"
    repository = dict(_gh_object(gh, prefix))
    pull_requests = _flatten_pages(_gh_json(gh, f"{prefix}/pulls?state=all&per_page=100"))
    reviews: list[Any] = []
    for pull in pull_requests:
        if not isinstance(pull, Mapping) or not isinstance(pull.get("number"), int):
            raise RuntimeError("pull request inventory contained an invalid number")
        number = pull["number"]
        reviews.extend(
            _flatten_pages(_gh_json(gh, f"{prefix}/pulls/{number}/reviews?per_page=100"))
        )
    return {
        "repository": repository,
        "issues": _flatten_pages(_gh_json(gh, f"{prefix}/issues?state=all&per_page=100")),
        "pull_requests": pull_requests,
        "pull_request_reviews": reviews,
        "issue_comments": _flatten_pages(_gh_json(gh, f"{prefix}/issues/comments?per_page=100")),
        "review_comments": _flatten_pages(_gh_json(gh, f"{prefix}/pulls/comments?per_page=100")),
        "commit_comments": _flatten_pages(_gh_json(gh, f"{prefix}/comments?per_page=100")),
        "releases": _flatten_pages(_gh_json(gh, f"{prefix}/releases?per_page=100")),
        "workflow_runs": _flatten_pages(
            _gh_json(gh, f"{prefix}/actions/runs?per_page=100"), object_key="workflow_runs"
        ),
        "artifacts": _flatten_pages(
            _gh_json(gh, f"{prefix}/actions/artifacts?per_page=100"), object_key="artifacts"
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Audit GitHub-side surfaces exposed by a private-to-public repository visibility change."
    )
    parser.add_argument("--repo", required=True, help="GitHub repository in owner/name form")
    args = parser.parse_args(argv)

    snapshot = fetch_snapshot(args.repo)
    state = audit_snapshot(snapshot)

    print("GITHUB_PUBLIC_SURFACE_AUDIT v4")
    print(f"issues={len(snapshot['issues'])}")
    print(f"pull_requests={len(snapshot['pull_requests'])}")
    print(f"pull_request_reviews={len(snapshot['pull_request_reviews'])}")
    print(f"issue_comments={len(snapshot['issue_comments'])}")
    print(f"review_comments={len(snapshot['review_comments'])}")
    print(f"commit_comments={len(snapshot['commit_comments'])}")
    print(f"releases={len(snapshot['releases'])}")
    print(f"workflow_runs={len(snapshot['workflow_runs'])}")
    print(f"artifacts={len(snapshot['artifacts'])}")
    print(f"verified_synthetic_fixture_matches={state.synthetic_fixture_matches}")

    print("\nFINDINGS")
    if not state.findings:
        print("none")
    else:
        for index, finding in enumerate(state.findings, start=1):
            print(
                f"{index:03d} [{finding.severity}] {finding.kind} :: "
                f"{finding.location} :: {finding.detail}"
            )
        if state.omitted_findings:
            print(
                f"NOTE: {state.omitted_findings} additional findings omitted "
                f"after display cap {MAX_FINDINGS}"
            )

    high = state.totals.get("HIGH", 0)
    review = state.totals.get("REVIEW", 0)
    print(f"\nSUMMARY high={high} review={review}")
    if high:
        print("RESULT=BLOCK")
        return 2
    if review:
        print("RESULT=REVIEW_REQUIRED")
        return 1
    print("RESULT=PASS")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"AUDIT_ERROR: {redact_output_text(str(exc))}", file=sys.stderr)
        raise SystemExit(3)
