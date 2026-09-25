# SV3 — Durable Standalone Process Attempt v1

## Purpose

SV3 gives the restart-safe standalone process vertical a durable host-side
source of execution evidence.

It does not add a Gen2 Core primitive.

The new ownership boundary is:

\`\`\`text
CapabilityHandoff
→ standalone host
→ durable ProcessAttempt
→ durable one-shot authority consumption
→ independent runner process
→ terminal ProcessExecutionObservation
→ reconciliation
→ existing CapabilityOutcome admission
\`\`\`

## Semantic ownership

The attempt belongs to the standalone host.

It is not:

- Work lifecycle truth;
- a Workflow state;
- a Session;
- an ExperimentRun;
- a new Capability Core record;
- retry permission.

The attempt identity is derived from the exact CapabilityHandoff and exact
CapabilityNeed/Work binding.

\`\`\`text
one CapabilityHandoff
→ at most one canonical durable ProcessAttempt
\`\`\`

The first durable local proposal/authorization pair wins. A concurrent or
restarted caller cannot replace that authority identity with a new in-memory
receipt.

## Durable attempt states

\`\`\`text
AUTHORIZED_UNCONSUMED
DENIED
AUTHORITY_CONSUMED
OBSERVED
REJECTED_BEFORE_CONSUME
ERROR_AFTER_CONSUME
\`\`\`

These are standalone-host execution evidence states, not Work states.

## Authority boundary

Local approval policy remains owned by LocalApprovalAuthority.

The parent host may decide and persist an AuthorizationReceipt, but it does not
consume the receipt.

Consumption happens inside the independent runner through
SqliteStandaloneProcessAttemptStore, which implements the existing
AuthorizationConsumptionRegistryProtocol shape.

Therefore:

\`\`\`text
CapabilityHandoff != authorization
authorization receipt != authority consumption
authority consumption != observed execution
ProcessExecutionObservation != CapabilityOutcome
\`\`\`

The durable consumption row is deliberately interpreted as:

\`\`\`text
authority was consumed immediately before an external effect could start
\`\`\`

It does not claim that subprocess creation succeeded.

## Independent runner

The parent launches:

\`\`\`text
python -m codexia_manual_agent.standalone_host.process_attempt_runner
\`\`\`

with only:

- durable journal path;
- exact attempt id.

The runner reconstructs the exact proposal and receipt from durable state,
verifies the existing LocalApprovalAuthority binding, consumes the exact
single-use receipt durably, executes through the existing ProcessExecutor, and
records the exact ProcessExecutionObservation.

The runner is launched in an independent process group/session and uses no
parent stdout/stderr pipes. Parent death therefore does not inherently remove
its ability to publish the terminal observation.

## Crash windows

### Crash before attempt preparation

\`\`\`text
CapabilityHandoff durable
ProcessAttempt absent
\`\`\`

No SV3 runner could have crossed the effect boundary because runner launch is
strictly after durable attempt preparation.

Recovery may prepare the same handoff.

### Crash after preparation, before consumption

\`\`\`text
ProcessAttempt AUTHORIZED_UNCONSUMED
\`\`\`

Recovery may launch the same durable attempt again.

Multiple runner processes may race, but durable single-use receipt consumption
has one winner. Losing runners cannot execute the external process.

### Crash after consumption, before terminal observation

\`\`\`text
ProcessAttempt AUTHORITY_CONSUMED
observation absent
\`\`\`

No runner is launched again.

The state remains AWAITING_OUTCOME_RECONCILIATION at the SV2 layer.

This is intentionally ambiguous:

\`\`\`text
effect may have started
!= safe retry
\`\`\`

### Terminal observation durable

\`\`\`text
ProcessAttempt OBSERVED
→ exact ProcessExecutionObservation
→ reconcile to SUCCEEDED / FAILED CapabilityOutcome
→ CapabilityHostBridge.record_outcome(...)
\`\`\`

No second CapabilityHandoff and no second process attempt are created.

### Runner error before consumption

\`\`\`text
REJECTED_BEFORE_CONSUME
→ CapabilityOutcome.FAILED
\`\`\`

No external effect authority was consumed.

### Runner error after consumption

\`\`\`text
ERROR_AFTER_CONSUME
→ CapabilityOutcome.UNKNOWN
\`\`\`

The host does not manufacture success, failure, or retry permission.

## Relationship to M3/M4

Older Codexia code already proved useful invariants around durable authority
consumption and execution observations.

SV3 reuses the low-level authority/execution contracts, but does not make Gen2
Work depend on Session or ExperimentRun semantics.

Production SV3 modules must not import:

- session_events;
- lab execution registries;
- ExperimentRun.

## SV2 integration

When no attempt store is configured, SV2 behavior remains unchanged.

When a SqliteStandaloneProcessAttemptStore is configured:

\`\`\`text
DISPATCH_CAPABILITY
→ DurableStandaloneProcessCapabilityPort
\`\`\`

and:

\`\`\`text
AWAITING_OUTCOME_RECONCILIATION
→ attempt absent
   → prepare same handoff

→ AUTHORIZED_UNCONSUMED
   → launch same attempt

→ AUTHORITY_CONSUMED
   → no-op / wait

→ OBSERVED
   → record exact CapabilityOutcome

→ REJECTED_BEFORE_CONSUME
   → record FAILED CapabilityOutcome

→ ERROR_AFTER_CONSUME
   → record UNKNOWN CapabilityOutcome
\`\`\`

## Proof obligations

SV3 must prove:

1. one durable attempt per exact CapabilityHandoff;
2. first durable proposal/receipt identity cannot be replaced;
3. durable authority consumption has one winner;
4. a prepared unconsumed attempt can resume without a new handoff or attempt;
5. a consumed attempt without observation never launches another runner;
6. an independent runner can publish terminal observation after its launcher
   process exits;
7. an observed attempt reconciles through CapabilityHostBridge.record_outcome
   without replay;
8. the full SV1/SV2 Work vertical can still reach WorkCompletion;
9. production SV3 code does not import Session/Experiment semantics.
