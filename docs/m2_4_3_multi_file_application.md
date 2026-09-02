# M2.4.3 — Multi-file application and failure semantics

M2.4.3 turns the accepted M2.4.2 deterministic execution plan into one bounded
Windows application operation. It defines the commit/failure model only. Exact
changed-file observations and digest-bound mutation evidence remain M2.4.4.

## Accepted commit model

On the supported Windows boundary, one patch is applied through **one Windows TxF
transaction**:

```text
AUTHORIZED patch lifecycle + exact M2.4.2 PatchExecutionPlan
        ↓ validate exact parent/plan binding
retry unresolved prior transaction-handle cleanup
        ↓ no authority consumed
M2.4.2 execution-support preflight
        ↓
require TxF support for every target parent, including CREATE
        ↓
M2.4.2 final live whole-set revalidation
        ↓
create one inspection-only TxF transaction
        ↓
pin every M2.3 parent chain
pin every REPLACE target transactionally + capture preservable metadata
recheck every CREATE absence
        ↓ all admission checks passed
consume the single patch-level authority receipt
record one patch execution id
        ↓
stage every exact postimage inside the same transaction
        ↓
CREATE: transactional no-clobber publish
REPLACE: transactional strict replace + preserved accepted metadata
        ↓ first failure stops the sequence
close file handles needed before transaction end
        ↓
CommitTransaction()                 failure before commit
        ↓                            ↓
COMMITTED                       RollbackTransaction()
                                     ↓
                               ROLLED_BACK / INDETERMINATE
```

The single `CommitTransaction()` call is the only filesystem commit point for the
patch. M2.4.3 therefore selects all-or-fail semantics on the currently supported
Windows/local-NTFS/TxF execution boundary instead of exposing sequential partial
commit as the normal model.

TxF availability is treated as a runtime capability, never a permanent Windows
guarantee. The accepted M2.3 support probe remains fail-closed, and M2.4.3 extends
that requirement to CREATE parents because CREATE now participates in the shared
transaction. If a future Windows/volume no longer exposes the required TxF
contract, patch application must fail before authority consumption rather than
fall back to sequential non-atomic writes.

## Authority boundary

M2.4.3 consumes **exactly one authority receipt**: the receipt bound to the
original M2.4 patch `ActionProposal`.

It does not mint, authorize, or consume synthetic per-file receipts. Each
`PatchExecutionStep` is re-parsed through the accepted M2.3 CREATE/REPLACE
structural proposal schema solely to recover the canonical M2.3 mutation plan and
reuse its boundary checks. Those internal structural `ActionProposal` objects are
not authority objects for execution.

Before the patch receipt is consumed, M2.4.3 must have already established:

- exact M2.4.2 parent-plan binding;
- Windows-only execution;
- accepted Windows target spelling;
- local writable NTFS + TxF availability for every target parent;
- final live M2.4.2 whole-set preimage/namespace match;
- a live shared TxF transaction;
- pinned M2.3 parent chains for every step;
- exact transacted REPLACE target identity and preservable metadata;
- final CREATE absence at the pinned parent.

Any failure in that admission phase raises before receipt consumption and leaves
the lifecycle `AUTHORIZED`.

### Relationship to the M2.4.2 “final live gate”

M2.4.2 described `revalidate_patch_execution_plan()` as the final M2.4.2 live
gate immediately before future authority use. M2.4.3 does not weaken that rule:
after that whole-set reconstruction it performs only a **stronger read-only commit
admission** step before consumption — opening the shared TxF transaction, pinning
the accepted M2.3 parent chains, transactionally pinning every REPLACE target and
rechecking CREATE absence. No staging, publish, mutation, or authority use occurs
between the M2.4.2 gate and those exact commit pins.

The commit-admission pins are deliberately required because the accepted M2.3
primitive itself performs exact object pinning immediately before consuming
authority. Treating M2.4.2's point-in-time revalidation as a durable freshness
receipt would be weaker, not stronger. The authoritative pre-consumption sequence
is therefore: whole-set revalidation → stronger exact commit-admission pins →
consume once.

## CREATE semantics

CREATE remains no-clobber.

The postimage is staged as a transacted file under the already-pinned parent and
published with `MoveFileTransactedW` **without** `MOVEFILE_REPLACE_EXISTING`.
Therefore a foreign target that appears after authority consumption cannot be
overwritten. The create publish fails and the entire patch transaction is rolled
back.

