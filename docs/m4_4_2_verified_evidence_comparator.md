# M4.4.2 — Verified Evidence Comparator

## Purpose

M4.4.1 proved that one exact comparison policy can be frozen before either comparison arm has registered run evidence.

M4.4.2 answers the next question:

> Can Codexia evaluate that frozen policy over the whole final verified evidence set, without allowing the caller to select convenient runs or values after seeing outcomes?

Primary invariant:

```text
frozen policy + selected values != verified comparison
```

## Public evaluation boundary

The comparator accepts only a frozen `policy_id`.

It does not accept:

- caller-provided run ids;
- caller-provided metric values;
- a caller-selected evidence subset;
- a replacement direction, threshold, aggregation, or missing-evidence rule.

Every comparison input is recovered from durable M4 state.

## Final evidence-set rule

Both baseline and candidate experiments must be irreversibly sealed before evaluation.

This is stronger than requiring only individual run seals. Once the experiment is sealed, no later run can extend the arm and retroactively make a previously selected subset look complete.

For each arm, policy seed position `i` binds exactly:

```text
run.ordinal == i
run.seed == policy.seeds[i]
```

An undeclared extra ordinal, a seed/ordinal substitution, or an unsealed arm fails closed.

## What `seed` means in this slice

M4.3.1 execution provenance binds the full exact `ExperimentRun`, including its `seed`, so M4.4.2 can prove which seed/repetition identity belongs to each governed execution.

The existing `python-inline-json-result.v1` process profile does **not** claim that `ExperimentRun.seed` is automatically injected into the experiment program as an RNG input. Therefore M4.4.2 does not claim that the program semantically consumed the seed merely because the run record carries it.

Experiments that require controlled stochastic execution need an execution profile or experiment procedure that explicitly binds the RNG seed as a computational input. That is a separate execution-contract concern, not something the comparator should pretend to infer.

## Physical-evidence admission

For every declared run that exists, the comparator admits values only through `SqlitePhysicalEvidenceRegistry.recover(run_id)`.

That recovery revalidates the M4.3 chain and rereads the physical result bytes. M4.4.2 therefore never treats a caller-supplied number or a bare M4 metric record as comparison evidence.

The recovered metric must match the frozen metric name and unit exactly.

## Missing/failure policy

A declared run or physical receipt can be absent only according to the rule frozen in M4.4.1.

### `inconclusive`

The result records the missing seed identities and is `inconclusive`.

No baseline mean, candidate mean, or effect is calculated from the surviving subset:

```text
missing evidence
-> no partial aggregate
-> no favorable partial result
```

### `error`

Evaluation fails and no comparison result is published.

If a physical receipt exists but its durable lineage or physical bytes fail recovery, that is an integrity failure rather than ordinary missing evidence.

## Deterministic numeric semantics

M4 metric values retain their exact JSON numeric representation (`int` versus `float`) in the evidence entries and metric digests.

Aggregation converts each recovered value to an exact `fractions.Fraction`:

- integer -> exact integer fraction;
- float -> exact fraction of the underlying binary64 value.

The mean and effect are stored as canonical rational text rather than a newly rounded float.

For `lower_is_better`:

```text
effect = baseline_mean - candidate_mean
```

For `higher_is_better`:

```text
effect = candidate_mean - baseline_mean
```

The comparison outcome is `supported` only when the exact effect meets the frozen minimum effect. Otherwise it is `refuted`.

These labels describe the result of the frozen comparison policy. They are **not** an M4.5 scientific `Conclusion` and do not claim scientific truth outside the declared policy and evidence.

## Durable result

A `ComparisonResult` binds:

- exact policy id/digest and freeze digest;
- exact terminal sealed event id/sequence/digest for both arms;
- each admitted run id/digest, ordinal, and seed;
- exact metric id/digest/value;
- exact artifact id/digest;
- exact M4.3 physical-receipt id/digest;
- missing seed sets;
- exact means/effect when complete;
- policy outcome;
- result digest.

The result id is deterministic from the exact policy freeze.

## Restart recovery

`recover_result(policy_id)` does not trust the persisted comparison row by itself.

It:

1. validates canonical persisted result bytes and indexes;
2. recovers the exact frozen policy again;
3. recovers both final sealed experiments again;
4. recovers every required M4.3 physical receipt again, including physical-file verification;
5. recomputes the deterministic comparison;
6. requires the recomputed result to equal the persisted result exactly.

No experiment is rerun.

A physical mutation or result-row tamper after publication therefore cannot silently produce a new valid comparison result.

## Demonstrated adversarial cases

The M4.4.2 regression suite covers:

- comparison before arm sealing;
- undeclared extra run;
- seed/ordinal substitution;
- missing declared repetitions with `inconclusive` and no partial aggregate;
- missing evidence with `error` and no published result;
- metric name/unit reinterpretation;
- lower/higher direction semantics;
- exact int/float evidence representation;
- physical mutation after result publication;
- persisted result tamper;
- fresh-registry deterministic recovery.

## Non-claims

M4.4.2 does not claim:

- that a run seed was consumed by experiment code unless the execution profile/procedure explicitly provides that contract;
- statistical significance, confidence intervals, p-values, or Bayesian evidence;
- adaptive stopping or adaptive experiment design;
- protection against a hostile local administrator coherently rewriting every trust store;
- automatic scientific-truth adjudication;
- M4.5 `Conclusion` semantics.

## Exit gate

> A caller cannot cherry-pick runs, substitute caller-provided metric values, change the frozen criterion, silently ignore extra runs, or turn missing evidence into a favorable partial aggregate.

M4.4.3 is responsible for using this comparator in one real baseline/candidate experiment and proving full fresh-process closure of policy -> verified runs -> comparison result.
