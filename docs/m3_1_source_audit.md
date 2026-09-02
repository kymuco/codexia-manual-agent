# M3.1 — Source and Recovery Audit

## Audit target

PR #19 implements the first M3 persistence state machine over the merged M2.6
base. The reviewed boundary is intentionally limited to durable read-only session
chronology, provider/tool recovery state, and durable M2.x authorization-consumption
evidence.

Primary invariant:

```text
restart may recover state, but recovery may never recreate spent authority
```

M2.6 delegation coordinator persistence is deliberately deferred to M3.2 because
its budget reservations, request replay claims, lifetime node counts and
escalation/continuation state form a separate crash-atomic state machine.

## Findings repaired before candidate validation

### 1. JSON manifest was insufficient as authoritative history

The pre-M3 `JsonSessionStore` intentionally stored metadata rather than an exact
chronology. M3.1 therefore uses a separate SQLite ledger as authoritative history
while retaining JSON manifests only as discovery/summary state.

### 2. First-event foreign-key publication order was initially invalid

The first draft attempted to insert `session_started` before the parent session row
while foreign keys were enabled. The final store computes the event first, inserts
the session row, inserts the event, and commits both in one transaction.

### 3. Generic event bags would weaken replay guarantees

Every event kind now has an exact payload-key schema, bounded canonical JSON and a
digest-bound immutable receipt. Duplicate JSON keys and non-finite constants fail
closed when persisted payloads are read.

### 4. M2.x proposal/receipt JSON could not be trusted by shape alone

Embedded `ActionProposal` and `AuthorizationReceipt` payloads are reconstructed
through the existing M2.x model classes so their original digest contracts are
validated again.

### 5. Generic append could forge authority chronology

`action_proposed`, `authorization_recorded`, `authorization_consumed`,
`action_executed` and `action_observed` are excluded from generic `append()` and use
narrow record/consume APIs with predecessor checks.

### 6. Process-local receipt consumption did not survive restart

`LocalApprovalAuthority` now accepts an optional consumption-registry protocol. Its
default remains the existing process-local registry, while M3 can inject a
session-bound durable implementation without making persistence an authorization
source.

### 7. Receipt UUID alone was not sufficient durable provenance

Durable consumption requires the exact tuple:

```text
receipt_id
receipt_digest
proposal_id
proposal_digest
```

A separately valid receipt that reuses the same UUID but has another actor/time/
digest cannot be substituted for the recorded receipt.

### 8. Consumption event and replay row needed one commit point

The `authorization_consumed` chronology event and `consumed_authorizations` row are
published in one `BEGIN IMMEDIATE` transaction. A failed second insert rolls the
whole transition back.

### 9. Concurrent double consumption needed a single winner

SQLite writer serialization plus the durable receipt key guarantees one winner.
The second attempt fails closed and no second consumption event is published.

### 10. Consumption-row/event drift was originally detected too late

The final implementation treats the consumption event and consumed row as a
bijection. Missing or rebound state is detected by load/recovery and before a new
consume or execution can advance chronology.

### 11. Observation and session completion could initially advance after consumed-state corruption

The same consumption-bijection validation now runs before observation publication
and before terminal session completion. Corrupted authority evidence cannot be
papered over by later terminal events.

### 12. Model calls needed a durable pre-call boundary

`model_request_started` is committed before `provider.send()`. A response is only
recorded after the provider returns. A crash between them therefore becomes an
explicit unknown remote outcome rather than an implicit retry opportunity.

### 13. Provider failure interruption could not erase an unresolved request

A provider-failure `run_interrupted` event must name the exact unresolved local
request id. Recovery retains that request and returns
`UNKNOWN_PROVIDER_OUTCOME`; it never silently resends it.

### 14. Multiple active runs could branch one persistent conversation

M3.1 now admits at most one active run and at most one unresolved provider request
per persistent session. The rule is checked transactionally before append and
again during recovery.

### 15. Provider identity substitution needed explicit rejection

Every request/response provider id must equal the provider bound by
`session_started`. Runtime append and recovery both reject provider substitution.

### 16. Conversation identity could be overwritten or erased during replay

Recovery rejects a switch between non-null conversation ids. A response that
omits conversation metadata does not erase the last durable conversation identity.
Requests must carry the exact recovered conversation when one exists.

### 17. Run totals could not depend only on a terminal summary event

Turns, model characters and tool-call totals are reconstructed from durable model
responses and tool observations. `run_completed` counters are only a cross-check,
so a crash before a terminal run event does not erase already proven progress.

### 18. Terminal session state could hide unfinished authority

`session_completed` is rejected while a proposal awaits authorization or an ALLOW
authorized action lacks terminal observation. Recovery independently checks the
same condition.

### 19. Manifest representation parity broke resume identity

Frozen event arrays become tuples while JSON-derived manifest capabilities are
list-like. Stable identity comparison was normalized so a valid session does not
fail resume merely because of list/tuple representation.

### 20. A lagging JSON manifest could overwrite authoritative recovered state

`PersistentRunAgentService` replays the SQLite ledger before a safe continuation,
repairs conversation identity and cumulative counters in the summary manifest, and
only then contacts the provider.

### 21. Recovery inspection was initially coupled to the currently configured provider

Pure `recover()` now validates the ledger against the persisted session identity
without requiring the current runtime provider. Actual `resume_and_run()` performs
a separate exact provider-id check. Another provider may inspect a session but may
not continue its conversation.

