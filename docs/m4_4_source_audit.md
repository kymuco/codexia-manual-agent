# M4.4 — Comparison and Falsification Source Audit

## Scope

This audit closes the M4.4 milestone over the merged M4.4.1 and M4.4.2 runtime surfaces plus the repository-contained M4.4.3 closure experiment.

Reviewed milestone surface:

- `codexia_manual_agent.lab.comparison`;
- `codexia_manual_agent.lab.comparison_result`;
- M4.2 durable experiment/run chronology and sealing used to prove pre-evidence ordering and final arm completeness;
- M4.3 physical-evidence recovery used as the only admitted metric source;
- M4.4.1 regressions in `tests/test_lab_comparison_policy.py`;
- M4.4.2 regressions in `tests/test_lab_comparison_result.py`;
- M4.4.3 repository fixture and fresh-process closure regression.

Merged milestone bases:

```text
M4.4.1  6e19df4b452f962826d6f37e6e424ae444a0bb32
M4.4.2  eced35065fdb055bb2e4c4232c0e40ef8a8f6b10
```

M4.4 adds no scheduler, adaptive experiment planner, arbitrary aggregation code, new execution authority, statistical-significance layer, or automatic scientific conclusion adjudicator.

## Primary invariant

```text
verified evidence != precommitted comparison
```

M4.4 therefore requires three independent properties:

1. criterion freeze before compared run evidence exists;
2. complete verified evidence admission without caller-selected subsets or values;
3. deterministic restart recovery of the same comparison result without rerun or criterion replacement.

## M4.4.1 — frozen comparison policy

M4.4.1 makes the comparison criterion a durable pre-evidence object.

One exact policy binds:

- exact hypothesis id/digest;
- exact baseline/candidate experiment ids and manifest digests;
- metric name/unit;
- direction;
- minimum effect;
- ordered seed/repetition plan;
- mean aggregation;
- missing/failure policy.

The freeze is admitted only while both exact experiment chronologies contain zero runs.

The registry shares the same SQLite trust domain and writer serialization as M4.2 run registration, so freeze-vs-run ordering is not inferred from wall-clock timestamps.

M4.4.1 additionally constrains one unordered exact manifest pair to one deterministic policy identity. A second threshold, direction, metric, seed plan, or baseline/candidate reversal therefore cannot create a menu of valid pre-registered policies for later outcome shopping.

Representative adversarial coverage in `tests/test_lab_comparison_policy.py` includes:

- late freeze after run registration;
- concurrent freeze/run ordering boundary;
- different policy for the same exact manifest pair;
- reversed-arm policy shopping;
- persisted freeze/root tamper and rebinding;
- restart recovery against the original pre-run experiment anchors.

## M4.4.2 — verified evidence comparator

M4.4.2 evaluates only by `policy_id`. Its public evaluation surface accepts no caller-provided run ids, metric values, evidence subset, threshold, direction, or alternate missing-evidence rule.

Both arm experiments must be irreversibly sealed before evaluation. That converts the exact experiment chronology into a final run set.

For every declared repetition position `i`, the comparator requires:

```text
run.ordinal == i
run.seed == policy.seeds[i]
```

Extra undeclared runs, substitutions, or open experiments fail closed.

Metric evidence is admitted only through `SqlitePhysicalEvidenceRegistry.recover(run_id)`, which revalidates the full M4.3 execution/physical chain and rereads the physical file.

The comparator therefore cannot treat a caller-supplied number or a bare metric row as verified comparison evidence.

Missing evidence follows only the frozen policy:

- `inconclusive` produces no partial aggregate;
- `error` refuses publication.

Complete evidence is aggregated with exact `Fraction` arithmetic. The original exact metric representation and digest remain bound in each evidence entry even when two numeric representations map to the same rational value.

A durable `ComparisonResult` binds the exact freeze, final sealed arm heads, run/metric/artifact/physical-receipt evidence, missing sets, exact aggregates/effect, and outcome.

`recover_result(policy_id)` recomputes the result from durable state and physical evidence rather than trusting the persisted result row alone.

Representative adversarial coverage in `tests/test_lab_comparison_result.py` includes:

- comparison before arm sealing;
- extra undeclared run;
- seed/ordinal substitution;
- missing evidence with both frozen policies;
- metric-name/unit reinterpretation;
- direction semantics;
- exact int/float evidence identity;
- physical mutation after result publication;
- persisted result tamper;
- fresh-registry deterministic recomputation.

