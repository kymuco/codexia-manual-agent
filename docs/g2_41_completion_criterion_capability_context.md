# G2.41 — Completion Criterion Capability Context

## Problem discovered by SV1

The first standalone end-to-end vertical exposed a completion seam that the
isolated G2.36–G2.40 tests did not exercise.

A CompletionClaim may use an EvidenceRef as basis. EvidenceAdmission deliberately
records relevance only:

```text
EvidenceRef != verified truth
```

Before G2.41, CompletionCriterionContext contained only:

```text
CompletionClaim
WorkSnapshot
WorkflowRunSnapshot
PackWorkflowBinding
```

Therefore a Pack criterion could inspect the declared EvidenceRef but could not
compare it with the durable CapabilityOutcome that supposedly produced it.

A caller could record a structurally valid, self-consistent EvidenceRef with a
domain-looking evidence kind and then submit a CompletionClaim directly. Generic
basis validation would correctly prove only that the reference was durably
recorded, not that the claimed CapabilityOutcome existed.

## Change

G2.41 appends:

```text
CompletionCriterionContext.capabilities:
    tuple[CapabilityNeedSnapshot, ...]
```

The tuple is derived from the exact Work chronology already bound by the
CompletionClaim and filtered to the exact WorkflowRun being judged.

Each supplied CapabilityNeedSnapshot must remain bound to:

- the same Work identity and digest;
- the same WorkflowRun identity and digest;
- a CapabilityBinding contained by the exact pinned Pack.

The field is appended to preserve the existing positional context shape.

## Concurrency

No new read-set is introduced.

CapabilityNeed and CapabilityOutcome records live in parent Work chronology.

The existing completion checkpoint already binds:

```text
work_revision
+
work_event_digest
```

and the final `completion.claim-admitted` append still uses the existing Work
CAS.

Thus:

```text
late capability outcome/change
→ parent Work revision changes
→ stale CompletionClaim admission fails
```

## Authority boundary

Capability context is derived semantic state only.

G2.41 does not add:

- CapabilityHostPort;
- host selection;
- execution;
- authorization;
- retry permission;
- scheduling;
- WorkCompletion admission.

The completion criterion can observe what happened. It still cannot cause the
capability attempt or manufacture authority.

## Evidence semantics remain unchanged

G2.41 does not redefine EvidenceRef.

```text
EvidenceRef
!= CapabilityOutcome
!= verified truth
!= completion proof
```

A domain Pack may now compare an EvidenceRef against the exact durable
CapabilityOutcome and decide whether that relation is sufficient for its own
completion semantics.

## Proof obligations

Tests cover:

1. exact succeeded CapabilityOutcome is visible to the completion criterion;
2. a forged process-success EvidenceRef without a durable CapabilityOutcome is
   rejected by an outcome-bound criterion;
3. capability state from another WorkflowRun on the same Work is excluded;
4. the new context field is appended for compatibility;
5. no host, execution, scheduler, terminal-authority, or retry surface enters
   Completion Core.
