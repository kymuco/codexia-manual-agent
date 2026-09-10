# M6.2 — Continuation Admission

## Goal

M6.1 made a human work handoff and Codexia's derived interpretation explicit. M6.2 adds the next boundary: a cognitive worker may propose what to do next, but that proposal must not become orchestration authority merely because it appears plausible in a chat.

Primary invariant:

```text
worker proposal != admitted continuation
```

A second boundary is equally important:

```text
admitted continuation != execution authority
```

M6.2 therefore classifies the semantic trajectory of delegated work only. Existing M2/M3 authority remains responsible for any process, filesystem, Git, network, or other governed effect.

## ContinuationProposal

`ContinuationProposal` binds one exact non-human next-step statement to:

```text
exact WorkHandoff
+ exact WorkIntentInterpretation
+ exact work checkpoint digest
```

The statement must preserve either `worker` or `codexia` authorship. HUMAN statements are not down-cast into worker proposals: a new human instruction belongs in human-intent/continuity handling, not in a record that pretends the worker proposed it.

SYSTEM observations also cannot become proposals in v1.

This preserves:

```text
observed state != proposed action
human instruction != worker proposal
```

## ContinuationAdmission

`ContinuationAdmission.evaluate()` is a narrow deterministic admission gate over an exact proposal.

Codexia records explicit semantic judgments for:

```text
objective_fit
constraint_fit
scope_fit
depth_fit
evidence_fit
material_human_choice
```

The first four fits use:

```text
aligned
misaligned
uncertain
```

Evidence uses:

```text
supported
unsupported
uncertain
not_required
```

No `probe / MVP / production` mode is introduced. `depth_fit` only asks whether the proposal matches the free attributed depth interpretation already derived in M6.1.

## Deterministic decision table

The v1 admission decision is not caller-authored. It is derived from the exact criteria.

### REJECT

A proposal is rejected when it directly conflicts with the human objective or human constraints.

A clearly out-of-scope proposal is also rejected unless the assessment explicitly identifies it as a material human choice.

This prevents a worker from silently rewriting the delegated goal.

### ASK_HUMAN

Human judgment is required when:

- objective or human-constraint compatibility is genuinely uncertain;
- scope compatibility is uncertain;
- an out-of-scope direction is material enough that the human may want to change the delegation;
- the proposal otherwise represents a material human choice.

`ASK_HUMAN` requires a bounded `requested_human_response` and cannot simultaneously issue a worker revision.

### REVISE

Codexia requests a worker-side revision when the proposed action is still inside the delegated trajectory but its execution depth or evidence basis is not adequate.

Examples:

```text
production-quality task
→ worker proposes disposable shortcut
→ REVISE
```

or:

```text
worker wants to declare a result complete
→ evidence is unsupported / uncertain
→ REVISE and verify first
```

`REVISE` requires a bounded revision request and does not interrupt the human.

### ADMIT

A proposal is admitted only when:

```text
objective aligned
human constraints aligned
scope aligned
depth aligned
evidence supported or not required
no material human choice
```

The result means only:

> this proposed next unit of work is semantically admissible under the current delegated trajectory.

It does **not** mean:

> execute any action necessary to accomplish it.

## Provenance

The proposal preserves the original non-human `WorkStatement` rather than converting it into a human message.

The admission itself must be attributed to `WorkActorKind.CODEXIA`; a worker cannot author its own admission record in v1.

This gives the intended peer boundary:

```text
worker proposes
→ Codexia admits/revises/rejects/escalates
```

M6.3 will later establish trusted live capture from the real ChatGPT surface. As in M6.1, the records preserve provenance supplied by the integration layer; they do not authenticate physical UI origin by themselves.

## Exact binding

Both records are immutable, schema-versioned, canonical-JSON digest-bound, and strict-key decoded.

A proposal binds:

```text
handoff id/digest
interpretation id/digest
checkpoint digest
attributed proposal statement
```

An admission additionally binds the exact proposal id/digest and must reproduce the deterministic decision implied by its criteria when decoded.

Changing the interpretation or checkpoint therefore invalidates reuse of the old proposal/admission as current work state.

## Authority boundary

M6.2 imports no process executor, Git mutation, network mutation, filesystem mutation, AuthorizationReceipt, or M3 action authority.

`ContinuationAdmission` has no executable argv, capability grant, permission flag, receipt, token, or action method.

```text
ADMIT
!= AUTHORIZED
!= EXECUTED
```

Future orchestration may use an ADMIT result to decide which semantic work item to prepare next, but every concrete effect still passes through its own existing authority surface.

## Demonstrated regressions

The M6.2 test slice covers:

1. routine project continuation → `ADMIT`;
2. plan-less research continuation → `ADMIT`;
3. depth mismatch → `REVISE` without human interruption;
4. unsupported evidence → `REVISE` before completion;
5. objective conflict → `REJECT`;
6. explicit human-constraint conflict → `REJECT`;
7. material new direction → `ASK_HUMAN`;
8. uncertain human-constraint interpretation → `ASK_HUMAN`;
9. HUMAN/SYSTEM statements cannot be down-cast into worker proposals;
10. a worker cannot author its own admission;
11. proposal/admission bind exact handoff, interpretation, and checkpoint;
12. missing follow-up payloads for `REVISE` / `ASK_HUMAN` fail closed;
13. decision tamper and authority-shaped extra fields fail strict decoding.

## Non-goals

M6.2 does not add:

- automatic extraction of proposals from ChatGPT text;
- live ChatGPT website interaction;
- a provider origin authenticator;
- durable work queues or background scheduling;
- notification delivery;
- learned attention preferences;
- local execution;
- automatic execution after `ADMIT`;
- a new execution-authority system.

Those remain downstream M6 work.

## Exit gate

M6.2 is complete when Codexia can represent an exact worker-proposed next action and deterministically classify it as:

```text
ADMIT / REVISE / REJECT / ASK_HUMAN
```

while preserving:

```text
worker proposal != admitted continuation
admitted continuation != execution authority
```
