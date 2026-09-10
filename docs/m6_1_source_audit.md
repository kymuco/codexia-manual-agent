# M6.1 Source Audit — General Work Handoff and Attention Boundary

## Audit target

M6.1 adds the first general delegated-work semantic layer under:

```text
src/codexia_manual_agent/work/
```

The source audit asks whether the new surface accidentally widens authority, conflates human intent with model interpretation, makes project plans mandatory, or lets dynamic attention decisions masquerade as human approval.

## Public surface

The new package exports:

- `WorkActorKind`
- `WorkResourceKind`
- `AttentionUrgency`
- `WorkStatement`
- `WorkResourceRef`
- `WorkHandoff`
- `WorkIntentInterpretation`
- `AttentionAssessment`
- `InvalidWorkRecordError`
- schema-version constants

No executor, scheduler, provider mutation, process proposal, authorization receipt, Git mutation, network transport, or filesystem mutation API is exported.

## Dependency audit

`work/contracts.py` depends only on Python standard-library functionality for:

- immutable dataclasses;
- enums;
- UUID/timestamp validation;
- canonical JSON;
- SHA-256/HMAC digest comparison.

It does **not** import:

- `authority`
- `execution`
- `git_mutation`
- `mutation`
- `providers`
- `lab`
- `delegation`

This is intentional. M6.1 semantic work records cannot call an existing authority surface merely by being constructed or decoded.

## Human authorship audit

`WorkHandoff.objective` must be a `WorkStatement` attributed to `WorkActorKind.HUMAN`.

`human_constraints` and `attention_constraints` are also restricted to HUMAN-attributed statements.

A worker statement such as:

```text
"The next logical step is PR78."
```

is valid context, but cannot become the human objective of a handoff.

This preserves:

```text
worker suggestion != human instruction
```

## Handoff versus interpretation audit

The initial draft embedded `WorkIntentInterpretation` inside `WorkHandoff`. Source review rejected that shape because it would make a model-produced interpretation part of the identity of the human-authored delegation.

The corrected design separates them:

```text
human WorkHandoff
        ↓ exact identity/digest
WorkIntentInterpretation
```

`WorkHandoff.handoff_digest` therefore does not contain completion/depth/scope interpretation fields.

A new interpretation can change:

- `completion_expectation`
- `continuation_scope`
- `depth_interpretation`

without changing the original handoff digest.

`WorkIntentInterpretation.create()` requires the exact handoff and a bounded statement basis that includes the exact human objective and contains no statement outside that handoff.

The interpreter cannot be attributed as HUMAN.

This preserves:

```text
human handoff != inferred work interpretation
```

## Plan audit

`plan_resource_id` is optional.

When present, it must refer to one exact `WorkResourceRef` already contained in the handoff.

When absent, a non-project research or creation request is still a valid handoff.

This preserves:

```text
plan != prerequisite for delegation
```

M6.1 does not yet generate a plan or authorize following one.

## Resource audit

`WorkResourceRef` contains only:

```text
resource identity
kind
locator
optional label
digest
```

There is no access flag, credential, token, capability, mutation bit, or authority field.

Therefore:

```text
resource reference != resource access authority
```

## Dynamic attention audit

`AttentionAssessment` requires:

- exact handoff id/digest;
- exact interpretation id/digest;
- exact checkpoint digest;
- non-HUMAN assessor provenance;
- `needs_human`;
- bounded confidence;
- urgency;
- reason;
- optional requested response.

If `needs_human=false`:

- urgency must be `none`;
- `requested_response` must be absent.

If `needs_human=true`, urgency cannot be `none`.

The assessment schema contains no permission, approval, authorization receipt, capability grant, or execution command.

This preserves:

```text
dynamic attention judgment != permission
```

## Decoder / tamper audit

All M6.1 records use exact-key decoders and canonical SHA-256 digests.

The regression suite verifies:

- attributed statement tamper is rejected;
- extra handoff fields are rejected;
- an interpretation cannot bind a statement from another handoff;
- an attention assessment cannot use an interpretation belonging to another handoff;
- a no-attention result cannot hide a response request.

## Explicit limits

M6.1 does not yet prove background continuity. In particular, it has no durable registry or work state machine.

It also does not decide whether a worker's proposed next action should be admitted. That is intentionally deferred to M6.2, where the key boundary will be:

```text
worker proposal != admitted continuation
```

## Audit conclusion

The M6.1 source surface is a semantic contract layer only. It makes human work handoff, derived intent interpretation, resource provenance, and dynamic attention judgment explicit without widening Codexia's existing execution authority.
