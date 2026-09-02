# M3 — Persistent Sessions and Event Receipts

## M3.1 — Durable session, event and authority core

This PR establishes the first M3 persistence boundary. It makes the existing
read-only agent session chronology and M2.x authorization-consumption state durable
across process restart without turning persistence into authority.

The primary invariant is:

```text
restart may recover state, but recovery may never recreate spent authority
```

A durable record is evidence. It is not a new authorization source. Reloading a
previous HUMAN allow receipt does not make that receipt fresh again, and a receipt
whose durable consumption record exists remains consumed after every restart.

M3 is intentionally split at the persistence-state-machine boundary. M3.1 covers
the session/event/authority core in this PR. Durable M2.6 delegation coordinator
state is M3.2 because its budget reservation and model-control replay registry need
their own crash-atomic proof. M3.1 does not pretend that persisting a few M2.6
snapshots is equivalent to preserving those invariants.

## Storage split

The existing JSON `SessionManifest` remains a small discovery/summary surface.
M3.1 adds a separate SQLite event ledger as the authoritative chronology/replay
store.

A JSON manifest may lag the event ledger if a process dies after a durable event
commit and before the summary manifest is replaced. `PersistentRunAgentService`
therefore derives recovered conversation identity and cumulative counters from the
ledger before a safe continuation and repairs those summary fields before the next
provider call.

The caller chooses the state location. M3.1 does not claim that the SQLite file is
a hostile-local-machine security boundary. A process with arbitrary write access
to the state database can replace the whole database; the hash chain detects
ordinary corruption/tampering that does not also recompute a coherent replacement
history.

The SQLite store uses:

- `PRAGMA foreign_keys = ON`;
- `PRAGMA synchronous = FULL`;
- rollback-journal mode;
- one session row per persistent session;
- append-only event rows with exact monotonic sequence numbers;
- SHA-256 hash chaining between consecutive events;
- canonical JSON payloads;
- one durable consumed-authorization registry row per consumed receipt;
- transaction-local chronology checks before publication;
- integrity checks again during load and deterministic recovery.

On POSIX, the implementation best-effort restricts the state directory/database
permissions to the local user. That is hygiene, not an authorization boundary.

## Event receipt contract

Every event is immutable and digest-bound to:

- schema version;
- event id;
- session id;
- exact sequence number;
- timestamp;
- event kind;
- canonical JSON payload;
- previous event digest, or null for sequence zero;
- event digest.

For event `N > 0`:

```text
previous_event_digest(N) == event_digest(N - 1)
```

Sequence gaps, reorder, broken previous-digest links, payload changes, event-id
changes, digest changes, head-metadata drift, or an inconsistent completed marker
fail closed during load/replay.

The first event is always `session_started`. The first session row and first event
are published in one SQLite transaction; the parent row is inserted first so the
foreign-key relation is valid without exposing a partial committed session.

## Event kinds

M3.1 defines explicit chronology rather than a generic untyped event bag.

Session/run chronology:

- `session_started`;
- `run_started`;
- `model_request_started`;
- `model_response_recorded`;
- `tool_observation_recorded`;
- `run_completed`;
- `run_interrupted`;
- `session_completed`.

Authority chronology:

- `action_proposed`;
- `authorization_recorded`;
- `authorization_consumed`;
- `action_executed`;
- `action_observed`.

Each kind has an exact payload-key schema and bounded JSON payload. M2.x proposals
and authorization receipts embedded in events are reconstructed through their
existing model classes, so their pre-existing SHA-256 contracts are checked again.

Authority events cannot be published through generic `append()`. They use narrow
`record_proposal`, `record_authorization`, `consume_recorded_authorization`,
`record_execution`, and `record_observation` APIs that verify durable predecessor
state before publishing the next event.

## Linear runtime chronology

M3.1 deliberately supports one linear provider conversation per persistent
session. A transaction-local state replay is performed before every runtime event
append.

The store admits:

```text
at most one active run per session
at most one unresolved provider request per session
```

It rejects overlapping runs, a request outside the active run, a second unresolved
request, a response for a different request, a tool observation before the pending
response, terminal run state for the wrong run, and session completion while run or
provider state is unresolved.

This is not an M5 running-work lease. It is only the M3.1 local chronology
invariant needed to prevent one durable conversation from branching silently.

## Exact tool output

