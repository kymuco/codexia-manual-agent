from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from codexia_manual_agent.domain.errors import (
    GitMutationExecutionError,
    GitMutationPreconditionChangedError,
    GitRepositoryBoundaryError,
    InvalidGitMutationError,
)
from codexia_manual_agent.git_mutation.models import GitExecutableIdentity, GitIndexEntry

MAX_COMMIT_MESSAGE_BYTES = 64 * 1024
MAX_STAGED_DIFF_BYTES = 512 * 1024
MAX_INDEX_BYTES = 16 * 1024 * 1024
MAX_INDEX_ENTRIES = 10000
MAX_REMOTE_URL_CHARS = 4096
MAX_GIT_CONFIG_BYTES = 1024 * 1024
GIT_COMMAND_TIMEOUT_SECONDS = 30
GIT_PUSH_TIMEOUT_SECONDS = 120

_REMOTE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_REF_RE = re.compile(r"^refs/heads/[A-Za-z0-9][A-Za-z0-9._/-]{0,1023}$")
_HEX_RE = re.compile(r"^[0-9a-f]+$")

_GIT_ENV_EXACT = {
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_NAMESPACE",
    "GIT_CEILING_DIRECTORIES",
    "GIT_DISCOVERY_ACROSS_FILESYSTEM",
    "GIT_EXEC_PATH",
    "GIT_GRAFT_FILE",
    "GIT_SSH",
    "GIT_SSH_COMMAND",
    "GIT_SSH_VARIANT",
    "GIT_PROXY_COMMAND",
    "GIT_ASKPASS",
    "SSH_ASKPASS",
    "GIT_ALLOW_PROTOCOL",
    "GIT_PROTOCOL",
    "GIT_PROTOCOL_FROM_USER",
    "GIT_EXTERNAL_DIFF",
    "GIT_DIFF_OPTS",
    "GIT_CONFIG",
    "GIT_CONFIG_COUNT",
    "GIT_CONFIG_PARAMETERS",
    "GIT_CONFIG_GLOBAL",
    "GIT_CONFIG_SYSTEM",
    "GIT_CONFIG_NOSYSTEM",
    "GIT_TEMPLATE_DIR",
    "GIT_AUTHOR_NAME",
    "GIT_AUTHOR_EMAIL",
    "GIT_AUTHOR_DATE",
    "GIT_COMMITTER_NAME",
    "GIT_COMMITTER_EMAIL",
    "GIT_COMMITTER_DATE",
    "GIT_EDITOR",
    "GIT_SEQUENCE_EDITOR",
    "GIT_PAGER",
    "GIT_NO_LAZY_FETCH",
}
_GIT_ENV_PREFIXES = ("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_", "GIT_TRACE")


@dataclass(frozen=True, slots=True)
class RepositorySnapshot:
    workspace_root: Path
    git_dir: Path
    git: GitExecutableIdentity
    object_format: str
    oid_length: int
    head_ref: str
    head_oid: str


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def digest_json(value: Any) -> str:
    return sha256_bytes(canonical_json(value).encode("utf-8"))


def require_sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise InvalidGitMutationError(f"{label} must be SHA-256 hex")
    try:
        int(value, 16)
    except ValueError as exc:
        raise InvalidGitMutationError(f"{label} must be SHA-256 hex") from exc
    return value


def workspace_root(workspace: str | Path) -> Path:
    try:
        root = Path(workspace).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise GitRepositoryBoundaryError("Git workspace does not resolve") from exc
    if not root.is_dir():
        raise GitRepositoryBoundaryError("Git workspace must be a directory")
    return root


