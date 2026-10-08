# Issue #88 — SQLite claim/epoch/strict-CAS contract spike v0

**Status:** offline hypothesis only; no migration, production admission, provider calls, or permission to enable recovery.  
**Source:** [Issue #88](https://github.com/kymuco/codexia-manual-agent/issues/88), merged [ADR-001](issue_88_adr_001_reconciliation_proof_authority.md), [82-case test matrix](issue_88_cognition_reconciliation_test_matrix_v0.md).  
**Artifacts:** [scratch adapter](../../tools/issue88_sqlite_contract_spike.py) and [offline fixtures](../../tests/test_issue88_sqlite_contract_spike.py).

## Concrete question

Can an SQLite write transaction hold the provider-result uniqueness claim, current enabled epoch and coverage digest, Work revision/previous-digest check, and durable append behind one barrier, while a separate process tries to revoke authority?

**Experiment:** an isolated scratch-only adapter uses SQLite's `BEGIN IMMEDIATE` and a `BEFORE INSERT` trigger on the actual `g2_work_event_v1` table shape. It inserts a **synthetic** `issue88.spike-outcome` event, **not** an admitted `role.completed`. Legacy `role.completed`, `role.failed` and `role.outcome-unknown` inserts without an intent+claim are rejected by the scratch trigger. Claim keys are provider-side identities (service/account/execution/response), excluding adapter version and Work ID. **Epoch/coverage authority is keyed by both provider service and account namespace**; two services with the same account label cannot share an authorization.

On a fixture-enabled namespace the scratch append runs in one transaction: read current epoch/coverage, optionally pause for D19, check unique result claim, compare exact Work revision/head digest, insert claim and ephemeral write intent, append synthetic event, remove intent, commit. Rollback removes all intermediate writes. An exact same-handoff/result replay returns `ALREADY_ADMITTED` without another event **even after epoch revocation**: this is a read-only acknowledgement of a committed event, not a new admission. A different Work attempting the same result fails closed.

### Verified scope of the deterministic fixtures

- Fail closed with disabled/stale/changed epoch or coverage for **new** writes; already-committed exact replay remains read-only.
- Host-wide cross-Work result uniqueness and adapter-version-independent claim key.
- Reject missing provider identity, malformed digest and failed claim index read.
- Preserve event-count/replay properties and strict Work revision/head comparison.
- Old direct role event inserts are rejected by storage, including after opening a new SQLite connection.
- **D19:** pause *after* final authority read while its write transaction is still open; a second connection attempting revocation cannot commit until the admission transaction completes. Reverse order (revocation first) rejects the stale candidate.
- Separate targeted test opens a real disposable `SqliteWorkStore`, verifies its native `append` is fenced, then verifies the scratch synthetic event is parseable by its Work event projection.

## Non-claims and limitations

1. **Do not install this experiment in an existing WorkStore.** Its trigger intentionally breaks current live role callbacks, which have no provider-result claim. Tests use disposable databases only.
2. `fixture_enable` asserts a **synthetic** coverage digest. It does not authenticate a provider, prove historical completeness, fence previously opened processes during schema rollout, or authorize real recovery. A passing test says nothing about safe in-place migration, backfill, external session histories or malicious DB writers.
3. The scratch event is a structurally digest-valid WorkEvent, **not** a validated RoleRun state transition. This adapter bypasses `RoleAdmission` on purpose to probe storage atomicity and must never be invoked as a product API.
4. This is a **single SQLite file** experiment. Cross-database atomicity, durability under physical power loss, filesystem corruption, and multiple independent installations are not proven.
5. Proof event ordering, CAS-integrated `RoleAdmission`, production DB writer epoch cutover, global index migration/retention, real provider causal receipts, and source/host permissions are **not implemented**.
6. The local fixture tests are not the full 82-case matrix. A green GitHub CI run, if obtained, validates this narrow set only.

The store's native `SqliteWorkStore._append_event()` already performs `BEGIN IMMEDIATE`, revision and previous-digest checks; however its existing `record_outcome()` path does **not** have the result claim, activation/coverage gate, or migration fence described in ADR-001. The scratch experiment does not change that.

## Reproduce

From the repository checkout, without CWA, browser, model credentials, GPU, or a production WorkStore:

```powershell
python -m ruff check tools/issue88_sqlite_contract_spike.py tests/test_issue88_sqlite_contract_spike.py
python -m ruff format --check tools/issue88_sqlite_contract_spike.py tests/test_issue88_sqlite_contract_spike.py
python -m pytest tests/test_issue88_sqlite_contract_spike.py -q
```

**Exit to next stage:** review the observed storage properties and **separately** design a live-writer migration fence and proof-admission API. Do not enable recovery for any provider namespace based on this spike alone.
