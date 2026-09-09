# M4.5.3 — First Real Conclusion Closure

## Purpose

M4.5.3 closes the manual M4 research loop by applying the M4.5 adjudication path to the already-established M4.4.3 falsification case rather than introducing another synthetic comparison.

The milestone question is:

> Can Codexia take one exact frozen comparison whose verified result is `REFUTED`, publish the only admitted policy-scoped conclusion, and reconstruct that same conclusion in a fresh Python process without rerunning experiments, replacing the criterion, or accepting caller-authored adjudication semantics?

## Reused real comparison

M4.5.3 deliberately reuses `tests/fixtures/m4_4_3_integration_error_experiment.py` and the M4.4.3 comparison scenario.

Hypothesis:

> For `n=8`, trapezoid integration reduces the exact common-denominator error numerator by at least `180` relative to left Riemann.

Frozen falsification criterion:

> The lower-is-better comparison effect is less than `180`.

Frozen comparison plan:

- baseline: left-Riemann approximation of `∫₀¹x²dx` at `n=8`;
- candidate: trapezoid approximation of the same integral and `n`;
- metric: `integration_error_numerator`;
- unit: `over_6n_cubed`;
- direction: lower is better;
- minimum effect: `180`;
- repetition/seed identities: `(101, 202)`;
- missing-run policy: error.

Verified M4.4 evidence produces:

```text
baseline mean = 184
candidate mean = 8
effect         = 176
threshold      = 180

176 < 180
→ ComparisonOutcome.REFUTED
```

The candidate is substantially better, but the precommitted claim required an improvement of at least `180`. Codexia therefore preserves the inconvenient `REFUTED` result instead of weakening the criterion after seeing evidence.

## Adjudicated conclusion

M4.5.1 maps that exact recovered comparison result to one fixed policy-scoped conclusion:

```text
scope   = frozen_comparison_policy.v1
verdict = REFUTED
```

Canonical bounded wording:

> The verified evidence does not satisfy the exact frozen comparison policy for this hypothesis; refutation is limited to that declared policy and evidence.

This does not mean that trapezoid integration is generally worse, that the mathematical method is scientifically false, or that the hypothesis is universally false. It means only that the exact verified evidence failed the exact precommitted effect threshold.

## Durable publication

`SqliteAdjudicatedConclusionRegistry.publish(policy_id)` accepts only the frozen policy identity. It does not accept caller-provided verdict, summary, conclusion id, evidence subset, replacement result, or alternate arm identities.

Publication derives:

```text
policy_id
→ exact frozen M4.4 policy
→ exact durable M4.4 result
→ exact baseline/candidate experiment recovery
→ deterministic AdjudicatedConclusion
→ durable derived-cache row
```

The persisted row is not conclusion authority. M4.5.2 recovery re-recovers the M4.4 dependencies and deterministically recomputes the conclusion.

## Fresh-process closure

`tests/test_lab_m4_5_3_conclusion_closure.py` performs the complete chain and then starts a genuinely fresh Python process.

The child process receives only:

- the SQLite database path;
- the stable `policy_id`.

It constructs only recovery registries:

- `SqliteLabRegistry`;
- `SqliteSessionEventStore`;
- `SqliteRunExecutionRegistry`;
- `SqlitePhysicalEvidenceRegistry`;
- `SqliteComparisonRegistry`;
- `SqliteComparisonResultRegistry`;
- `SqliteAdjudicatedConclusionRegistry`.

It does **not** construct:

- `GovernedPythonJsonRunner`;
- `LocalApprovalAuthority`;
- a new M2 process proposal;
- a new authorization receipt;
- a replacement comparison policy;
- caller-authored conclusion text.

The fresh process independently recovers the conclusion identity and checks the exact policy, result, conclusion id/digest, bounded scope, `REFUTED` verdict, canonical summary, and both exact manifest digests. The parent then compares the recovered identity/digest against the conclusion published before restart.

Before starting the child process, the parent records the bytes and `mtime_ns` of all four governed physical result files. After recovery, every file must remain byte-identical with unchanged modification time. The fresh process therefore demonstrates recovery rather than experiment replay.

## Authority nuance

As in M4.3/M4.4 closure tests, HUMAN-source authorization receipts are supplied programmatically through the existing authority API by the test harness. The test does not claim a literal interactive UI approval gesture. The relevant property is that the experiment runner itself cannot mint an `ALLOW` decision.

## What M4.5 now proves

The composed M4.5 chain is:

```text
exact M4.4 policy/result
→ deterministic bounded adjudication
→ durable derived conclusion cache
→ fresh dependency recovery
→ exact deterministic recomputation
→ same conclusion after restart
```

Combined with M4.1–M4.4, the complete manual Computational Lab vertical is now:

```text
Hypothesis
→ exact experiment manifests
→ frozen comparison policy before evidence
→ governed executions under external HUMAN-source authorization
→ verified physical evidence
→ irreversibly sealed complete run sets
→ deterministic comparison
→ policy-scoped adjudicated conclusion
→ durable fresh-process recovery without replay
```

## Explicit non-claims

M4.5.3 does not prove:

- unrestricted scientific truth or falsity;
- absence of experimental bias or bad hypothesis design;
- generalization beyond the declared manifests, metric, seeds, aggregation, threshold, and evidence;
- confidence intervals, p-values, Bayesian evidence, or external replication;
- actor identity beyond the existing authority-record semantics;
- hostile-local-machine attestation;
- automatic recommendation or action authority from a conclusion;
- autonomous experiment scheduling.

## Exit gate

M4.5 is closed when the exact candidate proves:

> Starting only from durable state after restart, Codexia reconstructs the same evidence-bounded policy-scoped conclusion from the exact frozen policy and verified comparison result without rerunning experiments, changing the criterion, or accepting caller-authored adjudication semantics.
