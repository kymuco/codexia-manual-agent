# M6.5 — Background Work Supervisor

## Goal

M6.5 makes delegated work durable independently of whether the human is currently viewing its chat.

Primary property:

```text
human absence != work suspension
```

Primary safety boundary:

```text
background progress != autonomous authority
```

The supervisor owns orchestration continuity only. M6.2 still decides whether a worker candidate is admitted, revised, rejected, or escalated; M6.4 still decides whether human attention is needed; existing execution/provider boundaries remain independent.

## Durable work state

`BackgroundWorkSupervisor` stores an append-only SQLite event chain for each exact `WorkHandoff`. The handoff id is the supervisor work id; the supervisor does not invent a second identity for the delegated objective.

Registration binds the exact `WorkHandoff`, exact `WorkIntentInterpretation`, and exact M6.3 `ChatPeerCursor`. Recovered state is derived by replaying the event chain. The `work_supervisor_heads` row is integrity metadata for the terminal event, not an independent mutable workflow truth.

The status surface is deliberately small:

```text
READY
PREPARED
IN_FLIGHT
WAITING_HUMAN
COMPLETED
```

These are orchestration/readiness states. None is an execution permission.

## Checkpoint admission and worker revision

A `READY` work may record an exact M6.2 proposal/admission plus exact M6.4 attention decision. The supervisor revalidates all bindings against the current handoff, interpretation and chat cursor.

```text
M6.4 ASK_HUMAN
→ WAITING_HUMAN

M6.2 ADMIT + M6.4 KEEP_MOVING
→ PREPARED continuation dispatch

M6.2 REVISE + M6.4 KEEP_MOVING
→ PREPARED worker-revision dispatch

M6.2 REJECT + M6.4 KEEP_MOVING
→ READY for Codexia-side replanning
```

`REVISE` is not silently converted into Codexia-side thinking. It means what M6.2 defined: Codexia sends the exact bounded `revision_request` back to the worker without interrupting the human. The revision message preserves explicit CODEXIA semantic authorship even though the ChatGPT transport role is `user`.

Both ADMIT continuation and REVISE worker revision enter the same durable claim/no-replay boundary. The admission decision determines which deterministic Codexia envelope is sent; no new authority flag or generic execution mode is introduced.

## Live peer evidence

M6.5 does not trust an arbitrary caller merely because it can construct a valid `ChatPeerObservation` or `ChatPeerTurn`.

The M6.3 live capture methods mint bounded process-local, single-use evidence tickets for the exact observation/turn digest and its pre-event cursor. Supervisor-owned capture additionally binds the ticket to the exact `work_id` before the provider is read. The same provenance guard covers both admitted continuation turns and worker-revision turns.

Preferred external observation path:

```text
BackgroundWorkSupervisor.capture_external_chat(work_id, peer_loop=...)
→ recover exact supervisor cursor
→ ChatGPTPeerLoop.observe(exact cursor)
→ exact live observation ticket bound to work_id
→ record or deliberately discard that exact ticket
```

A synthetic record receives no ticket and cannot advance supervisor state. An unbound live observation is accepted only when its exact chat cursor belongs to one active delegated work; if multiple works share that cursor, Codexia must use the work-bound supervisor path instead of guessing which work the human meant.

This is a provenance boundary, not an execution-authority mechanism.

## Exact worker evidence into the next checkpoint

A successful provider turn is persisted as an exact `PEER_TURN_RECORDED` event. When the work returns to `READY`, `BackgroundWorkDriver` supplies the checkpoint source with the exact terminal persisted `ChatPeerTurn` if and only if that turn is the current durable terminal event and its `after_cursor` matches the recovered supervisor cursor.

The checkpoint source signature is conceptually:

```text
(snapshot, peer_loop, latest_exact_peer_turn_or_none)
→ (proposal, admission, attention) | None
```

The worker response therefore does not disappear into generic chat history. Codexia can derive the next `ContinuationProposal` from `ChatPeerTurn.followup_proposal(...)`, preserving the exact WORKER-authored statement and new checkpoint digest. A fresh recovery after process restart reconstructs the same evidence from the append-only event log.

If the durable work advances while that terminal evidence is being derived, the driver fails rather than using a stale worker turn.

## Bounded background driver

Durable state alone is not the M6.5 product property. `BackgroundWorkDriver` is the event-driven pump that keeps one delegated work moving across routine worker turns without requiring the human to act as the scheduler.

One host invocation can perform the bounded loop:

```text
recover exact work
→ synchronize work-bound live peer activity
→ READY: give Codexia the exact latest persisted worker turn
→ record exact M6.2 + M6.4 checkpoint
→ PREPARED: durably claim one continuation/revision dispatch
→ execute one M6.3 peer turn
→ persist exact worker response
→ READY again
→ continue until blocked or budgeted
```

The checkpoint source is cognition-only input. It cannot bypass `BackgroundWorkSupervisor.record_checkpoint()`, so M6.2 admission, M6.4 attention, exact cursor binding, and all existing no-authority invariants are revalidated before any dispatch can be prepared.

The driver stops on explicit bounded reasons:

- `NO_CHECKPOINT` — Codexia-side cognition has no next checkpoint to admit now;
- `WAITING_HUMAN` — M6.4 requires genuine human judgment;
- `IN_FLIGHT_AMBIGUOUS` — an already-claimed provider effect cannot be safely replayed;
- `COMPLETED` — the durable supervisor work is terminal;
- `STEP_BUDGET` — the bounded pump reached its explicit iteration ceiling.

The step budget bounds both ordinary continuation chains and repeated worker revision chains. A `REVISE → worker revision → REVISE` sequence therefore cannot become unbounded background activity.

