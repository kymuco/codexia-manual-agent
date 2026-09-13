# M6.5 Source Audit — Background Work Supervisor

## Audit target

M6.5 introduces durable orchestration over exact M6.1–M6.4 work state. The audit asks whether background persistence creates a second authority root, whether stale or concurrent work can be advanced, whether a provider side effect can be replayed after crash, whether human activity can be overwritten by prepared work, whether caller-supplied records can impersonate live provider evidence, whether worker-side `REVISE` really returns to the worker, whether exact worker output survives into the next checkpoint, and whether worker output can terminate work by declaration.

## Authority audit

Supervisor status is explicitly readiness/orchestration state:

```text
READY / PREPARED / IN_FLIGHT / WAITING_HUMAN / COMPLETED
!= execution authority
```

A provider dispatch can be constructed only from an exact M6.2 `ADMIT` or `REVISE` plus exact M6.4 `KEEP_MOVING`, all bound to the current peer cursor. `ADMIT` means continue the admitted work; `REVISE` means send the exact bounded worker-side revision request defined by M6.2. Neither decision grants local process, filesystem, Git, network, merge, or arbitrary execution authority.

M6.4 remains a hard downstream attention gate. `REVISE + ASK_HUMAN` has no dispatch and enters `WAITING_HUMAN`; the background supervisor cannot use worker revision as a way to suppress human judgment.

## Second-truth audit

The supervisor does not replace `WorkHandoff`, `WorkIntentInterpretation`, `ContinuationAdmission`, `DynamicAttentionDecision`, or `ChatPeerCursor`. It stores those exact digest-bound records as evidence for orchestration transitions.

Recovered supervisor status is replay-derived from an append-only event chain. The head row stores only terminal sequence/digest integrity metadata.

The same `SupervisorDispatch` representation is used for ADMIT continuation and REVISE worker revision. The exact admission decision is part of the digest-bound payload and determines the deterministic Codexia envelope. No independent caller-authored dispatch mode is introduced.

## Live-evidence provenance audit

A structurally valid `ChatPeerObservation` or `ChatPeerTurn` is not sufficient proof that the live provider actually produced it. M6.5 therefore installs process-local, single-use provenance tickets at the M6.3 capture boundary:

```text
ChatGPTPeerLoop.observe()
→ exact observation digest ticket

ChatGPTPeerLoop.continue_admitted()
→ exact continuation-turn digest ticket

ChatGPTPeerLoop.revise_requested()
→ exact revision-turn digest ticket
```

A caller that merely constructs an equivalent observation/turn object does not receive a ticket and cannot use that object to advance supervisor state. Tickets bind the exact pre-event cursor and may also bind the exact `work_id`; they are consumed once.

`capture_external_chat(work_id, ...)` is the preferred driver surface. It binds the intended delegated work before reading the provider. An unbound live observation is accepted only when one active work owns the exact cursor; same-cursor multi-work ambiguity fails closed.

Post-crash reconciliation is the intentional peer-turn exception: M6.5 reconstructs an exact turn itself from the live current branch under `reconcile_in_flight_chat()`, then commits it through a narrowly scoped internal reconciliation allowance rather than minting a reusable external ticket.

## Exact worker-evidence audit

After a successful continuation or revision, the exact `ChatPeerTurn` is persisted in `PEER_TURN_RECORDED`. When the work is again `READY`, the background driver reads only the terminal event at the recovered sequence and returns a `ChatPeerTurn` to cognition only when:

- the terminal event is exactly `PEER_TURN_RECORDED`;
- its strict payload decodes successfully;
- its `after_cursor` is the exact recovered cursor;
- handoff and interpretation identities match;
- a second recovery confirms the work did not advance concurrently.

The checkpoint source therefore receives:

```text
(snapshot, peer_loop, latest_exact_peer_turn_or_none)
```

and can derive the next worker-authored candidate through `ChatPeerTurn.followup_proposal(...)`. This preserves:

```text
worker output != work completion
worker output = exact evidence for possible next proposal
```

A restart does not force cognition to reconstruct the worker answer from loose provider history; the exact turn remains in the durable event chain.

## Worker-revision audit

M6.2 defines `REVISE` as a worker-side revision request that should not interrupt the human. M6.5 preserves that semantics rather than treating REVISE as invisible Codexia-side replanning.

For `REVISE + KEEP_MOVING`:

```text
exact worker proposal
→ exact M6.2 REVISE + revision_request
→ exact M6.4 KEEP_MOVING
→ durable PREPARED revision dispatch
→ DISPATCH_STARTED
→ explicit Codexia-authored revision envelope
→ worker response
→ exact ChatPeerTurn
→ READY
```

