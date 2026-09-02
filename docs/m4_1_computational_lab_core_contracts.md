# M4.1 — Computational Lab Core Contracts

## Purpose

M4.1 establishes the first computational-lab data boundary for Codexia.

The milestone does not execute experiments. It defines immutable, bounded,
digest-bound records that make later experiment execution, comparison, artifact
registration and evidence review reproducible instead of conversational.

Primary invariant:

```text
conclusion provenance may not claim evidence outside its exact experiment lineage
```

A second equally important non-claim is:

```text
digest-bound evidence provenance does not prove scientific truth
```

M4.1 can prove which declared hypothesis, manifest, run and evidence records a
conclusion references. It cannot prove that a metric is scientifically useful,
that an experimental procedure is unbiased, or that the conclusion text is the
only reasonable interpretation of the evidence.

## Contracts

### Hypothesis

`Hypothesis` binds:

- a UUID identity;
- timezone-aware creation timestamp;
- bounded hypothesis statement;
- explicit bounded falsification criterion;
- SHA-256 digest of the exact canonical payload.

The falsification criterion is required. A free-form claim without any stated
condition under which it could fail is not admitted as an M4.1 hypothesis.

### ExperimentManifest

`ExperimentManifest` binds one exact hypothesis to:

- experiment UUID;
- timezone-aware creation timestamp;
- bounded procedure description;
- deeply immutable JSON-compatible parameter object;
- canonical parameter ordering;
- finite-number-only parameter values;
- 64 KiB canonical parameter budget;
- exact manifest SHA-256 digest.

The manifest does not grant process, filesystem, network, Git or provider
authority. It is a declaration of intended experimental configuration only.

### ExperimentRun

`ExperimentRun` is immutable run identity, not an execution receipt.

It binds:

- run UUID;
- exact experiment UUID and manifest digest;
- non-negative run ordinal;
- optional integer seed;
- timezone-aware creation timestamp;
- exact run digest.

M4.1 does not claim that constructing an `ExperimentRun` means the experiment
actually ran. Later M4 work must introduce durable run lifecycle and execution
evidence separately.

### ArtifactRecord

`ArtifactRecord` describes evidence produced by a run without reading or mutating
the filesystem itself.

It binds:

- exact run UUID, run digest and manifest digest;
- logical relative POSIX artifact path;
- exact artifact byte size;
- lowercase SHA-256 content digest;
- optional bounded media type;
- artifact-record digest.

Logical paths reject absolute paths, traversal, backslashes and non-canonical
separator forms. This is record canonicalization only, not a filesystem sandbox
or workspace-admission authority.

### MetricRecord

`MetricRecord` binds:

- exact run UUID, run digest and manifest digest;
- bounded canonical metric name;
- finite integer or float value;
- optional bounded unit;
- exact metric-record digest.

Boolean values and NaN/infinity are rejected. Metric meaning is not inferred by
M4.1; a recorded number remains evidence whose interpretation must be explicit.

### Conclusion

`Conclusion` has exactly three v1 verdicts:

- `supported`;
- `refuted`;
- `inconclusive`.

The constructor used for normal M4.1 creation requires the exact `Hypothesis` and
`ExperimentManifest`, verifies their digest binding, and accepts only
`MetricRecord` / `ArtifactRecord` evidence whose `manifest_digest` matches that
exact manifest.

Evidence digests are sorted canonically, duplicate evidence is rejected, and
`SUPPORTED` / `REFUTED` conclusions require at least one bound evidence record.
An empty evidence set is admitted only for `INCONCLUSIVE`.

The conclusion digest binds:

- exact hypothesis identity/digest;
- exact experiment identity/manifest digest;
- verdict;
- bounded summary;
- exact canonical metric evidence digests;
- exact canonical artifact evidence digests.

This prevents accidental or silent cross-experiment evidence mixing in the
contract construction path. It does not certify the semantic quality of the
summary or verdict.

## Canonicalization and bounds

M4.1 uses Python v1 canonical JSON rules for digest payloads:

- UTF-8;
- sorted object keys;
- compact separators;
- JSON-compatible values only;
- finite floats only;
- deep immutable manifest parameters;
- lowercase 64-character SHA-256 hex digests;
- explicit bounded text and evidence-count limits.

All public records are frozen dataclasses with exact digest revalidation in
`__post_init__`, so direct reconstruction with a modified payload and stale digest
fails closed.

## Authority boundary

M4.1 introduces no new `Capability` and no authority-bearing API.

It does not:

- execute a process;
- call a model/provider;
- read or write an artifact path;
- create an M2.x proposal or authorization receipt;
- commit or push Git state;
- launch delegated work;
- schedule autonomous experiments.

Existing M2.x authority remains the only path for governed side effects. M5
remains the milestone for bounded automation.

## Validation surface

`tests/test_lab_contracts.py` covers the initial contract boundary:

- hypothesis payload tamper rejection;
- deterministic manifest parameter canonicalization;
- deep detachment from later caller mutation;
- non-finite manifest value rejection;
- exact run/manifest digest binding;
- artifact traversal/non-canonical path rejection;
- boolean/NaN/infinite metric rejection;
- no-evidence `supported` / `refuted` rejection;
- empty-evidence `inconclusive` admission;
- cross-manifest evidence rejection;
- manifest/hypothesis rebinding rejection;
- duplicate evidence rejection;
- deterministic evidence ordering;
- evidence-type separation.

## Explicit non-claims / next work

M4.1 intentionally does not yet provide:

- persistent experiment registry;
- durable run lifecycle (`planned/running/completed/failed/...`);
- execution receipts or subprocess integration;
- physical artifact verification against disk;
- metric schema/aggregation semantics;
- baseline or multi-run comparison;
- confidence intervals/statistical testing;
- conclusion policy based on declared thresholds;
- model-generated research plans;
- autonomous exploration.

The next logical slice is M4.2: a durable experiment/run/evidence registry with
exact append/recovery semantics, followed by explicit comparison and conclusion
policy in later M4.x slices.
