# M4.4.3 — First Real Baseline/Candidate Comparison

## Purpose

M4.4.1 froze one exact comparison policy before run evidence existed. M4.4.2 evaluated only complete sealed M4.3 physical evidence under that frozen policy.

M4.4.3 closes the milestone by using those layers end to end in one repository-contained falsification experiment rather than adding another runtime abstraction.

Primary closure question:

> Starting only from durable state after restart, can Codexia reconstruct the exact pre-evidence comparison policy, every verified run used by it, and the same comparison result without rerunning experiments or choosing a new criterion?

## Experiment

The fixture compares two deterministic numerical integration methods for

```text
integral from 0 to 1 of x^2 dx = 1/3
```

with `n=8` subintervals.

Baseline:

```text
left Riemann sum
```

Candidate:

```text
trapezoid rule
```

Both arms emit the absolute approximation-error numerator over one common exact denominator:

```text
6 * n^3
```

This avoids introducing a floating-point comparison ambiguity into the closure experiment.

Repository fixture:

```text
tests/fixtures/m4_4_3_integration_error_experiment.py
```

## Frozen hypothesis and falsification criterion

Before either arm has any registered run, the hypothesis declares:

> For `n=8`, trapezoid integration reduces the exact common-denominator error numerator by at least `180` relative to left Riemann.

The falsification criterion is frozen as:

> The lower-is-better comparison effect is less than `180`.

The comparison policy additionally freezes:

- metric: `integration_error_numerator`;
- unit: `over_6n_cubed`;
- direction: `lower_is_better`;
- minimum effect: `180`;
- seeds/repetition identities: `(101, 202)`;
- aggregation: `mean`;
- missing/failure policy: `error`.

The deterministic fixture does not semantically consume the run seed as RNG input. The seeds are exact repetition identities, matching the M4.4.2 non-claim.

## Exact result

For `n=8`, both methods are represented over the same denominator `6*n^3`.

Baseline left-Riemann error numerator:

```text
184
```

Candidate trapezoid error numerator:

```text
8
```

Therefore:

```text
baseline mean  = 184
candidate mean = 8
effect         = 184 - 8 = 176
threshold      = 180
```

The candidate is substantially better, but it does **not** meet the frozen claim.

The required comparison outcome is therefore:

```text
REFUTED
```

This is the point of the closure experiment: Codexia must not silently weaken a threshold merely because the candidate looks favorable.

## End-to-end chronology

The closure regression performs this exact order:

```text
Hypothesis + baseline manifest + candidate manifest
-> register both experiments with zero runs
-> freeze one exact comparison policy
-> register baseline run ordinal 0 / seed 101
-> governed M4.3 execution + physical evidence + run seal
-> register baseline run ordinal 1 / seed 202
-> governed M4.3 execution + physical evidence + run seal
-> register candidate run ordinal 0 / seed 101
-> governed M4.3 execution + physical evidence + run seal
-> register candidate run ordinal 1 / seed 202
-> governed M4.3 execution + physical evidence + run seal
-> seal both experiments
-> evaluate frozen policy
-> durable REFUTED ComparisonResult
```

No caller supplies comparison metric values, a run subset, a replacement threshold, or a replacement direction.

## Fresh-process recovery

`tests/test_lab_m4_4_3_comparison_closure.py` then starts a new Python process with only:

- the durable SQLite database path;
- the stable comparison `policy_id`.

The fresh process creates recovery registries only. It does not construct `GovernedPythonJsonRunner`, issue authorization, register runs, or execute either experiment.

It must recover and revalidate:

- exact policy id/digest;
- exact freeze digest;
- exact comparison result id/digest;
- `REFUTED` outcome;
- baseline mean `184`;
- candidate mean `8`;
- effect `176`;
- exact baseline/candidate seed identities;
- exact M4.3 physical receipt digests used by the result.

The parent process records all four physical output files before restart recovery and requires their bytes and modification times to remain unchanged afterward.

Therefore recovery demonstrates reconstruction, not replay.

## Why a refuted experiment is the closure fixture

A supported result would prove that Codexia can carry favorable evidence through the pipeline. A refuted result tests the stronger product behavior: the system must preserve an inconvenient precommitment.

The experiment intentionally creates this situation:

```text
candidate improves strongly
but
candidate misses frozen threshold
therefore
claim remains refuted
```

That is the first direct proof that the comparison layer can act as a falsification surface rather than only a result-reporting surface.

## Existing adversarial coverage composed by this closure

M4.4.3 does not duplicate every lower-level regression. Milestone closure composes the already-proven boundaries:

- M4.4.1: late policy freeze, policy shopping, reversed-arm shopping, freeze tamper/rebinding, and restart recovery;
- M4.4.2: open-arm comparison, extra undeclared run, seed/ordinal substitution, missing evidence under both frozen policies, metric mismatch, physical mutation, result tamper, and deterministic restart recomputation;
- M4.4.3: one real pre-evidence policy -> four governed physical runs -> sealed arms -> refuted result -> fresh-process recovery without rerun.

The source audit maps these threats to their exact regression surfaces.

## Non-claims

M4.4.3 does not claim:

- statistical significance, confidence intervals, or population-level inference;
- that run seeds were consumed as RNG inputs by this deterministic fixture;
- adaptive stopping or adaptive experiment design;
- automatic scientific-truth adjudication;
- M4.5 `Conclusion` semantics;
- hostile-local-admin tamper resistance;
- autonomous scheduling or unattended experiment execution.

`REFUTED` means only that the complete verified evidence fails the exact frozen comparison policy.

## Exit gate

M4.4 is closed when the cross-platform candidate demonstrates:

> Starting only from durable state after restart, Codexia reconstructs the exact pre-evidence comparison policy, every verified run used by it, and the same comparison result without rerunning experiments or choosing a new criterion.

The M4.4.3 closure regression exercises that path directly.
