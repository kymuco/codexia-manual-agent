# M4.4 — Comparison and Falsification Plan

## Why this is the next milestone

M4.3 proved that Codexia can bind a declared run to one governed execution, verified physical result bytes, a locally extracted metric, sealing, and restart-safe recovery.

That still does not make a scientific comparison trustworthy.

A comparison can be invalid even when every individual run is perfectly verified if the target metric, direction, threshold, repetitions, aggregation, or missing-run treatment are chosen after seeing the outcomes.

M4.4 therefore starts from a different invariant:

```text
verified evidence != precommitted comparison
```

The first job is not to add more statistics. It is to make post-hoc criterion selection structurally visible and fail closed inside the trusted Codexia runtime.

## Milestone question

> Can Codexia freeze one exact comparison policy before any run evidence exists for the compared manifests, then evaluate only the complete verified evidence set declared by that policy and recover the same result after restart?

M4.4 does not claim that Codexia can prove what a human saw outside the runtime before registering a policy. The guarantee is narrower and auditable: inside the durable Codexia chronology, the policy must be frozen before either comparison arm has a registered run.

## First comparison surface

The first slice compares exactly two experiment manifests bound to the same exact hypothesis:

- `baseline` arm;
- `candidate` arm.

The policy freezes at least:

- exact hypothesis id/digest;
- exact baseline experiment id/manifest digest;
- exact candidate experiment id/manifest digest;
- target metric name and unit;
- direction (`lower_is_better` or `higher_is_better`);
- minimum effect threshold;
- exact ordered seed/repetition plan;
- aggregation rule;
- missing/failure policy.

The initial aggregation surface should remain deliberately narrow. `mean` is sufficient for the first proof. Confidence intervals, significance tests, adaptive stopping, and model-selected criteria are not required for M4.4.

## Temporal freeze rule

The strongest simple v1 rule is:

```text
both manifests durably registered
+ zero runs in baseline
+ zero runs in candidate
-> policy may freeze
```

After either experiment has a registered run:

```text
new pre-evidence policy freeze for that comparison = forbidden
```

M4.4 v1 also admits exactly one frozen policy for one unordered exact manifest pair. The policy identity is deterministic from both experiment ids and manifest digests, independent of which arm is written first. Therefore a second threshold, metric, direction, seed plan, or baseline/candidate reversal for the same exact pair conflicts with the already-frozen policy instead of creating a menu of pre-registered criteria that could be selected after outcomes are known.

A durable freeze receipt should bind the policy digest to the exact M4.2 `experiment_registered` event digest for each arm. Later runs may extend those experiment chronologies, but recovery must still prove that the frozen policy was anchored to the pre-run state.

This does not provide hostile-database attestation. It provides causal ordering inside the existing same-host SQLite trust domain used by M3/M4.

## Evaluation rule

M4.4 evaluation must consume only evidence that satisfies all of the following:

1. policy is already durably frozen;
2. each arm is the exact manifest bound by the policy;
3. each arm contains exactly the declared ordinal/seed repetition set, with no cherry-picked subset;
4. required run evidence is sealed;
5. metric values come from successful M4.3 physical-evidence recovery, not caller-supplied numbers;
6. metric name/unit match the policy;
7. missing/failed evidence is handled only by the policy frozen before the runs;
8. aggregation and effect direction are deterministic;
9. the result binds the exact policy freeze and exact evidence digests used.

For `lower_is_better`:

```text
effect = baseline_aggregate - candidate_aggregate
```

For `higher_is_better`:

```text
effect = candidate_aggregate - baseline_aggregate
```

A complete evidence set supports the declared comparison only when the frozen effect threshold is met. Otherwise it refutes that declared comparison. Missing/failure policy may instead make the result inconclusive where explicitly frozen in advance.

## Three delivery slices

### M4.4.1 — Frozen comparison policy

Goal: make precommitment explicit and durable before run evidence exists.

Deliverables:

- immutable digest-bound comparison-policy contract;
- exact baseline/candidate manifest and hypothesis binding;
- metric/direction/threshold/seeds/aggregation/missing-policy binding;
- exactly one frozen policy per unordered exact manifest pair in v1;
- durable freeze receipt anchored to both pre-run M4.2 experiment chronologies;
- recovery and tamper/rebinding tests;
- atomic exclusion against concurrent run registration through the shared SQLite trust domain.

Exit gate:

> Once a policy is frozen for an exact manifest pair, neither a different policy for that pair nor a new pre-evidence policy after either arm has a registered run can be admitted as another valid M4.4.1 freeze.

### M4.4.2 — Verified evidence comparator

Goal: evaluate a frozen policy over only complete, sealed M4.3 evidence.

Deliverables:

- exact ordinal/seed-set admission for both arms;
- M4.3 physical-evidence recovery for every admitted run;
- deterministic `mean` aggregation;
- exact metric name/unit and numeric representation checks;
- frozen missing/failure behavior;
- durable comparison result binding policy freeze plus exact run/metric/physical-evidence digests;
- restart recovery that recomputes and revalidates without rerunning experiments.

Exit gate:

> A caller cannot cherry-pick runs, substitute caller-provided metric values, change the threshold/direction after evidence exists, or turn missing evidence into a favorable result outside the frozen policy.

### M4.4.3 — First real baseline/candidate comparison and closure

Goal: use M4.3 + M4.4 end to end rather than add another abstraction layer.

Deliverables:

- one repository-contained baseline/candidate pair under one exact hypothesis;
- policy frozen while both experiment manifests have zero runs;
- all declared runs executed through the governed M4.3 path;
- both evidence sets sealed;
- comparison evaluated under the frozen policy;
- fresh-process recovery of the exact policy -> evidence -> result chain;
- adversarial closure tests for late policy registration, missing run, extra/cherry-picked run, metric mismatch, physical mutation, result tamper, and attempted policy/result rebinding;
- source audit and milestone closure document.

Exit gate:

> Starting only from durable state after restart, Codexia reconstructs the exact pre-evidence comparison policy, every verified run used by it, and the same comparison result without rerunning experiments or choosing a new criterion.

## Explicit non-goals

Do not add these merely because they are nearby:

- adaptive experiment design;
- sequential hypothesis testing;
- confidence intervals or p-values;
- Bayesian model selection;
- arbitrary user-defined aggregation code;
- automatic scientific-truth claims;
- autonomous run scheduling;
- remote execution;
- TUI or plotting;
- M4.5 `Conclusion` adjudication before M4.4 comparison semantics are proven useful.

## Anti-drift criterion

A proposed M4.4 change should answer at least one of these:

1. Does it make the comparison criterion provably pre-evidence inside Codexia?
2. Does it prevent evidence-set cherry-picking or substitution?
3. Does it make the frozen comparison deterministic and restart-recoverable?
4. Does it directly falsify an assumption required by one of the above?

If the answer to all four is no, the change is outside the M4.4 critical path.
