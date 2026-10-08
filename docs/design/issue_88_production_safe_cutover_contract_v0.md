# Issue #88 — Production-safe SQLite cutover contract v0

**Status:** design proposal + disposable-DB concurrency tests. **Not an executable production migration or approval to enable reconciliation.**

Related: [ADR-001](issue_88_adr_001_reconciliation_proof_authority.md), [82-case recovery matrix](issue_88_cognition_reconciliation_test_matrix_v0.md), [offline SQLite spike](issue_88_sqlite_contract_spike_v0.md), [Issue #88](https://github.com/kymuco/codexia-manual-agent/issues/88).

## Existing code — factual baseline

1. `SqliteWorkStore._initialize()` creates `g2_work_v1` and `g2_work_event_v1` with `event_id` uniqueness, `(work_id, sequence)` uniqueness and a Work foreign key. There are **no** provider-result claim, epoch, coverage, or proof-association tables today.
2. `SqliteWorkStore._append_event()` uses `BEGIN IMMEDIATE`, checks Work revision, prior event digest and Work state, then appends one hash-chained event. `append_with_child_create()` is a **second** direct insertion site in the same Work event table. Both require coverage by any cutover fence.
3. `CognitionTransportBridge.record_outcome()` validates durable handoff bindings, then obtains the **latest** Work snapshot, calls `outcome.bind_to_snapshot(current)` and `RoleAdmission.admit_outcome()`. This is not the strict preauthorized head-CAS boundary needed for reconciliation.
4. `RoleAdmission.admit_outcome()` admits a `CognitionOutcome` through the workflow candidate boundary and checks the pending RoleRun. Existing `CognitionOutcome` and the generic model provider port do **not** guarantee stable provider-side result IDs. Normal live callbacks therefore cannot be presumed present in a global result-claim index.
5. Merged PR #91 tests a **synthetic** outcome with a scratch trigger against disposable Gen2 SQLite tables. It establishes a useful transactional hypothesis but **not** migration/backfill safety or provider-account coverage.

**Consequence:** adding an index alone does not make reconciliation safe. Without a database-enforced write barrier, an already-running older callback can append an unindexed `role.completed` after new recovery is enabled.

## Proposed durable authorities (future schema; no DDL shipped here)

| Concept | Canonical key | Meaning |
| --- | --- | --- |
| Writer protocol state | Installation-local SQLite DB identity | Monotonic writer generation, schema/migration state, recovery default **disabled** |
| Provider coverage | `(provider_service, tenant_namespace)` | Epoch, enabled/disabled, immutable coverage evidence digest and scope of provider history |
| Provider result claim | Stable provider-side `(service, tenant, execution/branch, response_id)` | Installation-wide unique assignment to one Work/request/handoff; adapter version is **metadata only** |
| Frontier proof attempt | Work/request/handoff + exact previous Work digest/revision | Immutable *pending* evidence association; never a completed RoleRun |
| Outcome append intent | Exact provider result + Work head + authorized epoch/coverage | Storage-only, transaction-bound admission token; not a permission for legacy clients |

The claim index, Work chronology and final admission authority must live inside **one** atomic SQLite transaction (one DB file). External databases, per-process locks, successful timestamps and detached authorization reads cannot establish the required guarantee. Schema changes must be versioned and idempotent only where exact schema and digest equivalence has been validated; `CREATE TABLE IF NOT EXISTS` alone is not proof of compatibility.

## State machine and cutover order

```text
LEGACY (recovery forbidden)
  → PREPARED_DISABLED (additive schema only; legacy live paths still work)
  → FENCED_QUARANTINED (storage-installed legacy writer barrier)
  → COVERAGE_VERIFIED (proof of retained provider-result source window)
  → RECOVERY_ENABLED(epoch, coverage_digest) (explicit separate authorization)
```

**Phase 0 — inspect and protect.** Identify all writable instances/processes, file paths, SQLite journal mode/WAL sidecars and every Work event insertion path. Obtain a tested consistent backup while writes are quiesced or through SQLite's supported backup mechanism; checksum the backup and independently verify restore. Do not migrate a DB with incomplete inspection, inconsistent backup or unknown alternate writers. A file copy that omits WAL pages is not a verified backup.

**Phase 1 — prepare only.** Deploy a new writer that can preserve authenticated provider-side result identity and use a shared claim boundary for **both normal callbacks and recovered responses**, without enabling reconciliation. Add tables and schema-version metadata using a transactional migration with introspection and exact schema validation. Existing live writers can remain operational during this stage, but coverage is explicitly **unproven**. Do not write `RECOVERY_ENABLED` based on an empty/new index.

**Phase 2 — enforce a storage-level barrier.** On the same SQLite database, perform a `BEGIN IMMEDIATE` migration transaction that serializes against preexisting Work event append transactions and installs a mandatory storage-side check for *all* terminal cognition role events (`role.completed`, `role.failed`, `role.outcome-unknown`) and **every insertion path**, including `append_with_child_create()`. A client that predates the migration and opens its next write after the barrier must be rejected **by SQLite**, not only by newer Python. Old transactions that acquired write authority before the barrier commit first (and enter the historical review window), or migration cannot proceed. No automatic provider resend follows a rejected callback.

The future fence must require a same-transaction, provider-identified claim/intent and writer generation, with no path for arbitrary unindexed terminal cognition outcomes. The scratch PR #91 trigger is a *probe*, **not** deployment-ready SQL. A downgraded client able to alter schema/disable triggers is outside the assumed cooperative-writer model; if untrusted clients have raw database schema-write access, SQLite alone is not a security boundary. Migration must be blocked until this trust boundary is addressed.

**Phase 3 — prove historical coverage.** Once legacy writes are fenced, inventory every already committed ordinary callback visible in the relevant provider source-history window. A completed Work event without a stable provider-side result identity cannot be trivially backfilled by matching output text, timestamps or hash. Require either (a) authenticated, unambiguous source-side identity backfill with collision resolution, or (b) a provider-supported, independently verifiable source-history exclusion boundary proving that old unindexed results are *unavailable for recovery*. Unknown history/retention, provider namespace aliases, mixed adapter IDs or partial backfill mean `FENCED_QUARANTINED`, **not** enabled. Claiming one provider's namespace says nothing about another.

**Phase 4 — enable deliberately.** A distinct authorized operation may set `RECOVERY_ENABLED(epoch, coverage_digest)` only after verifying the fence, existing live-writer claim path, claim-index completeness, source-history proof, and local DB generation. The new **strict** outcome append transaction must:
- Check matching already committed outcome/claim **first** and report `ALREADY_ADMITTED` read-only, even if recovery has since been disabled (never silently rewrite an event).
- For **new** writes, under the same `BEGIN IMMEDIATE` transaction: validate current enabled epoch and exact coverage digest, namespace-scoped claim uniqueness, human authority/proof policy, expected Work revision **and** previous event digest, pending RoleRun/Handoff binding and exact prospective event bytes; then append the claim, frontier proof association and outcome according to the separately reviewed crash-safe event protocol.
- Serialize disabling/epoch rotation/coverage revocation through the same DB write barrier. Test D19 after the final authority read and before insertion; only append-before-revocation or revocation-before-rejected-append is acceptable.
- Reconcile ambiguous acknowledgement by re-reading the exact immutable event and claim without a resend or a second role outcome.

**Critical sequencing:** The current `record_outcome()` snapshot-rebinding path must **not** be reused as this new final-append gate. Do not enable recovery without replacing the relevant admission boundary and proving normal callback participation.

## Abort, downgrade, rollback and restart

- **Before the barrier (PREPARED_DISABLED):** an additive schema-only rollout may be backed out, provided the database snapshot and schema are consistent, no new writers use the new tables, and existing live behavior is verified. **No recovery had been enabled.**
- **After the barrier:** the safe operational fallback is to **disable reconciliation and preserve the writer fence and global claims**. Do **not** drop the trigger, lower the writer generation, clear claims or roll back an old binary that can append unindexed role outcomes. Older callbacks may become unavailable until upgraded; availability loss is preferable to double admission.
- **Power loss or ambiguous cutover acknowledgement:** reopen the DB, inspect schema version, exact trigger SQL/digest, generation, claim uniqueness, coverage state and Work chronology. If proof is incomplete or altered, force `FENCED_QUARANTINED` or halt new writes; do not auto-promote. A stale recovery-enabled record is not sufficient.
- **Restore from backup:** restores *also* restore older writer generations/claims and can resurrect an unindexed source window. Do not pair restored DB state with later provider readback without a new coverage review; invalidate recovery authorization until re-proven. Never silently replay old provider writes.
- **Cross-installation copy:** claim uniqueness is guaranteed only inside one installation/DB lineage. Clones that can point at the same external provider identity need an explicit shared owner/lease policy, or recovery stays off.
- **Unsupported backend:** if final read-to-append serialization, trigger enforcement, or durable claim uniqueness cannot be proven, reject migration and keep recovery disabled.

## Proposed adversarial migration gates (new; not production tests)

| ID | Injection | Required outcome |
| --- | --- | --- |
| M01 | Native writer holds `BEGIN IMMEDIATE` while cutover installer attempts schema/trigger migration | Installer cannot commit until old writer commits/rolls back; old event included in pre-barrier chronology |
| M02 | Existing pre-opened `SqliteWorkStore` performs another role terminal append **after** trigger activation | SQLite rejects the unindexed outcome, no new Work event |
| M03 | Reopen database or use a new connection after cutover | Same trigger fence persists; no process-local-only protection |
| M04 | Revoke coverage/epoch after activation | Reconciliation disabled; legacy writer fence is **not removed** |
| M05 | Historical callback lacks provider-side result ID, backfill/exclusion proof missing | Remain quarantined; no recovery enable, even with human approval |
| M06 | Cutover DDL interrupted before commit, or a schema mismatch exists | Transaction rolls back or migration rejects; never advertise an active fence based on partial DDL |
| M07 | Recovering same external result across another Work or adapter upgrade | One stable claim; second Work rejected, no duplicate role outcome |
| M08 | Final authority read paused before append and competing revocation commits/blocks | Only two linearizable commit orders, no stale successful append |
| M09 | Lost acknowledgement after successful admission, then epoch revoked | Exact read-only replay returns `ALREADY_ADMITTED`; no new append |
| M10 | DB restore loses recent claims, or source history overlaps pre-cutover results | No re-enable without revalidated external-history coverage |
| M11 | Provider service A and B share tenant label | Distinct authority scopes; A's coverage cannot enable B |
| M12 | `append_with_child_create()` or alternate raw SQLite insert emits unindexed terminal cognition event | Storage-side barrier rejects regardless of high-level call path |

## Current evidence and exit criterion

- The accompanying new offline test file covers **M01–M04**, against freshly created, disposable `SqliteWorkStore` databases. Existing PR #91 covers related scratch-only result-claim/D19 checks.
- **M05–M12 remain proposed gates** for a separate, scoped implementation and are **not** asserted complete here.
- No provider adapter, migration tooling for existing installations, WorkStore table mutation, permission elevation or real RoleAdmission is included in this PR.

**Accepting this design does not authorize production rollout.** The next implementation needs a schema-by-schema migration plan, authenticated provider identity contract for ordinary callback writes, end-to-end crash-injection of pending proof/outcome and a separate human-reviewed enablement gate.
