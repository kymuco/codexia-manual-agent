# Issue #88 — ADR-001: Reconciliation proof, authority and replay

**Status:** proposed for review (2026-10-08). No implementation authorization.  
**Applies to:** recovering the result of an already-admitted cognition handoff.  
**Does not apply to:** re-arm / retry of a proved-unsubmitted effect (#21, #23).  
**Related:** [architecture v0](issue_88_cognition_outcome_reconciliation_v0.md), [test matrix](issue_88_cognition_reconciliation_test_matrix_v0.md), pure verifier PR #90.

## Decision 1 — Canonical proof binding belongs to Work chronology

**Choose:** an append-only, schema-versioned Work event for *reconciliation proof association*; optionally use a host-managed content-addressed payload store for large/sensitive provider evidence. An external blob store by itself is **never** the Work truth.

- The proof event binds the existing request/handoff/role/Work digests, provider namespace, original wire-input digest, response locator/IDs, raw + canonical content digests, transformation policy, provenance/proof level, and authorized human decision reference where required.
- It contains no credentials, cookies, raw browser lease, auth header or provider token. Any raw provider evidence outside Work is privacy-governed, integrity checked and addressed by content digest. The existing CognitionOutcome can still carry its canonical output text; the proof event need not duplicate it.
- Crash-ordering protocol for later implementation: (1) authenticate/read and bound the provider result; (2) acquire a durable **provider-result claim** through the host-wide unique index; (3) CAS-append a frontier-specific immutable *proof pending-admission* event; (4) use a **new strict-frontier outcome admission operation** (not the current rebinding `record_outcome`) to CAS-append the role outcome against precisely that proof event's revision/head; (5) re-project and compare. The proof event or result claim alone **never means the role completed**.
- If proof append is acknowledged ambiguously, locate its deterministic identity and exact digest before taking another step. If outcome append is acknowledged ambiguously, recover the terminal RoleRun and compare the expected semantic response and proof binding before returning ALREADY_ADMITTED.
- A Work revision movement between steps fails closed. A fresh attempt must re-project the Work and generate a **new proof-attempt ID bound to the new frontier**; the provider-result claim and canonical outcome identity remain the same. No weak bypass of the WorkStore CAS frontier is allowed.

**Why:** a separate host-only ledger cannot by itself show which result became authoritative in the canonical Work timeline. A single atomic two-event append would be ideal but would require a new WorkStore transaction API; a recoverable two-step CAS sequence is the narrower initial proposal. Critically, today's `CognitionTransportBridge.record_outcome()` **is not itself a strict-frontier CAS API**: it rereads the current snapshot and rebinds the outcome, so it cannot be called unmodified after an external preflight to enforce D02. The implementation must introduce a narrow atomic expected-head admission path, using WorkStore's actual `append(expected_revision=...)` boundary to reject concurrent movement.

**Risk to resolve in implementation:** recovery must not expose a pending proof as an admitted outcome; event projection, retention and referential availability must be validated before new event kinds are introduced. A pending proof must store both its **observed subject frontier** and its **post-proof admission frontier**. Only the proof event itself may advance the Work between authorization and admission; any unrelated event rejects the attempt.

## Decision 2 — Provider correlation threshold

**Choose:** treat a response as EXACT_PROVIDER_CORRELATED only with *trusted* provider-read provenance and a causal request-to-execution binding established at original send time. A message ID and matching prompt alone are not proof that the response belongs to this specific dispatched effect.

Minimum for exact tier:

1. The host preserves a stable, unique original outbound attempt token or operation ID associated with the durable request/handoff before or during the write boundary; if provider-native, the provider echoes it.
2. An authenticated read-only provider endpoint/adapter returns that same causal token and a stable execution/turn identifier (including tenant/account namespace).
3. The correct request/wire bytes or cryptographic digest are bound to that token, including adapter serialization version.
4. A final completion signal binds the response ID, exact raw content hash and branch/execution lineage. The response is unique for this attempt; conflicting sibling completions fail closed.

Where any of those is not available, maximum tier is **CONTEXT_CORROBORATED**, even if the plaintext matches perfectly. CWA conversation history by itself is not assumed to meet exact-tier requirements.

Provider-read APIs must expose declared capabilities (stable execution ID, branch lineage, completion signal, original-wire readback, receipt verification), with a versioned trust contract. Unsupported or unavailable features downgrade/fail closed; no guessed correlation based on timestamps.

## Decision 2a — Claim each provider result exactly once within the host

**Choose:** a durable, atomic, host-wide **ProviderResultClaimIndex** keyed by the stable **provider-side result identity**, **not** the adapter implementation or its version: canonical provider service identity + authenticated tenant/account namespace + immutable provider execution/turn (or conversation/branch namespace if needed for unique message IDs) + immutable response message/receipt ID. The key MUST exclude adapter version, wire-serialization version, Work/request/handoff IDs, local timestamps and normalization policy. Its uniqueness scope is the **local Codexia installation**, not a universal cross-machine proof.

Adapter/schema versions, serializer version and verification capability are **claim metadata**, never additional uniqueness-key dimensions. If the provider changes result-ID format or the canonical provider namespace, an explicit backwards-compatible alias/migration lookup must prove that previously claimed results cannot be claimed again under the new encoding **before** the upgraded adapter is eligible. Fail closed if stable identity cannot be mapped across versions.

- A claim value binds that result key to exactly one original Work/request/handoff. A second handoff, including one in another Work or after an adapter upgrade, cannot claim the same provider result: return `PROVIDER_RESULT_ALREADY_CLAIMED`. An exact same-handoff replay may reuse the original claim only after checking its stored binding and result digest.
- Claiming must use a unique constraint or transactional compare-and-set across **all Work IDs in that installation**; per-Work event projections do not satisfy test B17. A process-global in-memory set is not enough.
- A successful claim is sticky across crash and even if proof/outcome admission fails; the **same** handoff may resume idempotently, another handoff may not appropriate the existing external result. Claims do not imply completion or provider write authority.
- If the result lacks a stable namespace-scoped identity, the index cannot be **read** or **atomically written**, the index has not been initialized/recovered consistently, a schema migration is ambiguous, or collision cannot be ruled out, **do not admit** it through this flow. Never silently fall back to Work-local proof admission or human approval. A human's contextual approval cannot override a known duplicate claim or an unavailable uniqueness gate.
- A provider result may have a stable immutable response identity without a provider-issued **causal receipt** for the original dispatch. That is sufficient to guard cross-Work uniqueness, but **not** to elevate contextual matching to `EXACT_PROVIDER_CORRELATED`; keep these proof dimensions separate.
- Prefer keeping the unique claim index in the same durable host database as Work events; if the index and WorkStore cannot transact together, define replay/compensation as a conservative recoverable saga and prove it before integration. Do not promise atomicity across arbitrary backends.

### Normal callbacks must participate in the same uniqueness boundary

**Normative requirement:** the unique claim namespace covers **all provider-identified result admissions**, not merely the recovery command. A completed provider response entering via the ordinary live `CognitionTransportBridge.record_outcome()` callback must acquire/check the **same canonical provider-result claim** bound to its Work/request/handoff **before** that outcome becomes durable. Otherwise an already consumed response can be claimed again by reconciliation into a different Work.

The current `ModelProviderCognitionPort` and `CognitionOutcome` do **not** expose an authenticated provider-result identity, and `record_outcome()` does **not** maintain this index. Therefore the current code cannot truthfully claim claim-index completeness. A future host integration must explicitly route **both** live callback outcomes and recovered outcomes through a shared atomic claim boundary; normal synchronous admission must remain a separately reviewed behavior rather than silently repurposing the existing bridge as proof of coverage.

**Rollout/completeness gate:** reconciliation admission is **disabled by default** for each canonical provider/account namespace until a durable **storage-enforced legacy-writer activation barrier** (Decision 2b) is in place and a coverage assessment proves the full relevant historical admission window is indexed with stable provider-side result identities. Existing legacy callbacks that lack provider result IDs prevent automatic coverage certification. Allow activation only after a complete, verifiable historical backfill/alias reconciliation **or** a source-supported exclusion fence proving no older unindexed result can be supplied to this recovery path. A local software upgrade date or `index_initialized=true` marker **is not** such a fence. If no such proof exists, keep the namespace quarantined for reconciliation, including human-authorized contextual recovery.

Index availability, historical completeness, and **writer-epoch fencing** are three distinct conditions: all must be proven **at final admission**, and any race with a newly completing live callback must be serialized by the shared unique claim constraint plus storage-enforced writer epoch. A human approval, matching raw hash, Work-local proof event, or successful lookup against an incomplete index cannot waive this gate. The existing legacy live callback path may continue according to its existing safety contract while reconciliation remains disabled for that namespace; do not pretend it has retroactively minted an index claim.

## Decision 2b — Fence in-flight legacy writers before enabling recovery

**Choose:** a **storage-enforced writer epoch / activation barrier**, serialized with all writes to the WorkStore, before certifying historical claim coverage or enabling recovery admission. This is distinct from the provider-side source-history/exclusion fence in Decision 2a: both are required.

**Activation sequence (future implementation contract):**

1. Keep the provider/account namespace in durable `RECOVERY_DISABLED`. Install the new live-callback admission path and shared result claim index, but do not authorize recovery.
2. Under a transaction that serializes against **every legacy Work event append**, activate a monotonically increasing **mandatory write epoch** (or equivalent schema-enforced format/constraint). Every subsequent role outcome append must prove it comes through the epoch-aware, shared-claim admission boundary. Storage itself rejects a legacy callback that omits the epoch/claim reference. Merely checking epoch inside a new Python process is **insufficient**: already-running old processes do not consult it.
3. The transaction cannot commit while a pre-existing legacy writer's write transaction remains active; that writer must commit/roll back before the activation barrier. If the database cannot guarantee this transaction ordering or enforce rejection of old-format inserts (including direct old SQLite clients), **do not enable recovery**. A worker shutdown/drain is useful operationally but is not proof without a common atomic storage fence.
4. Only **after** the barrier, reconcile the committed pre-barrier historical outcomes (including callbacks that completed during upgrade), using stable provider identities and a verifiable backfill or a provider-supported source-history exclusion fence. Missing identity, partial index migration, uncertain retained history, or any unfenced alternate writer keeps `RECOVERY_DISABLED`.
5. Transition to durable `RECOVERY_ENABLED(epoch, coverage_digest)` using the **same serialized storage authority** as the claim index and outcome append. A live callback or recovered admission must verify the **current enabled epoch and claim uniqueness at its final append transaction**. A previously checked status cannot authorize a write after epoch or coverage changes.
6. Restart and schema-migration recovery revalidates the barrier and coverage proof. If fencing is lost, downgraded, or unverifiable, disable reconciliation. An old callback rejected at the fence yields an ambiguous provider effect that must be handled separately; its failure is **not** automatic retry permission.

**Implementation caveat:** current `SqliteWorkStore` and `CognitionTransportBridge.record_outcome()` do not provide this writer fence. A database-enforced constraint/trigger, incompatible legacy event format gate, or truly exclusive and enforceable storage-writer generation protocol must be designed and migration-tested separately. A process-local lock, deploy timestamp, assumption that the old process exited, or one-time index scan is not sufficient. If an old writer can still use an accepted event schema or an alternate append path, activation is prohibited.

**Ordering invariant:** no pre-barrier unindexed result may remain unaccounted for in a provider-visible history that reconciliation can inspect, and no post-barrier unindexed live callback may append. These are distinct proof obligations. This requirement intentionally prioritizes preventing duplicate provider-result admission over availability during a rolling upgrade.

## Decision 3 — Human approval only for qualified contextual recovery

**Choose:** v1 defaults to **zero automatic outcome admission**, including exact-tier evidence, until a separate product policy explicitly authorizes a narrow provider. The pure verifier in PR #90 has no admission capability.

A human may approve a CONTEXT_CORROBORATED result only when all of the following hold:

- A trusted host has authenticated the provider account/tenant and performed a genuinely read-only operation.
- The durable request/handoff, actual wire input, one completed response, history ordering and branch lineage can be reconstructed unambiguously; no known conflict.
- A review display presents the raw and canonical digests, normalization version, exact request/handoff IDs, work head and remaining uncertainty (not a model-generated claim that "all is resolved").
- Approval is explicitly tied to those immutable identities, digests and a human reason/decision ID; it is distinguishable from provider-issued proof and is recorded as its own provenance.
- Before admission, the host re-checks permissions, active Work/Role state and exact CAS Work head.

A human "approve" does **not** transform CONTEXT_CORROBORATED into EXACT_PROVIDER_CORRELATED, ignore a CONFLICT, grant permission to resend, or imply domain WorkCompletion. If branch identity/provider read access is too weak even for contextual corroboration, no generic approval path is available: escalate as a separate adjudication, not this recovery flow.

## Decision 4 — One outcome identity per exact provider result

**Choose:** deterministic identity over a versioned canonical tuple:

- original Work/request/handoff IDs and immutable digests;
- provider namespace + execution/turn identity where available, plus response message/receipt identity;
- canonical output digest and status (and raw digest + normalization policy/version).

Use separate namespace-hashed IDs (for example UUIDv5) for (a) a **provider-result claim** independent of Work ID; (b) the **frontier-bound proof attempt** incorporating Work revision and previous event digest; and (c) a stable **proposed outcome identity** for this handoff/result, independent of proof refreshes. Do **not** use a newly generated random ID on every retry. Persist the exact proposed outcome metadata, including creation timestamp, before first admission attempt or derive it from a durable record. A replay must reuse the same identity and payload bytes; deterministic ID alone is not sufficient because changed timestamp changes the event digest.

If a Work already has a matching completed outcome, report ALREADY_ADMITTED with zero appended events. If a different provider response or canonical digest claims the same handoff, return CONFLICT, not a second terminal outcome. A new proof attempt after changed Work head uses a **new proof-event identity**, not a replay of the old event ID with different bytes. Concurrency is resolved via WorkStore CAS then re-projection; compare original source provenance and semantic digest after losing a race.

## Explicitly deferred

- Detailed proof-attempt and result-claim schemas, migration and collision behavior in Gen2 Core.
- Atomic API shape for strict expected-revision **and** expected-head-digest outcome admission; must be enforced on the final append, not in a separate stale preflight.
- Selection of the first provider with genuine authenticated readback + stable causal receipt; CWA does **not** automatically qualify.
- Token/receipt retention, privacy limits, encrypted payload storage and deletion policy.
- Whether a privileged **exact-tier** result may ever be admitted automatically in a later product policy; v1 default is no.
- A real integration test of crash between proof and outcome; these decisions are not an E2E proof.

## Review/acceptance checklist

- [ ] Verify the proof is authoritative only as a Work event association, not as a provider action or RoleRun completion.
- [ ] Verify authenticated causal correlation is never inferred from a prompt or readback message ID alone.
- [ ] Verify human contextual approval is separately recorded and cannot remove conflicts or permit retries.
- [ ] Verify stable result + timestamp replay and single terminal outcome across crashes and CAS races.
- [ ] Verify strict expected-head CAS at the **outcome append**; existing rebinding `record_outcome()` is insufficient without a new boundary.
- [ ] Verify host-wide unique provider-result claim across **different Work IDs** and **adapter versions**; an upgrade may not change the claim key.
- [ ] Verify **normal live callbacks and reconciliation** share the same atomic claim; a response consumed by live Work A cannot be recovered into Work B.
- [ ] Verify historical coverage or a provider-supported exclusion fence before enabling reconciliation; legacy unindexed live results, missing identity and missing coverage proof quarantine the provider namespace even with human approval.
- [ ] Verify a **DB-serialized writer-epoch barrier** blocks old callbacks begun before upgrade but appending **after** recovery activation, including across process and DB reopen.
- [ ] Verify no legacy write path can bypass epoch/claim enforcement: a new-process-only guard, process-local lock and initial backfill alone are not acceptable; uncertain fencing keeps recovery disabled.
- [ ] Fail closed on claim-index read failure, write/transaction failure, missing stable provider result ID and unresolved namespace migration — including with valid-looking human contextual approval.
- [ ] Verify a new Work frontier yields a **new proof-attempt ID**, while existing source claim and proposed outcome identity remain stable.
- [ ] Confirm the provider-read boundary remains read-only and outside Gen2 Core.
- [ ] Decide scope for the first bounded provider adapter before starting implementation.

**Safety invariant:** absence of evidence of an external effect is never evidence that the effect did not occur. Reconciliation issues no new external request.
