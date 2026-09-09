# M5.3 — First Real Bounded Automation Closure

## Purpose

M5.1 freezes one exact automation target, workspace, budget, and stop policy before automated work. M5.2 advances the exact predeclared run set while preserving the external M2 authorization boundary. M5.3 closes the remaining scientific part of that same frozen loop.

The milestone does not add another executor, evidence system, policy selector, or conclusion author. It composes the already-proven M4 operations under the same immutable M5 plan.

Primary invariant:

```text
bounded automation progress != execution authority
```

Full-loop budget invariant:

```text
post-execution scientific closure consumes the same frozen automation budget
!=
free work after the run budget has been spent
```

Terminal invariant:

```text
bounded conclusion reached
→ STOPPED_CONCLUSION
→ no further automated work
```

## Why M5.3 needs a closure coordinator

Stopping M5.2 at `RUN_SET_COMPLETE` is intentional: run orchestration and scientific adjudication are different responsibilities. But a test that manually called experiment sealing, comparison evaluation, and conclusion publication after M5.2 would only prove compatibility. It would not prove that M5 can automate the already-proven M4 loop end to end.

`GovernedAutomationClosure` therefore provides one narrow post-run orchestration surface. It accepts only the stable `automation_id` and derives every target from the existing frozen plan and policy.

It performs at most one non-authority durable scientific transition per `advance()`:

```text
RUN_SET_COMPLETE
→ seal exact baseline experiment
→ BASELINE_EXPERIMENT_SEALED
→ seal exact candidate experiment
→ EXPERIMENTS_SEALED
→ evaluate exact frozen M4.4 comparison
→ COMPARISON_COMPLETE
→ publish exact bounded M4.5 conclusion
→ STOPPED_CONCLUSION
```

There is no caller parameter for experiment id, policy id, threshold, metric, direction, result, verdict, or conclusion text.

## One frozen budget for the whole loop

The real closure fixture has four declared run slots: two baseline repetitions and two candidate repetitions.

M5.2 assigns three durable stages to each run:

```text
register exact run  = 1
prepare exact M4.3 proposal/binding = 1
authorized execution reaches terminal evidence = 1
```

Therefore the complete governed run set consumes:

```text
4 runs × 3 stages = 12 steps
```

M5.3 adds exactly four durable closure stages:

```text
baseline experiment seal = 1
candidate experiment seal = 1
comparison result         = 1
bounded conclusion        = 1
```

The real M5.3 plan is frozen before any run with:

```text
max_runs  = 4
max_steps = 16
```

No budget is enlarged after evidence is observed.

`GovernedAutomationClosure` derives `closure_steps_used` from existing irreversible M4 state rather than storing another mutable counter. Total use is:

```text
steps_used = M5.2 run_steps_used + derived M5.3 closure_steps_used
```

If durable state is already beyond the frozen budget, recovery fails integrity. If the next transition would exceed the budget, the coordinator returns `STOPPED_BUDGET` and remains inert.

A dedicated regression freezes a one-seed plan with a budget that permits both experiment seals and the comparison but not conclusion publication. The result becomes durable, then the state stops at the exact budget boundary with no conclusion.

## Authority boundary remains external

All process execution still happens exclusively through M5.2:

```text
PAUSED_AUTHORIZATION_REQUIRED
→ externally supplied AuthorizationReceipt
→ existing GovernedPythonJsonRunner.execute_authorized(...)
```

The real M5.3 fixture supplies four HUMAN-source test receipts from outside the automation coordinator, one for each exact pending process proposal.

`GovernedAutomationClosure` itself has no receipt parameter, approval-decision API, process command, or process-execution method. Its transitions are scientific registry operations that already exist in M4.

## Exact real experiment

M5.3 deliberately reuses the M4.4.3/M4.5.3 integration-error fixture rather than creating a favorable new toy case.

Hypothesis:

> For `n=8`, trapezoid integration reduces the exact common-denominator error numerator by at least `180` relative to left Riemann.

Frozen comparison:

- metric: `integration_error_numerator`;
- unit: `over_6n_cubed`;
- direction: lower is better;
- required improvement: `180`;
- seeds/repetition identities: `101`, `202`;
- missing-run policy: error.

