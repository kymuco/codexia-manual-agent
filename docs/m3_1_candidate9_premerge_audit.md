# M3.1 — Candidate 9 Pre-Merge Audit

## Candidate 9 executable evidence

Candidate 9 was frozen at:

```text
head: 4b16b3eb0b85e2528acb6e96608bedc1b6154db2
tree: a54dd47353742a19e2980624c0143bc702de3ee4
base: f36f2a54ac79a926dac910ab435981b3aeb6fa7d
```

Target Windows gates were green on that exact tree:

```text
focused: 94 passed, 13 subtests passed in 2.03s
full:    531 passed, 30 skipped, 247 subtests passed in 76.85s (0:01:16)
```

Those results remain valid historical evidence for Candidate 9, but Candidate 9 is
permanently invalid as merge evidence because the final pre-merge review-thread
check found finding #33 below. The explicitly authorized merge was deliberately not
executed.

## Finding 33 — rendered provider request could exceed the durable event budget after run publication

`RUN_STARTED` stored the raw task, while the first provider request stored the
rendered prompt produced by `build_initial_task_prompt()`. A task near the M3 text
ceiling could therefore fit the run event but exceed the request-event ceiling once
wrapper text was added. Before the repair, the recorder rejected
`MODEL_REQUEST_STARTED` after `RUN_STARTED` had already committed and before the
provider was contacted. Recovery then saw an open run and conservatively refused
normal continuation even though no external provider side effect had happened.

The same class of failure could occur on a later turn if a configured model-context
observation budget allowed a rendered observation prompt larger than the M3 request
representation.

## Repair for finding 33

The recorder protocol now exposes a side-effect-free `preflight_model_request()`.
`SqliteAgentEventRecorder` validates the exact request representation with fixed-size
placeholder UUIDs and the persisted provider binding before any event is published.
Only request-text/payload budget failures are translated to `ProtocolError`; NUL,
structural, conversation-metadata, and unrelated integrity failures remain
fail-closed.

`ReadOnlyAgentLoop` now uses the preflight in two places:

1. the rendered initial `ProviderRequest` is preflighted before `RUN_STARTED` is
   published; a bounded-representation failure returns `BUDGET_EXHAUSTED` with no
   durable run and no provider call;
2. every later rendered provider request is preflighted before
   `MODEL_REQUEST_STARTED`; if it cannot fit, the already-open run is durably closed
   as `BUDGET_EXHAUSTED` before any new provider call.

This preserves the causal boundary:

```text
render provider request
  -> prove durable representation fits
  -> publish run/request chronology
  -> call provider
```

Dedicated regressions cover both the near-limit initial task and a later request
constructed after a large legal `search_text` observation. In both cases no provider
side effect occurs for the rejected request and deterministic recovery remains
resumable rather than being stranded on an open run.

## Candidate status

Candidate 9 is permanently invalid as merge evidence despite its green Windows
gates. The repaired tree must be frozen under a new exact SHA and receive fresh
focused/full Windows validation before any Ready or merge cycle.
