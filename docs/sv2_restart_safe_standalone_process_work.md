# SV2 — Restart-Safe Standalone Process Work v1

## Purpose

SV2 extends the SV1 standalone process vertical with restart-safe host
composition.

It adds no new Gen2 Core primitive and no new canonical Work lifecycle state.

The governing rule is:

\`\`\`text
recovery reasoning != canonical Work truth
host sequencing != Core scheduler
\`\`\`

## Recovery source of truth

Recovery is derived only from existing durable records:

- Work;
- WorkflowRun;
- PackWorkflowBinding;
- CapabilityNeed;
- CapabilityHandoff;
- CapabilityOutcome;
- EvidenceRef;
- CompletionClaim;
- WorkCompletion.

The standalone host does not persist a separate recovery state.

## Derived checkpoints

\`\`\`text
START_WORKFLOW
PIN_PACK
PROGRESS_CAPABILITY_NEED
DISPATCH_CAPABILITY
AWAITING_OUTCOME_RECONCILIATION
CAPABILITY_FAILED
CAPABILITY_OUTCOME_UNKNOWN
RECORD_OUTCOME_EVIDENCE
PROGRESS_COMPLETION_CLAIM
FINALIZE_WORK
COMPLETED
CANCELLED
\`\`\`

These are host projections, not Work states.

Canonical Work remains:

\`\`\`text
ACTIVE | COMPLETED | CANCELLED
\`\`\`

## Ingress recovery

The existing WorkStore ingress rule is reused:

\`\`\`text
(source_namespace, source_id)
+ exact objective
+ exact ingress binding
→ same canonical Work
\`\`\`

A fresh restart may construct a new detached Work candidate, but WorkStore
returns the already-existing canonical Work when ingress semantics match.

All continuation uses that recovered canonical Work identity.

## Bounded advance

\`StandaloneProcessWorkRecoveryService.advance_once()\` performs at most one
host-level continuation transition after recovering current chronology.

There is no generic scheduler loop.

Typical sequence:

\`\`\`text
call 1 → create Work
call 2 → start WorkflowRun
call 3 → pin Pack
call 4 → admit CapabilityNeed
call 5 → dispatch process capability
call 6 → record exact outcome EvidenceRef
call 7 → admit CompletionClaim
call 8 → guarded WorkCompletion
\`\`\`

A process dispatch may durably record both CapabilityHandoff and a synchronous
CapabilityOutcome because that pair is owned by the existing host bridge.

## Ambiguous handoff rule

The critical recovery boundary is:

\`\`\`text
CapabilityHandoff durable
+
CapabilityOutcome absent
→ AWAITING_OUTCOME_RECONCILIATION
→ never redispatch
\`\`\`

Handoff presence proves only routing, not delivery or execution.

Therefore SV2 does not infer that the process ran and does not infer that it did
not run.

If an external/host-specific reconciliation path later establishes an exact
CapabilityOutcome, it enters through the existing:

\`\`\`text
CapabilityHostBridge.record_outcome(...)
\`\`\`

No second effect attempt is manufactured.

## Terminal capability outcomes

\`\`\`text
FAILED
→ CAPABILITY_FAILED
→ stable no-op under advance_once

UNKNOWN
→ CAPABILITY_OUTCOME_UNKNOWN
→ stable no-op under advance_once
\`\`\`

In particular:

\`\`\`text
Outcome.UNKNOWN != retry permission
\`\`\`

## Pure recovery

\`recover(work_id)\` performs projection only.

It does not:

- resolve Invariant providers;
- call a capability host;
- request authority;
- execute a process;
- evaluate a completion criterion;
- mutate Work.

This allows already-durable Work to remain inspectable even when technical
providers are temporarily unavailable.

## Completion recovery

For a completed process Work, recovery requires the exact chain:

\`\`\`text
SUCCEEDED CapabilityOutcome
→ exact outcome EvidenceRef
→ exact admitted CompletionClaim
→ structured WorkCompletion
→ Work state COMPLETED
\`\`\`

The recovered checkpoint can be converted back into the same
\`StandaloneProcessWorkResult\`.

## Proof obligations

SV2 tests prove:

1. repeated restart after every exposed durable boundary continues the same
   Work identity;
2. each semantic event is admitted once;
3. completed Work is an exact no-op on later start-or-resume calls;
4. a durable handoff without outcome becomes
   AWAITING_OUTCOME_RECONCILIATION and does not redispatch;
5. a reconciled outcome can continue through evidence, claim and completion
   without another effect attempt;
6. pure recovery succeeds without resolving provider code;
7. FAILED process outcome remains durable and is never retried automatically.
