# M6.4 Source Audit — Dynamic Attention

## Audit target

M6.4 decides whether the human should be interrupted at an exact delegated-work checkpoint. The audit asks whether model preference can override explicit human rules, whether worker output can own the attention decision, whether attention records can be rebound across work/checkpoints, and whether `KEEP_MOVING` can be confused with execution authority.

## Explicit human attention rules

`WorkHandoff.attention_constraints` are HUMAN-authored records from M6.1. M6.4 requires complete exact coverage: one `AttentionConstraintCheck` for every attention-constraint statement digest and no foreign or duplicate digest.

A check is `CLEAR`, `TRIGGERED`, or `UNCERTAIN`. `TRIGGERED` and `UNCERTAIN` both force human attention. This prevents omission or uncertainty from silently weakening an explicit human boundary.

```text
model wants KEEP_MOVING
+ explicit rule TRIGGERED/UNCERTAIN
→ ASK_HUMAN
```

## Dynamic cognition audit

Reversibility, trajectory impact, and alternative shape are preserved as Codexia-authored context signals. They are intentionally not converted into a static universal scoring threshold.

The cognitive recommendation may be `KEEP_MOVING` or `ASK_HUMAN`. A model may therefore decide that a bounded expensive step is still routine, or that a cheap reversible decision is nevertheless material enough to involve the human.

The context builder and the final attention assessor must both preserve explicit CODEXIA authorship. A WORKER cannot author the attention decision that determines whether the human is interrupted.

## M6.2 interaction audit

The dynamic attention context binds the exact `ContinuationProposal` and exact `ContinuationAdmission`, including the M6.2 decision and checkpoint.

An M6.2 `ASK_HUMAN` creates a hard attention override and cannot be suppressed by a later cognitive `KEEP_MOVING` recommendation.

`REVISE` and `REJECT` are not automatically human-attention events. They may stay in background orchestration if Codexia judges that no human input is needed. This does not alter the original M6.2 decision or authorize the rejected/revision-required proposal.

## Exact binding audit

`DynamicAttentionContext` binds:

- handoff identity/digest;
- interpretation identity/digest;
- proposal identity/digest and exact proposal statement digest;
- admission identity/digest/decision;
- checkpoint digest;
- attributed basis statements;
- Codexia context-builder provenance;
- structured contextual signals;
- complete attention-constraint checks.

`DynamicAttentionDecision` embeds the exact context and existing `AttentionAssessment`, and validates that the assessment binds the same handoff, interpretation, and checkpoint.

Canonical digest validation rejects post-capture mutation. `assert_binds()` must be used before operational consumption to revalidate relational bindings against the current exact M6.1/M6.2 records.

## Authority audit

Neither `DynamicAttentionContext`, `AttentionConstraintCheck`, `DynamicAttentionDecision`, nor `AttentionAssessment` imports or grants local process execution, filesystem mutation, Git mutation, network authority, provider-send authority, authorization receipts, or merge authority.

```text
KEEP_MOVING
!= ADMIT
!= authorization
!= execution
```

`KEEP_MOVING` is only a statement that human attention is not currently required. The next action must independently satisfy M6.2 admission and whatever execution authority layer applies.

## Scheduler / notification audit

M6.4 does not schedule background work and does not deliver notifications. It only produces the exact attention decision and, when useful, the bounded `requested_response` content that a later notification/companion surface may present.

M6.5 remains responsible for durable background supervision. Notification transport remains a later surface.

## Known trust limits

The structured signals and constraint statuses are Codexia cognition outputs, not objective physical measurements. M6.4 makes their provenance and exact payload durable/tamper-evident; it does not prove that the model's semantic interpretation is infallible.

Likewise, M6.1 HUMAN provenance remains attributed source provenance rather than cryptographic proof of the physical person, as already documented in the M6.1/M6.3 boundaries.

## Audit conclusion

M6.4 preserves the intended relationship:

```text
Codexia may decide not to bother the human
when the exact situation can keep moving,

but

explicit human attention rules
and M6.2 human-choice boundaries
cannot be cognitively waived.
```

No execution authority is widened.