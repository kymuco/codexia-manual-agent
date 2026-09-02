# M2.4.2 — Complete Preimage/Namespace Revalidation, Deterministic Multi-File Execution Planning and Pre-Authority Drift Governance

M2.4.2 adds the read-only execution-preparation boundary between the accepted
M2.4.1 exact patch proposal and the future M2.4.3 multi-file application layer.
It does **not** apply files and does **not** consume authorization.

## Contract

A patch can advance toward execution only through:

```text
accepted M2.4.1 ActionProposal
        ↓
build proposal-bound deterministic PatchExecutionPlan
        ↓ exact parent-plan binding required
whole-set execution-support preflight
        ↓ every primitive supported on this host
final fresh live whole-set revalidation
        ↓ exact preimage/namespace match required
future M2.4.3 authority-consumption/application boundary
```

The live gate reconstructs the current change-set observation using the same
hardened M2.4.1 proposal-preparation path and the exact already-bound postimages.
That deliberately reuses the accepted workspace/root/parent namespace guards,
case-semantics handling, exact preimage capture, target normalization and content
bounds rather than creating a second, weaker filesystem inspection path.

The freshly observed `change_set_digest` must equal the authorized proposal's
bound `change_set_digest`. A mismatch is fail-closed before any authority can be
consumed.

### Drift classification

- workspace/parent/target namespace boundary failures remain
  `WorkspaceMutationBoundaryError`;
- CREATE target appearance, REPLACE target disappearance, content/size/hash/mode
  drift, or another live preimage mismatch becomes
  `WorkspaceMutationPreimageChangedError`;
- malformed/tampered proposals are rejected by the existing M2.4.1 parser before
  live planning begins.

Transient historical changes that return to the exact authorized current
preimage are not separately observable history and are not represented as drift;
the authority contract remains bound to the current exact preimage plus the
accepted namespace boundary.

## Deterministic execution plan

CMA first builds `PatchExecutionPlan` schema v1 deterministically from the accepted proposal. Building the plan does not claim that the live workspace is still fresh; freshness is checked separately at the final pre-authority gate.

The plan is bound to:

- exact parent `proposal_id`;
- exact parent `proposal_digest`;
- canonical `workspace_root`;
- exact `change_set_digest`;
- backend `m2.3.workspace_create_replace.v1`;
- execution platform `windows`;
- one ordered deterministic step for every patch file.

Each `PatchExecutionStep` carries:

- zero-based ordered index;
- exact `create` or `replace` operation;
- accepted M2.3 action (`workspace.create_file.v1` or
  `workspace.replace_file.v1`);
- canonical target;
- exact expected preimage;
- exact postimage bytes/size/SHA-256;
- M2.4.1 per-file `change_digest`;
- `primitive_digest` over the exact M2.3 capability/action/workspace/parameter
  payload.

`plan_digest` binds the complete plan including the parent proposal identity.
Replanning the same unchanged proposal is deterministic. A distinct
`ActionProposal` with an identical `change_set_digest` receives a distinct plan
identity because patch authority is proposal-bound rather than transferable by
content equivalence alone.

The step serializer is intentionally the exact M2.3 primitive parameter schema.
M2.4.2 therefore specifies the ordered application target without introducing a
new create/replace filesystem primitive.

## Whole-set execution-support preflight

M2.4.2 also exposes `validate_patch_execution_plan_binding()` and
`preflight_patch_execution_plan()` so the future patch executor can first prove
that the plan belongs to the exact parent proposal and then prove that **every**
planned primitive is supported before burning a patch-level receipt.

The preflight is read-only and reuses the accepted M2.3 platform gates:

- fail closed when the execution host is not Windows;
- validate the Windows relative-target spelling for every step;
- require the accepted local-writable-NTFS/TxF strict-replace support for every
  REPLACE target;
- do not consume authority or stage/write any file.

