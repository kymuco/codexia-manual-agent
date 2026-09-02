# M2.4.5 retained TxF execution-lineage repair

## Finding

A final P2 review found that `PatchRecoveryManager.recover_live()` selected the sole unfinished
retained TxF transaction by count only. The selected transaction was not bound to the supplied
executed lifecycle/result. A transaction retained for execution A could therefore be queried or
rolled back while a recovery receipt was emitted for execution B.

## Repair boundary

M2.4.3 mutation and commit semantics remain unchanged, but its retained-transaction bookkeeping
now records immutable execution lineage as soon as `record_executed()` succeeds and before any
staging/publish or commit attempt can retain that transaction:

- transaction object identity;
- `execution_id`;
- `proposal_id`;
- `proposal_digest`;
- `plan_digest`;
- `change_set_digest`.

This preserves the accepted M2.4.3 -> M2.4.5 live-recovery path for a plain
`PatchApplicationExecutor`. A transaction retained before an EXECUTED lifecycle exists has no
execution lineage and is fail-closed for terminal live recovery; it cannot be attached to a later
execution merely because it is the only unfinished transaction.

`PatchRecoveryManager.recover_live()` validates the retained object identity and complete lineage
against both the supplied lifecycle/plan and exact `INDETERMINATE` `PatchApplicationResult`
**before** any `GetTransactionInformation` query or `RollbackTransaction` retry. A mismatch cannot
inspect, mutate, close, or observe the foreign transaction.

A successful handle cleanup removes both the retained transaction and its lineage binding. A failed
cleanup preserves both. The same cleanup bookkeeping also applies to finished retained handles, so
transaction identity is never silently rebound across executions.

## Regression gates

Four focused contract tests cover:

1. an unbound retained transaction is rejected before KTM query;
2. execution-A transaction cannot recover execution B and is rejected before query/rollback;
3. reusing the same `execution_id` with another proposal/plan still fails lineage validation;
4. an exact binding can reconcile and is forgotten only after successful handle cleanup.

The existing real-Windows retained-TxF test continues to use the plain M2.4.3
`PatchApplicationExecutor`, proving that accepted M2.4.3 retention now carries the exact lineage
needed by M2.4.5 without requiring a special executor type.

Fresh Windows focused and full-suite validation is required on the repaired head before merge.
