# Issue #88 — ADR-001: Reconciliation proof, authority and replay

**Status:** proposed for review (2026-10-08). No implementation authorization.  
**Applies to:** recovering the result of an already-admitted cognition handoff.  
**Does not apply to:** re-arm / retry of a proved-unsubmitted effect (#21, #23).  
**Related:** [architecture v0](issue_88_cognition_outcome_reconciliation_v0.md), [test matrix](issue_88_cognition_reconciliation_test_matrix_v0.md), pure verifier PR #90.

## Decision 1 — Canonical proof binding belongs to Work chronology

**Choose:** an append-only, schema-versioned Work event for *reconciliation proof association*; optionally use a host-managed content-addressed payload store for large/sensitive provider evidence. An external blob store by itself is **never** the Work truth.

- The proof event binds the existing request/handoff/role/Work digests, provider namespace, original wire-input digest, response locator/IDs, raw + canonical content digests, transformation policy, provenance/proof level, and authorized human decision reference where required.
- It contains no credentials, cookies, raw browser lease, auth header or provider token. Any raw provider evidence outside Work is privacy-governed, integrity checked and addressed by content digest. The existing CognitionOutcome can still carry its canonical output text; the proof event need not duplicate it.
- Crash-ordering protocol for later implementation: (1) verify/store referenced evidence bytes; (2) CAS-append an immutable *proof pending-admission* event; (3) CAS-append the existing outcome through the exact bridge; (4) re-project and compare. The proof event alone **never means the role completed**.
- If proof append is acknowledged ambiguously, locate its deterministic identity and exact digest before taking another step. If outcome append is acknowledged ambiguously, recover the terminal RoleRun and compare the expected semantic response and proof binding before returning ALREADY_ADMITTED.
- A Work revision movement between steps fails closed or triggers a *new read-only* verification. No weak bypass of the WorkStore CAS frontier is allowed.

**Why:** a separate host-only ledger cannot by itself show which result became authoritative in the canonical Work timeline. A single atomic two-event append would be ideal but would require a new WorkStore transaction API; a recoverable two-step CAS sequence is the narrower initial proposal.

**Risk to resolve in implementation:** recovery must not expose a pending proof as an admitted outcome; event projection, retention and referential availability must be validated before new event kinds are introduced.

## Decision 2 — Provider correlation threshold

**Choose:** treat a response as EXACT_PROVIDER_CORRELATED only with *trusted* provider-read provenance and a causal request-to-execution binding established at original send time. A message ID and matching prompt alone are not proof that the response belongs to this specific dispatched effect.

Minimum for exact tier:

1. The host preserves a stable, unique original outbound attempt token or operation ID associated with the durable request/handoff before or during the write boundary; if provider-native, the provider echoes it.
2. An authenticated read-only provider endpoint/adapter returns that same causal token and a stable execution/turn identifier (including tenant/account namespace).
3. The correct request/wire bytes or cryptographic digest are bound to that token, including adapter serialization version.
4. A final completion signal binds the response ID, exact raw content hash and branch/execution lineage. The response is unique for this attempt; conflicting sibling completions fail closed.

Where any of those is not available, maximum tier is **CONTEXT_CORROBORATED**, even if the plaintext matches perfectly. CWA conversation history by itself is not assumed to meet exact-tier requirements.

Provider-read APIs must expose declared capabilities (stable execution ID, branch lineage, completion signal, original-wire readback, receipt verification), with a versioned trust contract. Unsupported or unavailable features downgrade/fail closed; no guessed correlation based on timestamps.

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

Use a namespace-hashed key (for example UUIDv5) for proof and proposed outcome identity, **not** a newly generated random UUID on every retry. Persist the exact proposed outcome metadata, including creation timestamp, before first admission attempt or derive it from a durable record. A replay must reuse the same identity and payload bytes; deterministic ID alone is not sufficient because changed timestamp changes the event digest.

If a Work already has a matching completed outcome, report ALREADY_ADMITTED with zero appended events. If a different provider response or canonical digest claims the same handoff, return CONFLICT, not a second terminal outcome. Concurrency is resolved via WorkStore CAS then re-projection; compare original source provenance and semantic digest after losing a race.

## Explicitly deferred

- Detailed event schema, migration and collision behavior in Gen2 Core.
- Selection of the first provider with genuine authenticated readback + stable causal receipt; CWA does **not** automatically qualify.
- Token/receipt retention, privacy limits, encrypted payload storage and deletion policy.
- Whether a privileged **exact-tier** result may ever be admitted automatically in a later product policy; v1 default is no.
- A real integration test of crash between proof and outcome; these decisions are not an E2E proof.

## Review/acceptance checklist

- [ ] Verify the proof is authoritative only as a Work event association, not as a provider action or RoleRun completion.
- [ ] Verify authenticated causal correlation is never inferred from a prompt or readback message ID alone.
- [ ] Verify human contextual approval is separately recorded and cannot remove conflicts or permit retries.
- [ ] Verify stable result + timestamp replay and single terminal outcome across crashes and CAS races.
- [ ] Confirm the provider-read boundary remains read-only and outside Gen2 Core.
- [ ] Decide scope for the first bounded provider adapter before starting implementation.

**Safety invariant:** absence of evidence of an external effect is never evidence that the effect did not occur. Reconciliation issues no new external request.
