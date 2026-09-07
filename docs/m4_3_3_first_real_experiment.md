# M4.3.3 — First Real Experiment

## Purpose

M4.3.3 closes the M4.3 milestone by using the existing governed-execution and physical-evidence machinery for one complete deterministic computational experiment rather than adding another runtime abstraction.

Primary milestone question:

```text
Can durable state alone reconstruct
hypothesis -> manifest -> run -> governed execution -> verified physical evidence
without conversational history and without rerunning the experiment?
```

## Experiment

Hypothesis:

> For `n=37`, the sum of the first 37 positive odd integers equals `37²`.

Falsification criterion:

> The governed run yields `absolute_error != 0` for the declared input `n=37`.

The repository-contained fixture is:

```text
tests/fixtures/m4_3_3_sum_odds_experiment.py
```

It computes the sum explicitly, computes `n*n`, derives the absolute difference, and emits one canonical JSON result through the already-admitted `python-inline-json-result.v1` profile.

Declared metric:

```text
name:  absolute_error
unit:  integer
input: n=37
```

This experiment is intentionally simple. Its purpose is not mathematical novelty; it is to exercise the complete research evidence chain with a hypothesis whose falsification condition is fixed before execution and whose result is deterministic.

## End-to-end lifecycle under test

`tests/test_lab_m4_3_3_first_real_experiment.py` performs the complete lifecycle:

```text
Hypothesis
-> ExperimentManifest
-> ExperimentRun registered
-> M2 process proposal derived from the manifest
-> external HUMAN ALLOW receipt
-> durable M3 one-shot authority consumption
-> governed ProcessExecutor execution
-> M4.3.1 exact execution evidence
-> physical result.json verified byte-for-byte against retained stdout
-> local absolute_error extraction
-> ArtifactRecord + MetricRecord registration
-> run seal
-> experiment seal
-> fresh Python process recovery from durable SQLite state
```

The fresh recovery process does not construct `GovernedPythonJsonRunner`, does not mint authority, and does not execute the experiment. It only opens the durable registries, reconstructs the run/execution/physical-evidence chain, and re-verifies the physical artifact.

The parent test records the artifact bytes and modification time before fresh-process recovery and verifies that both are unchanged afterwards.

## Replay closure

A second closure regression attempts to prepare the already-sealed run in a fresh runtime/session.

Required result:

```text
sealed run
-> prepare rejected
-> no new M3 process proposal
-> no new authority
-> no replay
```

This is the final M4.3 lifecycle boundary: recovery may inspect and verify completed evidence, but completion never turns into fresh execution authority.

## What this experiment does and does not establish

Demonstrated:

- one real repository-contained experiment can traverse the complete governed M4.3 path;
- the experiment input, exact Python execution request, authority lineage, observed stdout, physical result bytes, metric, and durable run identity remain linked;
- successful physical evidence can be sealed through M4.2;
- a fresh Python process can reconstruct the sealed chain from durable state and a stable run identifier;
- physical evidence recovery re-reads the artifact rather than trusting only persisted metadata;
- recovery does not rerun the experiment;
- an already-sealed run cannot be prepared for replay.

Not claimed:

- the mathematical hypothesis is scientifically interesting;
- one run proves a universal scientific statement;
- M4.3 performs statistical comparison or automatic supported/refuted adjudication;
- Codexia has autonomous experiment planning or scheduling;
- the local OS/kernel is hostile-proof or remotely attested;
- M4.3 adds generic arbitrary process authority.

Comparison and policy-level falsification remain M4.4 concerns.
