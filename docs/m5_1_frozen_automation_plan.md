# M5.1 — Frozen Automation Plan

## Purpose

M5 begins only after the manual M4 Computational Lab loop is proven. The first automation step therefore does not invent a scheduler, a second executor, or a new authority path. It freezes the exact scope within which later orchestration may operate.

Primary invariant:

```text
automation intent != execution authority
```

Temporal boundary:

```text
frozen automation budget/stop policy before automated work
!=
budget/stop policy chosen after work has started
```

## Why M5.1 comes first

A bounded loop is not bounded merely because code contains counters. If the target, run budget, or stop rules can be changed after the loop has begun, the automation can effectively enlarge its own search space after seeing intermediate evidence.

M5.1 therefore freezes one automation plan against one already-frozen M4.4 comparison policy before either comparison arm advances beyond the policy-freeze point.

## Exact plan binding

`AutomationPlan` binds:

- deterministic `automation_id` derived from the stable M4.4 `policy_id`;
- exact policy id/digest and comparison freeze digest;
- exact baseline/candidate experiment ids and manifest digests;
- exact number of policy-declared run slots (`2 * len(seeds)`);
- an explicit `AutomationBudget`;
- an explicit strict-v1 `AutomationStopPolicy`;
- one canonical plan digest.

The v1 budget contains:

```text
max_steps
max_runs
```

`max_runs` may be smaller than the complete comparison set so a future coordinator can demonstrate deterministic budget exhaustion, but it may never exceed the exact frozen comparison run set.

## Strict v1 stop policy

M5.1 intentionally does not expose permissive stop-policy choices. All of the following are mandatory:

```text
pause_on_authorization_required = true
stop_on_error                  = true
stop_on_budget_exhaustion      = true
stop_on_conclusion             = true
```

Changing any of these requires a future schema/version change rather than a runtime toggle.

Most importantly:

```text
automation reaches M2 authorization boundary
→ automation pauses
→ external authority is still required
```

The plan has no field for an approval decision, authorization receipt, capability grant, process proposal, command, workspace mutation, Git operation, or network operation.

## One policy, one v1 automation identity

M4.4 prevents comparison-policy shopping. M5.1 applies the same principle to automation budgets:

```text
one exact frozen policy
→ one deterministic automation_id
→ one frozen v1 plan
```

After one plan is registered for a policy, a second plan with a larger or smaller budget is an identity conflict rather than an alternate plan that can be selected later.

## Admission boundary

M5.1 v1 automates only the execution surface already proven in M4.3. Both comparison arms must decode as the admitted `python-inline-json-result.v1` profile through `PythonJsonExperimentSpec`.

This deliberately rejects a paper automation plan for a manifest the current governed runtime cannot execute.

## Durable freeze

`SqliteAutomationPlanRegistry.register_plan(plan)` uses the same filesystem-backed SQLite trust domain as the M4 registries.

Registration acquires a SQLite `BEGIN IMMEDIATE` writer transaction before validating that neither arm has advanced beyond the M4.4 policy-freeze anchor. M4.2 run registration uses the same SQLite writer domain. Therefore a concurrent plan-freeze/run-registration race serializes into one of two orders:

```text
plan freeze wins
→ plan is frozen
→ run may register later
```

or:

```text
run registration wins
→ M5.1 sees advanced chronology
→ plan freeze is rejected
```

No wall-clock comparison is used as the causal gate.

The frozen record retains the exact baseline/candidate event anchors observed at registration. Recovery validates canonical JSON, indexes, digests, exact M4.4 policy lineage, exact manifest profiles, and those durable event anchors. Later valid runs do not invalidate the already-frozen plan.

## Adversarial regressions

`tests/test_lab_m5_1_frozen_automation_plan.py` covers:

- successful pre-run plan freeze and recovery after a later run is registered;
- late freeze rejection after the first run;
- budget-shopping rejection for the same policy identity;
- rejection when `max_runs` exceeds the frozen comparison run set;
- rejection when authorization pause is disabled;
- rejection of a non-M4.3 Python execution profile;
- fail-closed recovery after persisted plan payload tamper.

## Explicit non-claims

M5.1 does **not**:

- execute an experiment;
- register or prepare a run automatically;
- create an M2 process proposal;
- mint or approve an authorization receipt;
- consume an authorization receipt;
- schedule background work;
- choose new hypotheses/manifests/comparison criteria;
- evaluate a comparison;
- publish a conclusion;
- grant filesystem, Git, network, delegation, or external-write authority.

It freezes the control envelope within which later M5 orchestration must operate.

## M5 proof slices

The shortest intended M5 path is:

### M5.1 — Frozen Automation Plan

Freeze exact target, budget, and stop rules before automated work.

### M5.2 — Governed Automation State Machine

Advance the already-declared M4 run set one durable step at a time, consume budget monotonically, reuse `GovernedPythonJsonRunner`, and stop at the external authorization boundary. Recovery must reconstruct state without automatically replaying a side effect.

### M5.3 — First Real Bounded Automation Closure

Run the already-proven M4 comparison/conclusion vertical through the M5 coordinator with externally supplied authorization, bounded step/run budgets, deterministic stop behavior, and fresh-process recovery. No second executor or authority system.

## Exit gate

M5.1 is complete when the exact reviewed candidate proves:

> One exact frozen M4.4 policy can acquire only one pre-run automation plan whose target, run budget, and mandatory stop rules are durable and immutable; the plan admits only the already-proven M4.3 execution profile and cannot itself create execution authority.
