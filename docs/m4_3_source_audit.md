# M4.3 — Governed Experiment Execution Source Audit

## Scope

This audit closes M4.3 from the exact merged M4.3.2 base:

```text
bbf9bd629e2866a760f26bb716ce71c79fd1653a
```

Reviewed milestone surface:

- `codexia_manual_agent.lab.execution_evidence`;
- `codexia_manual_agent.lab.execution_registry`;
- `codexia_manual_agent.lab.governed_python`;
- M3 durable process proposal/authorization/execution/observation chronology used by M4.3;
- M4.2 run/artifact/metric sealing and recovery used by M4.3;
- M4.3.1 provenance regressions in `tests/test_lab_run_execution_registry.py`;
- M4.3.2 governed physical-evidence regressions in `tests/test_lab_governed_python.py` and `tests/test_lab_physical_evidence_adversarial.py`;
- M4.3.3 repository-contained experiment fixture and fresh-process closure regression.

The milestone adds no generic scheduler, autonomous experiment loop, new sandbox, arbitrary process authority, remote executor, Git authority, or statistical adjudication.

## Primary invariant

```text
declared run != executed run
```

M4.3 therefore requires three independent properties before a registered run may be treated as execution-backed evidence:

1. exact durable run-to-governed-execution provenance;
2. exact physical output/metric evidence from that execution;
3. restart-safe reconstruction without replay.

## M4.3.1 — execution provenance

M4.3.1 establishes a durable chronology that binds an M4 `ExperimentRun` to one exact M2 process proposal, one exact authorization receipt, and one exact digest-bound process observation.

The important negative properties include:

- a proposal that was never durably proposed cannot be bound as the run execution;
- authorization from another proposal cannot be rebound to the run;
- an already-authorized or already-observed action cannot be attached post-hoc as if the M4 binding existed before authority/execution;
- a forged or substituted `ProcessExecutionObservation` is insufficient unless its exact observation digest is anchored by the corresponding M3 chronology;
- restart recovery revalidates the same M3 provenance rather than trusting only the M4 object graph.

Representative regressions live in `tests/test_lab_run_execution_registry.py`.

Merged M4.3.1 commit:

```text
e2bd771aaea24522683f740b014b4092a1f00b89
```

## M4.3.2 — physical evidence

M4.3.2 introduces one deliberately narrow profile, `python-inline-json-result.v1`, over the existing M2/M3 authority spine.

Its central causal rule is:

```text
accepted result file bytes == exact retained stdout bytes
```

The exact retained stdout belongs to the digest-bound M2 observation already anchored by M3/M4.3.1. Therefore a file merely appearing after execution is not enough.

Physical evidence additionally requires:

- deterministic run-scoped output path;
- regular-file path with symlink/junction/reparse ambiguity rejected;
- bounded final byte read;
- exact SHA-256 and size computed locally;
- strict canonical JSON result shape;
- manifest-declared metric name/unit;
- finite numeric metric with boolean rejected;
- JSON numeric representation identity preserved (`6` is not `6.0` evidence);
- deterministic artifact/metric identities;
- exact current `sys.executable` for the admitted Python profile;
- recovery that re-reads the physical bytes and fails if they mutate.

Adversarial coverage includes missing output, file/stdout mismatch, output substitution after interruption, post-publication mutation, invalid metric types, metric identity rebinding, integer/float rebinding, alternate-executable admission, and crash-safe finalization without rerun.

Representative regressions live in `tests/test_lab_governed_python.py` and `tests/test_lab_physical_evidence_adversarial.py`.

Merged M4.3.2 commit:

```text
bbf9bd629e2866a760f26bb716ce71c79fd1653a
```

## M4.3.3 — first real experiment and restart closure

M4.3.3 intentionally adds no production runtime abstraction.

Repository-contained experiment fixture:

```text
tests/fixtures/m4_3_3_sum_odds_experiment.py
```

Declared hypothesis:

> For `n=37`, the sum of the first 37 positive odd integers equals `37²`.

Frozen falsification criterion:

> The governed run yields `absolute_error != 0` for the declared input `n=37`.

The end-to-end closure regression in `tests/test_lab_m4_3_3_first_real_experiment.py` performs a real governed run, obtains physical evidence, seals the run and experiment, and then starts a fresh Python process that receives only the durable database path plus the stable run identifier.

That fresh process constructs only recovery registries. It does not construct the governed runner, does not make an authorization decision, and does not invoke the experiment.

It must recover and cross-check:

- experiment/hypothesis/manifest identity and digests;
- exact run digest;
- exact M4.3.1 execution evidence digest;
- exact M4.3.2 physical receipt digest;
- physical artifact SHA-256;
- locally extracted `absolute_error` metric;
- irreversible run seal;
- irreversible experiment seal.

The parent process verifies that the physical output bytes and modification time are unchanged by recovery.

A second regression attempts to prepare the same sealed run in a fresh session and requires rejection before a new M3 process proposal is recorded.

## Adversarial closure matrix

| Required M4.3 threat | Evidence |
| --- | --- |
| input / proposal drift | M4.3.1 exact proposal/argv binding and pre-authority chronology; M4.3.2 manifest-derived exact argv |
| execution mismatch / rebinding | M4.3.1 proposal/receipt/observation lineage regressions |
| missing output | M4.3.2 successful-process-without-output regression |
| output mutation / substitution | M4.3.2 post-crash substitution and post-publication recovery regressions |
| malformed/ambiguous metric | M4.3.2 strict JSON and numeric regressions |
| interrupted lifecycle | M4.3.2 crash-after-observation `finalize(run_id)` regression |
| restart | M4.3.3 fresh Python process reconstructs the sealed chain |
| attempted replay | M4.3.3 sealed-run prepare rejection with no new M3 action |

No one test is treated as sufficient by itself; closure depends on the composition of these independently bounded layers.

## M4.3 milestone exit gate

The planned exit gate is:

> Starting only from durable state after restart, Codexia can reconstruct the exact hypothesis -> manifest -> run -> governed execution -> verified physical evidence chain without trusting conversational history and without rerunning the experiment.

M4.3.3 exercises exactly that path. The fresh process uses durable state plus a stable run identifier for navigation, revalidates the physical artifact, and never executes the experiment.

If the M4.3.3 cross-platform candidate passes the repository's existing CI and CodeQL gates, this audit considers the M4.3 implementation complete at the stated scope.

## Remaining non-claims

M4.3 does not claim:

- scientific truth beyond the declared evidence;
- automatic supported/refuted/inconclusive adjudication;
- multi-run comparison, aggregation, confidence intervals, or statistical testing;
- autonomous experiment generation or scheduling;
- generic arbitrary process authority;
- hostile-kernel or hostile-local-admin attestation;
- content-addressed artifact storage or external anti-rollback anchoring;
- remote execution;
- automatic Git commit/push of experiment results.

Those are either intentionally out of scope or belong to later milestones.

## Next milestone boundary

With M4.3 closed, the next research question is no longer “did this declared run really execute and produce these bytes?”

M4.4 should instead freeze the comparison/falsification policy before opening the evidence it judges: target metric, direction, threshold, repetitions/seeds, aggregation, and missing/failure policy.
