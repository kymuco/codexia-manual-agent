# M6.5 Source Audit — Background Work Supervisor

## Audit target

M6.5 introduces durable orchestration over exact M6.1–M6.4 work state. The audit asks whether background persistence creates a second authority root, whether stale or concurrent work can be advanced, whether a provider side effect can be replayed after crash, whether human activity can be overwritten by a prepared continuation, whether caller-supplied records can impersonate live provider evidence, whether the runtime actually removes routine human scheduling, and whether worker output can terminate work by declaration.

## Authority audit

Supervisor status is explicitly readiness/orchestration state:

```text
READY / PREPARED / IN_FLIGHT / WAITING_HUMAN / COMPLETED
!= execution authority
```

A provider dispatch can only be constructed from an exact M6.2 `ADMIT` plus exact M6.4 `KEEP_MOVING` result bound to the current peer cursor. The supervisor neither changes the admission decision nor suppresses an attention boundary.

No supervisor or driver record contains local process, filesystem, Git mutation, network capability, merge authority, authorization receipt, or generic command payload.

## Second-truth audit

The supervisor does not replace `WorkHandoff`, `WorkIntentInterpretation`, `ContinuationAdmission`, `DynamicAttentionDecision`, or `ChatPeerCursor`. It stores those exact digest-bound records as evidence for orchestration transitions.

Recovered supervisor status is replay-derived from an append-only event chain. The head row stores only terminal sequence/digest integrity metadata.

## Live-evidence provenance audit

A structurally valid `ChatPeerObservation` or `ChatPeerTurn` is not sufficient proof that the live provider actually produced it. M6.5 therefore installs process-local, single-use provenance tickets at the M6.3 capture boundary:

```text
ChatGPTPeerLoop.observe()
→ exact observation digest ticket
→ supervisor may consume once

ChatGPTPeerLoop.continue_admitted()
→ exact turn digest ticket
→ supervisor may consume once
```

A caller that merely constructs an equivalent observation/turn object does not receive a ticket and cannot use that object to advance supervisor state. Tickets bind the exact pre-event cursor and are consumed on commit.

`observe_external_chat()` is the preferred supervisor surface: the supervisor recovers its exact cursor, invokes the live M6.3 peer observation itself, and immediately commits only that verified result.

Post-crash reconciliation is the intentional exception for a peer turn: M6.5 reconstructs the exact turn itself from the live current branch under `reconcile_in_flight_chat()`, so that internal reconciliation path receives a narrowly scoped commit allowance without manufacturing a reusable external ticket.

This preserves the older Codexia rule: do not trust caller-supplied evidence when the authoritative source can be reread.

## Background-driver audit

Persistence alone would leave the human as the scheduler. `BackgroundWorkDriver.drive_chat_until_blocked()` closes that gap with one bounded host-invoked event pump.

For each iteration it recovers the exact durable work state, synchronizes live peer activity before any unclaimed provider dispatch, invokes an injected Codexia-side checkpoint source only from `READY`, and routes every returned checkpoint through the existing supervisor validation path.

The checkpoint source therefore does not gain orchestration or execution authority. It can propose only an exact `(ContinuationProposal, ContinuationAdmission, DynamicAttentionDecision)` tuple or no checkpoint. `record_checkpoint()` still revalidates M6.1 handoff/interpretation identity, M6.2 admission, M6.4 attention, and the exact current cursor before any `PREPARED` dispatch can exist.

A single driver call can cross multiple routine worker turns:

```text
READY
→ exact checkpoint
→ PREPARED
→ durable claim
→ one M6.3 peer turn
→ READY
→ next exact checkpoint
→ ...
```

No human `continue` event is required between those ordinary turns. The loop remains explicitly bounded by `max_steps`, so repeated `REVISE`/replanning cannot become an unbounded autonomous loop.

The driver stops rather than widening authority on `WAITING_HUMAN`, ambiguous `IN_FLIGHT`, completion, absent next checkpoint, or step-budget exhaustion.

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

A `PREPARED` continuation has not produced a side effect. Before claiming it, the background driver rereads the live M6.3 branch. If the conversation advanced, the verified observation clears that pending dispatch and returns the work to `READY` at the live cursor.

`WAITING_HUMAN` can resume only from a fresh live M6.3 observation containing an actual `EXTERNAL_USER` message. Assistant-only activity cannot fabricate the human answer, and a hand-constructed observation cannot impersonate one.

Generic observation is forbidden while `IN_FLIGHT`; the attempted dispatch must be reconciled first so a Codexia transport-role `user` message cannot be mislabeled as HUMAN.

The first ChatGPT adapter still has one narrow liveness race: live state may change after the durable claim but before provider send begins. M6.3 prevents the stale send, while M6.5 conservatively retains `IN_FLIGHT` instead of assuming retry is safe. This is a fail-closed liveness limit, not an authority leak or replay path.

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

The first M6.5 candidate is intentionally single-database/single-host orchestration, not a distributed scheduler. Its first proven transport adapter is the existing M6.3 ChatGPT peer loop; the delegated-work semantics are general, but other worker/tool transports have not yet been proven against this supervisor boundary.

It does not generate wake-up timers, a resident daemon, or notifications. A host/runtime must invoke the bounded driver when work is ready to advance. It also does not invent a new interpretation-lineage model for human evidence after the original M6.1 handoff. A crash or race after durable claim but before any safely attributable provider-visible effect may conservatively leave work `IN_FLIGHT`; absence of a visible effect is not treated as proof that retry is safe.

These are bounded non-claims rather than hidden assumptions.

## Audit conclusion

The candidate preserves the intended architecture:

```text
human intent / admission / attention / authority
remain authoritative boundaries

while

Codexia can durably remember what work is ready,
react to verified live state,
cross multiple routine worker turns without human scheduling,
stop at genuine human judgment,
and remember what remote dispatch may already have happened.
```

The new persistence and bounded-driver layers advance delegated-work continuity without minting a second execution authority, trusting synthetic live evidence, or turning ambiguity into retry permission.
