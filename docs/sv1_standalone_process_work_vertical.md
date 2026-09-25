# SV1 — Standalone Process Work Vertical v1

## Purpose

SV1 is the first end-to-end host composition proof over the completed Gen2
semantic Core.

It does **not** introduce G2.41 or another Core primitive.

The proof composes the existing boundaries into one explicit standalone path:

```text
standalone ingress
→ Work
→ WorkflowRun
→ exact Pack pin
→ Workflow progression
→ CapabilityNeed
→ durable CapabilityHandoff
→ standalone process host
→ CapabilityOutcome
→ EvidenceRef
→ Workflow progression
→ CompletionClaim
→ Pack completion criterion
→ completion.claim-admitted
→ WorkCompletion
→ guarded work.completed
```

## Versioning

The historical G2 standalone process example remains unchanged:

```text
Workflow 1.0.0
Pack 1.0.0
provider 7.3.0
```

SV1 uses a new semantic build:

```text
Workflow 2.0.0
Pack 2.0.0
provider 8.0.0
```

The process Capability contract remains `1.0.0` because its request/host
contract is unchanged.

## Outcome evidence

A successful process CapabilityOutcome is referenced as:

```text
EvidenceRef
  evidence_id     = CapabilityOutcome.outcome_id
  evidence_digest = CapabilityOutcome.outcome_digest
  evidence_kind   = codexia.capability-outcome.succeeded.v1
  locator         = work-event://<work_id>/<outcome_id>
```

The host records that EvidenceRef in Work chronology.

The v2 Workflow will not propose CompletionClaim until it observes the exact
outcome reference in `WorkflowStepContext.evidence_refs`.

The CompletionClaim uses that exact EvidenceRef as its completion basis.

This preserves:

```text
CapabilityOutcome != EvidenceRef
EvidenceRef != verified truth
EvidenceRef relevance != completion
CompletionClaim != WorkCompletion
```

## Completion criterion

The Pack-defined completion criterion requires the exact SV1 claim shape:

- exact completion summary;
- no ArtifactRef basis;
- no child WorkCompletion basis;
- exactly one process-success EvidenceRef;
- exact Work-event locator shape.

Criterion acceptance produces only:

```text
completion.claim-admitted
```

It does not publish `work.completed`.

## Terminal transition

The standalone composition root explicitly creates WorkCompletion from the
current admitted claim and submits it through:

```text
WorkCompletionAdmissionService
→ private owned-child terminal guard
→ private completion store primitive
→ work.completed
```

No direct WorkStore completion writer is added.

## Host ownership

`StandaloneProcessWorkService` is deliberately domain-specific and finite.

It owns the explicit standalone sequencing policy for this one vertical. It is
not canonical Work truth and it introduces no:

- Scheduler;
- Queue;
- generic runnable state;
- retry loop;
- Core background driver;
- HDE policy.

The Gen2 Core remains host-neutral.

## Proof obligations

SV1 tests must demonstrate:

1. v2 semantic identity does not rewrite the frozen v1 example;
2. real standalone process execution reaches structured WorkCompletion;
3. completion basis references the exact successful CapabilityOutcome;
4. denied process authority records the failed outcome but never fabricates
   EvidenceRef, CompletionClaim, or WorkCompletion;
5. the standalone composition contains no generic scheduler loop;
6. the old `workflow.completed` shortcut is absent from the v2 vertical.
