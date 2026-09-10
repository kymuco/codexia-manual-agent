# M6.4 — Dynamic Attention

## Goal

M6.4 decides whether delegated work should keep moving without interrupting the human or whether the current checkpoint genuinely requires human attention.

Primary boundary:

```text
attention recommendation != execution authority
```

Additional boundaries:

```text
model preference != explicit human attention constraint
worker problem != automatic human interruption
cost / irreversibility / alternatives != universal static threshold
KEEP_MOVING != permission to execute a rejected or unauthorized action
```

## Dynamic rather than profile-first

M6.4 does not introduce fixed `hands-off / balanced / close-control` profiles or a hardcoded probe/MVP/production mode. Those may become product preferences later.

The first runtime instead preserves a Codexia-authored cognitive judgment over the exact work checkpoint. The model sees structured signals such as:

- the exact continuation proposal and supporting attributed statements;
- the M6.2 admission outcome;
- reversibility;
- trajectory impact;
- whether alternatives are absent, effectively equivalent, or materially different;
- every explicit HUMAN attention constraint from the handoff.

Reversibility, impact, and alternatives inform cognition but do not independently force a universal answer. This leaves room for context: a costly step may still be routine and clearly delegated, while a cheap reversible choice may still deserve attention because it changes the research or product direction.

## Complete explicit attention-constraint coverage

A human attention rule is not merely an optional triggered flag. Every `WorkHandoff.attention_constraints` statement must have exactly one `AttentionConstraintCheck` with one of:

```text
CLEAR
TRIGGERED
UNCERTAIN
```

The context is rejected if a rule is omitted, duplicated, or replaced by a foreign statement digest.

`TRIGGERED` and `UNCERTAIN` both create a hard human-attention override. `UNCERTAIN` fails closed because Codexia must not silently continue when it cannot determine whether an explicit human attention rule applies.

```text
explicit HUMAN attention constraint
→ complete Codexia evaluation
→ triggered / uncertain
→ human attention required
```

## Cognitive recommendation and hard override

`DynamicAttentionDecision` records a Codexia cognitive recommendation:

```text
KEEP_MOVING
ASK_HUMAN
```

The final `AttentionAssessment.needs_human` is derived from:

```text
explicit attention constraint blocker
OR M6.2 ASK_HUMAN
OR Codexia cognitive ASK_HUMAN
```

Thus Codexia may choose not to interrupt on ordinary failures, revisions, or bounded choices, but it cannot suppress an explicit attention rule or an M6.2 material-human-choice boundary.

`REVISE` and `REJECT` do not automatically imply human attention. `KEEP_MOVING` in those cases means the orchestration layer may continue by revising/replanning inside the delegation; it does **not** turn the rejected proposal into an executable action.

## Exact binding

`DynamicAttentionContext` binds:

- exact handoff id/digest;
- exact interpretation id/digest;
- exact proposal id/digest and proposal statement digest;
- exact M6.2 admission id/digest/decision;
- exact checkpoint digest;
- exact attributed basis statements;
- Codexia-authored structured attention signals;
- complete explicit attention-constraint checks.

`DynamicAttentionDecision` then binds the exact context to the existing M6.1 `AttentionAssessment`.

Both records use canonical digest validation and strict decoding. Operational use must call `assert_binds()` against the current M6.1/M6.2 records before acting on the judgment.

## Authority boundary

M6.4 is an attention router only.

It grants no process, filesystem, Git, network, provider-send, merge, or other execution authority. A no-human decision means only:

> Human attention is not required at this checkpoint according to the exact recorded cognition and hard constraints.

The relevant admission/authority layer must still independently permit whatever work happens next.

## Exit gate

M6.4 is complete when the runtime can prove all of the following:

1. routine admitted work can receive `KEEP_MOVING` without human interruption;
2. Codexia can dynamically ask the human for a material contextual choice even when no static rule forces it;
3. every explicit human attention constraint is evaluated exactly once;
4. triggered or uncertain explicit rules override a model preference to keep moving;
5. an M6.2 `ASK_HUMAN` cannot be suppressed by M6.4;
6. a routine `REVISE` can remain background work without automatically interrupting the human;
7. worker-authored cognition cannot own the dynamic attention decision;
8. exact context/decision digests reject tamper and cross-work rebinding;
9. none of the above grants execution authority or introduces scheduling/notification transport.
