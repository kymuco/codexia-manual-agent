# Issue #88 — Reconciliation crash/failure gate matrix v0

**Status:** test-plan specification only; no test implementation in this branch.  
**Companion:** [design v0](issue_88_cognition_outcome_reconciliation_v0.md)  
**Scope:** cognition results **after an existing durable handoff**; no re-arm, no new provider send.

## Deterministic fixture model

Build fresh temporary `SqliteWorkStore` fixtures with the real Gen2 admission/projection interfaces. No live ChatGPT dependency or credentials. Inject a scripted read-only provider independent of the existing `CognitionPort.complete()`.

Fixture ingredients:

- Durable `Work`, pinned WorkflowRun and one `RoleRun` with exact `CognitionRequest` + `CognitionHandoff`. Record all expected UUIDs, digests, port ID, Work revision and last event digest.
- Fixed provider namespace + stable execution/turn and message IDs, immutable request wire hash and response raw bytes/hash, completion flag, parent/branch lineage and optional signed/provider-issued correlation token.
- Two alternate provider read capability levels: full correlated receipts; read-only conversation history without original provider receipt.
- Provider read harness records `read_calls`, `send_calls`, `write_calls` and returns typed observations; **all reconciliation scenarios assert `send_calls == 0`**.
- Explicit human attestation fixture for context-corroborated cases; never substitute its existence for missing provider evidence.
- Fault injection immediately before/after each durable append and after response observation, plus optimistic-concurrency Work revision advancement.
- Content canonicalization fixtures: identity, known isolated wrapper, malformed markup, changes to semantic control flags, oversize output.

Use stable seeded identifiers, no timing-sensitive sleeps, no real model invocation, and compare full immutable outputs/digests; test deterministic reopening after process restart.

## A. Handoff and provider-observation failure points

| ID | Injection / condition | Required decision / durable result |
| --- | --- | --- |
| A01 | Request exists, handoff absent | `NOT_APPLICABLE`; no reconciliation, no new handoff |
| A02 | Handoff appended, crash before `complete()` | Provider result `UNKNOWN`, RoleRun remains `REQUESTED`; no redispatch |
| A03 | Handoff exists, provider-read capability absent | `UNSUPPORTED`; no Work event |
| A04 | Handoff exists, provider history empty | `UNVERIFIABLE`; absence does not prove `NOT_SUBMITTED` |
| A05 | Provider accepted write, response pending | `NOT_YET_COMPLETE`; no succeeded outcome |
| A06 | Provider completion received, crash before local response read | Subsequent read can verify same turn when available; no resend |
| A07 | Complete response observed, crash before proof persistence | Subsequent read reconstructs exact proof if available; no admission yet |
| A08 | Frontier-specific proof appended, crash before role outcome | Recover existing proof at exact event identity; role remains `REQUESTED`; if head changed, require new frontier-bound proof-attempt ID, never resend |
| A09 | Outcome appended, acknowledgement lost | Reconcile reports `ALREADY_ADMITTED`; no additional role event |
| A10 | Provider read timeout/network/auth failure | `READ_UNAVAILABLE`, NOT terminal `UNKNOWN`; no new dispatch |
| A11 | Remote result absent due to retention expiry | `UNVERIFIABLE`; no invented result |
| A12 | Provider returns a failure *receipt* for executed request | Process only if correlated and supported by provider contract; do not turn local read failure into `FAILED` |

## B. Identity, binding, and provider-source attacks

