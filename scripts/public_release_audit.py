from __future__ import annotations

import hashlib
import io
import os
import re
import subprocess
import sys
import unicodedata
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Iterable, Mapping

MAX_BLOB_BYTES = 5 * 1024 * 1024
MAX_ZIP_MEMBER_BYTES = 5 * 1024 * 1024
MAX_ZIP_TOTAL_BYTES = 50 * 1024 * 1024
MAX_TREE_PATH_ENTRIES = 1_000_000
MAX_TREE_DEPTH = 128
MAX_FINDINGS = 200
MAX_BATCH_BYTES = 8 * 1024 * 1024
MAX_BATCH_OBJECTS = 64
ARCHIVE_SIGNATURE_SCAN_BYTES = 1024 * 1024

SECRET_PATTERNS: tuple[tuple[str, re.Pattern[bytes]], ...] = (
    ("private-key", re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----")),
    ("github-token", re.compile(rb"(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})")),
    ("openai-style-token", re.compile(rb"sk-[A-Za-z0-9_-]{20,}")),
    ("aws-access-key", re.compile(rb"AKIA[0-9A-Z]{16}")),
    (
        "aws-secret-access-key",
        re.compile(
            rb"(?i)AWS_SECRET_ACCESS_KEY[ \t]*[:=][ \t]*[\"']?[A-Za-z0-9/+=]{32,}"
        ),
    ),
    ("slack-token", re.compile(rb"xox[baprs]-[A-Za-z0-9-]{20,}")),
    (
        "jwt-like-token",
        re.compile(rb"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    ),
    (
        "bearer-token",
        re.compile(rb"(?i)Bearer[ \t]+[A-Za-z0-9._~+/-]{24,}"),
    ),
)

# Output redaction is intentionally at least as broad as detection and does not
# rely on lexical word boundaries. A credential embedded in an untrusted path,
# exception, or metadata field must never be echoed simply because adjacent
# bytes happen to be alphanumeric.
OUTPUT_SECRET_PATTERNS: tuple[re.Pattern[bytes], ...] = tuple(
    pattern for _label, pattern in SECRET_PATTERNS
)

EMAIL_RE = re.compile(
    rb"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?![A-Za-z0-9.-])"
)
WINDOWS_HOME_RE = re.compile(rb"(?i)\b[A-Z]:\\Users\\[^\\\r\n\t ]+")
POSIX_HOME_RE = re.compile(rb"(?<![A-Za-z0-9_])/(?:home|Users)/[^/\r\n\t ]+")
SAFE_EMAIL_DOMAINS = {b"example.com", b"example.org", b"example.net"}
SAFE_EMAIL_DOMAIN_SUFFIXES = (b".invalid", b".test", b".example")
SAFE_EMAIL_VALUES = {
    b"git@github.com",
    b"git@gitlab.com",
    b"git@bitbucket.org",
    b"noreply@github.com",
}

UNSUPPORTED_ARCHIVE_SUFFIXES: tuple[tuple[str, str], ...] = (
    (".tar.gz", "tar+gzip"),
    (".tgz", "tar+gzip"),
    (".tar.xz", "tar+xz"),
    (".txz", "tar+xz"),
    (".tar.bz2", "tar+bzip2"),
    (".tbz2", "tar+bzip2"),
    (".tar.zst", "tar+zstd"),
    (".tar", "tar"),
    (".gz", "gzip"),
    (".xz", "xz"),
    (".bz2", "bzip2"),
    (".zst", "zstd"),
    (".7z", "7z"),
    (".rar", "rar"),
)

# These signatures are searched inside the first bounded prefix as well as at
# offset zero. That makes self-extracting/prefixed containers fail closed.
UNSUPPORTED_ARCHIVE_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\x1f\x8b", "gzip"),
    (b"\xfd7zXZ\x00", "xz"),
    (b"BZh", "bzip2"),
    (b"7z\xbc\xaf\x27\x1c", "7z"),
    (b"Rar!\x1a\x07", "rar"),
    (b"\x28\xb5\x2f\xfd", "zstd"),
)

KNOWN_SYNTHETIC_SECRET_SHA256 = (
    "a6619dc5f8295943c364b3f71e8679c99e5bdfe69e8e3833bba8df8a37aff72a"
)
KNOWN_SYNTHETIC_FIXTURES: dict[str, tuple[bytes, ...]] = {
    (
        "git:6e2b2f973699 experimental/context-summarizer/evaluations/"
        "v0.2.1b-focused-regression.zip!/"
        "cma_context_summarizer_v0.2.1b_focused_regression_pack/scenarios/scenario_07.txt"
    ): (b"environment: staging", b"deploy.example.test", b"ghp_TESTTOKEN"),
    (
        "git:8f7c137f27ae experimental/context-summarizer/evaluations/"
        "v0.1-test-pack.zip!/"
        "cma_summarizer_test_pack_v0.1/manifest.json"
    ): (b'"forbidden_literals"', b"ghp_TESTTOKEN"),
    (
        "git:8f7c137f27ae experimental/context-summarizer/evaluations/"
        "v0.1-test-pack.zip!/"
        "cma_summarizer_test_pack_v0.1/scenarios/scenario_07.txt"
    ): (b"environment: staging", b"deploy.example.test", b"ghp_TESTTOKEN"),
}

_GIT_EXECUTABLE: Path | None = None
_GIT_ROOT: Path | None = None
_GIT_ENV: dict[str, str] | None = None


@dataclass(frozen=True)
class Finding:
    severity: str
    kind: str
    location: str
    detail: str


@dataclass
class AuditState:
    findings: list[Finding] = field(default_factory=list)
    totals: dict[str, int] = field(default_factory=lambda: {"HIGH": 0, "REVIEW": 0})
    omitted_findings: int = 0
    secret_groups: dict[bytes, int] = field(default_factory=dict)
    synthetic_fixture_matches: int = 0

    def add(self, finding: Finding) -> None:
        safe_finding = Finding(
            finding.severity,
            finding.kind,
            redact_output_text(finding.location),
            redact_output_text(finding.detail),
        )
        self.totals[safe_finding.severity] = self.totals.get(safe_finding.severity, 0) + 1
        if len(self.findings) < MAX_FINDINGS:
            self.findings.append(safe_finding)
        else:
            self.omitted_findings += 1

    def secret_group(self, value: bytes) -> int:
        group = self.secret_groups.get(value)
        if group is None:
            group = len(self.secret_groups) + 1
            self.secret_groups[value] = group
        return group


class _OversizedGitObject:
    __slots__ = ("size",)

    def __init__(self, size: int) -> None:
        self.size = size

    def __len__(self) -> int:
        return self.size


def sanitized_git_environment(env: Mapping[str, str]) -> dict[str, str]:
    """Remove every process-level Git override before repository inspection."""
    return {key: value for key, value in env.items() if not key.upper().startswith("GIT_")}


def _canonical(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def resolve_trusted_git_executable(
    *,
    path_value: str | None = None,
    audit_root: Path | None = None,
) -> Path:
    """Resolve Git only from explicit PATH directories, never cwd/workspace lookup."""
    root = _canonical(audit_root or Path.cwd())
    raw_path = os.environ.get("PATH", "") if path_value is None else path_value
    candidate_names = ("git.exe",) if os.name == "nt" else ("git",)

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
    raise RuntimeError("trusted Git executable was not found outside the audited workspace")


def _run_configured_git(
    *args: str,
    input_bytes: bytes | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    if _GIT_EXECUTABLE is None or _GIT_ROOT is None or _GIT_ENV is None:
        configure_git_runtime()
    assert _GIT_EXECUTABLE is not None
    assert _GIT_ROOT is not None
    assert _GIT_ENV is not None
    completed = subprocess.run(
        [str(_GIT_EXECUTABLE), "-C", str(_GIT_ROOT), *args],
        cwd=str(_GIT_ROOT),
        env=_GIT_ENV,
        input=input_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if check and completed.returncode != 0:
        message = completed.stderr.decode("utf-8", "replace").strip()
        raise RuntimeError(f"git {' '.join(args)} failed: {message}")
    return completed


def configure_git_runtime(audit_root: Path | None = None) -> Path:
    """Pin one Git executable and one canonical repository root for the whole audit."""
    global _GIT_EXECUTABLE, _GIT_ROOT, _GIT_ENV
    root = _canonical(audit_root or Path.cwd())
    executable = resolve_trusted_git_executable(audit_root=root)
    env = sanitized_git_environment(os.environ)

    old_executable, old_root, old_env = _GIT_EXECUTABLE, _GIT_ROOT, _GIT_ENV
    _GIT_EXECUTABLE, _GIT_ROOT, _GIT_ENV = executable, root, env
    try:
        top = _run_configured_git("rev-parse", "--show-toplevel").stdout.decode(
            "utf-8", "surrogateescape"
        ).strip()
        if _canonical(Path(top)) != root:
            raise RuntimeError(
                "audit must run from the canonical repository root; Git resolved a different worktree"
            )
    except BaseException:
        _GIT_EXECUTABLE, _GIT_ROOT, _GIT_ENV = old_executable, old_root, old_env
        raise
    return root


def git(*args: str, input_bytes: bytes | None = None) -> bytes:
    return _run_configured_git(*args, input_bytes=input_bytes, check=True).stdout


def git_optional(*args: str, input_bytes: bytes | None = None) -> bytes:
    completed = _run_configured_git(*args, input_bytes=input_bytes, check=False)
    if completed.returncode != 0:
        return b""
    return completed.stdout


def validate_repository_completeness() -> None:
    shallow = git("rev-parse", "--is-shallow-repository").strip().lower()
    if shallow != b"false":
        raise RuntimeError("shallow repository is not valid public-release audit evidence")

    partial_extension = git_optional("config", "--local", "--get", "extensions.partialClone").strip()
    promisor = git_optional(
        "config", "--local", "--get-regexp", r"^remote\..*\.promisor$"
    ).strip()
    partial_filter = git_optional(
        "config", "--local", "--get-regexp", r"^remote\..*\.partialclonefilter$"
    ).strip()
    if partial_extension or promisor or partial_filter:
        raise RuntimeError("partial/promisor repository is not valid public-release audit evidence")


def line_number(data: bytes, offset: int) -> int:
    return data.count(b"\n", 0, offset) + 1


def is_safe_email(address: bytes) -> bool:
    lowered = address.lower()
    domain = lowered.rsplit(b"@", 1)[-1]
    return (
        lowered in SAFE_EMAIL_VALUES
        or domain in SAFE_EMAIL_DOMAINS
        or domain.endswith(SAFE_EMAIL_DOMAIN_SUFFIXES)
        or domain == b"users.noreply.github.com"
    )


def is_noreply_history_email(address: bytes) -> bool:
    lowered = address.lower()
    return lowered == b"noreply@github.com" or lowered.endswith(b"@users.noreply.github.com")


def escape_output_controls(text: str) -> str:
    escaped: list[str] = []
    for character in text:
        codepoint = ord(character)
        category = unicodedata.category(character)
        if character == "\n":
            escaped.append("\\n")
        elif character == "\r":
            escaped.append("\\r")
        elif character == "\t":
            escaped.append("\\t")
        elif category.startswith("C") or character in {"\u2028", "\u2029"}:
            if codepoint <= 0xFFFF:
                escaped.append(f"\\u{codepoint:04x}")
            else:
                escaped.append(f"\\U{codepoint:08x}")
        else:
            escaped.append(character)
    return "".join(escaped)


def redact_output_text(text: str) -> str:
    data = text.encode("utf-8", "surrogateescape")
    for pattern in OUTPUT_SECRET_PATTERNS:
        data = pattern.sub(b"<REDACTED_SECRET>", data)
    data = EMAIL_RE.sub(b"<REDACTED_EMAIL>", data)
    data = WINDOWS_HOME_RE.sub(b"<REDACTED_HOME>", data)
    data = POSIX_HOME_RE.sub(b"<REDACTED_HOME>", data)
    return escape_output_controls(data.decode("utf-8", "replace"))


def is_verified_synthetic_secret(
    *,
    label: str,
    value: bytes,
    data: bytes,
    location: str,
) -> bool:
    required_markers = KNOWN_SYNTHETIC_FIXTURES.get(location)
    if label != "openai-style-token" or required_markers is None:
        return False
    if hashlib.sha256(value).hexdigest() != KNOWN_SYNTHETIC_SECRET_SHA256:
        return False
    return all(marker in data for marker in required_markers)


def scan_bytes(
    data: bytes,
    location: str,
    state: AuditState,
    *,
    scan_emails: bool = True,
) -> None:
    for label, pattern in SECRET_PATTERNS:
        for match in pattern.finditer(data):
            value = match.group(0)
            group = state.secret_group(value)
            if is_verified_synthetic_secret(
                label=label,
                value=value,
                data=data,
                location=location,
            ):
                state.synthetic_fixture_matches += 1
                continue
            state.add(
                Finding(
                    "HIGH",
                    label,
                    location,
                    (
                        f"signature at byte {match.start()} "
                        f"(line {line_number(data, match.start())}); "
                        f"secret_group={group}; value redacted; surrounding content omitted"
                    ),
                )
            )

    if scan_emails:
        for match in EMAIL_RE.finditer(data):
            address = match.group(0)
            if is_safe_email(address):
                continue
            state.add(
                Finding(
                    "REVIEW",
                    "email-address",
                    location,
                    (
                        f"email-like value at byte {match.start()} "
                        f"(line {line_number(data, match.start())}); value redacted"
                    ),
                )
            )

    for label, pattern in (
        ("windows-home-path", WINDOWS_HOME_RE),
        ("posix-home-path", POSIX_HOME_RE),
    ):
        for match in pattern.finditer(data):
            state.add(
                Finding(
                    "REVIEW",
                    label,
                    location,
                    (
                        f"user-home-like path at byte {match.start()} "
                        f"(line {line_number(data, match.start())}); value redacted"
                    ),
                )
            )


def identity_prefixes(object_type: str) -> tuple[bytes, ...]:
    if object_type == "commit":
        return (b"author ", b"committer ")
    if object_type == "tag":
        return (b"tagger ",)
    return ()


def formal_identity_email_match(line: bytes) -> re.Match[bytes] | None:
    left = line.rfind(b"<")
    if left < 0:
        return None
    right = line.find(b">", left + 1)
    if right < 0:
        return None
    matches = list(EMAIL_RE.finditer(line, left + 1, right))
    if len(matches) != 1:
        return None
    return matches[0]


def scan_identity_headers(
    headers: bytes,
    object_type: str,
    location: str,
    state: AuditState,
) -> set[bytes]:
    scan_bytes(headers, location, state, scan_emails=False)
    prefixes = identity_prefixes(object_type)
    formal_addresses: set[bytes] = set()
    for header_line_number, line in enumerate(headers.splitlines(), start=1):
        formal_match: re.Match[bytes] | None = None
        if any(line.startswith(prefix) for prefix in prefixes):
            formal_match = formal_identity_email_match(line)
            if formal_match is not None:
                formal_addresses.add(formal_match.group(0))
        for match in EMAIL_RE.finditer(line):
            if formal_match is not None and match.span() == formal_match.span():
                continue
            if is_safe_email(match.group(0)):
                continue
            state.add(
                Finding(
                    "REVIEW",
                    "email-address",
                    location,
                    (
                        "email-like value in commit/tag header metadata at "
                        f"header line {header_line_number}; value redacted"
                    ),
                )
            )
    return formal_addresses


def extract_tagger_emails(headers: bytes) -> set[bytes]:
    addresses: set[bytes] = set()
    for line in headers.splitlines():
        if not line.startswith(b"tagger "):
            continue
        match = formal_identity_email_match(line)
        if match is not None:
            addresses.add(match.group(0))
    return addresses


def is_safe_zip_path(name: str) -> bool:
    path = PurePosixPath(name.replace("\\", "/"))
    return not path.is_absolute() and ".." not in path.parts


def is_zip_blob(blob: bytes, paths: Iterable[str]) -> bool:
    path_list = tuple(paths)
    if any(path.lower().endswith(".zip") for path in path_list):
        return True
    if blob.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")):
        return True
    try:
        return zipfile.is_zipfile(io.BytesIO(blob))
    except (OSError, ValueError):
        return False


def unsupported_archive_kind(blob: bytes, paths: Iterable[str]) -> str | None:
    prefix = blob[:ARCHIVE_SIGNATURE_SCAN_BYTES]
    for signature, kind in UNSUPPORTED_ARCHIVE_SIGNATURES:
        if signature in prefix:
            return kind
    # Tar magic is relative to the start of a tar stream; searching the bounded
    # prefix also catches a tar stream after a small self-extracting stub.
    if b"ustar" in prefix:
        return "tar"

    lowered_paths = [path.lower() for path in paths]
    for suffix, kind in UNSUPPORTED_ARCHIVE_SUFFIXES:
        if any(path.endswith(suffix) for path in lowered_paths):
            return kind
    return None


def scan_unsupported_archive(
    blob: bytes,
    paths: Iterable[str],
    location: str,
    state: AuditState,
) -> bool:
    path_list = tuple(paths)
    kind = unsupported_archive_kind(blob, path_list)
    if kind is None:
        return False
    state.add(
        Finding(
            "REVIEW",
            "unsupported-archive",
            location,
            f"archive format={kind}; compressed/container contents require separate audit",
        )
    )
    scan_bytes(blob, location, state)
    return True


def _local_zip_record_end(blob: bytes, info: zipfile.ZipInfo) -> int | None:
    offset = info.header_offset
    if offset < 0 or offset + 30 > len(blob) or blob[offset : offset + 4] != b"PK\x03\x04":
        return None
    name_length = int.from_bytes(blob[offset + 26 : offset + 28], "little")
    extra_length = int.from_bytes(blob[offset + 28 : offset + 30], "little")
    end = offset + 30 + name_length + extra_length + info.compress_size
    if end > len(blob):
        return None
    return end


def _scan_local_zip_metadata(
    blob: bytes,
    info: zipfile.ZipInfo,
    identity: str,
    member_index: int,
    state: AuditState,
) -> None:
    offset = info.header_offset
    if offset < 0 or offset + 30 > len(blob) or blob[offset : offset + 4] != b"PK\x03\x04":
        state.add(
            Finding(
                "REVIEW",
                "unreadable-zip-local-metadata",
                f"{identity}!/<local-metadata:{member_index}>",
                "local file header could not be validated",
            )
        )
        return
    name_length = int.from_bytes(blob[offset + 26 : offset + 28], "little")
    extra_length = int.from_bytes(blob[offset + 28 : offset + 30], "little")
    metadata_start = offset + 30
    name_end = metadata_start + name_length
    extra_end = name_end + extra_length
    if extra_end > len(blob):
        state.add(
            Finding(
                "REVIEW",
                "unreadable-zip-local-metadata",
                f"{identity}!/<local-metadata:{member_index}>",
                "local filename/extra fields are truncated",
            )
        )
        return
    local_name = blob[metadata_start:name_end]
    local_extra = blob[name_end:extra_end]
    encoding = "utf-8" if info.flag_bits & 0x800 else "cp437"
    try:
        local_name_matches_central = local_name.decode(encoding) == info.filename
    except UnicodeDecodeError:
        local_name_matches_central = False
    if not local_name_matches_central:
        scan_bytes(
            local_name,
            f"{identity}!/<local-member-name-metadata:{member_index}>",
            state,
        )
    if local_extra and local_extra != info.extra:
        scan_bytes(
            local_extra,
            f"{identity}!/<local-member-extra-metadata:{member_index}>",
            state,
        )


def _scan_unparsed_zip_regions(
    blob: bytes,
    archive: zipfile.ZipFile,
    identity: str,
    state: AuditState,
) -> None:
    infos = sorted(archive.infolist(), key=lambda item: item.header_offset)
    cursor = 0
    start_dir = max(0, int(getattr(archive, "start_dir", len(blob))))
    for member_index, info in enumerate(infos):
        offset = info.header_offset
        if 0 <= cursor < offset <= len(blob):
            scan_bytes(
                blob[cursor:offset],
                f"{identity}!/<unparsed-container-region:{member_index}>",
                state,
            )
        record_end = _local_zip_record_end(blob, info)
        if record_end is not None:
            cursor = max(cursor, record_end)
    if cursor < start_dir <= len(blob):
        scan_bytes(
            blob[cursor:start_dir],
            f"{identity}!/<unparsed-container-region:before-central-directory>",
            state,
        )
    eocd = blob.rfind(b"PK\x05\x06", start_dir)
    if eocd >= 0 and eocd + 22 <= len(blob):
        comment_length = int.from_bytes(blob[eocd + 20 : eocd + 22], "little")
        archive_end = eocd + 22 + comment_length
        if archive_end < len(blob):
            scan_bytes(
                blob[archive_end:],
                f"{identity}!/<unparsed-container-region:trailing-bytes>",
                state,
            )


def _scan_zip_members(
    archive: zipfile.ZipFile,
    identity: str,
    state: AuditState,
    archive_summaries: list[str],
) -> None:
    members = archive.infolist()
    archive_summaries.append(f"{redact_output_text(identity)}: members={len(members)}")
    total = 0
    for info in members:
        member_location = f"{identity}!/{info.filename}"
        scan_bytes(
            info.filename.encode("utf-8", "surrogateescape"),
            f"{identity}!/<member-name-metadata>",
            state,
        )
        if not is_safe_zip_path(info.filename):
            state.add(
                Finding("HIGH", "unsafe-zip-path", member_location, "absolute or parent-traversal path")
            )
        if info.flag_bits & 0x1:
            state.add(
                Finding("REVIEW", "encrypted-zip-member", member_location, "content could not be audited")
            )
            continue
        if info.is_dir():
            continue
        total += info.file_size
        if info.file_size > MAX_ZIP_MEMBER_BYTES:
            state.add(
                Finding(
                    "REVIEW",
                    "oversized-zip-member",
                    member_location,
                    f"uncompressed bytes={info.file_size}",
                )
            )
            continue
        if total > MAX_ZIP_TOTAL_BYTES:
            state.add(
                Finding(
                    "REVIEW",
                    "zip-budget-exceeded",
                    identity,
                    f"uncompressed bytes>{MAX_ZIP_TOTAL_BYTES}",
                )
            )
            break
        if info.compress_size and info.file_size / max(info.compress_size, 1) > 1000:
            state.add(
                Finding("REVIEW", "extreme-zip-ratio", member_location, "compression ratio >1000")
            )
            continue
        try:
            member_data = archive.read(info)
        except (RuntimeError, NotImplementedError, zipfile.BadZipFile, OSError) as exc:
            state.add(
                Finding("REVIEW", "unreadable-zip-member", member_location, type(exc).__name__)
            )
            continue
        scan_bytes(member_data, member_location, state)
        if unsupported_archive_kind(member_data, [info.filename]) is not None or is_zip_blob(
            member_data, [info.filename]
        ):
            state.add(
                Finding(
                    "REVIEW",
                    "nested-archive",
                    member_location,
                    "nested archive requires separate audit",
                )
            )


def scan_zip(
    blob: bytes,
    identity: str,
    state: AuditState,
    archive_summaries: list[str],
) -> None:
    try:
        archive = zipfile.ZipFile(io.BytesIO(blob))
    except (zipfile.BadZipFile, ValueError) as exc:
        state.add(Finding("HIGH", "invalid-zip", identity, type(exc).__name__))
        return
    with archive:
        _scan_unparsed_zip_regions(blob, archive, identity, state)
        if archive.comment:
            scan_bytes(archive.comment, f"{identity}!/<archive-comment-metadata>", state)
        for member_index, info in enumerate(archive.infolist()):
            if info.comment:
                scan_bytes(
                    info.comment,
                    f"{identity}!/<member-comment-metadata:{member_index}>",
                    state,
                )
            if info.extra:
                scan_bytes(
                    info.extra,
                    f"{identity}!/<member-extra-metadata:{member_index}>",
                    state,
                )
            _scan_local_zip_metadata(blob, info, identity, member_index, state)
        _scan_zip_members(archive, identity, state, archive_summaries)


def reachable_object_shas() -> list[str]:
    output = git("rev-list", "--objects", "--all")
    shas: list[str] = []
    for line in output.splitlines():
        sha = line.split(b" ", 1)[0].decode("ascii")
        shas.append(sha)
    return sorted(set(shas))


def reachable_object_types(shas: list[str]) -> dict[str, str]:
    if not shas:
        return {}
    payload = b"".join(sha.encode("ascii") + b"\n" for sha in shas)
    output = git("cat-file", "--batch-check=%(objectname) %(objecttype)", input_bytes=payload)
    result: dict[str, str] = {}
    for line in output.splitlines():
        fields = line.split()
        if len(fields) != 2:
            raise RuntimeError(f"unexpected git cat-file --batch-check line: {line!r}")
        result[fields[0].decode("ascii")] = fields[1].decode("ascii")
    if set(result) != set(shas):
        raise RuntimeError("git object type inventory did not cover every reachable object")
    return result


def _batch_object_inventory(shas: list[str]) -> list[tuple[str, str, int]]:
    if not shas:
        return []
    payload = b"".join(sha.encode("ascii") + b"\n" for sha in shas)
    output = git(
        "cat-file",
        "--batch-check=%(objectname) %(objecttype) %(objectsize)",
        input_bytes=payload,
    )
    lines = output.splitlines()
    if len(lines) != len(shas):
        raise RuntimeError("git batch object inventory length mismatch")
    inventory: list[tuple[str, str, int]] = []
    for requested_sha, line in zip(shas, lines, strict=True):
        fields = line.split()
        if len(fields) == 2 and fields[1] == b"missing":
            raise RuntimeError(f"git object missing: {requested_sha}")
        if len(fields) != 3:
            raise RuntimeError(f"unexpected git cat-file --batch-check line: {line!r}")
        object_sha = fields[0].decode("ascii")
        object_type = fields[1].decode("ascii")
        try:
            size = int(fields[2])
        except ValueError as exc:
            raise RuntimeError(f"invalid git object size: {line!r}") from exc
        if object_sha != requested_sha:
            raise RuntimeError(
                f"git batch object inventory mismatch: expected {requested_sha}, got {object_sha}"
            )
        inventory.append((object_sha, object_type, size))
    return inventory


def _read_git_batch_chunk(records: list[tuple[str, str, int]]):
    payload = b"".join(sha.encode("ascii") + b"\n" for sha, _type, _size in records)
    output = git("cat-file", "--batch", input_bytes=payload)
    stream = io.BytesIO(output)
    for expected_sha, expected_type, expected_size in records:
        header = stream.readline()
        if not header:
            raise RuntimeError(f"git cat-file ended before {expected_sha}")
        fields = header.rstrip(b"\n").split()
        if len(fields) != 3:
            raise RuntimeError(f"unexpected git cat-file header: {header!r}")
        object_sha = fields[0].decode("ascii")
        object_type = fields[1].decode("ascii")
        size = int(fields[2])
        if (object_sha, object_type, size) != (expected_sha, expected_type, expected_size):
            raise RuntimeError("git batch object response did not match preflight inventory")
        data = stream.read(size)
        trailing = stream.read(1)
        if len(data) != size or trailing != b"\n":
            raise RuntimeError(f"truncated git object: {object_sha}")
        yield object_sha, object_type, data
    if stream.read(1):
        raise RuntimeError("unexpected trailing bytes from git cat-file --batch")


def iter_git_objects(shas: list[str]):
    inventory = _batch_object_inventory(shas)
    chunk: list[tuple[str, str, int]] = []
    chunk_bytes = 0

    def flush_chunk():
        nonlocal chunk, chunk_bytes
        if not chunk:
            return []
        records = list(_read_git_batch_chunk(chunk))
        chunk = []
        chunk_bytes = 0
        return records

    for record in inventory:
        sha, object_type, size = record
        if object_type == "blob" and size > MAX_BLOB_BYTES:
            for item in flush_chunk():
                yield item
            yield sha, object_type, _OversizedGitObject(size)
            continue
        if chunk and (len(chunk) >= MAX_BATCH_OBJECTS or chunk_bytes + size > MAX_BATCH_BYTES):
            for item in flush_chunk():
                yield item
        chunk.append(record)
        chunk_bytes += size
    for item in flush_chunk():
        yield item


def reachable_tree_roots() -> set[str]:
    roots: set[str] = set()
    history = git("rev-list", "--all", "--format=%T")
    for line in history.splitlines():
        candidate = line.strip()
        if re.fullmatch(rb"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", candidate):
            roots.add(candidate.decode("ascii").lower())
    refs = git("for-each-ref", "--format=%(refname)")
    for raw_ref in refs.splitlines():
        ref = raw_ref.decode("utf-8", "surrogateescape")
        tree = git_optional("rev-parse", "--verify", f"{ref}^{{tree}}").strip()
        if re.fullmatch(rb"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", tree):
            roots.add(tree.decode("ascii").lower())
    return roots


def parse_tree_entries(data: bytes, oid_bytes: int):
    offset = 0
    while offset < len(data):
        space = data.find(b" ", offset)
        nul = data.find(b"\x00", space + 1 if space >= 0 else offset)
        if space <= offset or nul <= space:
            raise RuntimeError("malformed raw git tree entry")
        mode = data[offset:space]
        name = data[space + 1 : nul]
        oid_start = nul + 1
        oid_end = oid_start + oid_bytes
        if oid_end > len(data):
            raise RuntimeError("truncated raw git tree object id")
        target_sha = data[oid_start:oid_end].hex()
        yield mode, name, target_sha
        offset = oid_end


def _tree_oid_bytes(tree_objects: dict[str, bytes]) -> int:
    if not tree_objects:
        return 20
    oid_lengths = {len(sha) for sha in tree_objects}
    if len(oid_lengths) != 1:
        raise RuntimeError("mixed git object id lengths are unsupported")
    oid_hex_length = next(iter(oid_lengths))
    if oid_hex_length not in {40, 64}:
        raise RuntimeError(f"unsupported git object id length: {oid_hex_length}")
    return oid_hex_length // 2


def enumerate_tree_paths(
    tree_objects: dict[str, bytes],
    root_shas: set[str],
) -> dict[str, set[str]]:
    oid_bytes = _tree_oid_bytes(tree_objects)
    paths: dict[str, set[str]] = {}
    stack: list[tuple[str, bytes, int]] = [(sha, b"", 0) for sha in sorted(root_shas)]
    emitted = 0
    while stack:
        tree_sha, prefix, depth = stack.pop()
        if depth > MAX_TREE_DEPTH:
            raise RuntimeError("git tree nesting depth exceeded safety limit")
        tree_data = tree_objects.get(tree_sha)
        if tree_data is None:
            raise RuntimeError(f"reachable tree object missing from inventory: {tree_sha}")
        for mode, name, target_sha in parse_tree_entries(tree_data, oid_bytes):
            full_path = name if not prefix else prefix + b"/" + name
            emitted += 1
            if emitted > MAX_TREE_PATH_ENTRIES:
                raise RuntimeError("reachable git tree path inventory exceeded safety limit")
            if mode in {b"40000", b"040000"}:
                stack.append((target_sha, full_path, depth + 1))
            else:
                paths.setdefault(target_sha, set()).add(
                    full_path.decode("utf-8", "surrogateescape")
                )
    return paths


def enumerate_tree_entry_paths(
    tree_objects: dict[str, bytes],
    root_shas: set[str],
) -> set[str]:
    paths: set[str] = set()
    oid_bytes = _tree_oid_bytes(tree_objects)
    stack: list[tuple[str, bytes, int]] = [(sha, b"", 0) for sha in sorted(root_shas)]
    seen: set[tuple[str, bytes]] = set()
    emitted = 0
    while stack:
        tree_sha, prefix, depth = stack.pop()
        if depth > MAX_TREE_DEPTH:
            raise RuntimeError("reachable git tree depth exceeded safety limit")
        key = (tree_sha, prefix)
        if key in seen:
            continue
        seen.add(key)
        raw_tree = tree_objects.get(tree_sha)
        if raw_tree is None:
            continue
        for mode, name, target_sha in parse_tree_entries(raw_tree, oid_bytes):
            full_path = name if not prefix else prefix + b"/" + name
            emitted += 1
            if emitted > MAX_TREE_PATH_ENTRIES:
                raise RuntimeError("reachable git tree entry inventory exceeded safety limit")
            paths.add(full_path.decode("utf-8", "surrogateescape"))
            if mode in {b"40000", b"040000"} and target_sha in tree_objects:
                stack.append((target_sha, full_path, depth + 1))
    return paths


def _reachable_tree_objects() -> tuple[dict[str, bytes], set[str]]:
    shas = reachable_object_shas()
    object_types = reachable_object_types(shas)
    tree_shas = sorted(sha for sha, kind in object_types.items() if kind == "tree")
    tree_objects = {
        sha: data
        for sha, object_type, data in iter_git_objects(tree_shas)
        if object_type == "tree"
    }
    return tree_objects, reachable_tree_roots()


def rev_list_paths() -> dict[str, set[str]]:
    shas = reachable_object_shas()
    paths: dict[str, set[str]] = {sha: set() for sha in shas}
    object_types = reachable_object_types(shas)
    tree_shas = sorted(sha for sha, kind in object_types.items() if kind == "tree")
    tree_objects = {
        sha: data
        for sha, object_type, data in iter_git_objects(tree_shas)
        if object_type == "tree"
    }
    aliases = enumerate_tree_paths(tree_objects, reachable_tree_roots())
    for target_sha, target_paths in aliases.items():
        if target_sha in paths:
            paths[target_sha].update(target_paths)
    return paths


def audit_ref_names(state: AuditState) -> int:
    refs = [line for line in git("for-each-ref", "--format=%(refname)").splitlines() if line]
    for ref in refs:
        scan_bytes(ref, "git-ref-name metadata", state)
    return len(refs)


def audit_tree_entry_paths(state: AuditState) -> int:
    tree_objects, roots = _reachable_tree_objects()
    paths = enumerate_tree_entry_paths(tree_objects, roots)
    for path in sorted(paths):
        scan_bytes(
            path.encode("utf-8", "surrogateescape"),
            "git-tree-entry path-metadata",
            state,
        )
    return len(paths)


def audit_history_emails(
    state: AuditState,
    header_addresses: set[bytes],
) -> tuple[int, int]:
    output = git("log", "--all", "--format=%ae%n%ce")
    commit_addresses = {line.strip() for line in output.splitlines() if line.strip()}
    addresses = commit_addresses | header_addresses
    ordinary = {address for address in addresses if not is_noreply_history_email(address)}
    for _address in sorted(ordinary):
        state.add(
            Finding(
                "REVIEW",
                "history-author-email",
                "git history --all",
                "non-noreply author/committer/tagger email is publicly exposed by reachable history; value redacted",
            )
        )
    return len(addresses), len(ordinary)


def main() -> int:
    configure_git_runtime()
    validate_repository_completeness()

    state = AuditState()
    ref_names_scanned = audit_ref_names(state)
    tree_entry_paths_scanned = audit_tree_entry_paths(state)
    path_map = rev_list_paths()
    archive_summaries: list[str] = []
    header_addresses: set[bytes] = set()
    scanned_blobs = 0
    scanned_messages = 0
    skipped_large_blobs = 0
    zip_blobs = 0
    unsupported_archive_blobs = 0

    for sha, object_type, data in iter_git_objects(sorted(path_map)):
        if object_type in {"commit", "tag"}:
            headers, separator, message = data.partition(b"\n\n")
            header_addresses.update(
                scan_identity_headers(
                    headers,
                    object_type,
                    f"git-{object_type}:{sha[:12]} headers",
                    state,
                )
            )
            if separator:
                scanned_messages += 1
                scan_bytes(message, f"git-{object_type}:{sha[:12]} message", state)
            continue
        if object_type != "blob":
            continue

        paths = sorted(path_map.get(sha) or {"<unknown-path>"})
        display_path = " | ".join(paths[:4])
        if len(paths) > 4:
            display_path += f" | +{len(paths) - 4} aliases"
        location = f"git:{sha[:12]} {display_path}"

        if len(data) > MAX_BLOB_BYTES:
            skipped_large_blobs += 1
            state.add(
                Finding(
                    "REVIEW",
                    "oversized-history-blob",
                    location,
                    f"bytes={len(data)}; content not scanned",
                )
            )
            continue

        scanned_blobs += 1
        if is_zip_blob(data, paths):
            zip_blobs += 1
            scan_zip(data, location, state, archive_summaries)
        elif scan_unsupported_archive(data, paths, location, state):
            unsupported_archive_blobs += 1
        else:
            scan_bytes(data, location, state)

    unique_emails, ordinary_emails = audit_history_emails(state, header_addresses)

    print("PUBLIC_RELEASE_AUDIT v8")
    print("scope=git-object-and-ref-data-only")
    print("github_public_surface_gate=REQUIRED_SEPARATELY")
    print(f"reachable_objects={len(path_map)}")
    print(f"reachable_ref_names={ref_names_scanned}")
    print(f"reachable_tree_entry_paths={tree_entry_paths_scanned}")
    print(f"scanned_blobs={scanned_blobs}")
    print(f"scanned_commit_tag_messages={scanned_messages}")
    print(f"skipped_large_blobs={skipped_large_blobs}")
    print(f"zip_blobs={zip_blobs}")
    print(f"unsupported_archive_blobs={unsupported_archive_blobs}")
    print(f"secret_groups={len(state.secret_groups)}")
    print(f"synthetic_fixture_matches={state.synthetic_fixture_matches}")
    print(f"unique_history_emails={unique_emails}")
    print(f"ordinary_history_emails={ordinary_emails}")

    if archive_summaries:
        print("\nARCHIVES")
        for summary in archive_summaries:
            print(redact_output_text(summary))

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
    print("PUBLICATION_READY=no  # requires separate GitHub public-surface gate")
    if high:
        print("RESULT=GIT_BLOCK")
        return 2
    if review:
        print("RESULT=GIT_REVIEW_REQUIRED")
        return 1
    print("RESULT=GIT_PASS")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"AUDIT_ERROR: {redact_output_text(str(exc))}", file=sys.stderr)
        raise SystemExit(3)