Verified result remains:

```text
baseline mean = 184
candidate mean = 8
effect         = 176
threshold      = 180

176 < 180
→ REFUTED
```

The automation does not weaken the threshold after discovering that the candidate is substantially better but still misses the precommitted claim.

The M4.5 conclusion remains the bounded policy-scoped `REFUTED` conclusion. It is not upgraded into universal scientific falsity.

## Crash-safe derived closure

M5.3 does not create another mutable closure ledger.

Recovery derives its phase from:

- M5.1 frozen plan;
- M5.2 `RUN_SET_COMPLETE` recovery;
- exact baseline/candidate experiment seal state;
- authoritative M4.4 comparison-result recovery if present;
- authoritative M4.5 conclusion recovery if present.

This makes all four post-run boundaries restartable without inventing replay semantics.

For example:

```text
baseline experiment sealed
→ process exits
→ new process recovers BASELINE_EXPERIMENT_SEALED
→ candidate seal is still the next deterministic transition
```

Likewise, an already-persisted comparison or conclusion is recovered through the existing M4.4/M4.5 deterministic recomputation path rather than trusted as an M5 cache.

## Fresh-process closure recovery

The final regression starts a new Python process after terminal closure and supplies only:

```text
SQLite path
+ automation_id
```

The child constructs `GovernedAutomationClosure` and recovers:

- exact frozen automation identity and plan digest;
- exact policy id derived from that plan;
- terminal `STOPPED_CONCLUSION` phase;
- `16/16` total steps with `12` run steps and `4` closure steps;
- four completed declared runs;
- exact durable M4.4 result id/digest and `REFUTED` outcome/effect `176`;
- exact durable M4.5 conclusion id/digest, bounded scope, verdict, and canonical summary.

No authorization receipt is supplied to the child and no execution-continuation method is called.

The parent also records the bytes and `mtime_ns` of all four governed physical result artifacts before fresh-process recovery and verifies they are unchanged afterward. Recovery therefore cannot hide a process rerun behind an identical scientific result.

## Stop on conclusion

M5.1 v1 requires `stop_on_conclusion = true`.

M5.3 makes that rule observable:

```text
bounded conclusion durable
→ closure_steps_used = 4
→ STOPPED_CONCLUSION
```

A repeated `advance(automation_id)` at that terminal phase returns the same state and creates no further work.

## Explicit non-claims

M5.3 does **not**:

- grant or manufacture process authorization;
- choose a new hypothesis, manifest, seed set, metric, direction, threshold, or missing-run policy;
- add an executor or physical-evidence path;
- infer scientific truth outside the frozen comparison scope;
- generate free-form conclusion prose;
- schedule background or periodic experiments;
- automatically design the next experiment;
- recover liveness by replaying an ambiguous side effect;
- grant Git, network, filesystem, delegation, or external-write authority;
- prove hostile local-machine or kernel attestation.

## M5 closure

The complete demonstrated bounded automation chain is now:

```text
Hypothesis + exact manifests
→ M4.4 comparison policy frozen before evidence
→ M5.1 plan/workspace/budget/stop policy frozen before runs
→ M5.2 deterministic exact run slot
→ exact M4.3 proposal
→ PAUSED_AUTHORIZATION_REQUIRED
→ external HUMAN receipt
→ existing M2/M3 governed execution
→ verified M4.3 physical evidence
→ irreversible run seal
→ repeat exact declared run set
→ RUN_SET_COMPLETE at 12/16 steps
→ M5.3 exact baseline seal
→ exact candidate seal
→ exact M4.4 comparison
→ exact M4.5 bounded conclusion
→ STOPPED_CONCLUSION at 16/16 steps
→ fresh process from SQLite + automation_id only
→ exact same terminal state/result/conclusion
→ physical bytes and mtime unchanged
```

## Exit gate

M5 is complete when the exact reviewed candidate demonstrates:

> One pre-frozen automation identity can carry the already-proven M4 research loop from exact governed run progression through exact comparison and bounded conclusion under one immutable step/run budget, while process authority remains externally supplied, budget/conclusion stops are real admission boundaries, restart recovery derives the same state without replay, and no second executor, evidence path, scientific-policy selector, or authority system is introduced.