## M4.4.3 — first real baseline/candidate falsification and restart closure

M4.4.3 intentionally adds no new production runtime abstraction.

Repository-contained experiment fixture:

```text
tests/fixtures/m4_4_3_integration_error_experiment.py
```

The closure hypothesis is:

> For `n=8`, trapezoid integration reduces the exact common-denominator integration-error numerator by at least `180` relative to left Riemann.

The criterion is frozen before either arm registers a run:

> lower-is-better effect must be at least `180`; otherwise the comparison is refuted.

Two repetition identities are declared:

```text
(101, 202)
```

All four runs execute through the governed M4.3 path and are individually sealed. Both experiments are then sealed before comparison.

Exact verified means:

```text
baseline left Riemann = 184
candidate trapezoid  = 8
effect               = 176
threshold            = 180
outcome              = REFUTED
```

The candidate improves substantially but misses the precommitted threshold. Codexia must preserve the inconvenient criterion rather than reinterpret the improvement as support.

The closure regression in `tests/test_lab_m4_4_3_comparison_closure.py` then starts a fresh Python process that receives only the durable database path and stable `policy_id`.

The fresh process creates recovery registries only and must reconstruct:

- exact policy id/digest;
- exact freeze digest;
- exact result id/digest;
- `REFUTED` outcome;
- exact baseline/candidate means and effect;
- exact repetition identities;
- exact physical-receipt digests used by both arms.

The parent process verifies that all four physical output files retain identical bytes and modification times across recovery, proving reconstruction rather than rerun.

## Adversarial closure matrix

| Required M4.4 threat | Evidence |
| --- | --- |
| criterion chosen after run evidence | M4.4.1 zero-run freeze gate and shared SQLite writer ordering |
| multiple convenient policies frozen in advance | M4.4.1 deterministic one-policy identity per unordered exact manifest pair |
| baseline/candidate reversal shopping | M4.4.1 unordered pair identity regression |
| caller cherry-picks runs | M4.4.2 policy-only evaluation surface plus sealed final arm state |
| undeclared extra run | M4.4.2 exact final run-set regression |
| seed/ordinal substitution | M4.4.2 repetition-identity regression |
| missing evidence becomes favorable subset | M4.4.2 frozen missing policy; inconclusive has no partial aggregate |
| metric/value substitution | M4.4.2 M4.3 physical recovery and exact metric identity |
| physical evidence mutates after result | M4.4.2 physical-mutation recovery regression |
| persisted result is rewritten | M4.4.2 deterministic recomputation and result-tamper regression |
| restart changes result or replays work | M4.4.3 fresh Python process recovers exact REFUTED result while output bytes/mtime remain unchanged |

No one test is treated as sufficient alone. Milestone closure depends on the composition of precommitment, complete physical-evidence admission, and restart-safe deterministic recomputation.

## M4.4 milestone exit gate

The planned exit gate is:

> Starting only from durable state after restart, Codexia reconstructs the exact pre-evidence comparison policy, every verified run used by it, and the same comparison result without rerunning experiments or choosing a new criterion.

The M4.4.3 closure regression exercises exactly that path.

If the M4.4.3 exact candidate passes the repository's existing cross-platform CI and CodeQL gates, this audit considers M4.4 complete at the stated scope.

## Remaining non-claims

M4.4 does not claim:

- statistical significance, confidence intervals, p-values, Bayesian evidence, or population-level inference;
- that `ExperimentRun.seed` was consumed by experiment code unless an execution contract explicitly binds it as computational input;
- adaptive stopping or adaptive experimental design;
- automatic scientific truth;
- hostile-local-admin or hostile-kernel attestation;
- autonomous experiment generation or scheduling;
- remote execution;
- M4.5 scientific `Conclusion` semantics.

`SUPPORTED`, `REFUTED`, and `INCONCLUSIVE` remain exact outcomes of one frozen comparison policy over verified evidence.

## Next milestone boundary

With M4.4 closed, the next unresolved question is no longer whether a comparison was precommitted and computed honestly.

M4.5 should decide whether and how one frozen comparison result can be turned into a durable evidence-bounded `Conclusion` without silently upgrading a policy-local outcome into a claim of scientific truth.
