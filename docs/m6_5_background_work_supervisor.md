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

The supervisor owns orchestration continuity only. M6.2 still decides whether a candidate continuation is semantically admitted, M6.4 still decides whether human attention is needed, and existing execution/provider boundaries remain independent.

## Durable work state

`BackgroundWorkSupervisor` stores an append-only SQLite event chain for each exact `WorkHandoff`. The handoff id is the supervisor work id; the supervisor does not invent a second identity for the delegated objective.

Registration binds:

- exact `WorkHandoff`;
- exact `WorkIntentInterpretation`;
- exact M6.3 `ChatPeerCursor`.

Recovered state is derived by replaying the event chain. The `work_supervisor_heads` row is integrity metadata for the terminal event, not an independent mutable workflow truth.

The first status surface is deliberately small:

```text
READY
PREPARED
IN_FLIGHT
WAITING_HUMAN
COMPLETED
```

These are orchestration/readiness states. None is an execution permission.

## Checkpoint admission

A `READY` work may record an exact M6.2 proposal/admission plus exact M6.4 attention decision.

The supervisor revalidates all bindings against the current handoff, interpretation and chat cursor.

```text
ASK_HUMAN
→ WAITING_HUMAN

ADMIT + KEEP_MOVING
→ PREPARED

REVISE/REJECT + KEEP_MOVING
→ READY for more cognition/replanning
```

`REVISE` or `REJECT` is never transformed into provider-send permission.

## Live peer evidence

M6.5 does not trust an arbitrary caller merely because it can construct a valid `ChatPeerObservation` or `ChatPeerTurn`.

The M6.3 live capture methods mint bounded process-local, single-use evidence tickets for the exact observation/turn digest and its pre-event cursor. Supervisor-owned capture additionally binds the ticket to the exact `work_id` before the provider is read.

Preferred human/external observation path:

```text
BackgroundWorkSupervisor.observe_external_chat(work_id, peer_loop=...)
→ recover exact supervisor cursor
→ ChatGPTPeerLoop.observe(exact cursor)
→ exact live observation ticket bound to work_id
→ durable supervisor transition
```

A synthetic record receives no ticket and cannot advance supervisor state. An unbound live observation is accepted only when its exact chat cursor belongs to one active delegated work; if multiple works share that cursor, Codexia must use the work-bound supervisor path instead of guessing which work the human meant.

This is a provenance boundary, not an execution-authority mechanism.

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

A provider continuation therefore passes through two separate durable stages:

```text
exact admitted+no-attention checkpoint
→ PREPARED dispatch intent
→ durable DISPATCH_STARTED claim
→ IN_FLIGHT
→ at most one live-process provider attempt
```

`claim_dispatch()` writes `IN_FLIGHT` before any provider side effect and returns an ephemeral `SupervisorDispatchLease`. The live lease exists only inside that supervisor process. A restarted supervisor can recover the in-flight dispatch but cannot reconstruct a retry permission.

If the process dies before, during or after the remote send, recovery therefore does **not** resend automatically.

## Post-crash reconciliation

`reconcile_in_flight_chat()` is observation-only. It never calls the provider send path.

It compares the current branch against the exact pre-dispatch cursor and exact deterministic Codexia envelope:

- no visible delta → remain `IN_FLIGHT`; absence is not proof that the side effect did not happen;
- exact Codexia envelope only → remain `IN_FLIGHT` while waiting for the worker response;
- exact Codexia envelope followed by the assistant response → reconstruct the exact `ChatPeerTurn`, persist it, advance to the new cursor and return to `READY`;
- any other first/intervening activity → fail closed rather than relabel it as Codexia.

Later branch activity after the exact recovered Codexia/assistant pair is not silently swallowed: the recovered turn advances only through that pair, leaving later activity to be observed normally from the new cursor.

## Human precedence and attention

A checkpoint whose M6.4 result requires the human enters `WAITING_HUMAN` and has no pending dispatch.

A fresh live M6.3 turn can resume it only when that observation contains an actual `EXTERNAL_USER` message. The same live observation mechanism invalidates a merely `PREPARED` dispatch if the conversation advanced before it was claimed.

Thus:

```text
human writes before dispatch
→ old prepared continuation becomes stale
→ no provider send
→ current live branch becomes the new READY checkpoint
```

An `IN_FLIGHT` work is different: generic external observation cannot bypass its ambiguity. It must first reconcile the exact attempted dispatch.

## Completion

Worker output is not completion. `complete()` accepts only an explicitly CODEXIA-authored completion statement from a `READY` checkpoint.

This is still not truth-by-declaration: M6.6 is where real daily-use completion quality is tested. M6.5 only preserves the responsibility boundary that a worker cannot terminate work merely because it returned an answer.

## Concurrency and persistence

Each transition uses `BEGIN IMMEDIATE` and compares the expected terminal sequence/digest before appending. Concurrent stale supervisors fail rather than both advancing the same work.

Recovery checks:

- contiguous event sequence;
- exact previous-digest chain;
- canonical event digest;
- strict event payload keys;
- exact nested M6 records and digests;
- terminal head sequence/digest;
- legal state transition.

## Transport scope

The supervisor state model is about general delegated work, but this first background-dispatch proof is intentionally wired to the existing M6.3 ChatGPT peer loop. Other cognitive workers and tool/event adapters must preserve the same admission, attention, provenance and no-replay boundaries; M6.5 does not claim those adapters already exist.

## Exit gate

M6.5 is a complete candidate when the runtime proves:

1. multiple delegated works survive fresh-process recovery independently;
2. an exact `ADMIT + KEEP_MOVING` checkpoint becomes durable `PREPARED` work without sending anything by itself;
3. a claimed provider dispatch has at most one live-process send attempt;
4. restart cannot recreate the dispatch lease or replay an ambiguous send;
5. an exact already-visible Codexia/worker pair can be reconciled after crash without another send;
6. absence or partial visibility after crash remains `IN_FLIGHT` rather than manufacturing retry authority;
7. M6.4 human-attention outcomes become durable `WAITING_HUMAN` state;
8. fresh live external human activity can resume waiting work and invalidate stale prepared work;
9. synthetic live-evidence records cannot advance supervisor state;
10. ambiguous unbound observations cannot choose between multiple works sharing one chat cursor;
11. persisted event tamper/rebinding fails closed;
12. no supervisor state grants process, filesystem, Git, network, merge or other execution authority.
