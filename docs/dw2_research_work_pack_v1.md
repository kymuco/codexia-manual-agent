# DW2 — Research Work Pack v1

> Historical note: DW2 closed the exact Research Pack 1.0.0 JSON-envelope
> contract. DW3 empirical work later introduced the 1.1.0 hybrid
> content/control contract documented in
> `docs/dw3_research_hybrid_output_contract.md`. This document preserves the
> DW2 closure semantics rather than rewriting that historical proof.

## Purpose

Research Work Pack v1 is the first non-process cognitive Gen2 Pack.

It proves that one durable Work can move through bounded research cognition, critique, revision, synthesis, inspectable evidence/artifact materialization, genuine human attention, and semantic completion without adding a research scheduler or parallel Work state.

## Semantic stages

```text
researcher
→ critic
→ reviser
→ synthesizer
→ materialized EvidenceRef / ArtifactRef
→ CompletionClaim
→ guarded WorkCompletion
```

The four stages are exact RoleBindings inside one exact Pack.

## Cognition output contract

Each role returns one exact `ResearchRoleOutput` JSON record.

The common fields include:

- stage;
- bounded content;
- explicit human-attention request fields;
- synthesis-only objective coverage;
- synthesis-only evidence sufficiency;
- synthesis-only material unresolved uncertainty.

The schema is Pack/domain output carried inside ordinary durable CognitionOutcome text. It is not a new Core record type.

## Critique and revision

Research Work Pack v1 always routes an ordinary successful path through:

```text
initial research
→ critique
→ revision
→ synthesis
```

This is the v1 proof that weak/unsupported findings can be challenged and revised before completion.

## Attention

Only the initial research stage may request AttentionNeed in v1.

It must provide an explicit question and reason. Ordinary scheduling/iteration must keep `needs_human=false`.

After an exact AttentionResponse, the same Work continues into critique.

## ContextProjection

Each RoleRun receives an exact ContextProjection over canonical JSON derived from:

- immutable Work objective;
- prior durable research role outputs;
- prior AttentionResponse text;
- already durable ArtifactRef/EvidenceRef metadata.

`ResearchContextMaterialPort` reconstructs those exact bytes from the RoleRun start revision and verifies the projection digest on restart.

## Evidence and artifacts

`ResearchWorkMaterializer` is a thin host/product adapter.

It does not run cognition, choose Workflow steps, grant authority, or decide completion.

For each completed research RoleRun it records an EvidenceRef whose locator is the exact durable role terminal Work event.

For synthesis it additionally records:

- one Markdown ArtifactRef bound to `synthesis.content`;
- explicit objective-coverage evidence when true;
- explicit evidence-sufficiency evidence when true;
- explicit no-material-uncertainty evidence when false uncertainty is observed.

EvidenceRef remains a reference to inspectable evidence, not verified truth.

## Completion

The Workflow proposes CompletionClaim only when:

- all four roles completed in exact order;
- synthesis says objective coverage is complete;
- synthesis says evidence is sufficient;
- synthesis says no material unresolved uncertainty remains;
- required materialized evidence kinds exist;
- exactly one final synthesis artifact is bound to the synthesis role output.

The Pack completion criterion independently requires the same material basis shape before accepting the claim.

An incomplete synthesis remains active/non-yielded rather than fabricating completion.

## External evidence / retrieval

DW2 does not claim to be a web-research engine.

Pack v1 proves cognitive/evidence semantics using durable cognition outcome provenance. Retrieval/read capabilities remain host-owned and may be added in DW3 only if the real research vertical demonstrates a concrete need.

## Restart

Role runs, cognition handoffs/outcomes, evidence refs, artifact refs, AttentionNeed/Response, and completion remain in existing durable Gen2 chronology.

Reopening the same SQLite WorkStore therefore resumes from the same Work and does not repeat completed roles.

Materialization is idempotent through deterministic evidence/artifact identity plus existing Core admission guards.

## Non-goals

No generic research framework.
No hidden scheduler.
No daemon.
No HDE/IRR semantics.
No network authority.
No implicit retrieval authority.
No claim that DW2 alone is the real-world research pilot; that is DW3.