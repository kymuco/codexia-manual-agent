# SV4 — Process Runner Ownership Reconciliation v1

## Purpose

SV4 closes the remaining operational ambiguity after SV3:

\`\`\`text
ProcessAttempt AUTHORITY_CONSUMED
+
no terminal ProcessExecutionObservation
\`\`\`

SV3 correctly refused replay, but without additional evidence this state could
remain AWAITING_OUTCOME_RECONCILIATION forever.

SV4 adds exact runner ownership evidence without adding a Gen2 Core primitive.

## Primary boundary

\`\`\`text
durable attempt truth != ephemeral runner liveness
runner death != process success/failure
runner death after authority consumption → UNKNOWN
\`\`\`

## Why timeout is insufficient

Process execution is bounded, but absence of an observation after a wall-clock
deadline does not prove that the runner is dead. The machine may have been
suspended or the runner may have been delayed.

Publishing CapabilityOutcome.UNKNOWN while a live runner can still publish a
terminal observation would create competing terminal truths.

Therefore SV4 does not use:

- elapsed wall-clock time;
- PID existence alone;
- heartbeat staleness alone;

as proof that an attempt is no longer live.

## Exact runner ownership

Every durable ProcessAttempt has a deterministic lock-file path derived from:

\`\`\`text
journal path
+
attempt_id
\`\`\`

The file itself is not the ownership record.

The operating-system exclusive lock is the ownership evidence.

\`StandaloneProcessRunnerOwnership\` uses:

- \`fcntl.flock(... LOCK_EX | LOCK_NB)\` on POSIX;
- \`msvcrt.locking(... LK_NBLCK ...)\` on Windows.

The operating system releases the lock automatically when the owning runner
process exits or is killed.

The runner acquires this exact ownership **before** it can consume the durable
AuthorizationReceipt and holds it until after it has either:

- written the terminal ProcessExecutionObservation; or
- written a runner error.

Therefore:

\`\`\`text
AUTHORITY_CONSUMED
+
runner ownership held
→ effect may still be active
→ wait

AUTHORITY_CONSUMED
+
runner ownership free
+
no terminal observation
→ exact runner is gone
→ no replay
→ durable ERROR_AFTER_CONSUME
→ CapabilityOutcome.UNKNOWN
\`\`\`

## Why runner death ends the live effect tree

The existing controlled ProcessExecutor already owns process-tree containment.

On Linux, the target is launched under bubblewrap with:

\`\`\`text
--unshare-pid
--die-with-parent
\`\`\`

On Windows, the target is assigned before resume to a Job Object configured
with:

\`\`\`text
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
\`\`\`

The Job Object handle is owned by the runner process.

Therefore once exact runner ownership is provably released, the runner cannot
still publish a future observation and its contained target tree cannot remain
owned by that runner.

This does **not** prove whether partial external side effects happened before
the runner died. That is exactly why the reconciled status is UNKNOWN rather
than FAILED or SUCCEEDED.

## Race handling

### Multiple runners before consumption

\`\`\`text
AUTHORIZED_UNCONSUMED
→ only one exact runner can acquire ownership
→ other runners exit without touching authority/effect state
\`\`\`

The existing durable one-shot receipt registry remains a second independent
safety boundary.

### Recovery while a live unconsumed runner exists

Recovery observes the exact ownership lock and does not launch another runner.

### Recovery while a live consumed runner exists

Recovery leaves the attempt AUTHORITY_CONSUMED and returns no outcome.

### Runner finishes between recovery read and ownership probe

If the runner writes OBSERVED and exits before reconciliation records an error,
\`record_runner_error()\` re-reads durable state and preserves the already
terminal observation. Reconciliation then uses the observed attempt rather than
manufacturing UNKNOWN.

## Hard-kill closure

The adversarial proof launches a real long-running contained process, waits
until:

\`\`\`text
attempt == AUTHORITY_CONSUMED
and
exact runner ownership == live
\`\`\`

then hard-kills the runner.

Only after the operating system releases exact ownership does reconciliation
admit:

\`\`\`text
RunnerOwnershipLostAfterConsumption
→ ERROR_AFTER_CONSUME
→ CapabilityOutcome.UNKNOWN
\`\`\`

No new runner and no second external process are created.

## Nonclaims

SV4 does not claim:

- that UNKNOWN means no side effect happened;
- that PID absence proves execution status;
- that a timeout proves runner death;
- that UNKNOWN grants retry permission;
- that runner ownership is Work lifecycle truth;
- that the standalone host is now a generic scheduler.

The preserved invariant remains:

\`\`\`text
Outcome.UNKNOWN != retry permission
\`\`\`

## Proof obligations

SV4 must prove:

1. one exact attempt has at most one live runner owner;
2. ownership is released automatically when its process exits;
3. live AUTHORITY_CONSUMED stays ambiguous;
4. live AUTHORIZED_UNCONSUMED suppresses redundant relaunch;
5. hard-killed runner releases ownership;
6. consumed + ownership released + no observation becomes UNKNOWN;
7. the UNKNOWN result enters only through CapabilityHostBridge.record_outcome;
8. no second attempt, handoff, or process replay is manufactured.