A `tool_observation_recorded` event stores the exact deterministic serialized
observation for a completed tool call before the smaller model-context observation
budget is applied. If that exact observation is too large for the next model prompt,
the run may terminate as `BUDGET_EXHAUSTED`, but the already-completed tool outcome
remains durable and terminal tool counters still agree with recovery.

Recovery never re-reads the workspace and labels the new bytes as an old
observation. If a tool completed but its observation was not durably published
before a crash, that historical output is unknown.

## Model conversation identity and bounded response evidence

Model continuity is persisted as data, not inferred from the current provider
process. Request/response events bind the configured provider and the exact
conversation object:

- `conversation_id`;
- `message_id`;
- `parent_message_id`;
- `finish_reason`.

Normal responses retain exact response text, observed model name, reasoning effort
and metrics. The ledger itself is bounded, however. If a known provider response
cannot fit the M3 event text/payload budget, `model_response_recorded` uses a
strict digest-only representation instead of leaving the preceding request falsely
unresolved. That representation binds:

```text
run_id
request_id
provider
response_chars
response_bytes
response_digest (SHA-256)
response_storage = digest_only
conversation
model
reasoning_effort
```

This fallback is only a bounded provenance representation for an outcome the local
process actually received. It is not used for ordinary responses and it does not
turn an unknown provider outcome into a known one.

Recovery rejects a provider substitution. Once a non-null provider conversation id
is established, a later response cannot silently switch to a different
conversation id. A response that omits conversation metadata does not erase a
previously recovered identity.

## In-flight external-call boundary

Before `provider.send()` is invoked, the read-only loop durably records
`model_request_started` with its exact prompt, system prompt and conversation
identity. `model_response_recorded` is appended only after the provider returns.

Therefore this crash window is explicit:

```text
model_request_started
        |
        +-- provider may have accepted the request
        |
        X process dies before response persistence
```

Recovery sees an unresolved request and returns `UNKNOWN_PROVIDER_OUTCOME`. M3.1
must not silently resend it because the remote provider may already have created or
advanced a conversation.

If the provider did return but its response is too large to retain verbatim, the
bounded digest-only response receipt closes that request as a known outcome before
normal runtime response-budget handling continues.

A `run_interrupted` event associated with provider failure retains the exact local
provider-request id; it does not erase the unresolved remote-outcome uncertainty.

## Clean continuation boundary

M3.1 is conservative about continuation. `can_resume_provider` is true only for a
clean `RESUMABLE` recovery state, normally after a completely recorded run boundary.

A process crash in the middle of a run is classified as interruption even when no
mutation authority exists. M3.1 reconstructs what is known but does not synthesize
a new run boundary. Automatic retry/resumption policy belongs to M5.

`SqliteAgentEventRecorder.recover_from_manifest()` performs identity validation and
pure recovery even for non-resumable states. `resume_from_manifest()` additionally
requires `can_resume_provider`; therefore inspection of a blocked state and
permission to continue are separate operations.

## Durable authorization receipts and one-shot consumption

`authorization_recorded` stores the exact existing `AuthorizationReceipt`. It does
not consume authority.

`LocalApprovalAuthority` still verifies the proposal, approval mode, decision and
HUMAN/POLICY source exactly as before. M3.1 only adds an optional injected
consumption-registry implementation; the default remains the original process-local
registry for non-persistent callers.

The durable registry requires all of:

```text
receipt_id
receipt_digest
proposal_id
proposal_digest
```

This prevents another individually valid receipt that happens to reuse the same
UUID from being substituted for the recorded receipt.

For an ALLOW receipt, durable consumption commits both of these in the same
`BEGIN IMMEDIATE` transaction:

1. the exact `authorization_consumed` chronology event;
2. the durable `consumed_authorizations` row that points back to that event.

A concurrent second consume has no second winner. Event and registry row are also
validated as a one-to-one relation during load, `is_consumed`, re-consumption and
execution recording. Public read paths use explicit SQLite read transactions where
multiple rows/events must describe one logical state, so a concurrent atomic writer
cannot manufacture a false integrity mismatch between snapshots.

If either side is missing or their exact digests/ids differ, M3.1 fails closed before
publishing a replacement consumption or execution event.

A DENY receipt can be retained as history but cannot be consumed.

## Action lifecycle recovery

