# DW3 — Research Hybrid Output Contract

## Finding

The first real Research Work vertical exposed that requiring every worker to emit
one exact private JSON envelope couples useful cognition content to workflow-control
serialization.

A live researcher produced useful structured analysis, but the whole role became
unusable because its worker-owned JSON shape did not match the Pack's private
nine-field schema.

That failure is now treated as evidence for a narrower boundary:

```text
useful cognition content != workflow control metadata
```

## Contract

Research Pack 1.1 separates two planes.

### Content plane

Researcher, critic, reviser, and synthesizer produce ordinary text. The synthesizer
uses Markdown-ready text.

The content may itself contain prose, Markdown, tables, JSON, or another useful
representation. Codexia does not require the content body to match a private
research schema.

Only the bounded content is propagated to later research roles and used for the
final synthesis ArtifactRef.

### Control plane

Only stages that need machine-readable workflow signals use a small trailer:

```text
<<<CODEXIA_CONTROL_V1>>>
{...small JSON...}
<<<END_CODEXIA_CONTROL_V1>>>
```

Initial research may append a trailer only for genuine human-owned judgment:

```json
{
  "schema_version": 1,
  "needs_human": true,
  "human_question": "...",
  "human_reason": "..."
}
```

Synthesis appends three positive completion-assessment booleans:

```json
{
  "schema_version": 1,
  "objective_coverage_complete": true,
  "evidence_sufficient": true,
  "material_uncertainty_resolved": true
}
```

Critic and reviser require no control trailer.

The parser reads only the recognized control fields with exact primitive types.
Unknown extra control keys are ignored. Missing, malformed, or wrongly typed
recognized fields do not become authority; they leave the corresponding workflow
signal unproven.

## Failure semantics

Malformed or missing control metadata does not erase otherwise useful cognition
content.

For initial research:

```text
missing / malformed control
→ no AttentionNeed
→ content remains durable and usable
```

For synthesis:

```text
missing / malformed control
→ synthesis content remains durable
→ final ArtifactRef can still materialize
→ completion assessment evidence is not admitted
→ CompletionClaim cannot be established
```

This preserves fail-closed workflow control without treating formatting failure as
loss of completed intellectual work.

No hidden retry is introduced.

## Context semantics

Later roles receive prior durable research content, not the control trailer and not
an outer private JSON envelope.

This keeps provider-facing cognition portable across different hosted or local
models while preserving exact durable provenance at the Work-event boundary.

## Versioning

The hybrid contract changes Role instruction digests and therefore exact Pack
semantics.

It is published as Research Pack / Workflow / Role version `1.1.0` rather than
silently changing the DW2 `1.0.0` binding.

## Boundary

This is a Research Pack repair, not a new Core transport.

Core still carries ordinary `CognitionOutcome.output_text` and retains the same
handoff, retry, authority, evidence, artifact, and completion boundaries.
