# M4.3 — Governed Experiment Execution Plan

## Why this is the next milestone

Codexia already has the pieces needed to represent hypotheses, manifests, runs, metrics, artifacts, conclusions, durable authority, controlled local execution, and durable experiment chronology. What it does not yet have is a trustworthy bridge between those two worlds.

M4.2 can prove that a run or evidence record was registered. It cannot prove that one actual governed process execution produced the bytes and metrics attributed to that run.

M4.3 exists to close that gap.

Primary invariant:

```text
declared run != executed run
```

A registered `ExperimentRun` is an intended run identity. It becomes execution-backed evidence only when an exact M2-governed execution is durably bound to it and the resulting physical outputs are verified before registration.

## Milestone question

M4.3 should answer one question:

> Can Codexia take one declared computational experiment through real governed execution and recover a sealed evidence chain that proves exactly what was run and which physical outputs were accepted as evidence?

If the answer is no, the milestone should expose why rather than adding abstraction around the failure.

## First vertical slice

The first experiment profile is deliberately narrow. M4.3 v1 is not a generic scheduler or arbitrary-code sandbox.

A v1 experiment uses:

- one admitted local Python interpreter/executable identity;
- structured argv with `shell=False`;
- a canonical workspace-bound experiment cwd;
- a bounded manifest-derived parameter/input surface;
- one bounded machine-readable result format;
- explicitly declared output artifacts;
- existing M2 authority and containment contracts.

The model may propose the experiment, but it does not gain execution authority by constructing an M4 record.

## Target lifecycle

```text
Hypothesis
-> ExperimentManifest
-> ExperimentRun registered
-> exact execution request derived
-> M2 PROPOSED
-> AUTHORIZED
-> one-shot receipt consumed
-> process EXECUTED
-> exact execution OBSERVED
-> physical outputs verified
-> MetricRecord / ArtifactRecord registered
-> run sealed
-> durable recovery verifies the whole chain
```

Important negative form:

```text
registered run
!= authorized execution
!= successful execution
!= verified evidence
```

Each transition must remain explicit.

## Evidence required from execution

M4.3 should introduce an execution evidence record or equivalent durable contract that binds at least:

- exact experiment/run identity and digests;
- exact executable identity already admitted by the M2 execution path;
- exact argv;
- exact canonical cwd;
- declared experiment inputs or their exact digests;
- authority proposal/receipt lineage sufficient to prove governed admission without treating the model as the authority source;
- start/end or equivalent bounded execution chronology;
- terminal process outcome, including exit code and timeout/containment failure state;
- exact stdout/stderr observation digests or an explicitly bounded equivalent where retained;
- exact output-verification result.

The execution evidence contract must not imply that exit code zero proves scientific validity.

## Physical artifact verification

M4.2 artifact registration currently records metadata supplied by the caller. M4.3 must verify physical output bytes before those bytes become execution-backed evidence.

For each declared output artifact admitted by v1:

1. resolve the expected path under the governed experiment output boundary;
2. reject traversal, redirected paths, symlink/junction ambiguity, or paths outside the admitted boundary;
3. read the final physical bytes after execution;
4. enforce a bounded byte budget;
5. calculate exact size and SHA-256 from those bytes;
6. construct/register `ArtifactRecord` from the observed size/digest rather than model-supplied claims;
7. ensure later mutation cannot silently rewrite the evidence claim without failing verification/recovery.

M4.3 does not need content-addressed storage or external anti-rollback anchoring to prove this first slice.

## Metric capture

The first metric path should be intentionally boring and auditable.

Prefer one bounded machine-readable result artifact (for example canonical JSON) from which Codexia locally extracts one or more declared numeric metrics. The model does not get to supply the final metric value after observing prose output.

Metric extraction must:

- use a declared schema/name mapping;
- reject NaN/infinity/boolean-as-number and malformed values;
- bind the metric to the exact execution-backed run;
- fail closed if the result artifact is missing, malformed, ambiguous, or outside budget.

