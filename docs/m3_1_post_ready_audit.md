# M3.1 — Post-Ready Audit Addendum

## Candidate 4 executable evidence

Candidate 4 was frozen at:

```text
head: 0c9679f1e9950a1b67cfa15f28901ab71fdcc759
tree: 3003eac4a8ea85c55d889c3cd69012d34f779f64
base: f36f2a54ac79a926dac910ab435981b3aeb6fa7d
```

Target Windows gates were green on that exact tree:

```text
focused: 75 passed, 13 subtests passed in 1.63s
full:    521 passed, 30 skipped, 247 subtests passed in 69.77s (0:01:09)
```

Those results remain valid historical evidence for Candidate 4, but Candidate 4 is
not merge evidence because the post-Ready review below found a further state-machine
gap.

## Finding 27 — historical runtime identifiers were recovery-only unique

Pure `recover_session()` already rejects reuse of a previously seen `run_id` or
provider `request_id`, even after the earlier run/request is terminal. The public
SQLite writer, however, previously tracked only the currently active run and
currently unresolved provider request. Consequently a caller using public
`append()` could publish a later `run_started` or `model_request_started` event that
reused an already terminal historical identifier. The resulting chronology was
hash-valid and accepted by the writer but deterministic recovery rejected it.

This is narrower than general semantic replay validation. M3.1 deliberately retains
some recovery-only semantic cross-checks such as conversation continuity and
terminal counter agreement. Historical run/request identifiers are different: they
are keys in the recovery state machine itself and must not be rebound to later
runtime episodes.

## Repair for finding 27

The public `SqliteSessionEventStore` now performs a replay-parity identifier check
inside the same `BEGIN IMMEDIATE` transaction used by `append()`:

- every historical `run_started.run_id` must be unique within the session;
- every historical `model_request_started.request_id` must be unique within the
  session;
- a new run/request identifier may not equal any historical identifier of the same
  kind;
- if an already persisted chronology contains duplicate historical runtime ids,
  later append attempts fail closed with an integrity error;
- the check executes after the base chronology replay has strict-parsed and
  schema-validated the stored events, and before publication of the new event.

Dedicated regressions verify that a completed `run_id` and a resolved provider
`request_id` cannot be rebound and that rejected transitions do not append events.

Candidate 4 is permanently invalid as merge evidence despite its green executable
gates.

## Candidate 5 executable evidence

Candidate 5 was frozen at:

```text
head: 623d2ba36015cfee5cbd8c78ac5a72b2df56f08e
tree: d0e14a2f9c971e9426c13a7e861fa2427af44cf8
base: f36f2a54ac79a926dac910ab435981b3aeb6fa7d
```

Target Windows gates were green on that exact tree:

```text
focused: 77 passed, 13 subtests passed in 1.59s
full:    523 passed, 30 skipped, 247 subtests passed in 69.69s (0:01:09)
```

Those results remain valid historical evidence for Candidate 5, but Candidate 5 is
not merge evidence because post-Ready review found finding #28 below.

## Finding 28 — observation-context budget could create an unrecoverable completed run

A read-only tool was executed and `tool_calls` was incremented before the result was
serialized for the next model turn. If the exact observation exceeded
`max_observation_chars`, `serialize_observation()` raised `ProtocolError` and the
loop called `_finish()` before `tool_observation_recorded()` ran. The durable ledger
therefore recorded `run_completed.tool_calls = 1` but contained zero durable tool
observation events for that run. Deterministic recovery correctly rejected the
resulting chronology because terminal counters disagreed with durable events.

This was reachable through an ordinary configured budget, not only through database
tampering or a crash.

## Repair for finding 28

Observation durability is now separated from the smaller model-context budget:

```text
tool executes
  -> render exact deterministic observation
  -> persist TOOL_OBSERVATION_RECORDED
  -> apply max_observation_chars for the next model turn
  -> if over budget, record a terminal BUDGET_EXHAUSTED run whose counters match
     the already durable tool event
```

`agent.protocol.render_observation()` provides deterministic JSON rendering without
the model-context limit. `serialize_observation()` reuses the same rendering and
applies only the caller-supplied context bound. The agent loop records the exact
rendered observation through the recorder before it evaluates the smaller context
budget.

A dedicated regression uses a real read-only tool result that exceeds the context
budget and verifies:

- runtime status is `BUDGET_EXHAUSTED`;
- runtime `tool_calls == 1`;
- the ledger contains exactly one `tool_observation_recorded` before
  `run_completed`;
- the durable observation contains the exact tool output even though it is too large
  for the next model turn;
- deterministic recovery succeeds with `tool_calls == 1` and returns the same exact
  observation.

Candidate 5 is permanently invalid as merge evidence despite its green executable
gates.

## Candidate 6 status

Candidate 6 was frozen at:

```text
head: 0954c59e9dcd448012816ceb302c7faaeb59b001
tree: 675d24a9e19cfea9ac6c33cdd6ec351ded532833
base: f36f2a54ac79a926dac910ab435981b3aeb6fa7d
```

Candidate 6 included the observation-budget durability repair but was invalidated
before executable validation because post-freeze review found finding #29 below.

## Finding 29 — ledger reads could combine multiple SQLite snapshots

