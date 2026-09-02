# M2.6 — Bounded Delegation and Human Escalation

## Purpose

M2.6 adds an in-memory orchestration boundary after M2.5.1. It allows one bounded
piece of work to delegate a smaller read-only piece of work and to pause for an
operator decision without turning orchestration state into local action authority.

The primary invariant is:

```text
delegation cannot mint authority
```

A delegated task is not an `ActionProposal`, a delegation decision is not an
`AuthorizationReceipt`, and operator continuation does not authorize a workspace,
process, network, Git, destructive, or outside-workspace action.

Existing M2.x action authority remains independent:

```text
delegation
    != write_workspace authority
    != execute_process authority
    != network_access authority
    != git_commit authority
    != git_push authority
    != delete_files authority
```

If delegated work needs one of those actions, it must escalate. A caller may then
construct the corresponding ordinary M2.x proposal and obtain whatever HUMAN
receipt that action requires. Resuming the delegation afterwards does not reuse or
inherit that receipt.

## Delegable capability surface

M2.6 v1 admits only the existing read-only workspace capability into a delegation
envelope:

```text
read_workspace
```

A root or child delegation that asks to carry `write_workspace`,
`execute_process`, `network_access`, `git_commit`, `git_push`, `delete_files`, or
`outside_workspace` fails closed.

Child capabilities must also be a subset of the parent's delegated capabilities.
Therefore child authority can only stay the same or shrink; it cannot grow.

This is intentionally narrower than the complete local capability registry. Later
milestones may add another explicitly delegable non-authority surface, but mutation
or external authority must never appear merely because a parent task possessed or
used it elsewhere.

## Exact delegation envelope and lineage

Each delegation envelope is immutable, canonical and digest-bound. It contains:

- one UUID delegation id;
- exact root and optional parent delegation identity;
- exact parent delegation digest for child lineage;
- depth;
- canonical existing workspace root;
- bounded task text;
- exact sorted delegated capability set;
- an exact resource-budget allocation;
- root depth/total-node limits;
- creation timestamp;
- SHA-256 digest of the canonical envelope payload.

A child binds the exact parent id and parent digest. Reconstructing a child under a
different parent therefore changes the child digest and lineage identity.

Every valid envelope also satisfies the static lineage lower bounds implied by its
own fields: `depth <= max_depth` and `depth + 1 <= max_total_delegations`.
Depth-one children bind the root as their parent; deeper children cannot claim that
they skipped directly to the root. The coordinator separately owns the dynamic
lifetime tree count and remaining-budget state.

The canonical workspace path here is an orchestration identity. M2.6 does not add
a new filesystem sandbox, namespace pin or mutation boundary; existing read-only
workspace inspection and M2.x mutation boundaries remain responsible for those
properties.

No proposal id, proposal digest, receipt id, receipt digest, approval value, or
receipt-consumption state is part of the delegation envelope.

## Resource budgets

M2.6 budgets are consumable resources, not per-child copies.

The v1 budget dimensions are:

- model turns;
- read-only tool calls;
- cumulative model characters.

When a parent creates a child, the child's entire requested budget slice is
reserved from the parent's remaining budget immediately. Reserved budget is not
silently refunded when the child completes or is cancelled.

Example:

```text
parent remaining:  8 turns / 8 tools / 100000 chars
child allocation:  3 turns / 2 tools / 30000 chars
--------------------------------------------------
parent remaining:  5 turns / 6 tools / 70000 chars
```

This prevents delegation fan-out from multiplying a parent's execution budget.
Nested reservation preserves the same conservation rule: a grandchild consumes a
slice already reserved to its parent, not a fresh copy of the root budget.

A root also binds a maximum delegation depth and maximum total lifetime node count.
Completing or cancelling a child does not refund its reserved budget or its node
slot; this prevents churn from turning a bounded tree into unbounded work.

## Lifecycle

The M2.6 orchestration lifecycle is separate from `ActionLifecycle`:

```text
ACTIVE
  |  \
  |   \ complete
  |    -> COMPLETED
  |
  +-> human escalation -> WAITING_HUMAN
                            |       \
                            |        \ cancel
                            |         -> CANCELLED
                            |
                            +-- continue --> ACTIVE
```

