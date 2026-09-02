# M2.6 Source Audit — Bounded Delegation and Human Escalation

This note records static invariants and implementation review findings for M2.6.
It is not executable PASS evidence.

Exact merged M2.5.1 base:

```text
b8431832bf54de9f24749437978f64fe96fb74a3
```

Primary invariant:

```text
delegation cannot mint authority
```

## Authority separation

The M2.6 package defines no new `Capability`. `DELEGABLE_CAPABILITIES` contains
only the pre-existing `read_workspace` capability.

The coordinator does not own `LocalApprovalAuthority`, does not accept an
`AuthorizationReceipt`, does not consume a receipt, and has no generic action
execution API. Its operator continuation record is a separate orchestration model
and is not M2.x mutation authority.

A requested mutation/external capability can appear only as data in an
`EscalationRequest`. It is never inserted into a delegation envelope.

## Static findings and repairs

### 1. Delegation capability growth

Risk: a generic child capability list could turn delegation into authority
transfer.

Repair: root and child envelopes admit only `read_workspace`; coordinator child
creation additionally requires the child capability set to be a subset of the
parent envelope. The public low-level child factory repeats the subset check so
calling it directly cannot create static privilege growth.

### 2. Budget multiplication through fan-out

Risk: copying a parent's full budget into every child would turn an `N`-turn
parent into unbounded `N * children` work.

Repair: child allocation is reserved atomically from the parent's remaining
budget before child publication. Reserved budget is never silently refunded.
Depth and total-node limits are root-bound.

### 3. Concurrent double reservation

Risk: two threads could both observe the same remaining parent budget and publish
two children whose combined allocation exceeds it.

Repair: node lookup, state check, remaining-budget check, subtraction, child
publication and root-count update occur under one coordinator `RLock`. A focused
concurrency regression requires exactly one winner when two oversized concurrent
reservations compete for the same remaining slice.

### 4. Public child-factory bypass

Risk: coordinator checks alone would not protect callers that used
`DelegationEnvelope.create_child()` directly.

Repair: the public factory independently verifies capability subset, root depth
limit and that one child allocation does not exceed the parent's total envelope
budget. Dynamic remaining budget and lifetime tree count remain coordinator state
and are checked there atomically.

### 5. Human continuation becoming authority

Risk: an operator `continue` response could be interpreted as approval for the
capability/action named by the escalation.

Repair: continuation changes only `WAITING_HUMAN -> ACTIVE`; the immutable
delegation envelope and remaining budget are unchanged. A regression escalates
for `git_push`, continues, and then proves `assert_capability(git_push)` still
fails.

### 6. Escalation replay / cross-binding

Risk: an old or different escalation could resume a delegation that is waiting on
another decision.

Repair: resolution requires exact stored escalation id, SHA-256 digest, complete
payload equality and equality with the node's exact pending escalation. Replaying
a resolved request or substituting an earlier request fails closed.

### 7. Model-controlled lineage or authority fields

Risk: a model delegation request could choose workspace/root/parent identity,
proposal ids, receipts, approval flags or other local authority state.

Repair: the separate M2.6 control parser uses exact-key schemas. A model may
supply only bounded child task/capabilities/budget or bounded escalation intent.
The local bridge supplies the current parent delegation id and derives workspace
and lineage from coordinator state.

### 8. Mutation capability hidden in delegation request

Risk: the model could ask for `git_push`, `write_workspace`, process/network or
other mutation capability in a child envelope.

Repair: `delegate_request` rejects every non-delegable capability and directs the
caller to `escalation_request`. An escalation may name the desired capability as
non-authority intent only.

### 9. Accidental M1.1 protocol widening

Risk: adding delegation request types to the existing `parse_model_reply()` would
silently expose delegation in the old read-only agent loop.

Repair: M2.6 uses a separate parser and bridge. Regressions prove the M1.1 parser
still rejects both `delegate_request` and `escalation_request`.

### 10. Structural exception leakage

Risk: direct enum/capability conversion could expose raw `ValueError` and make
fail-closed handling inconsistent.

Repair: public delegation model factories normalize unsupported escalation
reasons, requested capabilities and continuation decisions to
`InvalidDelegationError`; the model-control protocol normalizes structural errors
to `ProtocolError`.

### 11. Waiting-state work continuation

Risk: a delegation could keep spawning work or spending budget after it had asked
for a human decision.