| ID | Mismatch / adversarial case | Required behavior |
| --- | --- | --- |
| B01 | Wrong Work ID or work digest | Reject before provider read when possible |
| B02 | Wrong WorkflowRun, RoleRun or request digest | Reject; zero Work writes |
| B03 | Handoff ID correct but wrong `port_id` | Reject; routing immutable |
| B04 | Handoff duplicated or event prefix invalid | Reject via existing projection |
| B05 | Same prompt text, different original request IDs | Never infer causal match from text alone |
| B06 | Provider account/tenant namespace differs | Reject |
| B07 | A second plausible response on another branch | Conflict; no “pick latest” |
| B08 | Missing parent node, branch ancestry or edit history | Downgrade proof to `UNVERIFIABLE` / `CONTEXT_CORROBORATED` per capability; no automatic admission |
| B09 | Intervening user prompt before candidate result | Reject same-turn attribution |
| B10 | Original wire prompt differs from admitted semantic projection | Reject; hot-delta and system prefix must be versioned |
| B11 | Correct request text but wrong provider execution ID | Reject strong correlation |
| B12 | Provider receipt claims completion but text is empty/truncated | Reject succeeded result |
| B13 | Wrong/stale finished signal (`streaming` vs final) | Reject succeeded result |
| B14 | Candidate response SHA differs from independently read bytes | Reject; audit difference |
| B15 | Same response ID now returns changed bytes | Conflict; no silent overwrite |
| B16 | Receipt signature/correlation token invalid or missing | No `EXACT_PROVIDER_CORRELATED` proof |
| B17 | Same immutable provider result claimed by handoffs in **two different Work IDs** | Host-wide unique provider-result index rejects second claim; per-Work proof events alone are insufficient; zero second outcomes |
| B18 | Duplicate provider entries with identical text but different turn IDs | Ambiguous unless distinct original receipt identifies one |
| B19 | Response exceeds Gen2 bounds or invalid UTF-8 | Reject; never truncate silently |
| B20 | Provider read unexpectedly invokes `send` | Test fails immediately, no admission |
| B21 | Same provider execution and response after **adapter v1 → v2 upgrade**, second Work claims it | One stable provider-side claim key across adapter versions; second Work rejected; no second outcome |
| B22 | ProviderResultClaimIndex **read unavailable** (I/O error, permission, corruption) | Fail closed before proof/outcome admission; no Work-local fallback even with human attestation |
| B23 | ProviderResultClaimIndex atomic **write/transaction fails** | No proof/outcome append; fail closed; recovery may safely retry identical source claim after index repair |
| B24 | Result lacks immutable namespace-scoped provider result identity (or stable message/turn IDs) | No claim permitted; no outcome admission even with human contextual approval |
| B25 | Adapter upgrade changes provider ID encoding and alias/migration lookup is unavailable or ambiguous | Fail closed; cannot mint a fresh distinct claim for the same provider-side result |
| B26 | **Ordinary live callback** admits provider result R into Work A; later reconciliation attempts R in Work B | Shared installation-wide provider-result claim is already owned by A; B is rejected with zero outcome/proof admission even though the first result was not recovered |
| B27 | Provider namespace has **legacy live outcomes without result IDs or index entries**, then new reconciliation candidate appears | Provider-result coverage unproven: reconciliation disabled for namespace, no claim/admission even when response history appears unique |
| B28 | New claim index created after adapter upgrade, but older source history can still supply pre-index responses | Merely initializing index or recording a cutover timestamp is insufficient; require verifiable backfill or provider-supported exclusion fence; otherwise reject |
| B29 | Live callback and recovery simultaneously claim same provider response for distinct Work IDs | Shared atomic uniqueness constraint admits at most one claimant; loser gets conflict; no two role outcomes |
| B30 | Old process constructed an outcome before cutover and attempts an **unindexed append after** new writer epoch activation | DB-enforced epoch/format constraint rejects the legacy append; no unindexed post-cutover role outcome, no claim bypass |
| B31 | Legacy callback uses alternate direct WorkStore append path bypassing new Python-level checks | Storage-level fence rejects old-format/epochless role outcome; reconciliation never enabled where any legacy writer bypass remains possible |

## C. Proof strength, normalization and authorization

