# M6.2 Source Audit — Continuation Admission

## Audit target

M6.2 adds semantic admission for proposed next work under:

```text
src/codexia_manual_agent/work/admission.py
```

The audit asks whether the new surface lets a worker promote its own suggestion into human instruction, lets admission mint execution authority, silently widens scope, or loses binding to the exact handoff/interpreted work checkpoint.

## Public surface

M6.2 exports:

- `ContinuationProposal`
- `ContinuationAdmission`
- `ContinuationDecision`
- `ContinuationFit`
- `ContinuationEvidenceFit`
- proposal/admission schema-version constants

No process executor, scheduler, provider mutation, Git mutation, filesystem mutation, network mutation, approval authority, or AuthorizationReceipt API is exported.

## Proposal provenance audit

`ContinuationProposal` requires an exact attributed `WorkStatement` whose author kind is either:

```text
worker
codexia
```

HUMAN statements cannot be down-cast into worker proposals. SYSTEM observations cannot become action proposals in v1.

The proposal digest jointly binds:

- exact handoff id/digest;
- exact interpretation id/digest;
- exact checkpoint digest;
- full attributed statement record.

This preserves:

```text
worker proposal != human instruction
observed state != proposed action
```

As in M6.1, the record preserves provenance supplied by the capture layer; it does not authenticate physical UI origin itself.

## Admission authorship audit

`ContinuationAdmission.evaluate()` requires `assessor_kind=CODEXIA`.

A worker cannot author its own admission record.

This establishes the v1 peer shape:

```text
worker proposal
→ Codexia admission judgment
```

It does not claim that Codexia cognition is infallible. The purpose is role separation and exact provenance, not semantic omniscience.

## Deterministic decision audit

The caller cannot freely choose `ADMIT / REVISE / REJECT / ASK_HUMAN`.

The decision is derived from exact structured criteria:

```text
objective_fit
constraint_fit
scope_fit
depth_fit
evidence_fit
material_human_choice
```

Decoder recovery recomputes the expected decision and rejects a mismatched stored decision.

This prevents a serialized record from changing `REJECT` to `ADMIT` while keeping the original criteria.

## Human-goal preservation audit

Direct objective or human-constraint misalignment deterministically yields `REJECT`.

Uncertainty about the objective or human constraints yields `ASK_HUMAN` rather than guessing.

A clearly out-of-scope proposal is rejected unless it represents a material human choice, in which case it yields `ASK_HUMAN`.

This preserves:

```text
worker-proposed scope expansion
!= silent human-goal rewrite
```

## Routine repair versus human attention audit

Depth mismatch or unsupported/uncertain evidence yields `REVISE`, not automatic human interruption.

This is intentionally different from objective/scope uncertainty. The worker can often repair insufficient rigor or gather missing evidence while the human remains focused elsewhere.

`REVISE` must carry exactly one bounded worker revision request.

`ASK_HUMAN` must carry exactly one bounded human response request.

`ADMIT` and `REJECT` cannot smuggle either follow-up payload.

## Authority audit

An admitted continuation contains no:

- capability grant;
- approval bit;
- authorization receipt;
- executable argv;
- shell command;
- filesystem access token;
- Git permission;
- network permission;
- mutation method.

`admitted=True` is only a convenience predicate over `decision == ADMIT`.

Therefore:

```text
admitted continuation != execution authority
```

Any downstream concrete effect still requires the existing relevant authority path.

## Binding audit

`ContinuationProposal.assert_binds()` requires the exact M6.1 handoff and interpretation.

`ContinuationAdmission.assert_binds()` additionally requires the exact proposal id/digest and checkpoint digest.

A reinterpretation of the same human handoff does not make an old proposal current under the new interpretation.

This avoids silently carrying a once-reasonable action across a changed understanding of scope or implementation depth.

## Decoder / tamper audit

Both new records use strict-key decoding and canonical digests.

The regression suite verifies:

- proposal/admission round-trip identity;
- stored decision tamper is rejected because decision criteria no longer match;
- authority-shaped extra fields are rejected;
- HUMAN/SYSTEM proposal-source relabelling is rejected;
- worker-authored admission is rejected;
- cross-interpretation proposal reuse fails closed;
- required REVISE/ASK_HUMAN follow-up payloads cannot be omitted.

## Dependency audit

`admission.py` depends only on:

- Python standard-library dataclass/enum/typing/UUID support;
- M6.1 semantic contracts and their internal canonical validation helpers.

It does not import execution, authority, Git mutation, provider, lab, or delegation runtime modules.

## Explicit limits

M6.2 does not yet extract a proposed action from a live ChatGPT conversation, judge the semantic fit with a model, persist work durably, schedule background activity, or deliver notifications.

The structured fit values are inputs to the deterministic admission gate. M6.3+ must establish how exact live conversation state and cognition produce those values while preserving provenance.

M6.2 also does not execute an admitted proposal.

## Audit conclusion

The M6.2 surface adds a bounded semantic checkpoint between cognitive suggestion and orchestration continuation:

```text
worker proposal
→ exact Codexia admission judgment
→ ADMIT / REVISE / REJECT / ASK_HUMAN
```

without widening Codexia's existing execution authority.