Repair: child creation, model-control claim, budget consumption, capability
assertion and completion all require `ACTIVE`. `WAITING_HUMAN` therefore freezes
that node until an exact operator continuation or cancellation is recorded.

### 12. Parent completion with live descendants

Risk: a parent could report terminal completion while delegated child work was
still active or waiting.

Repair: parent completion fails while any direct child is neither `COMPLETED` nor
`CANCELLED`. Cancellation recursively cancels live descendants while preserving
already completed child records.

### 13. Ambiguous model JSON / weak request provenance

Risk: ordinary `json.loads` accepts duplicate keys with last-value-wins semantics
and non-finite constants, and a parsed orchestration request without its own digest
has weaker provenance than existing hardened model request protocols.

Repair: the M2.6 parser rejects duplicate JSON keys and `NaN`/Infinity constants.
Both request variants are canonical SHA-256 digest-bound, including schema,
request id, request type and their complete bounded semantic payload.

### 14. Model control-request replay / request-id rebinding

Risk: replaying one valid `delegate_request` could create duplicate children, or
the same request id could later be reused for a different payload.

Repair: before applying a model control request, the bridge atomically claims the
exact `(request_id, request_digest)` in the current delegation node. Any repeat is
rejected as replay; the same id with another digest is rejected as rebinding. A
claim is conservative and is not refunded if later application fails, so a failed
request cannot be replayed into a changed state.

### 15. Non-canonical direct immutable-object construction

Risk: factory-created envelopes were canonical, but a caller using the exported
frozen dataclass constructor directly could construct a digest-valid object with a
non-canonical absolute workspace spelling, non-canonical bounded text, depth above
the root limit, or impossible self-lineage if it recomputed the digest itself.
That would weaken the statement that every valid M2.6 object has one canonical
semantic representation.

Repair: `DelegationEnvelope.__post_init__` canonicalizes the exact existing
workspace root, canonicalizes bounded text/capabilities, enforces `depth <=
max_depth`, rejects child self-parent/root-id collisions, and only then verifies
the digest against the canonical payload. `EscalationRequest` and
`OperatorContinuation` likewise canonicalize their bounded text/enum fields before
digest verification. Equivalent spellings normalize to the same object payload;
semantic changes still invalidate the digest.

### 16. Direct model-control factory/parser mismatch

Risk: the JSON parser normalized string enum/capability values before constructing
a request, but a direct call to `DelegateWorkRequest.create()` or
`EscalateWorkRequest.create()` could reach `.value` on an unnormalized string and
leak `AttributeError` rather than satisfying the same strict protocol contract.

Repair: both public request factories perform the same request-id, text,
capability, enum and budget normalization as the parser before creating their
canonical digest. Direct API and parsed JSON therefore share one fail-closed
semantic contract.

### 17. Static lineage and total-node lower-bound bypass

Risk: the coordinator enforced `max_total_delegations`, but the exported low-level
child factory could still create a child under a root whose total-node limit was
`1`. A recomputed direct envelope could also claim a depth that already implies
more lineage nodes than the root limit, or claim depth-one/nested lineage with an
impossible parent/root relationship.

Repair: every envelope now requires `depth + 1 <= max_total_delegations`, which is
the minimum number of nodes required by its own lineage. The low-level child
factory enforces the same bound before construction. Depth-one children must bind
the root as parent; deeper children may not skip directly to the root. Dynamic
sibling/lifetime tree counts still remain coordinator-owned state. Focused
regressions cover the root-only limit and impossible depth/parent combinations.

## M2.6 / M3 boundary

M2.6 state is process-local and intentionally non-durable. It does not claim
restart-safe continuation, persistent event chronology, receipt persistence or
deterministic replay across process restart. Those remain M3. The in-memory
model-control replay registry is deliberately not presented as M3 durability.

## M2.6 / M5 boundary

The M2.6 parser and bridge are not wired into the existing M1.1 read-only agent
loop and do not start autonomous nested-agent execution. M2.6 establishes the
safe delegation/escalation contract; unattended bounded automation remains M5.

This separation is deliberate. Starting a nested loop would require a separate
running-work lease and atomic runtime budget charging so child execution could not
race child creation or other budget consumption. M2.6 does not pretend that
problem is already solved.

## Validation status

Implementation and regression files exist in the Draft PR, but no focused or full
suite PASS is claimed until a frozen exact head is executed on the target Windows
environment.
