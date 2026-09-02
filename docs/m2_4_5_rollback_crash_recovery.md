# M2.4.5 — rollback, crash and recovery semantics

M2.4.5 adds bounded recovery semantics around the accepted M2.4.3 single-TxF
multi-file commit model and the M2.4.4 terminal evidence layer.

It does **not** weaken or replace M2.4.3. Normal application still uses one
Windows TxF transaction and one authority consumption. M2.4.5 adds a separate,
opt-in recovery-aware executor and a recovery manager for outcomes that cannot be
safely completed by the ordinary M2.4.3/M2.4.4 path.

## Accepted lower-layer boundary

M2.4.3 already guarantees:

- one patch-level authorization receipt consumed once;
- exact M2.4.2 plan binding and pre-authority revalidation;
- one shared Windows TxF transaction for the whole patch;
- no best-effort partial commit;
- one `CommitTransaction()` point;
- successful rollback yields `ROLLED_BACK`;
- rollback failure yields `INDETERMINATE` and retains the unfinished TxF handle;
- a retained unfinished transaction blocks a new patch before new authority use.

M2.4.4 already guarantees terminal changed-file/set evidence for only
`COMMITTED` or `ROLLED_BACK`. It deliberately refuses `INDETERMINATE`.

M2.4.5 fills that gap.

## Windows/KTM assumptions used by this layer

The recovery contract relies only on documented KTM behavior:

- closing the last transaction handle before a successful commit causes KTM to
  roll the transaction back;
- failure of a process participating in a transaction causes the transaction to
  abort;
- `RollbackTransaction()` is synchronous;
- `GetTransactionInformation()` reports a `TRANSACTION_OUTCOME` of
  undetermined, committed, or aborted.

These facts permit a strong pre-commit crash classification, but they do **not**
make a crash after a commit request safely inferable from current file contents.

## Recovery-aware execution journal

`RecoverablePatchApplicationExecutor` subclasses the accepted
`PatchApplicationExecutor`; it does not duplicate the M2.4.3 transaction engine.

It places durable journal barriers around the existing mutation hooks:

```text
AUTHORIZATION CONSUMED
EXECUTED
    ↓
EXECUTION_STARTED       durable fsync before staging/publish
    ↓
existing M2.4.3 _stage_and_publish()
    ↓
COMMIT_INTENT           durable fsync after all transactional publishes,
                        immediately before the inherited commit point
    ↓
existing M2.4.3 CommitTransaction()
    ↓
COMMITTED / ROLLED_BACK
    ↓
TERMINAL                durable exact PatchApplicationResult projection
```

The journal is not authority and does not contain postimage bytes or secrets. It
contains only execution identity, plan/change-set digests, authority lineage,
phase markers, and (for a terminal marker) the exact application-result
projection.

## Journal integrity and storage boundary

`PatchRecoveryJournal` is intentionally small and fail-closed:

- absolute canonical path only;
- journal must live outside the patch workspace;
- parent state directory must already exist;
- one journal path is for one patch execution;
- a fresh execution requires that the journal path does not already exist;
- the first record is created with exclusive `O_CREAT | O_EXCL` semantics;
- records are append-only canonical JSON lines;
- every record contains a sequence number and previous-record digest;
- records for one execution must all have the same process id;
- every append is flushed with `os.fsync()`;
- complete-record corruption is rejected;
- only an unfinished final non-newline fragment may be treated as a torn crash
  append;
- execution never appends on top of a torn tail;
- the read budget is checked before allocating journal contents;
- maximum file size is 4 MiB;
- maximum complete records is 4096;
- maximum encoded record line is 64 KiB.

The journal is runtime-owned recovery state. It is deliberately outside the
workspace so journal bookkeeping is not an implicit `WRITE_WORKSPACE` mutation.

### Journal namespace pinning

Location validation and Win32 share modes are not treated as sufficient by
themselves. Windows can authorize a rename through delete-child rights in a
parent directory, so an ordinary open directory handle is not used as proof that
the namespace is physically immovable.

For each journal I/O operation, M2.4.5 opens and verifies the complete
non-reparse directory-handle chain and then creates a unique **transacted,
never-committed namespace marker** in the journal parent. The marker is written
and flushed inside a short TxF transaction. Windows TxF pins every directory
component of a file modified by that transaction against rename until the
transaction ends. The marker handle's final resolved path and the directory
chain are revalidated before any non-transacted journal I/O is admitted.

If a parent/ancestor was moved or redirected before transactional admission,
marker creation either fails or resolves to a path that does not match the
expected external state location; the marker transaction is rolled back before
any journal mutation occurs. After admission, TxF supplies the actual rename
pin while ordinary directory handles continue to reject reparse substitution
and prove exact path identity. The namespace-marker transaction is always
rolled back and never carries journal data.