| ID | Evidence / action | Required behavior |
| --- | --- | --- |
| C01 | Full provider-issued request-to-execution correlation, immutable completed response | `EXACT_PROVIDER_CORRELATED` candidate; explicit policy gate still applies |
| C02 | Exact prompt + unique response + branch history, no trusted original receipt | `CONTEXT_CORROBORATED` only; no automatic admission |
| C03 | `CONTEXT_CORROBORATED` without explicit human result-admission approval | Read-only report; no Work write |
| C04 | `CONTEXT_CORROBORATED` with correctly scoped attestation and compatible policy | May admit **qualified** outcome; persist attestation separate from provider proof |
| C05 | Human approval with conflicting digest/branch | Reject; approval cannot erase conflict |
| C06 | Partial text similarity or timestamp-only correlation | `UNVERIFIABLE`; no admission |
| C07 | Verbatim canonical response | Raw and admitted hashes equal |
| C08 | Narrow final-control escape wrapper | Raw and admitted hashes both recorded, exact wrapper removed and transformations versioned |
| C09 | Additional trailing text, nested wrappers, duplicated marker | Reject; do not strip arbitrary text |
| C10 | Control boolean changed during normalization | Reject; no coercion or flag upgrading |
| C11 | Any provider API key/session token in proposed generic audit metadata | Reject/redact; no credential persistence |
| C12 | Human attestation for retry/re-arm supplied to recovery-of-result command | Reject as wrong authority type |
| C13 | Valid human contextual result approval, but provider result claim index cannot be read/written | Reject; approval cannot replace global uniqueness or authorization evidence |
| C14 | Human approves context-corroborated recovery, but historical live callback claim coverage is not provably complete | Reject, no Work write: human approval cannot replace shared live/recovered index coverage |
| C15 | Human approves result while legacy writer epoch fence is absent or unverifiable | Recovery admission rejected even when index reports complete; human attestation cannot bypass storage writer fence |

## D. Concurrent, duplicate and lifecycle cases

| ID | Condition | Required behavior |
| --- | --- | --- |
| D01 | Two observers read same provider result | Both may read; one claim owner only, and at most one strict-CAS role outcome appends; loser re-projects |
| D02 | Unrelated Work event lands **after gate preflight but before final outcome append** | Strict expected revision/digest CAS on **final append** rejects; cannot call rebinding `record_outcome` unmodified; fresh attempt uses new proof ID |
| D03 | Same exact proof/result after restart | `ALREADY_ADMITTED`, zero new events, same terminal identity |
| D04 | Different response/hash after one outcome admitted | `CONFLICT`, not a second terminal result |
| D05 | Same result/handoff retried after frontier refresh | Provider claim and proposed outcome identity stay stable, **proof-attempt ID changes with frontier**; no WorkIdentityConflictError from reused proof event ID |
| D06 | Role completed by another authorized path during probe | Re-project and return conflict/already-admitted as appropriate |
| D07 | Work cancelled or terminal before admission | Reject, do not resurrect |
| D08 | Recovery proof append ack lost, then head advances | Re-read prior proof event by ID/digest, do not mutate it; create new frontier-bound proof attempt under new event ID; no resend |
| D09 | Response is valid but proof event has been corrupted | Reject projection, no successful outcome |
| D10 | Caller supplies a stale `expected_head_digest` | Reject, no independent write |
| D11 | Context-corroborated human approval scoped to wrong request | Reject |
| D12 | Result recovered with `material_uncertainty_resolved=false` | Durable synthesis output allowed by policy, but Research Pack completion remains unmet |
| D13 | Normal callback and recovery both claim same provider result at different process restart points | Across restart, stable provider-result claim belongs to first handoff; second rejected, including when live callback has already durably completed |
| D14 | Old callback begins before activation, holds a write transaction, then attempts commit while epoch migration waits | Storage serializes writers: old transaction finishes before barrier commit and is included in subsequent coverage scan, or migration fails closed; never silently races post-fence index |
| D15 | Old callback begins before activation but opens a **new** append transaction after barrier commit | DB rejects missing epoch/claim; Work chronology unchanged for that callback and no retry permission inferred from failure |
| D16 | Restart/rollback/stale process attempts legacy callback against activated epoch | Durable storage fence remains enforced across restart; lost/unverifiable epoch protection disables recovery; no process-local-only acceptance |
| D17 | Reconciliation passes `RECOVERY_ENABLED(epoch=7, coverage=A)` preflight, then disable/epoch 8 commits **before the final outcome append** | The final append re-reads current status **within the same serialized write transaction** and rejects stale epoch, with zero outcome events; cached preflight cannot override revocation |
| D18 | Recovery preflight passes under coverage digest A, then historical completeness is revoked or coverage changes to B **before final outcome append** | Final transaction rejects old coverage digest despite a previously valid provider claim and human attestation; no outcome append; disable/revalidation uses the same storage serialization barrier |
| D19 | **Final authority read-to-append race**: two DB connections, pause inside outcome transaction after reading current enabled epoch/coverage but before appending; concurrently try to commit disable/epoch rotation or coverage revocation | Shared DB serialization permits **only** (a) outcome commits under valid enabled authority, then revocation commits, or (b) revocation commits first and the outcome transaction rejects with zero new role events. No stale successful append; a standalone second preflight read followed by unrelated append transaction must fail this gate. Test both interleavings, restarts and work-head CAS together |

