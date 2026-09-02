# M3.2 Source / Recovery Audit

This audit tracks source-level findings before any frozen executable candidate is
accepted. A green test run cannot override an unresolved finding, and hosted CI
metadata without executed steps is not executable evidence.

Primary invariants:

```text
delegation cannot mint authority
restart cannot refund delegation budget, replay claims, or lifetime node slots
```

## Architecture reviewed

M3.2 intentionally leaves the M2.6 process-local `DelegationCoordinator` intact and
adds `SqliteDelegationCoordinator` as a separate durable implementation of the same
public orchestration surface. It subclasses the M2.6 coordinator only because the
existing model-control bridge deliberately performs a strict `isinstance` check;
all stateful public methods are overridden and the inherited RAM dictionaries are
not an authoritative source.

Durable state is an append-only root-scoped event chronology. Mutable coordinator
snapshots are not accepted as authoritative recovery input.

A fresh whole-diff source/security review was performed through development head
`943cdf79bd6afab19fc9da159a66f347383c29d2`. That review found one additional
error-boundary defect (finding 13 below); it was repaired and regression-covered.
No unresolved source-level finding remains that can mint delegation authority,
refund budget or lifetime node slots, replay a claimed request, or autonomously
resume work after restart.

## Findings and repairs

### 1. Do not persist a trusted mutable coordinator snapshot

A serialized `_DelegationNode` graph would allow `remaining_budget`, root lifetime
count, replay claims or lifecycle state to become independently editable values.
M3.2 instead persists only exact transition evidence and derives all mutable state
from replay.

### 2. Writer and recovery must share one transition function

Independent write-time and recovery state machines are a recurring source of
parity bugs. Both new writes and persisted replay use `apply_delegation_event()`.
A proposed mutation is first applied to replay state under the SQLite transaction;
the same event is what later recovery applies.

### 3. Multi-process resource reservation needs a database write boundary

A process-local lock cannot protect two coordinator instances. Every M3.2 mutation
starts `BEGIN IMMEDIATE` before root lookup/replay, so competing child reservation,
control-request claim and direct budget consumption serialize against one current
state. Concurrency regressions require one winner when two operations cannot both
fit.

### 4. Root lifetime count should be derived, not separately mutable

The root lifetime count is `len(all nodes ever created in the root event chain)`.
Nodes are never deleted. Completion/cancellation therefore cannot refund a slot and
there is no independent persisted counter to corrupt or reset.

### 5. Derived lookup indexes must not become authority

Delegation/escalation/continuation indexes exist only to find a root quickly. Root
recovery recomputes the expected index contents from the event chronology and
requires exact equality. Missing, extra or digest-rebound rows fail closed.

### 6. Event append, root head and derived index publication must be atomic

A new event row, updated root head and any new derived identity row are written
inside one SQLite transaction. Failure at any later step rolls back the earlier
writes. Root creation uses the same atomic publication shape.

### 7. Tail truncation needs explicit head metadata

Each root stores expected terminal sequence and digest. Recovery requires exact
event count, contiguous sequence, hash-chain linkage and exact terminal head match.
Deleting a tail event without correspondingly mutating the root head is detected.
The metadata is an integrity cross-check inside the same trust domain, not an
external anti-rollback anchor.

### 8. Public read/recovery paths need one SQLite snapshot

Delegation lookup, root head, event rows and derived-index verification are read
after one explicit `BEGIN` on one connection. Recovery cannot combine index/head
state from different commits.

### 9. SQLite handles must close deterministically on Windows

All public store operations allocate a connection inside `contextlib.closing` and
explicitly commit/rollback before leaving the scope. A TemporaryDirectory regression
exercises create/read/recover then immediately removes the SQLite files.

### 10. Exact M2.6 objects must be reconstructed, not accepted as loose bags

Persisted root/child envelopes, escalations and continuations are reconstructed via
the existing digest-validating M2.6 model constructors. Child replay additionally
checks the exact current parent id/digest, root, workspace, limits, depth,
capability subset and current remaining budget.

### 11. Missing workspace identity remains fail-closed

M2.6 `DelegationEnvelope` intentionally requires its canonical workspace directory
to resolve. M3.2 recovery reuses that exact constructor rather than weakening path
validation. If the persisted workspace no longer resolves, recovery blocks instead
of manufacturing a weaker envelope. This can reduce availability, but cannot refund
budget or create authority.

**Repaired:** an explicit regression now removes the persisted workspace directory
and requires recovery to fail with `DelegationPersistenceIntegrityError`.

### 12. Persisted unknown event kinds must be classified as integrity failures

Persisted enum/type conversion failures must not leak raw `ValueError`/`TypeError`
through the public recovery surface. The public managed event store normalizes those
failures to `DelegationPersistenceIntegrityError`, while the underlying transition
errors for valid requested mutations remain intact.

**Repaired:** an explicit regression corrupts a persisted event kind and requires the
M3.2 integrity error surface.

### 13. Caller-side mutation errors must not be mislabeled as persisted corruption

The first fail-closed wrapper caught `TypeError`/`ValueError` around the complete
`mutate_delegation()` / `mutate_escalation()` call. That scope was too broad: a
caller-supplied `prepare()` callback could legitimately reject a requested mutation
with one of those exceptions and be incorrectly reported as corrupt durable state.

**Repaired:** the managed store now guards and validates the prepare boundary
separately. Conversion failures arising while loading/replaying persisted bytes are
still normalized to `DelegationPersistenceIntegrityError`; caller-side prepare
conversion failures are re-raised unchanged. The regression also verifies that the
rejected prepare publishes no extra durable event.

## Regression surface

Current M3.2 regressions cover:

- child reservation and direct budget consumption across restart;
- exact control-request replay and same-id rebinding after restart;
- conservative claim retention when later model-control application fails;
- pending escalation + continue across restart with unchanged capability/budget;
- recursive cancel with completed descendants preserved;
- lifetime node-slot non-refund after cancellation;
- concurrent child reservation, control claim and budget consumption one-winner
  behavior;
- root-head tamper, tail deletion, event payload tamper and derived-index mismatch;
- corrupt persisted event-kind normalization;
- missing-workspace fail-closed recovery;
- caller-side prepare failures remaining distinct from persisted corruption and
  publishing no event;
- Windows-safe SQLite resource lifetime;
- absence of local action-authority methods on the durable coordinator.

## Hosted CI evidence

GitHub Actions runs observed for development heads `db30f038a49a78f6cf4ce82465fcb2635d68c22e`
and `943cdf79bd6afab19fc9da159a66f347383c29d2` both ended with failure metadata,
but all eight jobs reported an empty step list. No checkout, installation or test
step executed. These runs therefore provide neither a PASS nor an executable test
failure and must not be used as validation evidence.

## Open before freeze

- synchronize `docs/roadmap.md` and PR #20 with the reviewed implementation state;
- preserve the exact reviewed tree while creating one frozen Candidate 1 whose sole
  parent is merged M3.1 base
  `066983f6d61212cd1fd3307a0209e7664994ebf8`;
- run the focused real-Windows M3.2 gate on that exact candidate;
- run the full repository regression suite on that exact candidate;
- only after executable evidence may the PR move to Ready and post-Ready review.

No executable PASS is claimed yet.
