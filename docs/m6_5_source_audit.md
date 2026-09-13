# M6.5 Source Audit — Background Work Supervisor

## Audit target

M6.5 introduces durable orchestration over exact M6.1–M6.4 work state. The audit asks whether background persistence creates a second authority root, whether stale or concurrent work can be advanced, whether a provider side effect can be replayed after crash, whether human activity can be overwritten by a prepared continuation, and whether worker output can terminate work by declaration.

## Authority audit

Supervisor status is explicitly readiness/orchestration state:

```text
READY / PREPARED / IN_FLIGHT / WAITING_HUMAN / COMPLETED
!= execution authority
```

A provider dispatch can only be constructed from an exact M6.2 `ADMIT` plus exact M6.4 `KEEP_MOVING` result bound to the current peer cursor. The supervisor neither changes the admission decision nor suppresses an attention boundary.

No supervisor record contains local process, filesystem, Git mutation, network capability, merge authority, authorization receipt, or generic command payload.

## Second-truth audit

The supervisor does not replace `WorkHandoff`, `WorkIntentInterpretation`, `ContinuationAdmission`, `DynamicAttentionDecision`, or `ChatPeerCursor`. It stores those exact digest-bound records as evidence for orchestration transitions.

Recovered supervisor status is replay-derived from an append-only event chain. The head row stores only terminal sequence/digest integrity metadata.

## Provider replay audit

The provider send is the first M6.5 effect that cannot be made transactionally atomic with SQLite.

The supervisor therefore writes a `DISPATCH_STARTED` event before sending and returns an ephemeral process-local lease. The lease is removed before the send call. It is not serialized and cannot be reconstructed after restart.

Consequences:

```text
crash after DISPATCH_STARTED
→ durable IN_FLIGHT
→ no automatic claim/retry
```

This intentionally prefers a recoverable stall over a duplicate remote side effect.

## Reconciliation audit

`reconcile_in_flight_chat()` performs no send. It reads the live branch and requires the exact pre-dispatch cursor prefix.

Only the exact deterministic Codexia continuation envelope can be relabelled as `CODEXIA_SEND`. If that exact message is followed immediately by an assistant message, an exact `ChatPeerTurn` is reconstructed and committed. A missing delta or a lone exact Codexia envelope remains in flight. Foreign/intervening activity fails closed.

If later messages exist after the exact Codexia/assistant pair, reconciliation advances only through that pair. The remaining messages stay outside the recovered cursor and must be observed normally, preserving human precedence.

## Human-precedence audit

A `PREPARED` continuation has not produced a side effect. If the conversation advances before claim, an exact external observation clears that pending dispatch and returns the work to `READY` at the live cursor.

`WAITING_HUMAN` can resume through `record_external_observation()` only if the observation contains an M6.3 `EXTERNAL_USER` message. Assistant-only activity cannot fabricate the human answer.

Generic observation is forbidden while `IN_FLIGHT`; the attempted dispatch must be reconciled first so a Codexia transport-role `user` message cannot be mislabeled as HUMAN.

## Completion audit

`complete()` requires `READY` state and an explicitly CODEXIA-authored `WorkStatement`. WORKER output cannot mark itself complete.

This preserves:

```text
worker output != work completion
```

M6.5 does not claim that Codexia's completion judgment is infallible; M6.6 is the daily-use quality proof.

## Persistence and concurrency audit

Every write uses an immediate SQLite transaction. A transition is appended only when the durable head still matches the sequence/digest recovered by the caller. A concurrent winner causes the stale caller to fail and recover again rather than fork history.

Replay validates canonical event digests, previous-digest chaining, contiguous sequence, exact event payload keys, nested M6 record integrity, legal state transitions, and terminal head metadata.

## Known limits

The first M6.5 candidate is intentionally single-database/single-host orchestration, not a distributed scheduler. It does not generate wake-up timers or notifications. It also does not invent a new interpretation-lineage model for human evidence after the original M6.1 handoff.

These are bounded non-claims rather than hidden assumptions.

## Audit conclusion

The candidate preserves the intended architecture:

```text
human intent / admission / attention / authority
remain authoritative boundaries

while

Codexia can durably remember what work is ready,
what work is waiting for the human,
and what remote dispatch may already have happened.
```

The new persistence layer advances orchestration continuity without minting a second execution authority.
