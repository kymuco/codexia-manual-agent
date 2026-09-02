# M2.4.4 — Exact changed-file mutation observations and receipts

M2.4.4 turns a terminal M2.4.3 `PatchApplicationResult` into digest-bound
filesystem evidence. It performs **read-only observation only**. It does not
consume authority, stage files, publish namespace changes, commit/rollback a TxF
transaction, or define crash recovery.

## Contract

```text
M2.4.3 lifecycle = EXECUTED
        + exact PatchExecutionPlan
        + exact PatchApplicationResult
        ↓ binding / integrity validation
COMMITTED or ROLLED_BACK required
        ↓
read exact terminal state for every ordered plan step
        ↓
PatchFileMutationObservation × N
        ↓ ordered observation digests
PatchMutationReceipt
        ↓ exact receipt self/binding validation
lifecycle.record_observed(receipt_id)
        ↓
OBSERVED
```

`INDETERMINATE` is intentionally excluded. M2.4.4 must not convert an unresolved
transaction into either committed or rolled-back evidence. Such a lifecycle stays
`EXECUTED` until M2.4.5 recovery/reconciliation establishes a terminal outcome.

## Binding

Every per-file observation binds:

- proposal id + digest;
- exact originally consumed authorization receipt id + digest;
- M2.4.3 execution id;
- execution-plan digest;
- change-set digest;
- step index;
- per-file `change_digest`;
- exact M2.3 `primitive_digest`;
- operation and canonical target;
- terminal expectation (`postimage` or `preimage`);
- expected terminal state;
- exact observed terminal state when inspection succeeds;
- observation status and error evidence;
- its own observation digest.

The set-level `PatchMutationReceipt` additionally binds:

- the exact projected M2.4.3 application result and its digest;
- commit model + terminal commit state;
- application failure step/target/stage/error/cleanup evidence;
- ordered file-observation digests;
- aggregate verification outcome;
- its own receipt digest.

`validate_patch_mutation_receipt_binding()` binds the receipt back to the exact
proposal, deterministic M2.4.2 execution plan, M2.4.3 result, and every plan step.

## Terminal expectations

For `COMMITTED`:

- every CREATE/REPLACE must be present;
- size and SHA-256 must match the exact plan-bound postimage;
- REPLACE also binds the final permission mode to the original preimage mode;
- CREATE mode is recorded in the observation but is not a verification criterion,
  because M2.4.1 does not define an authority-bearing create-mode contract.

For `ROLLED_BACK`:

- every target must match its exact original preimage;
- CREATE therefore expects absence;
- REPLACE expects original state/size/SHA-256/mode.

The expected bytes themselves remain bound by the proposal/plan. The observation
records terminal state/size/SHA-256/mode rather than duplicating all file bytes in
the receipt.

## Verification outcomes

Per-file status:

- `VERIFIED` — the exact terminal expectation matched;
- `MISMATCH` — inspection succeeded but terminal state differs;
- `INSPECTION_FAILED` — an exact terminal state could not be established.

Set-level outcome:

- `VERIFIED` — every file observation is verified;
- `MISMATCH` — at least one exact mismatch exists and no inspection is incomplete;
- `INCOMPLETE` — at least one file could not be inspected exactly.

`OBSERVED` means authoritative evidence was recorded; it does **not** mean the
mutation necessarily succeeded or that the current filesystem still matches the
receipt later. A mismatch/incomplete receipt therefore still advances
`EXECUTED → OBSERVED` while preserving its non-success classification.

## Observation boundary

The supported M2.4.3 mutation backend is Windows-only, so M2.4.4 filesystem
observation is likewise fail-closed outside the accepted Windows boundary.

Each file is inspected through the accepted M2.3 parent-anchored target boundary,
with parent identity checked before and after bounded content hashing. Observation
is read-only.

The set-level receipt is an ordered aggregation of exact per-file point-in-time
observations. It is **not an atomic or durable filesystem snapshot**. In
particular, a target may be modified after its observation and before or after the
receipt is returned. M2.4.4 does not claim to detect transient changes that return
to the same exact observed state.

This is intentionally distinct from M2.4.3's atomic transaction commit. M2.4.4
proves what was exactly observed after that terminal transaction result; it does
not freeze the workspace after observation.

## Authority and lifecycle invariants

M2.4.4:

- accepts no `LocalApprovalAuthority` argument;
- calls no `consume_authorization()`;
- calls no `record_executed()`;
- performs no filesystem write/stage/publish/commit/rollback operation;
- calls `record_observed()` exactly once, after the complete mutation receipt has
  been created and validated;
- uses the set-level mutation receipt id as the lifecycle `observation_id`.

The original authorization receipt remains lineage evidence only. A mutation
receipt is evidence of execution/observation, not new authority.

## Failure evidence

A `ROLLED_BACK` application receipt preserves the M2.4.3 failed step, target,
stage, error and cleanup error while separately verifying the terminal preimages.

A `COMMITTED` result followed by external drift is not rewritten as a failed
transaction. The receipt remains bound to `COMMITTED` but reports terminal
`MISMATCH` evidence.

An inspection failure is not replaced by the expected state. The corresponding
file observation stores `observed_terminal = null`, `INSPECTION_FAILED`, and the
exact inspection error string.

## Focused gates

M2.4.4 coverage should prove at least:

- non-Windows observation fails before lifecycle observation;
- `INDETERMINATE` cannot become terminal evidence;
- application result must match the exact lifecycle execution and plan;
- replacing the consumed authorization receipt with another valid allow-receipt is rejected;
- a committed mixed CREATE/REPLACE patch yields exact verified per-file evidence
  and one verified set receipt;
- a rolled-back multi-file patch verifies original preimages and preserves exact
  M2.4.3 failure evidence;
- post-commit external drift yields `MISMATCH`, never success;
- post-rollback external drift yields `MISMATCH`;
- terminal inspection or live namespace reparse failure yields `INCOMPLETE` with no
  fabricated observed snapshot;
- nested file-observation tampering fails receipt/plan binding validation;
- observation never consumes authority a second time;
- the set receipt id becomes the lifecycle observation id;
- a second observation attempt is rejected by the lifecycle state machine.

## Non-goals

M2.4.4 does not:

- change M2.4.3 transaction application semantics;
- recover an indeterminate transaction;
- persist evidence across process restart;
- provide an atomic post-commit snapshot;
- add model write authority;
- add delete/rename/chmod;
- add Git/GitHub authority;
- enable Linux workspace mutation.

M2.4.5 owns rollback/crash/recovery reconciliation. M2.4.6 owns governed model
patch requests.