def _norm(path: Path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def _is_link_like(path: Path) -> bool:
    if path.is_symlink():
        return True
    junction = getattr(path, "is_junction", None)
    return bool(junction is not None and junction())


def _require_real_path(path: Path, *, label: str, directory: bool) -> Path:
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise GitRepositoryBoundaryError(f"{label} does not resolve") from exc
    if _is_link_like(path) or _norm(resolved) != _norm(path):
        raise GitRepositoryBoundaryError(
            f"{label} cannot be a symlink, junction, or redirected path"
        )
    if directory and not path.is_dir():
        raise GitRepositoryBoundaryError(f"{label} must be a directory")
    if not directory and not path.is_file():
        raise GitRepositoryBoundaryError(f"{label} must be a regular file")
    return resolved


def _require_optional_real_path(path: Path, *, label: str, directory: bool) -> None:
    if path.exists() or path.is_symlink():
        _require_real_path(path, label=label, directory=directory)


def _reject_external_object_semantics(git_dir: Path) -> None:
    for relative in (
        "info/grafts",
        "objects/info/alternates",
        "objects/info/http-alternates",
        "commondir",
    ):
        candidate = git_dir / relative
        if candidate.exists() or candidate.is_symlink():
            raise GitRepositoryBoundaryError(
                f"M2.5 v1 rejects external Git object/ancestry semantics: {candidate}"
            )


def _validate_ref_storage(git_dir: Path, head_ref: str) -> None:
    relative = head_ref.removeprefix("refs/")
    parts = relative.split("/")
    current = git_dir / "refs"
    _require_real_path(current, label="Git refs directory", directory=True)
    for part in parts[:-1]:
        current = current / part
        if current.exists() or current.is_symlink():
            _require_real_path(current, label="Git ref parent", directory=True)
        else:
            break
    leaf = git_dir.joinpath("refs", *parts)
    _require_optional_real_path(leaf, label="Git loose HEAD ref", directory=False)


def _hash_regular_file(path: Path, *, label: str = "Resolved executable") -> GitExecutableIdentity:
    if not path.is_file():
        raise InvalidGitMutationError(f"{label} is not a regular file")
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise InvalidGitMutationError(f"Cannot read {label.lower()}") from exc
    return GitExecutableIdentity(
        path=str(path),
        size_bytes=len(payload),
        sha256=sha256_bytes(payload),
    )


def _reject_workspace_executable(path: Path, root: Path, *, label: str) -> None:
    try:
        path.relative_to(root)
    except ValueError:
        return
    raise GitRepositoryBoundaryError(f"{label} cannot resolve from inside the governed workspace")


def resolve_git_executable(root: Path) -> GitExecutableIdentity:
    resolved = shutil.which("git")
    if not resolved:
        raise InvalidGitMutationError("Git executable was not found on PATH")
    try:
        path = Path(resolved).resolve(strict=True)
    except OSError as exc:
        raise InvalidGitMutationError("Resolved Git executable does not exist") from exc
    _reject_workspace_executable(path, root, label="Git executable")
    return _hash_regular_file(path, label="Resolved Git executable")


def resolve_ssh_executable(root: Path) -> GitExecutableIdentity:
    resolved = shutil.which("ssh")
    if not resolved:
        raise InvalidGitMutationError("SSH executable was not found on PATH")
    try:
        path = Path(resolved).resolve(strict=True)
    except OSError as exc:
        raise InvalidGitMutationError("Resolved SSH executable does not exist") from exc
    _reject_workspace_executable(path, root, label="SSH executable")
    return _hash_regular_file(path, label="Resolved SSH executable")


def resolve_git_helper(
    snapshot: RepositorySnapshot,
    helper_name: str,
) -> GitExecutableIdentity:
    exec_path_text = decode_line(
        run_git(snapshot.git, snapshot.workspace_root, ["--exec-path"]).stdout,
        "Git exec path",
    )
    try:
        exec_dir = Path(exec_path_text).resolve(strict=True)
    except OSError as exc:
        raise InvalidGitMutationError("Git exec path does not resolve") from exc
    _reject_workspace_executable(exec_dir, snapshot.workspace_root, label="Git exec path")
    candidates = (exec_dir / helper_name, exec_dir / f"{helper_name}.exe")
    for candidate in candidates:
        if candidate.is_file():
            try:
                resolved_target = candidate.resolve(strict=True)
            except OSError as exc:
                raise InvalidGitMutationError(
                    f"Required Git helper {helper_name!r} does not resolve"
                ) from exc
            _reject_workspace_executable(
                resolved_target,
                snapshot.workspace_root,
                label=helper_name,
            )
            # Preserve the exact helper entry Git will invoke from GIT_EXEC_PATH.
            # On modern POSIX Git installations git-remote-https may be a symlink
            # to git-remote-http; resolving it here would bind only the target and
            # leave replacement of the invoked alias itself outside revalidation.
            return _hash_regular_file(candidate.absolute(), label=helper_name)
    raise InvalidGitMutationError(f"Required Git helper {helper_name!r} was not found")


def revalidate_git_executable(identity: GitExecutableIdentity) -> None:
    current = _hash_regular_file(Path(identity.path), label="Bound executable")
    if current != identity:
        raise GitMutationPreconditionChangedError(
            "Bound executable identity changed before authorization consumption"
        )


def base_env() -> dict[str, str]:
    env: dict[str, str] = {}
    for key, value in os.environ.items():
        upper = key.upper()
        if upper in _GIT_ENV_EXACT or any(
            upper.startswith(prefix) for prefix in _GIT_ENV_PREFIXES
        ):
            continue
        env[key] = value
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env["GIT_NO_REPLACE_OBJECTS"] = "1"
    env["GIT_NO_LAZY_FETCH"] = "1"
    env["GIT_PAGER"] = "cat"
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_SYSTEM"] = os.devnull
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["LC_ALL"] = "C"
    return env


def run_git(
    git: GitExecutableIdentity,
    root: Path,
    args: list[str],
    *,
    env: dict[str, str] | None = None,
    input_bytes: bytes | None = None,
    timeout: int = GIT_COMMAND_TIMEOUT_SECONDS,
    check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    try:
        result = subprocess.run(
            [
                git.path,
                "-c",
                "core.fsmonitor=false",
                "-c",
                f"core.hooksPath={os.devnull}",
                "-c",
                "hook.reference-transaction.enabled=false",
                "-C",
                str(root),
                *args,
            ],
            input=input_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            env=env if env is not None else base_env(),
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        action = args[0] if args else "git"
        raise GitMutationExecutionError(f"Git command failed to start: {action}") from exc
    if check and result.returncode != 0:
        message = result.stderr.decode("utf-8", errors="replace")[:4096]
        action = args[0] if args else "git"
        raise GitMutationExecutionError(
            f"Git command {action!r} failed with {result.returncode}: {message}"
        )
    return result


def decode_line(payload: bytes, label: str) -> str:
    try:
        value = payload.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise InvalidGitMutationError(f"{label} is not UTF-8") from exc
    if not value:
        raise InvalidGitMutationError(f"{label} is empty")
    return value


def validate_oid(value: str, oid_length: int, label: str) -> str:
    if len(value) != oid_length or _HEX_RE.fullmatch(value) is None:
        raise InvalidGitMutationError(f"{label} is not a full repository object id")
    return value


def validate_head_ref(ref: str) -> str:
    if not isinstance(ref, str) or _REF_RE.fullmatch(ref) is None:
        raise InvalidGitMutationError("Git ref must be an explicit refs/heads/... ref")
    if (
        ".." in ref
        or "@{" in ref
        or ref.endswith(("/", ".", ".lock"))
        or "//" in ref
        or "/." in ref
        or "\\" in ref
    ):
        raise InvalidGitMutationError("Git ref contains a forbidden refname form")
    return ref


def snapshot_repository(
    workspace: str | Path,
    *,
    git: GitExecutableIdentity | None = None,
) -> RepositorySnapshot:
    root = workspace_root(workspace)
    if git is None:
        git = resolve_git_executable(root)
    else:
        if not isinstance(git, GitExecutableIdentity):
            raise TypeError("git must be GitExecutableIdentity")
        try:
            bound_path = Path(git.path).resolve(strict=True)
        except OSError as exc:
            raise GitMutationPreconditionChangedError(
                "Bound Git executable no longer resolves"
            ) from exc
        _reject_workspace_executable(bound_path, root, label="Bound Git executable")
        revalidate_git_executable(git)
    lexical_git_dir = root / ".git"
    _require_real_path(lexical_git_dir, label="Git metadata directory", directory=True)
    top = Path(
        decode_line(
            run_git(git, root, ["rev-parse", "--show-toplevel"]).stdout,
            "Git top level",
        )
    ).resolve(strict=True)
    if top != root:
        raise GitRepositoryBoundaryError(
            "M2.5 requires the governed workspace to be the repository top level"
        )
    git_dir = Path(
        decode_line(
            run_git(git, root, ["rev-parse", "--absolute-git-dir"]).stdout,
            "Git directory",
        )
    ).resolve(strict=True)
    if _norm(git_dir) != _norm(lexical_git_dir):
        raise GitRepositoryBoundaryError(
            "M2.5 v1 supports only an in-workspace normal .git directory"
        )
    for path, label, directory in (
        (git_dir / "objects", "Git objects directory", True),
        (git_dir / "refs", "Git refs directory", True),
        (git_dir / "HEAD", "Git HEAD file", False),
        (git_dir / "config", "Git local config", False),
        (git_dir / "index", "Git index", False),
    ):
        _require_real_path(path, label=label, directory=directory)
    _reject_external_object_semantics(git_dir)
    _require_optional_real_path(git_dir / "logs", label="Git logs directory", directory=True)
    _require_optional_real_path(
        git_dir / "packed-refs",
        label="Git packed refs",
        directory=False,
    )
    object_format = decode_line(
        run_git(git, root, ["rev-parse", "--show-object-format"]).stdout,
        "Git object format",
    )
    oid_length = 40 if object_format == "sha1" else 64 if object_format == "sha256" else 0
    if not oid_length:
        raise InvalidGitMutationError(f"Unsupported Git object format: {object_format}")
    head_ref = validate_head_ref(
        decode_line(
            run_git(git, root, ["symbolic-ref", "-q", "HEAD"]).stdout,
            "Git HEAD ref",
        )
    )
    _validate_ref_storage(git_dir, head_ref)
    head_oid = validate_oid(
        decode_line(
            run_git(git, root, ["rev-parse", "--verify", "HEAD^{commit}"]).stdout,
            "Git HEAD object",
        ),
        oid_length,
        "Git HEAD object",
    )
    return RepositorySnapshot(root, git_dir, git, object_format, oid_length, head_ref, head_oid)


def read_index(
    snapshot: RepositorySnapshot,
) -> tuple[bytes, tuple[GitIndexEntry, ...], str]:
    try:
        payload = (snapshot.git_dir / "index").read_bytes()
    except OSError as exc:
        raise InvalidGitMutationError("Git index cannot be read") from exc
    if not payload or len(payload) > MAX_INDEX_BYTES:
        raise InvalidGitMutationError("Git index is empty or exceeds the M2.5 budget")
    records = [
        item
        for item in run_git(
            snapshot.git,
            snapshot.workspace_root,
            ["ls-files", "--stage", "-z"],
        ).stdout.split(b"\0")
        if item
    ]
    if len(records) > MAX_INDEX_ENTRIES:
        raise InvalidGitMutationError("Git index contains too many entries")
    entries: list[GitIndexEntry] = []
    for record in records:
        try:
            metadata, raw_path = record.split(b"\t", 1)
            mode_b, oid_b, stage_b = metadata.split(b" ", 2)
            mode = mode_b.decode("ascii")
            oid = oid_b.decode("ascii")
            stage = int(stage_b.decode("ascii"))
            path = raw_path.decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise InvalidGitMutationError(
                "Git index contains an unsupported or non-UTF-8 entry"
            ) from exc
        validate_oid(oid, snapshot.oid_length, "Git index object")
        if stage != 0:
            raise InvalidGitMutationError("Git index contains unresolved merge stages")
        entries.append(GitIndexEntry(mode, oid, stage, path))
    return payload, tuple(entries), digest_json([entry.to_dict() for entry in entries])


def staged_diff(snapshot: RepositorySnapshot) -> tuple[str, str]:
    quiet = run_git(
        snapshot.git,
        snapshot.workspace_root,
        ["diff", "--cached", "--quiet", "--no-ext-diff", "--no-textconv"],
        check=False,
    )
    if quiet.returncode == 0:
        raise InvalidGitMutationError("Git commit proposal has no staged changes")
    if quiet.returncode != 1:
        raise GitMutationExecutionError("Git could not determine staged-change state")
    result = run_git(
        snapshot.git,
        snapshot.workspace_root,
        [
            "diff",
            "--cached",
            "--binary",
            "--no-ext-diff",
            "--no-textconv",
            "--no-renames",
            "--src-prefix=a/",
            "--dst-prefix=b/",
        ],
    )
    if len(result.stdout) > MAX_STAGED_DIFF_BYTES:
        raise InvalidGitMutationError("Staged Git diff exceeds the human-review budget")
    try:
        text = result.stdout.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise InvalidGitMutationError(
            "M2.5 commit preview requires a UTF-8 staged diff"
        ) from exc
    return text, sha256_bytes(result.stdout)


def git_config_value(snapshot: RepositorySnapshot, key: str) -> str:
    result = run_git(
        snapshot.git,
        snapshot.workspace_root,
        ["config", "--local", "--get", key],
        check=False,
    )
    if result.returncode != 0:
        raise InvalidGitMutationError(f"Local Git config {key!r} is required")
    value = decode_line(result.stdout, f"Local Git config {key}")
    if any(char in value for char in "\r\n\x00"):
        raise InvalidGitMutationError(f"Local Git config {key!r} contains control characters")
    return value


def read_local_config_identity(snapshot: RepositorySnapshot) -> tuple[int, str]:
    config = snapshot.git_dir / "config"
    _require_real_path(config, label="Git local config", directory=False)
    try:
        payload = config.read_bytes()
    except OSError as exc:
        raise InvalidGitMutationError("Local Git config cannot be read") from exc
    if not payload or len(payload) > MAX_GIT_CONFIG_BYTES:
        raise InvalidGitMutationError("Local Git config is empty or exceeds the M2.5 budget")
    return len(payload), sha256_bytes(payload)


def validate_commit_message(message: str) -> str:
    if not isinstance(message, str) or not message.strip() or "\x00" in message:
        raise InvalidGitMutationError(
            "Git commit message must be non-empty UTF-8 text without NUL"
        )
    try:
        payload = message.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise InvalidGitMutationError("Git commit message must be valid UTF-8") from exc
    if len(payload) > MAX_COMMIT_MESSAGE_BYTES:
        raise InvalidGitMutationError("Git commit message exceeds the M2.5 budget")
    return message


def validate_remote_name(remote: str) -> str:
    if not isinstance(remote, str) or _REMOTE_NAME_RE.fullmatch(remote) is None:
        raise InvalidGitMutationError("Git remote name has an invalid form")
    return remote


def classify_remote_transport(url: str) -> str:
    validate_remote_url(url)
    parsed = urlparse(url)
    if parsed.scheme == "file":
        return "file"
    return "ssh"


def validate_remote_url(url: str) -> str:
    if not isinstance(url, str) or not url or len(url) > MAX_REMOTE_URL_CHARS:
        raise InvalidGitMutationError("Git remote URL is missing or too long")
    if url.startswith("-") or any(char in url for char in "\r\n\x00") or "::" in url:
        raise InvalidGitMutationError("Git remote URL contains a forbidden form")
    parsed = urlparse(url)
    if parsed.scheme == "file":
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise InvalidGitMutationError("file:// Git remote contains unsupported URL components")
        if parsed.netloc not in {"", "localhost"}:
            raise InvalidGitMutationError("M2.5 v1 supports only local file:// remotes")
        if not parsed.path:
            raise InvalidGitMutationError("file:// Git remote path is empty")
        return url
    if parsed.scheme == "ssh":
        if not parsed.hostname or parsed.password is not None or parsed.query or parsed.fragment:
            raise InvalidGitMutationError("ssh:// Git remote contains an unsupported form")
        return url
    if parsed.scheme:
        raise InvalidGitMutationError(
            "M2.5 v1 supports only SSH/SCP-like and local file:// remotes"
        )
    if ":" in url and not url.startswith(("/", "\\")):
        host, path = url.split(":", 1)
        if host and path and " " not in host and not host.startswith("-"):
            return url
    raise InvalidGitMutationError(
        "M2.5 v1 requires an explicit ssh://, SCP-like SSH, or local file:// remote"
    )


def file_remote_path(url: str) -> Path:
    if classify_remote_transport(url) != "file":
        raise InvalidGitMutationError("Git remote is not a file:// destination")
    parsed = urlparse(url)
    raw_path = unquote(parsed.path)
    if os.name == "nt" and re.fullmatch(r"/[A-Za-z]:/.*", raw_path):
        raw_path = raw_path[1:]
    path = Path(raw_path)
    try:
        return path.resolve(strict=True)
    except OSError as exc:
        raise InvalidGitMutationError("file:// Git remote does not resolve") from exc


def parse_git_identity(value: Any) -> GitExecutableIdentity:
    if not isinstance(value, dict) or set(value) != {"path", "size_bytes", "sha256"}:
        raise InvalidGitMutationError("Executable identity schema mismatch")
    if not isinstance(value["path"], str) or not value["path"]:
        raise InvalidGitMutationError("Executable path is required")
    if type(value["size_bytes"]) is not int or value["size_bytes"] < 0:
        raise InvalidGitMutationError("Executable size is invalid")
    return GitExecutableIdentity(
        value["path"],
        value["size_bytes"],
        require_sha256(value["sha256"], "Executable digest"),
    )
