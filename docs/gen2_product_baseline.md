# Gen2 Product Baseline

## Status

DW0 freezes the current Gen2 architecture as the product-development baseline.

Baseline commit at the start of DW0:

```text
3a56b8131e1c7fb43e9d484a650ca372c485aac7
```

This is a product-development baseline, not a claim that the standalone daily-use product is complete.

## Product identity

> Codexia is a portable governed runtime for durable delegated intellectual work.

The unit Codexia owns is **Work continuity**.

Codexia may reason, progress a Workflow, run bounded Role cognition, delegate child Work, request host capabilities, accumulate artifact/evidence references, surface a human-attention boundary, and admit semantic completion.

It does not thereby own external execution authority.

Primary invariants:

```text
model intent != execution authority
worker output != WorkCompletion
AttentionNeed != approval
CapabilityNeed != host authority
CapabilityOutcome.UNKNOWN != retry permission
restart != replay
```

## Current Gen2 semantic runtime

The current baseline includes:

- `Work` and exact ingress identity;
- append-only Work chronology with exact snapshot/CAS semantics;
- `Workflow` / `WorkflowRun`;
- `Role` / `RoleRun` and bounded cognition progression;
- durable child-Work delegation;
- `ContextProjection`;
- `CapabilityNeed` / `CapabilityHandoff` / `CapabilityOutcome`;
- `AttentionNeed` / `AttentionResponse`;
- `ArtifactRef` / `EvidenceRef`;
- `CompletionClaim` / guarded `WorkCompletion`;
- semantic `Pack` membership and exact Pack pinning;
- durable Work-yield projection;
- bounded progression of one existing Work without a generic scheduler.

## Standalone viability proofs

The SV1–SV4 line demonstrates that Gen2 is not only an abstract semantic model.

### SV1

Real standalone process Work vertical from Work ingress through exact Pack/Workflow progression, capability handoff/outcome, evidence, CompletionClaim, and guarded WorkCompletion.

### SV2

Restart-safe recovery of the same standalone Work without redispatching already durable capability handoffs or replaying terminal states.

### SV3

Durable standalone process attempts with one-shot authority consumption and an independent runner that can publish terminal process observations after the parent host exits.

### SV4

Exact runner-ownership reconciliation. A consumed attempt with a live runner remains ambiguous; loss of exact runner ownership without a terminal observation becomes `CapabilityOutcome.UNKNOWN`, never implicit retry permission.

## Durable host-return frontier

Gen2 exposes a read-only durable Work-yield projection:

```text
COMPLETION
    exact guarded WorkCompletion

ATTENTION
    exact current-head AttentionNeed

NONE
    no current durable host-return frontier
```

`NONE` is not a new Work state and does not mean that progress is safe, required, or possible.

## Bounded existing-Work progression

One existing Work can be progressed under an explicit finite budget using the existing Workflow/Role/Capability/Completion services.

Mechanism-level results include:

```text
YIELDED
BOUND_EXHAUSTED
QUIESCENT
TERMINAL_NON_YIELD
```

These are not canonical Work states.

Each semantic step is bound to the exact projected Work frontier. A concurrent append invalidates the stale step before it can continue from the old decision frontier; the caller re-projects the durable Work state.

## Historical substrate remains valuable

M1–M5 and the M6.x / Simple Work line remain part of repository history and contain proven authority, execution, persistence, research, and product lessons.

They are not the normative product architecture for new Gen2 work.

In particular:

- historical PR #20 remains evidence from the M6.6 daily-use pilot effort;
- historical PR #27 documents a pre-Gen2 attempt to refresh product identity;
- useful acceptance criteria from those efforts are carried forward into the DW roadmap rather than forward-porting their implementation wholesale.

## Product-proof roadmap

Current product development is tracked by issue #84:

```text
DW0  baseline / legacy closure
DW1  standalone Gen2 Work surface
DW2  Research Work Pack v1
DW3  real Research Work vertical
DW4  restart + genuine human-attention continuation
DW5  Software Work Pack v1
DW6  general daily-use delegated-work pilot
```

The development rule for DW1–DW6 is failure-driven:

```text
real vertical
→ concrete failure
→ smallest missing invariant
→ bounded repair
→ rerun vertical
```

Do not add a scheduler, daemon, universal task graph, implicit authority root, or HDE-specific Core semantics merely because they might be useful later.

## Integration policy

HDE and Runplane are intentionally not dependencies of the DW product-proof line.

Codexia should first prove that Gen2 is useful standalone.

Major host integrations are reconsidered only after the daily-use product proof, using the live state of those projects at that future point.

## What is not yet proven

DW0 does not claim:

- that a polished standalone Gen2 CLI/product surface already exists;
- that a general Research Work Pack is complete;
- that a Software Work Pack is complete;
- that Gen2 has passed a real general daily-use pilot;
- that HDE integration is complete or currently desirable;
- that Codexia should run as a resident autonomous scheduler.

Those are product questions for DW1–DW6.