`WAITING_HUMAN` blocks delegation, model-control request claims, budget
consumption, capability use and completion until the exact pending escalation is
resolved.

A parent cannot become `COMPLETED` while a direct child remains active or waiting.
Cancellation recursively cancels live descendants while retaining already
completed child records.

## Model control boundary

M2.6 has a separate strict model-control protocol. It is deliberately not added to
the existing M1.1 `parse_model_reply()` surface.

The model may emit only:

```text
delegate_request
escalation_request
```

A `delegate_request` contains only a bounded request id, child task, delegable
capability subset and budget slice. An `escalation_request` contains only bounded
request id, reason, optional desired capability/action and summary.

The model cannot supply workspace/root/parent delegation identity, delegation
ids, proposal ids, receipt ids, authority state or approval fields. The bridge
derives the current parent/workspace/lineage locally from coordinator state.

Both request variants are canonical SHA-256 digest-bound. JSON duplicate keys and
non-finite constants fail closed. Before a model request is applied, the current
delegation atomically claims its exact `(request_id, request_digest)` pair. Exact
replay and same-id/different-payload rebinding are rejected. A claim is
conservatively retained if later application fails, so a failed request cannot be
replayed into changed state.

This registry is process-local M2.6 state, not durable M3 replay protection.

## Human escalation

M2.6 defines explicit escalation reasons:

- `novel`;
- `destructive`;
- `external`;
- `ambiguous`;
- `policy_sensitive`.

An escalation is immutable and digest-bound to:

- the exact delegation id and digest;
- one reason;
- an optional desired local capability;
- an optional desired action name;
- a bounded human-readable summary.

The desired capability is a request for operator attention, not a capability grant.
It may name `git_push` or another non-delegable capability without adding that
capability to the delegation envelope.

## Operator continuation

An operator resolves the exact pending escalation with either:

```text
continue
cancel
```

The continuation record binds the exact escalation id/digest, operator identity,
decision, optional bounded note, timestamp, and its own digest.

A `continue` decision only changes orchestration state from `WAITING_HUMAN` back to
`ACTIVE`. It does not alter the delegation capability set and does not replenish
budget. `OperatorContinuation` is not an `AuthorizationReceipt` and contains none
of the proposal/receipt binding fields required by `LocalApprovalAuthority` for an
authorized M2.x action.

Consequently this sequence is deliberately invalid:

```text
child requests git_push
    -> HUMAN says continue
    -> child now has git_push authority   # forbidden
```

The valid sequence is:

```text
child requests git_push
    -> escalation
    -> separate ordinary M2.5/M2.5.1 git.push.v1 proposal
    -> separate HUMAN authorization and exact execution
    -> operator continuation supplies non-authority workflow context
    -> child resumes with the same read-only delegation envelope
```

## M2.6 / M3 boundary

M2.6 v1 models continuation records and in-memory resume semantics only. It does
not persist a suspended workflow across process restart. The operator/proxy may
perform an independently authorized action outside the delegation and then resume
the same in-memory delegation with a bounded note/result summary.

Durable chronology, interruption state, receipt persistence, restart recovery and
deterministic replay remain M3.

## M2.6 / M5 boundary

M2.6 does not launch autonomous nested-agent execution. It establishes the
bounded child record, budget reservation, lineage, escalation and continuation
contract only.

Actually running child agent loops concurrently introduces another problem: work
must have a running lease and its real turns/tool/model-character consumption must
be charged atomically against the reserved runtime budget. Without that, execution
could race further delegation or budget spending. That execution scheduler belongs
to bounded automation work rather than being silently smuggled into M2.6.

## Explicit non-claims

- No delegation of mutation authority.
- No inheritance or transfer of `AuthorizationReceipt` objects.
- No generic agent-to-agent capability transfer.
- No automatic execution of an escalated action.
- No implicit approval because a parent previously received approval.
- No privilege growth through child creation.
- No budget growth through fan-out or nested delegation.
- No budget/node-slot refund through child churn.
- No persistent orchestration state across process restart in M2.6.
- No autonomous nested-agent scheduler in M2.6.
- No new filesystem sandbox or mutation boundary in M2.6.
- No unattended destructive or external writes.