The journal entry itself is opened with `FILE_FLAG_OPEN_REPARSE_POINT`, rejects
reparse-point entries, and omits delete sharing. Existing-journal append keeps
one read/write handle open across hash-chain validation and the durable append.
First-record creation remains no-clobber `CREATE_NEW`.

On POSIX, journal access uses a pinned parent `dir_fd` plus `O_NOFOLLOW`; POSIX
mutation/recovery remains outside the supported M2.4.5 execution boundary and no
Linux/POSIX PASS is claimed.

## Why `EXECUTION_STARTED` and `COMMIT_INTENT` are distinct

A process that is confirmed dead after a durable `EXECUTION_STARTED`, with no
durable `COMMIT_INTENT`, could not have reached the inherited M2.4.3 commit call.
The participating process is gone and the last transaction handle is no longer
held by that process. Under the accepted KTM boundary, recovery may classify the
transaction as presumed aborted/rolled back.

This still produces exact filesystem evidence. A later external write can cause
that evidence to be `MISMATCH`; it does not rewrite the historical transaction
classification.

A durable `COMMIT_INTENT` is different:

```text
COMMIT_INTENT durable
    ↓
CommitTransaction called
    ↓
process dies before CMA persists TERMINAL
```

The transaction may have committed or aborted. Current filesystem contents are
not historical proof because another actor may have changed the files later.
M2.4.5 therefore does not infer a terminal result from the workspace.

## Ambiguous commit-attempt crash assessment

If restart recovery sees `COMMIT_INTENT` without a durable terminal marker, it
returns `PatchCrashRecoveryAssessment`, not `PatchRecoveryReceipt`.

The assessment binds:

- exact proposal + consumed authorization lineage;
- execution id;
- exact plan and change-set digest;
- last journal phase and record digest;
- torn-tail status;
- exact per-file preimage/postimage expectations;
- exact current file observations or explicit inspection failures;
- one classification:
  - `PREIMAGE_SET`;
  - `POSTIMAGE_SET`;
  - `MIXED`;
  - `INCOMPLETE`.

Even `POSTIMAGE_SET` does **not** become a commit claim.

The lifecycle remains:

```text
EXECUTED
observation_id = None
```

This ambiguity is intentionally preserved until a stronger future source of
historical outcome evidence exists.

## Live retained TxF recovery

For the in-process M2.4.3 rollback-failure path, the original transaction handle
is still retained by the executor.

`PatchRecoveryManager.recover_live()` requires:

- exact EXECUTED lifecycle;
- exact consumed authorization receipt;
- exact plan;
- exact original `PatchApplicationResult` with `INDETERMINATE` state;
- exactly one unfinished retained transaction.

Recovery proceeds:

```text
GetTransactionInformation()
    ↓
COMMITTED  → terminal committed
ABORTED    → terminal rolled back
UNDETERMINED
    ↓
RollbackTransaction()       synchronous retry
    ↓
success                    → terminal ABORTED
failure                    → recovery still blocked
```

A terminal KTM outcome marks the retained transaction object as finished before
handle cleanup. If `CloseHandle` fails, the terminal outcome remains known and
the handle stays retained for the existing M2.4.3 cleanup gate to retry later.

M2.4.5 never treats a failed close as a return to transaction ambiguity.

## Restart recovery outcomes

`PatchRecoveryManager.recover_restart()` requires explicit confirmation that the
original execution process has terminated. Merely running recovery from another
runtime instance is insufficient.

### Durable TERMINAL record

A complete terminal journal record contains the exact M2.4.3 result. Recovery
reconstructs and validates that projection, re-observes all affected files, emits
`PatchRecoveryReceipt(source=JOURNAL_TERMINAL)`, and advances the lifecycle to
`OBSERVED`.

### EXECUTION_STARTED without COMMIT_INTENT

After confirmed original-process death, the transaction is classified as
presumed aborted. Recovery observes exact preimages, emits
`PatchRecoveryReceipt(source=PRESUMED_ABORT)`, and advances to `OBSERVED`.

A current-file mismatch is recorded as `MISMATCH`, not hidden.

### COMMIT_INTENT without TERMINAL

No terminal receipt is issued. Only a crash assessment is returned and the
lifecycle stays `EXECUTED`.

## Recovery receipt

`PatchRecoveryReceipt` is separate from both `PatchApplicationResult` and the
M2.4.4 `PatchMutationReceipt`.

It binds:

- proposal id/digest;
- exact consumed authorization receipt id/digest;
- execution id;
- plan digest and change-set digest;
- recovery source;
- recovered `COMMITTED` or `ROLLED_BACK` outcome;
- source-specific evidence:
  - original indeterminate application result + digest for `LIVE_KTM`;
  - terminal journal result + digest for `JOURNAL_TERMINAL`;
  - durable `EXECUTION_STARTED` journal phase + process-termination confirmation
    for `PRESUMED_ABORT`;