General statistical aggregation belongs to M4.4, not M4.3.

## Recovery semantics

Recovery must reconstruct execution-backed state without rerunning anything.

After restart it must remain possible to distinguish at least:

- run registered, execution never authorized;
- authorized but execution outcome unknown/unfinished where applicable;
- execution failed;
- execution succeeded but required evidence verification failed;
- execution and evidence verified, run still open;
- execution-backed run sealed.

No recovery path may mint fresh M2 authority or replay a process automatically.

## Three delivery slices

### M4.3.1 — Execution evidence and lifecycle contract

Goal: make the distinction between declared run and executed run explicit and durable.

Deliverables:

- execution evidence/lifecycle contract bound to exact M4 run identity;
- durable registration/recovery semantics for execution outcome;
- explicit states that cannot collapse `registered`, `authorized`, `executed`, and `verified` into one claim;
- tamper/rebinding/replay tests;
- no new generic execution capability.

Exit gate:

> A fake or mismatched execution receipt cannot make an arbitrary registered run appear execution-backed.

### M4.3.2 — Governed runner and physical evidence capture

Goal: connect one narrow experiment profile to the existing M2 execution spine.

Deliverables:

- exact Python experiment request derivation;
- M2-governed execution using existing authority rather than an M4 bypass;
- physical artifact verification from final bytes;
- bounded local metric extraction from a declared machine-readable output;
- failure/drift/path-mutation tests;
- recovery never reruns the process.

Exit gate:

> Codexia can prove which admitted execution produced which accepted artifact bytes and metric values, or fail closed when that chain cannot be proven.

### M4.3.3 — First real end-to-end experiment and milestone closure

Goal: use the system rather than add another abstraction layer.

Deliverables:

- one small deterministic repository-contained experiment fixture with a real hypothesis and falsification criterion;
- at least one actual governed run through the complete M4.3 lifecycle;
- sealed durable evidence recovered from a fresh process/session;
- adversarial tests for input drift, output mutation, missing outputs, execution mismatch, interrupted lifecycle, and attempted replay;
- source audit and closure document recording both demonstrated properties and remaining non-claims.

Exit gate:

> Starting only from durable state after restart, Codexia can reconstruct the exact hypothesis -> manifest -> run -> governed execution -> verified physical evidence chain without trusting conversational history and without rerunning the experiment.

If this gate requires large new infrastructure unrelated to that proof, stop and reassess the architecture instead of expanding the milestone indefinitely.

## Explicit non-goals for M4.3

Do not add these merely because they are adjacent:

- autonomous experiment planning;
- unattended experiment loops;
- scheduler/queue infrastructure;
- generic arbitrary process authority;
- a new sandbox competing with M2;
- baseline comparison or multi-run statistics;
- confidence intervals or hypothesis tests;
- automatic supported/refuted adjudication;
- remote experiment execution;
- Git commit/push as part of experiment completion;
- TUI/visualization work;
- broad cleanup or hardening unrelated to a demonstrated M4.3 blocker.

These belong to later milestones or separate narrowly justified hardening work.

## After M4.3

If the vertical slice demonstrates practical value, M4.4 should freeze comparison/falsification policy before inspecting the evidence it judges. Only after the manual research loop is proven should M5 automate bounded exploration over that same loop.

The intended direction is therefore:

```text
M4.3 real execution-backed evidence
-> M4.4 frozen comparison and falsification
-> M4.5 reproducible conclusion adjudication, if useful
-> M5 bounded autonomous exploration
```

## Anti-drift criterion

A proposed M4.3 change should answer at least one of these:

1. Does it make the declared-run -> actual-execution binding trustworthy?
2. Does it make actual output bytes/metrics trustworthy evidence for that run?
3. Does it make that chain durably recoverable without replay?
4. Does it directly falsify an assumption required by one of the above?

If the answer to all four is no, the change is probably outside the critical path for M4.3.