The base SQLite store used `isolation_level=None`, so it operated in autocommit mode.
`load_events()` fetched the session row, event rows, and consumed-authorization rows
through separate SELECT statements without an explicit read transaction. A
concurrent writer could therefore commit between those reads and make one logical
recovery operation observe multiple database versions. Examples included an older
session head with newer event rows, or a consumption chronology event without the
matching consumed-registry row from the same commit. Those states could produce
false integrity failures even though every committed database state was valid.

`recover()` compounded the problem by calling `load_events()` and then
`consumed_authorizations()` as separate public reads.

## Repair for finding 29

The public managed `SqliteSessionEventStore` now routes `load_events()`,
`consumed_authorizations()`, and `recover()` through one `_read_snapshot()` helper:

```text
open one connection
  -> BEGIN explicit read transaction
  -> read session head
  -> read ordered event chronology
  -> read consumed-authorization rows
  -> validate/decode the same snapshot
  -> commit/close the read transaction
```

`recover()` passes the events and consumed-authority mapping returned by that single
snapshot directly into pure `recover_session()`. It no longer composes multiple
autocommit reads.

Dedicated regressions instrument the public store through SQLite trace callbacks and
connection counting. They verify that `load_events()`, `recover()`, and
`consumed_authorizations()` each:

- open exactly one connection for the logical read;
- issue explicit `BEGIN` before the snapshot queries;
- read session/events/consumption state through that same connection.

Candidate 6 is permanently invalid as merge evidence.

## Candidate 7 executable evidence

Candidate 7 was frozen at:

```text
head: 3ba7e7595d3dfa0a3562255463b2d747bc808ccf
tree: b522eb0bda4449fdf70bcb4b6d113f9d40119268
base: f36f2a54ac79a926dac910ab435981b3aeb6fa7d
```

Target Windows gates were green on that exact tree:

```text
focused: 90 passed, 13 subtests passed in 1.72s
full:    527 passed, 30 skipped, 247 subtests passed in 73.64s (0:01:13)
```

Those results remain valid historical evidence for Candidate 7, but Candidate 7 was
invalidated immediately before merge when the final review-thread check surfaced
findings #30 and #31 below. The authorized merge was deliberately not executed.

## Finding 30 — consumption-status read still crossed SQLite snapshots

Finding #29 repaired session-level `load_events()`, `consumed_authorizations()` and
`recover()`, but the inherited `is_authorization_consumed()` path still ran in
autocommit mode. It read the consumed-registry row and matching chronology event as
separate SELECT statements without an explicit read transaction. A concurrent
consumption commit between those SELECTs could therefore make a valid database look
corrupt for one call.

### Repair for finding 30

The public managed store now overrides `is_authorization_consumed()` and performs the
registry-row query plus exact consumption-event query on one connection after an
explicit `BEGIN`. The existing fail-closed row/event binding checks are unchanged;
only read snapshot consistency is tightened.

`test_session_events_snapshot_reads.py` now verifies that this fourth public read
path opens one connection, issues `BEGIN` first, and queries both
`consumed_authorizations` and `events` through that same transaction.

## Finding 31 — an oversized known provider response could look unknown after restart

`MODEL_REQUEST_STARTED` is durably recorded before `provider.send()`. After the
provider returned, the recorder attempted to persist the complete response text.
The M3 event schema intentionally bounds text and payload size. If the returned
response exceeded that bound, durable response publication could fail before the
runtime reached its ordinary response/model budget handling. The ledger then ended
at `MODEL_REQUEST_STARTED`, so restart classified the provider outcome as
`UNKNOWN_PROVIDER_OUTCOME` even though the local process had in fact received a
known response.

### Repair for finding 31

`model_response_recorded` now has two exact schema variants under the same event kind:

1. normal responses retain the existing exact-text representation;
2. only when the exact response cannot fit the M3 text/payload budget, the recorder
   writes a bounded digest-only representation containing the exact request/run/
   provider binding, response character count, UTF-8 byte count, SHA-256 response
   digest, conversation identity, model and reasoning metadata.

The fallback is intentionally narrow. Unrelated integrity errors are not caught or
converted into digest-only receipts.

Pure recovery treats both representations as a resolved provider outcome. For the
digest-only form it reconstructs turn/model-character counters from `response_chars`
and preserves conversation continuity. Consequently the request is no longer open
and restart cannot falsely report `UNKNOWN_PROVIDER_OUTCOME` merely because a known
response was too large to retain verbatim.

A dedicated end-to-end regression drives a real `ReadOnlyAgentLoop` with a provider
response larger than `MAX_EVENT_TEXT_CHARS` and verifies:

- the runtime terminates through its ordinary `BUDGET_EXHAUSTED` path instead of
  leaking a persistence exception;
- the ledger contains `MODEL_RESPONSE_RECORDED` before `RUN_COMPLETED`;
- the response event is digest-only and contains exact chars/bytes/SHA-256 evidence;
- recovery has no unresolved provider request;
- recovered turn/model-character counters and conversation match the runtime result;
- disposition is not `UNKNOWN_PROVIDER_OUTCOME`.

## Candidate status

Candidate 7 is permanently invalid as merge evidence despite its green executable
gates. Findings #30 and #31 require a newly frozen exact tree and completely fresh
focused/full Windows validation before any new Ready/merge cycle.