The revision envelope contains the original worker candidate and exact bounded `revision_request`; it explicitly says the transport-role `user` message is authored by Codexia, not by the human. The returned assistant message remains WORKER-authored.

Revision dispatch uses the same no-replay lease, provenance, crash reconciliation, strict decoding, and human-precedence boundaries as ordinary continuation. It is not a second transport protocol with weaker rules.

## Background-driver audit

Persistence alone would leave the human as the scheduler. `BackgroundWorkDriver.drive_chat_until_blocked()` closes that gap with one bounded host-invoked event pump.

Each iteration recovers exact durable state, synchronizes work-bound live peer activity before any unclaimed provider dispatch, supplies exact persisted worker evidence to cognition while `READY`, routes every returned checkpoint through supervisor validation, executes one claimed continuation/revision when `PREPARED`, and then returns to exact recovery.

A single driver call can therefore cross:

```text
ADMIT → worker → ADMIT → worker
```

and:

```text
REVISE → worker revision → ADMIT → worker
```

without a human `continue` event between routine turns.

The checkpoint source does not gain orchestration or execution authority. It can return only exact M6.2/M6.4 records or no checkpoint; `record_checkpoint()` revalidates handoff, interpretation, proposal, admission, attention, and cursor before any dispatch can exist.

The loop remains explicitly bounded by `max_steps`, including repeated worker revision, so background cognition cannot become an unbounded autonomous loop.

## Provider replay audit

The provider send is the first M6.5 effect that cannot be made transactionally atomic with SQLite.

The supervisor writes a `DISPATCH_STARTED` event before either continuation or revision send and returns an ephemeral process-local lease. The lease is removed before the send call. It is not serialized and cannot be reconstructed after restart.

```text
crash after DISPATCH_STARTED
→ durable IN_FLIGHT
→ no automatic claim/retry
```

This intentionally prefers a recoverable stall over a duplicate remote side effect.

## Reconciliation audit

`reconcile_in_flight_chat()` performs no send. It reads the live branch and requires the exact pre-dispatch cursor prefix.

The expected first message is derived from the exact durable admission:

- ADMIT → deterministic continuation envelope;
- REVISE → deterministic revision envelope.

For either path, exact envelope + immediate assistant reply reconstructs one `ChatPeerTurn`; no delta or only the exact envelope remains `IN_FLIGHT`; foreign or intervening activity fails closed. Later activity after the exact pair remains outside the reconciled cursor for normal observation.

## Human-precedence audit

Before claiming `PREPARED` work, the driver rereads the live M6.3 branch through exact work-bound capture. Human activity therefore invalidates stale prepared continuation or revision before provider send.

`WAITING_HUMAN` can resume only from a fresh live M6.3 observation containing `EXTERNAL_USER`. Assistant-only activity cannot fabricate the human answer, and a hand-constructed observation cannot impersonate one.

Generic observation is forbidden while `IN_FLIGHT`; the attempted dispatch must be reconciled first so a Codexia transport-role `user` message cannot be mislabeled as HUMAN.

The first ChatGPT adapter still has one narrow liveness race: live state may change after durable claim but before provider send begins. The M6.3/revision pre-send reread prevents a stale send, while M6.5 conservatively retains `IN_FLIGHT` instead of assuming retry is safe. This is a fail-closed liveness limit, not an authority leak or replay path.

## Completion audit

`complete()` requires `READY` state and an explicitly CODEXIA-authored `WorkStatement`. WORKER output cannot mark itself complete.

M6.5 does not claim that Codexia's completion judgment is infallible; M6.6 is the daily-use quality proof.

## Persistence and concurrency audit

Every durable write uses an immediate SQLite transaction. A transition is appended only when the durable head still matches the sequence/digest recovered by the caller. A concurrent winner causes the stale caller to fail and recover again rather than fork history.

Replay validates canonical event digests, previous-digest chaining, contiguous sequence, exact event payload keys, nested M6 record integrity, legal state transitions, and terminal head metadata. REVISE dispatches are strict-key decoded and reject authority-shaped extra fields exactly like ADMIT dispatches.

## Known limits

The first M6.5 candidate is intentionally single-database/single-host orchestration, not a distributed scheduler. Its first proven transport adapter is the existing ChatGPT peer loop; other worker/tool transports have not yet been proven against this supervisor boundary.

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
return weak worker candidates to the worker,
carry exact worker evidence into the next checkpoint,
cross multiple routine worker turns without human scheduling,
stop at genuine human judgment,
and remember what remote dispatch may already have happened.
```

The persistence, provenance, revision, and bounded-driver layers advance delegated-work continuity without minting a second execution authority, trusting synthetic live evidence, or turning ambiguity into retry permission.
