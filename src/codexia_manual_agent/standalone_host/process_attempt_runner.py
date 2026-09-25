from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

from codexia_manual_agent.authority import (
    ActionLifecycle,
    ActionPhase,
    LocalApprovalAuthority,
)
from codexia_manual_agent.domain.errors import AuthorizationConsumedError
from codexia_manual_agent.execution import ProcessExecutor
from codexia_manual_agent.standalone_host.process_attempt import (
    SqliteStandaloneProcessAttemptStore,
    StandaloneProcessAttemptState,
)


def launch_process_attempt_runner(
    *,
    journal_path: str | Path,
    attempt_id: str,
) -> subprocess.Popen[bytes]:
    """Launch a runner process whose lifetime is independent of the caller."""

    journal = Path(journal_path).resolve()
    command = [
        sys.executable,
        "-m",
        "codexia_manual_agent.standalone_host.process_attempt_runner",
        "--journal",
        str(journal),
        "--attempt-id",
        attempt_id,
    ]
    kwargs: dict[str, object] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name == "nt":
        kwargs["creationflags"] = getattr(
            subprocess,
            "CREATE_NEW_PROCESS_GROUP",
            0,
        )
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(command, **kwargs)


def run_process_attempt(
    *,
    journal_path: str | Path,
    attempt_id: str,
) -> int:
    store = SqliteStandaloneProcessAttemptStore(journal_path)
    snapshot = store.recover(attempt_id)
    if snapshot.state in {
        StandaloneProcessAttemptState.DENIED,
        StandaloneProcessAttemptState.OBSERVED,
        StandaloneProcessAttemptState.REJECTED_BEFORE_CONSUME,
        StandaloneProcessAttemptState.ERROR_AFTER_CONSUME,
    }:
        return 0

    authority = LocalApprovalAuthority(consumption_registry=store)
    lifecycle = ActionLifecycle(
        snapshot.proposal,
        snapshot.receipt.mode,
    )
    phase = lifecycle.apply_receipt(
        snapshot.receipt,
        authority=authority,
    )
    if phase is ActionPhase.DENIED:
        return 0

    try:
        observation = ProcessExecutor().execute(
            lifecycle,
            authority=authority,
        )
    except AuthorizationConsumedError:
        # Another runner for this exact durable attempt won the one-shot
        # consumption race. It owns any possible external effect.
        return 0
    except BaseException as exc:
        store.record_runner_error(
            attempt_id,
            error_type=type(exc).__name__,
            detail=f"{type(exc).__name__}: {exc}",
        )
        return 2

    store.record_observation(attempt_id, observation)
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="codexia-standalone-process-attempt-runner"
    )
    parser.add_argument("--journal", required=True)
    parser.add_argument("--attempt-id", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    return run_process_attempt(
        journal_path=args.journal,
        attempt_id=args.attempt_id,
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
