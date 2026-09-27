# DW1 — Standalone Gen2 Work Surface

## Purpose

DW1 exposes the current Gen2 durable Work runtime as a small standalone product surface without adding a scheduler, a parallel Work ontology, or a new authority root.

Product commands:

```text
codexia work start
codexia work status
codexia work advance
codexia work inspect
codexia work answer
```

The nested `work` namespace avoids changing the historical root `codexia inspect` workspace-inspection command.

## Product boundary

DW1 composes existing Gen2 primitives:

- `SqliteWorkStore`;
- `Work` / `WorkIngressBinding`;
- `WorkflowRun` and exact Pack pinning;
- `BoundedExistingWorkProgressionService`;
- durable Work-yield projection;
- `AttentionResponse` admission.

It introduces no new canonical Work state.

## Explicit runtime selection

`start` and `advance` require:

```text
--host-factory module:function
--provider-ref <exact technical provider ref>
--workflow-id <exact semantic workflow id>
--workflow-version <exact semantic workflow version>
```

`--host-factory` is explicit trusted local composition. It is not plugin discovery.

The factory must return `StandaloneWorkHost`, which may supply the existing:

- Invariant `ManagedPluginServicePort`;
- cognition port;
- capability host port;
- Role instructions material source;
- ContextProjection material source.

Codexia does not scan modules, package entry points, manifests, or directories to discover a provider. Invariant Runtime remains the owner of technical extension discovery/lifecycle.

Technical `provider_ref` is product runtime configuration, not canonical Pack semantics.

## start

`start` creates one Work with standalone ingress identity and establishes only the exact activation frontier:

```text
Work
→ workflow.started
→ pack.workflow-bound
→ STOP
```

It does not execute the first Workflow proposal.

An exact retry with the same source/objective/selector recovers the same Work. If a crash happened after only `workflow.started`, `start` admits only the missing Pack pin.

Once activation is already durable, `start` does not require provider availability.

## status

`status` is read-only and provider-independent.

It exposes:

- Work identity/state/revision/ingress;
- exact Workflow binding and Pack pin;
- current durable yield;
- bounded unresolved Role/Capability/child-Work summaries with exact totals;
- latest Work event;
- event and response counts.

Provider availability is not required for readability.

## advance

`advance` progresses exactly one existing Work through an explicit finite `--max-steps` budget.

It delegates to `BoundedExistingWorkProgressionService` and preserves its mechanism statuses:

```text
YIELDED
BOUND_EXHAUSTED
QUIESCENT
TERMINAL_NON_YIELD
```

These remain ephemeral product-call results, not Work states.

If the Work already exposes exact `WorkCompletion` or current-head `AttentionNeed`, `advance` returns `YIELDED` with zero steps and does not load the host factory.

## inspect

`inspect` is read-only and returns the same status surface plus a bounded page of exact durable Work events.

No hidden agent/product state is required to explain the Work.

## answer

`answer` records `AttentionResponse` evidence bound to one exact `AttentionNeed`.

```text
AttentionResponse != approval
AttentionResponse != execution authority
AttentionResponse != automatic resume permission
```

The response advances durable chronology. A later explicit `advance` lets the pinned Workflow inspect the response and decide what proposal, if any, follows.

Standalone answer ingress uses:

```text
source_namespace = codexia.standalone.work.answer
```

and exact source identity is idempotent.

## Restart

All product truth remains in the existing WorkStore:

```text
process exits
→ reopen same SQLite WorkStore
→ same Work id
→ same chronology
→ same yield / attention / completion
```

No product daemon is needed for correctness.

## Non-goals

DW1 does not add:

- daemon/background scheduler;
- cross-Work queue;
- generic autonomous mode;
- plugin loader or technical registry;
- HDE/IRR dependency;
- implicit filesystem/process/network/Git authority;
- automatic retry after ambiguous effects;
- Research Work semantics;
- Software Work semantics.

Those later product semantics are tracked by DW2+.