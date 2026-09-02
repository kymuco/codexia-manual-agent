# M3.1 — Candidate 8 Pre-Merge Audit

## Candidate 8 executable evidence

Candidate 8 was frozen at:

```text
head: d4578cd0bf3e922778c530dc4fa1d977664c7f65
tree: 0f0faf1993af3cf3d85c903e1746e062c519c506
base: f36f2a54ac79a926dac910ab435981b3aeb6fa7d
```

Target Windows gates were green on that exact tree:

```text
focused: 92 passed, 13 subtests passed in 1.98s
full:    529 passed, 30 skipped, 247 subtests passed in 68.75s (0:01:08)
```

Those results remain valid historical evidence for Candidate 8, but Candidate 8 is
permanently invalid as merge evidence because the final pre-merge review-thread
check found finding #32 below. The explicitly authorized merge was deliberately not
executed.

## Finding 32 — exact tool observations could exceed the durable event budget

Finding #28 correctly moved tool-observation recording before the smaller
model-context observation budget. However, the persistence layer itself remains
bounded: `observation_json` may contain at most `MAX_EVENT_TEXT_CHARS` characters
and the whole event has its own byte ceiling.

`FilesystemWorkspace.search_text()` legitimately scans files up to 1,048,576 bytes.
A matching line can therefore be almost that large, and the deterministic JSON
observation adds structural overhead. The tool can complete successfully while its
exact rendered observation is larger than the M3 event ceiling.

Before this repair, `tool_observation_recorded()` would raise
`SessionEventIntegrityError` after the tool had already executed but before either a
`TOOL_OBSERVATION_RECORDED` or terminal run event became durable. The chronology was
then left with an active run even though the local tool outcome was known, and safe
provider continuation was blocked.

## Repair for finding 32

`SqliteAgentEventRecorder.tool_observation_recorded()` now mirrors the bounded known-
outcome strategy already used for oversized provider responses:

1. normal observations are validated and stored verbatim exactly as before;
2. only if the exact observation cannot fit the M3 text/payload budget, the recorder
   replaces the event's opaque `observation_json` value with a deterministic compact
   JSON marker containing:
   - `observation_storage = digest_only`;
   - exact original character count;
   - exact original UTF-8 byte count;
   - SHA-256 digest of the exact rendered observation;
3. unrelated integrity errors are not caught or converted;
4. the event kind, run id, request id and tool binding are unchanged;
5. deterministic recovery therefore still sees one durable tool event and increments
   the tool counter once, while the marker explicitly states that the original
   observation was too large to retain verbatim.

This keeps the causal invariant:

```text
tool executes
  -> durable known-outcome evidence exists
  -> model-context budget is evaluated
  -> terminal run counters match durable chronology
```

A dedicated regression creates a legal 1,048,576-byte searchable line, drives a real
`ReadOnlyAgentLoop` through `search_text`, and verifies that the oversized exact
observation becomes bounded digest evidence, the ordinary model-context budget path
returns `BUDGET_EXHAUSTED`, `RUN_COMPLETED` is durable, and recovery is `RESUMABLE`
with `tool_calls == 1` rather than an unrecoverable active run.

## Candidate status

Candidate 8 is permanently invalid as merge evidence despite its green Windows
gates. The repaired tree must be frozen under a new exact SHA and receive completely
fresh focused/full Windows validation before any Ready or merge cycle.