The M2.3 Windows parent chain stays pinned across staging, publish, and the final
transaction commit. These handles omit delete sharing, preserving the accepted
M2.3 parent-namespace boundary while the absolute final target spelling is used by
the Windows rename API.

## REPLACE semantics

REPLACE reuses the accepted M2.3 TxF boundary:

- exact destination is opened transactionally with zero sharing;
- the bound preimage must still match exactly before authority consumption;
- supported security descriptor / file-attribute / default-stream policy is
  captured from that exact target;
- metadata binding is checked again before publish;
- the exact postimage is staged in the same shared transaction;
- accepted metadata is applied to the staged object;
- `MoveFileTransactedW(..., MOVEFILE_REPLACE_EXISTING, ...)` schedules the strict
  replacement;
- transaction commit makes all scheduled patch transitions visible together.

No path-based fallback is added.

## Failure states

M2.4.3 exposes only transaction-level operational outcome:

- `COMMITTED` — `CommitTransaction()` succeeded; all scheduled patch transitions
  belong to the committed transaction;
- `ROLLED_BACK` — authority was consumed, application failed before a successful
  commit, and `RollbackTransaction()` succeeded;
- `INDETERMINATE` — rollback after a failed/uncertain transaction outcome could
  not be proven. No further patch execution may silently continue in-process.

The first classified post-consumption failure stops application immediately.
Later steps are not attempted as best-effort continuation.

A failed transaction-handle close after a proven commit or rollback does not
change the filesystem commit state, but the handle is retained and future patch
execution is blocked until cleanup succeeds. An unfinished retained transaction is
never automatically rewritten into a success/failure claim; M2.4.5 recovery owns
that reconciliation.

## Lifecycle boundary

M2.4.3 performs:

```text
AUTHORIZED
   ↓ consume patch receipt
EXECUTED
```

It deliberately does **not** call `record_observed()`.

`PatchApplicationResult` is an operational return object, not an authorization
receipt, mutation receipt, or proof of final target bytes/metadata. M2.4.4 must
inspect the terminal filesystem state, emit exact per-file/set mutation evidence,
and move the lifecycle from `EXECUTED` to `OBSERVED`.

## Crash boundary

This milestone does not claim complete process-death/restart recovery.

A shared TxF transaction naturally provides a strong rollback boundary before
commit, but durable reconciliation of interruption at/around the commit point,
retained transaction state across process lifetime, retry policy, and deterministic
recovery are explicitly M2.4.5 work.

M2.4.3 therefore does not label an unresolved rollback/commit outcome as either
success or failure. It returns/retains `INDETERMINATE` and stops.

## Platform boundary

Linux mutation remains disabled. No Linux/POSIX patch-application PASS is implied
by this contract. M2.3a remains the prerequisite for a Linux commit backend.

## Focused gates

M2.4.3 coverage should prove at least:

- one patch receipt is consumed exactly once;
- no synthetic per-file authority receipt is created or consumed;
- all steps are structurally accepted by the M2.3 CREATE/REPLACE schema;
- TxF support is required for CREATE as well as REPLACE before receipt use;
- final M2.4.2 whole-set revalidation happens before receipt use;
- a mixed CREATE/REPLACE patch uses one transaction and one commit point;
- successful commit changes every target and leaves lifecycle `EXECUTED`;
- a second-step staging/publish failure rolls back already-scheduled first-step
  changes and does not attempt later steps;
- CREATE target appearance after consumption cannot clobber the foreign file and
  rolls back the patch;
- REPLACE metadata drift before publish rolls back the patch;
- commit failure plus successful rollback reports `ROLLED_BACK`;
- rollback failure reports `INDETERMINATE` and blocks further execution;
- transaction-handle cleanup failure is retained and retried fail-closed;
- non-Windows application fails before authority consumption;
- M2.4.3 never emits M2.4.4 mutation observations or advances to `OBSERVED`.

## Non-goals

M2.4.3 does not:

- emit final per-file mutation observations/receipts;
- claim final postimage/hash/mode evidence;
- define durable crash/restart reconciliation;
- authorize model-produced patches;
- add delete/rename/chmod operations;
- add Git/GitHub mutation authority;
- enable Linux workspace mutation.

Those remain M2.4.4–M2.6 work.