Before claiming a `PREPARED` dispatch, the driver rereads the live chat through a work-bound capture. Ordinary human activity therefore invalidates the stale prepared dispatch before provider send and becomes the new exact `READY` cursor. If a conversation race occurs only after the durable claim, the driver never converts that ambiguity into retry permission; it stops at the existing `IN_FLIGHT` reconciliation boundary.

M6.5 intentionally does not add cron, a resident daemon, timers, or OS wake-up semantics. A host/runtime may invoke the bounded driver whenever work is ready to advance; the important property is that routine continuation and revision cycles no longer require a human `continue` turn.

## The no-replay dispatch boundary

The dangerous case is a process crash around a remote provider send.

A naïve background runner could do:

```text
send message
→ crash before durable commit
→ restart
→ send the same message again
```

M6.5 explicitly forbids that replay.

Every provider continuation or worker revision passes through:

```text
exact checkpoint + KEEP_MOVING
→ PREPARED dispatch intent
→ durable DISPATCH_STARTED claim
→ IN_FLIGHT
→ at most one live-process provider attempt
```

`claim_dispatch()` writes `IN_FLIGHT` before any provider side effect and returns an ephemeral `SupervisorDispatchLease`. The live lease exists only inside that supervisor process. A restarted supervisor can recover the in-flight dispatch but cannot reconstruct a retry permission.

If the process dies before, during or after the remote send, recovery therefore does **not** resend automatically.

## Post-crash reconciliation

`reconcile_in_flight_chat()` is observation-only. It never calls the provider send path.

It compares the current branch against the exact pre-dispatch cursor and the deterministic expected envelope for that admission decision:

- ADMIT → exact Codexia continuation envelope;
- REVISE → exact Codexia worker-revision envelope.

For either path:

- no visible delta → remain `IN_FLIGHT`; absence is not proof that the side effect did not happen;
- exact Codexia envelope only → remain `IN_FLIGHT` while waiting for the worker response;
- exact envelope followed by the assistant response → reconstruct the exact `ChatPeerTurn`, persist it, advance to the new cursor and return to `READY`;
- any other first/intervening activity → fail closed rather than relabel it as Codexia.

Later branch activity after the exact recovered Codexia/assistant pair is not silently swallowed: the recovered turn advances only through that pair, leaving later activity to be observed normally from the new cursor.

## Human precedence and attention

A checkpoint whose M6.4 result requires the human enters `WAITING_HUMAN` and has no pending dispatch — including a semantic REVISE that M6.4 judges to intersect a material human choice.

A fresh live M6.3 turn can resume waiting work only when that observation contains an actual `EXTERNAL_USER` message. The same live observation mechanism invalidates a merely `PREPARED` dispatch if the conversation advanced before it was claimed.

```text
human writes before dispatch
→ old prepared continuation/revision becomes stale
→ no provider send
→ current live branch becomes the new READY checkpoint
```

An `IN_FLIGHT` work is different: generic external observation cannot bypass its ambiguity. It must first reconcile the exact attempted dispatch.

## Completion

Worker output is not completion. `complete()` accepts only an explicitly CODEXIA-authored completion statement from a `READY` checkpoint.

This is still not truth-by-declaration: M6.6 is where real daily-use completion quality is tested. M6.5 only preserves the responsibility boundary that a worker cannot terminate work merely because it returned an answer.

## Concurrency and persistence

Each transition uses `BEGIN IMMEDIATE` and compares the expected terminal sequence/digest before appending. Concurrent stale supervisors fail rather than both advancing the same work.

Recovery checks contiguous event sequence, exact previous-digest chaining, canonical event digests, strict event payload keys, exact nested M6 records/digests, terminal head metadata, and legal state transitions. REVISE dispatches use the same strict `SupervisorDispatch` representation and reject authority-shaped extra fields just like ADMIT dispatches.

## Transport scope

The supervisor state model is about general delegated work, but this first background-dispatch proof is intentionally wired to the existing M6.3 ChatGPT peer loop. Other cognitive workers and tool/event adapters must preserve the same admission, attention, provenance and no-replay boundaries; M6.5 does not claim those adapters already exist.

## Exit gate

M6.5 is a complete candidate when the runtime proves:

1. multiple delegated works survive fresh-process recovery independently;
2. exact `ADMIT + KEEP_MOVING` and `REVISE + KEEP_MOVING` checkpoints become durable `PREPARED` provider work without sending by themselves;
3. REVISE sends the exact worker-side revision request rather than silently turning revision into Codexia-only cognition;
4. an exact persisted worker response is fed into the next Codexia checkpoint after ordinary execution and after crash recovery;
5. a claimed provider dispatch has at most one live-process send attempt;
6. restart cannot recreate the dispatch lease or replay an ambiguous continuation or revision;
7. exact already-visible Codexia/worker pairs can be reconciled after crash without another send;
8. absence or partial visibility after crash remains `IN_FLIGHT` rather than manufacturing retry authority;
9. M6.4 human-attention outcomes become durable `WAITING_HUMAN` state and suppress both continuation and revision dispatch;
10. fresh live external human activity can resume waiting work and invalidate stale prepared work;
11. synthetic live-evidence records cannot advance supervisor state;
12. ambiguous unbound observations cannot choose between multiple works sharing one chat cursor;
13. one bounded driver call can cross multiple continuation/revision worker turns without human `continue` scheduling;
14. repeated worker revision is explicitly step-bounded and cannot loop indefinitely;
15. persisted event tamper/rebinding fails closed;
16. no supervisor or driver state grants process, filesystem, Git, network, merge or other execution authority.