The intended future M2.4.3 pre-consumption sequence is therefore:

```text
build_patch_execution_plan(parent proposal)
        ↓ deterministic M2.3 mapping; no freshness claim
validate_patch_execution_plan_binding(parent proposal, plan)
        ↓ exact parent-plan identity
preflight_patch_execution_plan(parent proposal, plan)
        ↓ every primitive executable on this host
revalidate_patch_execution_plan(parent proposal, plan)
        ↓ final complete live preimage/namespace match
consume patch-level authority
        ↓
M2.4.3 commit/failure semantics
```

A capability failure on file N therefore cannot consume patch-level authority
after files 1..N-1 merely happened to be supported.

## Freshness rule

A `PatchExecutionPlan` is **not** an authority token and is **not** a freshness
receipt. It is deterministic proposal-bound execution data only.

The future M2.4.3 executor must run `revalidate_patch_execution_plan()` as the final M2.4.2 live gate immediately before any patch-level authorization consumption. It must also preserve the accepted M2.3
per-primitive pre-consumption/commit checks while applying the plan.

This distinction is intentional:

```text
M2.4.2 proves:
  the complete set matched when the pre-authority gate ran
  + the exact deterministic sequence is known

M2.4.2 does not prove:
  atomic multi-file commit
  or that no target can race after the gate
```

The handling of a race/failure after patch-level authority is consumed belongs to
M2.4.3's explicit multi-file commit/failure semantics. M2.4.2 must not pre-claim
all-or-fail semantics that the platform has not yet established.

## Authority invariant

`build_patch_execution_plan()`, `preflight_patch_execution_plan()` and
`revalidate_patch_execution_plan()` operate on proposal/plan data, not an
authority receipt or executable lifecycle. It performs read-only inspection only.

Therefore M2.4.2 itself cannot:

- consume a receipt;
- move `ActionLifecycle` out of `AUTHORIZED`;
- write a target;
- create staging state;
- call TxF commit;
- emit a mutation-success observation.

An already-authorized lifecycle can be revalidated through its proposal while its
receipt remains unconsumed.

## Platform boundary

Planning/revalidation is read-only and may be exercised on supported inspection
hosts, but the plan explicitly names `execution_platform="windows"` because the
accepted M2.3 mutation execution boundary remains Windows-only:

- CREATE uses the accepted Windows no-clobber primitive;
- REPLACE uses the accepted metadata-preserving TxF strict-replace primitive;
- Linux patch mutation remains disabled while M2.3a is unresolved.

M2.4.2 therefore does not reopen or weaken the deferred Linux execution boundary.

## Focused gates

M2.4.2 coverage must prove at least:

- unchanged multi-file patch produces a deterministic sorted plan;
- every plan step serializes into the accepted M2.3 primitive schema;
- plan generation does not mutate the workspace;
- a valid plan cannot be transferred to a distinct ActionProposal with the same
  change-set bytes;
- repeated planning of the same proposal has stable `plan_digest`;
- a distinct proposal identity cannot inherit the first plan identity;
- REPLACE content drift fails closed;
- CREATE target appearance fails closed;
- REPLACE disappearance fails closed;
- permission-mode drift is detected where mode semantics are observable;
- revalidation of an authorized proposal does not consume its receipt or advance
  its lifecycle;
- execution-support preflight fails closed outside Windows;
- Windows preflight validates every target and requires TxF support for every
  REPLACE before future patch-level authority consumption;
- existing M2.4.1 preview/parser behavior and M2.3 execution boundaries remain
  unchanged.

## Non-goals

M2.4.2 does not:

- execute one or more patch steps;
- define multi-file commit points;
- define partial-failure or rollback semantics;
- emit changed-file mutation observations/receipts;
- add a model write tool;
- add Git/GitHub authority;
- add bounded delegation;
- enable Linux workspace mutation.

Those remain M2.4.3–M2.6 work according to the synchronized roadmap.