- KTM outcome before/after and rollback-retry flag for live recovery;
- transaction cleanup error when a live terminal handle cannot yet close;
- exact per-file recovery observations;
- aggregate `VERIFIED`, `MISMATCH`, or `INCOMPLETE` evidence;
- receipt digest.

The receipt validator rebinds every nested observation to the exact plan step,
including `change_digest`, `primitive_digest`, operation, target, exact preimage,
and exact postimage expectation.

## Lifecycle and authority rules

M2.4.5 does not create or consume new authority.

The recovery implementation is deliberately split into transport-bounded internal
modules while preserving one public `patch_recovery` facade:

- `_patch_recovery_common.py` — shared enums, identity/binding helpers and terminal expectations;
- `_patch_recovery_parent.py` — platform-neutral journal parent admission and pinned-parent lifecycle;
- `_patch_recovery_windows_namespace.py` — Windows TxF namespace marker, resolved-path proof and secure journal handles;
- `_patch_recovery_journal.py` — durable hash-chained journal;
- `_patch_recovery_observation.py` — exact per-file recovery observations;
- `_patch_recovery_receipt.py` — terminal recovery receipt and binding validation;
- `_patch_recovery_assessment.py` — non-terminal crash assessment;
- `_patch_recovery_runtime.py` — KTM query, recovery-aware executor and recovery manager;
- `patch_recovery.py` — stable public facade only.

The original transport split preserved the pre-split frozen recovery algorithms.
The later journal-namespace security repair intentionally changes only the journal
admission/I/O boundary and its regression coverage; recovery receipt, assessment,
authority, lifecycle, and retained-TxF outcome semantics remain unchanged.

Across the production recovery layer:

- zero `consume_authorization()` calls;
- zero `record_executed()` calls;
- one `record_observed()` call site;
- zero transaction `commit()` calls in the recovery layer;
- one namespace-admission TxF `rollback()` call site;
- one intentional live-recovery `rollback()` retry call site.

A recovery receipt is evidence, never permission.

`record_observed()` is called only after the complete recovery receipt is
constructed and rebound to the exact lifecycle and plan.

An ambiguous crash assessment never calls `record_observed()`.

## Important durability boundary left for M3

The accepted `ActionLifecycle` is currently in-memory. The first durable M2.4.5
marker is written by the earliest mutation hook, immediately before transactional
staging/publish.

Therefore a very small window still exists between:

```text
lifecycle.record_executed()
    ↓
process death before EXECUTION_STARTED fsync
```

With no durable execution journal record, M2.4.5 refuses to invent history.
Durable lifecycle reconstruction across that window belongs to the later
persistent runtime (M3).

Likewise M2.4.5 does not turn the live in-memory retained-transaction object into
a restart-persistent transaction handle. Restart recovery is journal-based.

## Focused validation gates

The M2.4.5 focused suite is split across journal, evidence, and runtime test modules and covers:

- journal path must be absolute;
- journal must be outside workspace;
- existing journal blocks a new patch before authority use;
- bounded read rejects oversize journal before parsing;
- hash-chain tampering is rejected;
- torn final append is reported without accepting corrupt complete records;
- a second process cannot append to an existing execution journal;
- journal cannot begin at `COMMIT_INTENT`;
- Windows journal parent rename is blocked while the namespace is pinned;
- Windows ancestor rename is blocked by full-chain pinning;
- Windows journal-parent redirection into the patch workspace is blocked;
- first-record secure open occurs while the parent chain is still pinned;
- existing append holds one journal handle across validation and append, blocking entry replacement;
- source-specific recovery receipt fields are enforced;
- nested recovery file evidence is rebound to exact plan steps;
- nested crash-assessment evidence is rebound to exact plan steps;
- restart recovery requires explicit original-process termination confirmation;
- preimage-looking `COMMIT_INTENT` crash remains an assessment;
- postimage-looking `COMMIT_INTENT` crash also remains an assessment;
- `EXECUTION_STARTED` restart yields presumed-abort recovery;
- durable terminal restart reconstructs exact terminal result;
- real Windows recovery-aware commit writes all three durable markers;
- real Windows `COMMIT_INTENT` journal failure rolls back before commit;
- real Windows terminal-journal failure preserves the known committed result;
- real Windows retained TxF rollback failure is resolved through KTM query +
  synchronous rollback retry.

## Non-goals

M2.4.5 does not:

- replace the M2.4.3 transaction engine;
- add a second commit path;
- infer commit from current postimage bytes;
- silently resolve ambiguous `COMMIT_INTENT` crashes;
- persist the full action lifecycle across restart;
- make TxF available on unsupported filesystems/platforms;
- add Linux mutation support;
- add model write authority;
- add delete/rename/chmod;
- add Git/GitHub mutation governance.

M2.4.6 remains responsible for governed model patch requests. Persistent runtime
state and complete restart lifecycle reconstruction remain M3 responsibilities.
