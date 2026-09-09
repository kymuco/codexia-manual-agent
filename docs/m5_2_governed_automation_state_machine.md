# M5.2 — Governed Automation State Machine

## Purpose

M5.1 freezes the exact target, workspace, budget, and mandatory stop policy before automated work begins. M5.2 is the first component that is allowed to advance that frozen plan.

It does not introduce another executor, scheduler, approval authority, or evidence path. It coordinates the already-proven M4.3 governed run path one bounded durable stage at a time.

Primary invariant:

```text
automation progress != authorization authority
```

Recovery invariant:

```text
recovered progress != permission to replay a side effect
```

Budget invariant:

```text
budget consumption is derived from irreversible durable facts
!=
a mutable counter that can be rolled back after restart
```

## Exact run-set derivation

`GovernedAutomationStateMachine` accepts only the stable `automation_id` of an already-frozen M5.1 plan.

The state machine re-recovers:

- the exact `FrozenAutomationPlan`;
- its exact frozen M4.4 comparison policy;
- baseline and candidate experiment ids/manifest digests;
- the policy-declared ordered seed set;
- the exact canonical frozen workspace.

For v1, slot order is deterministic:

```text
all baseline ordinals in policy seed order
→ all candidate ordinals in policy seed order
```

Each `(arm, ordinal, seed)` receives deterministic UUIDv5 identities for:

- the M4 `ExperimentRun`;
- the M3 session used for that run.

A foreign run occupying a declared ordinal/seed slot is not adopted. Recovery fails integrity instead.

## Derived state instead of a second automation ledger

M5.2 deliberately does not create a mutable `steps_used` table.

For each declared slot, progress is reconstructed from existing authoritative M3/M4 state:

```text
stage 0: no exact run exists
stage 1: exact M4 run is durable
stage 2: exact M3 proposal + M4.3 execution binding are durable
stage 3: authorization/execution chronology has advanced to terminal observation
```

The public `steps_used` value is the sum of these durable stages.

Likewise:

- `runs_started` is the number of slots at stage >= 1;
- `runs_completed` is the number of stage-3 runs with verified physical evidence and an irreversible M4 run seal.

This means a restart cannot regain budget merely by losing an M5-specific mutable counter. If durable work exceeds the previously frozen budget, recovery fails integrity.

## One-step orchestration surface

`advance(automation_id)` performs at most one non-authority progression for the current deterministic slot.

The normal path is:

```text
READY
→ register exact ExperimentRun
→ RUN_REGISTERED
→ create/recover deterministic M3 session
→ prepare exact existing M4.3 Python proposal/binding
→ PAUSED_AUTHORIZATION_REQUIRED
```

At `PAUSED_AUTHORIZATION_REQUIRED`, repeated `advance()` calls are inert. No process runs and no authorization receipt is created.

The returned `AutomationState.proposal` is the exact durable M2 proposal that an external authority may inspect and decide upon.

## External authorization boundary

M5.2 has no API that takes `approved=True`, an actor name, a capability grant, or an approval policy override.

Execution continuation requires:

```python
continue_authorized(automation_id, receipt=external_receipt)
```

The supplied receipt must satisfy the existing M2/M3/M4.3 authority chain for the exact pending proposal. M5.2 delegates verification and one-shot consumption to the already-proven authority/runtime path.

The resulting execution therefore remains:

```text
exact M4 run
→ exact M4.3 binding
→ exact externally supplied M2 receipt
→ durable M3 consumption/execution/observation
→ M4.3 execution evidence
→ verified physical evidence
→ irreversible run seal
```

M5.2 never constructs an `AuthorizationReceipt` internally.

## Budget stops

The frozen M5.1 budget is applied before the next irreversible stage.

Examples:

```text
max_runs exhausted before a fresh slot
→ STOPPED_BUDGET
→ next run is not registered
```

```text
max_steps exhausted after proposal preparation
→ STOPPED_BUDGET
→ even a subsequently supplied receipt cannot be consumed by M5.2
```

Budget exhaustion is therefore a real admission boundary, not merely a status label after work has already happened.

## Error and replay boundary

M5.2 distinguishes safe recovery from replay.

A normal restart before authorization re-recovers the exact same durable proposal and returns `PAUSED_AUTHORIZATION_REQUIRED` without executing anything.

If recovery instead finds an authority chronology that advanced beyond the exact durable M4.3 state in a way that cannot prove one safe continuation, M5.2 stops:

```text
unknown/partial post-authority state
→ STOPPED_ERROR
→ no automatic process replay
```

Likewise, a durable M3 proposal that was never bound into M4.3 is not silently replaced by a fresh approval target. The current v1 behavior is fail-closed rather than minting another proposal identity after a partial preparation crash.

This intentionally preserves the M3 rule that recovery reconstructs state but does not infer permission to repeat a side effect.

## Run-set completion and M5.3 handoff

M5.2 terminates its responsibility at:

```text
RUN_SET_COMPLETE
```

This means every frozen M4.4 run slot has:

- the exact deterministic run identity;
- governed M4.3 execution provenance;
- verified physical evidence;
- an irreversible M4 run seal.

M5.2 does **not** seal the two experiments, evaluate the comparison, or publish a conclusion. Those are the closure operations for M5.3.

To make that handoff crash-safe, `RUN_SET_COMPLETE` remains recoverable if downstream M5.3 has already sealed one arm and not yet the other. M5.2 validates that any sealed arm contains the complete exact automated run set, but it does not require the two experiment-seal mutations to appear atomically.

## Adversarial regressions

`tests/test_lab_m5_2_governed_automation.py` covers:

- initial `READY` recovery from only a frozen M5.1 plan;
- deterministic run registration;
- pause before any process execution;
- fresh-process recovery of the byte-for-byte same pending proposal;
- repeated `advance()` while paused does not execute;
- externally supplied HUMAN receipt drives the existing governed execution path;
- complete baseline → candidate run-set progression;
- fresh recovery after completion does not rewrite output files or rerun processes;
- exact step-budget stop at the prepared authorization boundary;
- exact run-budget stop before another run registration;
- foreign run/seed identity rejection;
- partial unbound-proposal crash fails closed without minting a replacement approval target;
- complete run-set recovery remains stable while M5.3 sequentially seals the two experiment arms.

## Explicit non-claims

M5.2 does **not**:

- approve its own actions;
- generate HUMAN receipts;
- broaden M2 capabilities;
- introduce another process executor;
- choose hypotheses, manifests, metrics, seeds, thresholds, or comparison direction;
- alter the frozen M5.1 budget or stop policy;
- execute beyond the exact frozen comparison run set;
- schedule background or periodic work;
- automatically retry an unknown side effect after crash;
- seal comparison experiments as part of its terminal state;
- evaluate an M4.4 comparison;
- publish an M4.5 conclusion;
- grant Git, network, filesystem, delegation, or external-write authority.

## Exit gate

M5.2 is complete when the exact reviewed candidate proves:

> Starting only from one frozen M5.1 automation identity, Codexia can deterministically advance the exact predeclared M4 run set under monotonic derived budgets, pause on the exact external authorization boundary, resume only with an externally supplied receipt, recover the same state after restart without process replay, and finish with the complete verified sealed run set ready for M5.3 without creating a second executor or authority system.
