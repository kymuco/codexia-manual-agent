# M3.2 — Durable Bounded-Delegation Recovery

## Purpose

M3.2 moves the M2.6 bounded-delegation coordinator across the process-restart
boundary without turning persistence into authority or replenishing resources.

The governing invariants are:

```text
delegation cannot mint authority
restart cannot refund delegation budget, replay claims, or lifetime node slots
```

M3.2 does not replace the M2.6 in-memory coordinator. It adds a persistent
coordinator whose externally visible orchestration semantics remain bounded by the
same M2.6 envelopes, budgets, lifecycle, escalation and continuation models.

## Durable state that must survive restart

The current M2.6 coordinator keeps five security-relevant categories only in RAM:

- each delegation's remaining budget, including child-budget reservations;
- claimed `(request_id, request_digest)` model-control pairs;
- the root lifetime delegation count;
- exact pending escalations and operator continuations;
- `ACTIVE / WAITING_HUMAN / COMPLETED / CANCELLED` lifecycle state and result
  summaries.

Losing any of these on restart can recreate work capacity or replay opportunity.
M3.2 therefore reconstructs them from durable chronology rather than accepting a
serialized mutable coordinator snapshot as authoritative state.

## Persistence shape

Each root delegation owns one append-only event chain. A small root-head row stores
the expected terminal sequence and digest so tail deletion/truncation is detectable.
Events are exact, canonical, bounded and SHA-256 chained.

Event kinds:

- `root_created` — exact root `DelegationEnvelope`;
- `child_created` — exact child envelope; replay derives reservation and lifetime
  node-count increment;
- `control_request_claimed` — exact delegation/request id/request digest;
- `budget_consumed` — exact delegation id and consumed budget amount;
- `escalation_requested` — exact digest-bound `EscalationRequest`;
- `escalation_resolved` — exact digest-bound `OperatorContinuation`;
- `delegation_completed` — exact delegation id plus bounded result summary.

Cancellation remains a consequence of a `cancel` continuation and is reconstructed
recursively. M3.2 does not add a second cancellation authority or an autonomous
scheduler.

## One transition function for write and replay

The persistence writer and recovery path must not implement independent state
machines. Both apply the same deterministic transition function to the same event
model.

For each mutation the persistent coordinator:

```text
BEGIN IMMEDIATE
    -> read root head + exact event chain
    -> verify head metadata and hash chain
    -> replay deterministic M2.6 orchestration state
    -> validate requested transition
    -> append one exact event
    -> update root head
COMMIT
```

The event row, root head and any new derived identity index are published in that
same transaction. This serializes competing multi-process mutations around the same
SQLite write lock. Two processes therefore cannot both reserve the same remaining
budget, claim the same control request, or consume the same resource slice from one
pre-mutation state.

## Replay rules

### Root creation

`root_created` must be sequence zero and contain a valid exact root envelope. The
root id must equal the envelope's delegation id/root id. The node begins `ACTIVE`
with its exact allocated budget and root lifetime count `1`.

### Child creation

Replay requires the exact parent to exist and be `ACTIVE`. The child envelope must
bind the exact parent id and digest, same root/workspace/limits, valid depth,
capability subset and an allocation contained by the parent's current remaining
budget. The full child slice is subtracted immediately, the child begins `ACTIVE`,
and the root lifetime count increments. Neither completion nor cancellation refunds
the budget or node slot.

### Control-request claim

Claims are accepted only while the delegation is `ACTIVE`. Request ids are bound to
one SHA-256 request digest for the lifetime of the node. Exact replay and same-id
payload rebinding are rejected. A claim remains consumed even if later model-control
application fails.

### Budget consumption

Consumption is accepted only while `ACTIVE`, must consume at least one unit and must
fit the current remaining budget. Replay subtracts the exact amount. Restart never
reconstructs the original allocation as fresh remaining budget.

### Escalation

An exact `EscalationRequest` must bind the current delegation id and delegation
digest. The node moves `ACTIVE -> WAITING_HUMAN` and stores one exact pending
escalation.

An exact `OperatorContinuation` must bind the exact pending escalation id/digest.
`continue` changes only orchestration state back to `ACTIVE`; capabilities and
remaining budget are unchanged. `cancel` recursively marks live descendants
`CANCELLED`; already `COMPLETED` descendants remain completed.

### Completion

A delegation may complete only from `ACTIVE`, with a bounded non-empty result
summary and no live direct child. Completion never refunds resources.

## Recovery surface

Recovery reconstructs immutable snapshots, continuation lookup and root lifetime
count. It does not contact a model provider, launch child work, perform a tool call,
construct an `ActionProposal`, consume an `AuthorizationReceipt`, or execute any
mutation/network/Git action.

Corrupt hash chains, root-head mismatch, malformed persisted model objects,
impossible transitions, duplicate ids, missing persisted workspace identity and
resource underflow fail closed. Public recovery normalizes malformed persisted
enum/type conversion failures to `DelegationPersistenceIntegrityError` without
reclassifying caller-side mutation validation failures as durable corruption.

## Concurrency and resource lifetime

All mutating operations use `BEGIN IMMEDIATE`. Public read/recovery paths use one
explicit SQLite read transaction so root head and event rows come from one snapshot.
Connections are deterministically closed; M3.2 must not repeat M3.1's Windows
`sqlite3.Connection` lifetime mistake.

## Explicit non-claims

- No new delegated capability beyond M2.6 `read_workspace`.
- No transfer or persistence of local action authority through delegation.
- No `AuthorizationReceipt` API on the persistent coordinator.
- No budget or lifetime-node refund on restart, completion or cancellation.
- No automatic replay of a model-control request.
- No autonomous nested-agent execution.
- No scheduler, running lease or child-agent concurrency in M3.2.
- No hidden migration of existing M2.6 in-memory coordinator state.

## Current validation state

Core implementation, corruption hardening and fresh whole-diff source/security
review are complete on the development branch. The source audit has no unresolved
finding that can mint authority, refund resources or reopen replay after restart.

No executable PASS is claimed yet. Observed hosted GitHub Actions jobs terminate
before executing workflow steps, so the next validation boundary is a frozen
Candidate 1 over exact merged M3.1 base
`066983f6d61212cd1fd3307a0209e7664994ebf8`, followed by focused real-Windows and
full-repository gates.
