# M2.4.5 indeterminate-result preservation repair

## Finding

A final pre-merge P2 review found that a post-EXECUTED failure such as a durable `COMMIT_INTENT` journal append error could be followed by an indeterminate TxF rollback. The accepted M2.4.3 broad exception path retained the unfinished transaction but re-raised only the original exception, so the caller received no `PatchApplicationResult(INDETERMINATE)`. `PatchRecoveryManager.recover_live()` requires that exact result, leaving the retained transaction outside the supported live-recovery path.

## Repair

`RecoverablePatchApplicationExecutor` now inspects an ordinary `Exception` escaping the base executor before it leaves the M2.4.5 boundary. It synthesizes an operational `INDETERMINATE` result only when all of the following are true:

- the lifecycle has an executed `execution_id`;
- exactly one unfinished retained TxF transaction exists;
- that transaction has the immutable retained-transaction lineage recorded after `record_executed()`;
- the lineage matches the exact transaction object, execution id, proposal id/digest, plan digest and change-set digest supplied to the current execution.

The returned result is bound to the exact current proposal/plan, carries `failure_stage=ROLLBACK`, preserves the original escaping exception as `error`, and states that rollback could not be proven and the unfinished TxF was retained for live recovery.

If no exact retained executed lineage exists, the original exception is re-raised. Pre-authority/unbound transaction failures therefore remain fail-closed and cannot be converted into an executed result.

`KeyboardInterrupt`, `SystemExit` and other non-`Exception` control-flow failures are not swallowed or converted.

## Recovery continuity

The repair does not add authority, commit or observation paths. The synthesized result is only operational evidence needed to enter the already-governed `recover_live()` flow. The existing retained-lineage checks still execute before KTM outcome query or rollback retry.

A new real-Windows regression exercises:

`COMMIT_INTENT append failure -> rollback indeterminate -> exact INDETERMINATE result -> retained lineage -> recover_live() -> synchronous rollback -> exact preimage observation -> OBSERVED`.

The focused M2.4.5 suite therefore increases from 29 to 30 tests. Fresh real-Windows focused and full-suite validation is required on the repaired exact head before merge.