M3.1 reconstructs the following evidence states:

- `PROPOSED` — proposal durable, no receipt durable;
- `AUTHORIZED_UNCONSUMED` — matching ALLOW receipt durable and not spent;
- `DENIED` — matching DENY receipt durable;
- `CONSUMED_NOT_EXECUTION_RECORDED` — one-shot receipt durably spent but no durable
  execution event exists;
- `EXECUTED` — exact execution id durable, no terminal observation durable;
- `OBSERVED` — exact terminal observation id durable.

A consumed receipt is never downgraded to `AUTHORIZED_UNCONSUMED`. A crash between
receipt consumption and durable execution evidence therefore becomes
`BLOCKED_CONSUMED_AUTHORITY`; M3.1 cannot know that rerunning the side effect would
be safe.

An executed action without terminal observation remains interrupted rather than
being replayed. An authorized-but-not-consumed action also blocks provider
continuation: persistence records the pending authority but does not decide on its
behalf.

`session_completed` cannot be published while a proposal is awaiting a receipt or
an ALLOW-authorized action lacks terminal observation. Recovery independently
checks that terminal session state does not hide unfinished authority chronology.

## Deterministic replay

Replay means reconstruction of durable knowledge, never re-execution.

Given the same valid ledger, recovery deterministically reconstructs:

- ordered event sequence;
- latest provider conversation identity;
- cumulative model-turn count derived from durable response receipts;
- cumulative model-character count derived from exact response text or the exact
  `response_chars` retained by a bounded digest-only receipt;
- cumulative tool-call count derived from durable tool observations;
- exact stored observation JSON strings;
- proposal/receipt/consumption/execution/observation state;
- unresolved provider request ids;
- interruption/recovery disposition.

`run_completed` counters are not trusted as the sole source of totals. They are
cross-checked against the response/tool chronology and fail closed on disagreement.
This preserves already proven progress when a process dies before a terminal run
event.

Replay never calls the provider, reads a tool, mutates the workspace, launches a
process, mutates Git, pushes to a network destination, or consumes authority.

## Application composition

`PersistentRunAgentService` is the M3.1 application composition root for the
existing read-only agent loop. It combines:

- the existing JSON manifest store;
- `SqliteSessionEventStore`;
- `SqliteAgentEventRecorder`;
- the existing `RunAgentService` / `ReadOnlyAgentLoop`.

On safe resume, the SQLite ledger is replayed before provider contact. If the JSON
manifest lagged a previously completed durable run, its conversation and counters
are repaired from the ledger first. If recovery is not resumable, no provider call
is made.

M3.1 does not silently migrate the existing CLI `run/resume` commands to a new
durable format in this PR. The application seam is explicit and tested first; CLI
adoption can be layered after the exact-head persistence boundary is proven.

## M2.6 / M3.2 boundary

M2.6 deliberately ended with process-local orchestration state. Its durable form
requires more than serializing `DelegationSnapshot`:

- child budget reservations must never be refunded by crash/restart;
- claimed `(request_id, request_digest)` pairs must survive restart;
- root lifetime node counts must remain monotonic;
- pending escalation and exact continuation digests must survive;
- restart must reconstruct `ACTIVE / WAITING_HUMAN / COMPLETED / CANCELLED`
  without adding capabilities or budget.

Those properties form a second persistence state machine and are M3.2. They will
build on the M3.1 ledger rather than being approximated by an unsafe snapshot in
this PR.

## M3 / M4 boundary

M3 records durable facts and recovery state. It does not add experiment manifests,
metric comparison, artifact interpretation or evidence-bounded scientific
conclusions; those are M4 Computational Lab concerns.

## M3 / M5 boundary

M3 recovery does not imply unattended restart of work. Automatic schedulers,
running-work leases, retry policies and autonomous continuation remain M5 bounded
automation concerns.

## Explicit non-claims

- No authority minted by persistence or recovery.
- No consumed receipt becomes reusable after restart.
- No automatic replay of workspace/process/network/Git side effects.
- No automatic resend of an in-flight provider request with unknown outcome.
- No automatic continuation from a mid-run crash.
- No hostile-local-machine tamper resistance or cryptographic authenticity claim.
- No distributed or multi-host session consensus in M3.1.
- No durable M2.6 budget/control-request coordinator yet; that is M3.2.
- No autonomous scheduler.
