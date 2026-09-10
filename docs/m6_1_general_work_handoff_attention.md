# M6.1 — General Work Handoff and Attention Boundary

## Goal

M6 starts the transition from bounded scientific automation to general delegated work continuity.

The first problem is not background scheduling yet. It is to make an arbitrary human work handoff exact enough that later orchestration can continue it without confusing model output, inferred intent, attention judgment, or resource availability with human instruction or execution authority.

M6.1 is deliberately not project-specific. A handoff may describe an ongoing repository milestone, a standalone research question, a document-preparation task, an application to build, or another bounded piece of work.

## M6 direction

The overall M6 product direction is:

```text
human absence != work suspension
```

M6.1 does not yet prove that property operationally. It establishes the semantic boundary needed before M6.2+ can safely decide and execute continuation.

## Primary invariants

```text
human handoff != inferred work interpretation
worker statement != human instruction
resource reference != access authority
dynamic attention judgment != execution authority
plan != prerequisite for delegation
```

A related product rule is:

```text
inferred work depth != permanent project mode
```

M6.1 therefore does not add `PROBE`, `MVP`, or `PRODUCTION` modes. `depth_interpretation` is bounded attributed text derived from the exact handoff. Later cognition may interpret phrases such as "quickly test the idea" or "make this production-grade" without forcing every work item into a global enum.

## Package boundary

M6.1 introduces:

```text
codexia_manual_agent.work
```

This is intentionally separate from:

- `codexia_manual_agent.delegation`, whose M2.6 responsibility is bounded capability/budget delegation and escalation;
- `codexia_manual_agent.lab`, whose M4/M5 responsibility is governed computational experiments and bounded scientific automation.

The new package is semantic work-continuity state. It does not create process, filesystem, Git, network, provider, or scheduler authority.

## WorkStatement

`WorkStatement` preserves the source of text through a digest-bound record:

```text
human
codexia
worker
system
```

This is the first explicit protection against Codexia using a website input box and later losing the fact that a continuation message was authored by Codexia rather than the human.

A worker may say:

```text
"The next logical step is PR78."
```

but that statement cannot be used as the HUMAN-authored objective of a `WorkHandoff`.

The boundary is:

```text
worker proposal != human instruction
```

## WorkResourceRef

A handoff may name context sources such as:

```text
chat
repository
file
URL
other locator
```

A resource reference is only a locator. It makes no claim that Codexia can currently read, mutate, authenticate to, or otherwise use that resource.

```text
resource reference != capability != authority
```

This lets the same handoff eventually bind a ChatGPT thread, GitHub repository, uploaded archive, roadmap, or other source without moving access policy into the semantic record.

## WorkHandoff

`WorkHandoff` is the immutable human-authored delegation record.

It contains:

```text
human objective
context statements
human constraints
resource references
optional plan resource
human attention constraints
```

The objective must be explicitly attributed to `human`. Human constraints and hard attention constraints must also preserve human authorship.

A plan is optional. This is intentional:

```text
well-defined goal + context
can be delegated
without a prewritten plan
```

Later M6 work may derive a plan or accept a worker-proposed next action, but that derived structure does not become part of the original human handoff identity.

## Human handoff and interpretation are separate records

A critical M6.1 boundary is that `WorkIntentInterpretation` is **not embedded in `WorkHandoff`**.

Instead:

```text
WorkHandoff
    │ exact handoff_id + handoff_digest
    ▼
WorkIntentInterpretation
```

The interpretation binds:

- the exact handoff identity/digest;
- an exact subset of attributed handoff statements, including the human objective;
- an attributed interpreter that cannot be `human`;
- a completion expectation;
- a continuation-scope interpretation;
- a free-text depth interpretation.

This means Codexia can revise its understanding without rewriting what the human actually delegated:

```text
same WorkHandoff
    ├── interpretation A
    └── interpretation B
```

The two interpretations have different digests while the human handoff digest remains unchanged.

This distinction matters because model interpretation is fallible and may evolve as new context arrives.

## Attention constraints versus dynamic attention judgment

Human-authored `attention_constraints` live on the handoff. They are durable statements of what the human explicitly asked about interruption/attention.

`AttentionAssessment` is different. It represents a dynamic cognitive judgment at one exact work checkpoint:

```text
exact WorkHandoff
+ exact WorkIntentInterpretation
+ exact checkpoint digest
→ AttentionAssessment
```

It records:

```text
needs_human
confidence_basis_points
urgency
reason
optional requested_response
```

The assessment is attributed to `codexia`, a `worker`, or `system`; it cannot impersonate the human.

A no-attention assessment cannot simultaneously carry a human response request, and it must use `urgency=none`.

The record deliberately has no permission, approval, capability, receipt, or execution-authority field.

```text
dynamic attention judgment != permission
```

Future M6 logic can combine human constraints, current evidence, reversibility, alternatives, and model judgment to decide whether to interrupt. M6.1 only defines the exact record boundary.

## Strict decoding and integrity

Every public record is:

- immutable (`frozen=True`, `slots=True`);
- schema-versioned;
- structurally bounded;
- canonical-JSON digest-bound;
- strict about exact decoder keys;
- strict about canonical UUID/timestamp/digest forms.

Tampering with attributed text while retaining its old digest fails closed. Extra fields such as a forged `authority_granted` key fail strict decoding rather than being silently ignored.

## Demonstrated M6.1 regressions

The M6.1 test slice covers both project and non-project work:

1. A standalone research request is a valid handoff with no plan.
2. An ongoing project handoff can reference an existing chat, repository, and roadmap plan.
3. Reinterpreting depth/scope creates a new interpretation digest without changing the human handoff digest.
4. Worker/Codexia text cannot become a HUMAN objective by relabelling orchestration state.
5. Derived intent cannot claim HUMAN authorship.
6. Hard handoff attention constraints preserve HUMAN authorship.
7. An interpretation cannot use statements from another handoff.
8. Handoff round-trip is exact and payload tamper fails closed.
9. Dynamic attention binds the exact handoff, interpretation, and checkpoint.
10. An attention assessment for one handoff cannot be rebound to another.
11. `needs_human=false` cannot smuggle a human response request.
12. Dynamic attention cognition cannot be attributed to HUMAN.

## Non-goals

M6.1 does not add:

- continuation admission (`ADMIT / REVISE / REJECT / ASK_HUMAN`);
- ChatGPT website orchestration;
- provider/background workers;
- a background supervisor or scheduler;
- durable work persistence/recovery;
- notification transport;
- attention-learning profiles;
- local PC execution;
- automatic plan generation;
- a new authority system;
- TUI/UI work.

These remain downstream M6 concerns.

## Exit gate

M6.1 is complete when the repository proves that:

```text
arbitrary human work
→ exact immutable WorkHandoff
→ separate exact inferred interpretation
→ exact checkpoint-bound dynamic attention judgment
```

while preserving:

```text
human authorship
!= worker authorship
!= inferred intent
!= attention judgment
!= execution authority
```

The next direct milestone is M6.2: continuation admission — deciding whether a worker-proposed next action is actually justified by the current handoff and interpretation.