### 22. Durable-registry validation relied on assertion/type assumptions

The durable registry now validates exact binding inputs explicitly before calling
the store. Structural caller errors cannot rely on `assert` execution or accidental
hashability.

### 23. SQLite connection context managers did not close database handles

The first frozen Windows candidate exposed a portability defect that POSIX cleanup
had hidden. Python's `sqlite3.Connection` context manager commits or rolls back on
`__exit__`, but it does not close the connection. M3.1 used short-lived
`with self._connect()` scopes and therefore relied on garbage collection to release
the underlying database handle. Windows correctly kept the SQLite database/journal
path busy during `TemporaryDirectory` cleanup.

The public M3.1 store now preserves the original transaction `__exit__` semantics
and deterministically closes the underlying SQLite handle in a `finally` block at
the end of every internal connection scope. A dedicated cross-platform regression
asserts that the underlying connection is unusable immediately after scope exit,
so this property no longer depends on Windows unlink behavior.

### 24. Direct corruption-test connections repeated the same lifetime mistake

The SQLite tamper/corruption regressions also used bare `with sqlite3.connect(...)`,
which does not close the connection. Those explicit test-only connections now use
`contextlib.closing`, so the tests do not themselves hold Windows file handles past
the intended scope.

### 25. Pure recovery did not independently enforce the linear runtime invariant

Candidate 2 passed both target-Windows gates, but post-Ready source review found a
recovery-only gap. SQLite write-time validation already rejected overlapping runs
and more than one unresolved provider request per session, while `recover_session()`
only rejected duplicate run ids and a second unresolved request inside the same
run. A directly supplied hash-chained chronology could therefore replay two open
runs, or start a later run while an interrupted provider request still had an
unknown remote outcome.

Pure recovery now independently rejects:

- any `run_started` while another run is open;
- any `run_started` while a provider request outcome remains unresolved;
- any `model_request_started` while any provider request is already unresolved in
  the persistent session.

Dedicated regressions construct valid digest-linked event receipts directly and
call `recover_session()` without SQLite, proving the recovery layer itself enforces
the same linear runtime state machine rather than inheriting safety accidentally
from the writer.

### 26. Manifest publication preceded authoritative session creation

External review of Candidate 3 found that `PersistentRunAgentService.start_and_run()`
used `RunSessionService.start()`, which saved the JSON discovery manifest before
`SESSION_STARTED` was committed to the authoritative ledger. A process failure or
ledger-start failure in that window could therefore expose a user-visible session
that every later recover/resume attempt rejected as unknown to M3.

`RunSessionService` now has a non-publishing `prepare()` seam that performs the same
workspace and prompt validation and creates the same manifest without saving it.
Its existing `start()` method preserves legacy behavior by calling
`prepare()` and then saving. M3 uses the stronger publication order:

```text
prepare manifest in memory
        -> commit SESSION_STARTED to authoritative ledger
        -> publish JSON discovery manifest
        -> begin provider work
```

A regression injects failure into durable session creation and proves that no JSON
manifest becomes discoverable and no provider request is sent. The inverse crash
window may leave an unexposed orphan ledger row if manifest publication itself
fails, but it no longer exposes a manifest that falsely claims a recoverable M3
session; cleanup/discovery of such unexposed storage debris is outside this core
publication invariant.

## Invalidated candidate history

### Candidate 1 — invalid

```text
143bfcbe3eb2f3b8dc0d86c2642de4ff787708bc
```

Target Windows focused result:

```text
28 failed, 43 passed, 13 subtests passed in 2.83s
```

All reported failures ended as `PermissionError: [WinError 32]` while cleaning up
M3 SQLite-backed temporary directories. This candidate is permanently invalid and
must never be used as PASS/merge evidence.

### Candidate 2 — invalid after post-Ready review

```text
706bb2d7144e845a4aafcf0bfc7b72dbbcb73273
```

Target Windows executable evidence was green:

```text
focused: 72 passed, 13 subtests passed in 1.51s
full:    518 passed, 30 skipped, 247 subtests passed in 70.68s (0:01:10)
```

Those results remain valid historical evidence for that exact tree, but Candidate 2
is permanently invalid as merge evidence because post-Ready review found finding
#25.

### Candidate 3 — invalid before executable validation

```text
895bc81751bf7157dedcfb9ab06fdbc6e1fafd19
```

Candidate 3 repaired finding #25 and was frozen, but external review then found
finding #26 before a target Windows gate was requested. Candidate 3 therefore has
no executable PASS claim and is permanently invalid as merge evidence.

## Deliberate boundaries

M3.1 does not claim:

- hostile-local-machine tamper resistance;
- automatic retry after an unknown provider outcome;
- automatic continuation after a mid-run crash;
- automatic replay of process/workspace/network/Git side effects;
- automatic conversion of a durable receipt into fresh authority;
- durable M2.6 delegation coordinator state;
- autonomous scheduling or running-work leases;
- transparent migration of the existing CLI `run/resume` path in this PR.

The next M3 persistence PR is M3.2 and must separately preserve M2.6 budget
reservations, model-control request claims, root lifetime node counts, pending
escalation state and exact operator continuations across restart without minting
capability or budget.

## Validation status

Source/security review is recorded before each candidate freeze. Hosted CI failures
with no executed steps/logs are classified as infrastructure non-execution and are
not PASS or code-failure evidence.

Exact-head focused/full executable PASS is not claimed until the current repaired
candidate is run on the target Windows environment.