**Review-critical gate requirements (P1):** D02 must inject a competing Work append **between** authorized preflight and the final append, not merely before a preliminary snapshot; a gate wrapping existing rebinding `record_outcome()` must fail this test. B17 must use **two separate Work IDs** and a shared installation-scoped atomic result-claim index. B21/B25 must reuse one immutable provider result across adapter upgrade with unchanged global claim identity. B22/B23/B24 and C13 assert fail-closed behavior when the uniqueness index or stable result identity is unavailable, **including a human-approved candidate**. B26/B29/D13 assert the uniqueness gate is shared by **normal live callbacks and recovery**; B27/B28/C14 assert fail-closed coverage for historical unindexed callbacks even after adapter upgrades or human approval. B30/B31/C15/D14/D15/D16 require a **storage-serialized writer epoch barrier** that rejects old callbacks straddling cutover and survives restart; no process-local or deployment-time substitute. D17/D18 must inject epoch disable/rotation or coverage invalidation strictly **between preflight and the final append**, proving current authority is checked under the same storage transaction as outcome CAS rather than in a cached or preflight-only snapshot. D19 further pauses **after the final authority read within the append transaction** and races a revocation through a second DB connection; prove only the two serializable histories, never an append authenticated by a stale read. D05/D08 must verify deterministic-but-distinct proof-attempt IDs for distinct Work frontiers, and stable provider claim/outcome semantics without duplicate append.

## E. Explicit separation from M6.6 re-arm

| ID | Case | Requirement |
| --- | --- | --- |
| E01 | Provider proves `BROWSER_OWNED_WRITE_NOT_SUBMITTED` with exact disposition | Hand to the separately governed re-arm path from #21; **do not** invent a completed response |
| E02 | Historical in-flight effect with explicit human re-arm decision | #23 boundary only; must not impersonate provider proof |
| E03 | Generic exception after handoff, no receipt | Stay unresolved; no automatic retry or completed outcome |
| E04 | Verified completed response exists | Reconcile result; must not invoke re-arm |
| E05 | Both provider `NOT_SUBMITTED` and completed-turn evidence for same attempt | Conflict requiring investigation; no automatic action |

## F. Minimum gate expectations for future implementation

1. `ruff check` / `ruff format --check` and offline `pytest`, with no CWA, network or browser dependency for pure gates.
2. Exact event chronology equality after every dry-run/rejected case (read-only proof observation must not mutate Work).
3. Zero `CognitionPort.complete()` calls and zero provider writes for **all** reconciliation cases.
4. Count exact append events across process restarts; no duplicate handoff/proof/outcome events.
5. Verify role result hash and reconciliation proof after restart using durable projections, not in-memory caches.
6. Include one integration fixture with a provider adapter that *does not* support historical reads; it must fail closed without weakening Gen2.
7. Maintain separate completion gate: role recovered != Work completed.
8. Explicitly record what tests do *not* establish: real provider stability, remote session expiry behavior, hardware crash durability, cross-account authorization and universal exactly-once effect semantics.

### Future conformance proof (not part of this design-only deliverable)

For the first provider that exposes an authenticated read contract, run a bounded **single actual submission** with a deliberate post-completion/pre-outcome interruption, inspect the provider read-only, reconcile, check Work events and verify that total external submission count is exactly one. Capture account/branch identity, provider response ID, source raw/hash, canonicalization policy, and admission provenance. A success here would prove **one provider vertical** only.

**Open review decisions:** proof storage boundary, provider-correlated receipt minimum, context-corroborated human admission policy, and deterministic outcome identity strategy. Do not implement until those are settled.