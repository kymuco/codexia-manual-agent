# M6.6 — First General Daily-Use Pilot

## Goal

M6.6 is a product proof, not another autonomy layer.

The target property is:

```text
human absence != work suspension
```

while preserving:

```text
worker output != work completion
worker proposal != admitted continuation
attention boundary != authority boundary
background progress != autonomous authority
ambiguous provider effect != retry permission
```

M6.1–M6.5 already define the handoff, admission, attention, peer transport, durable supervisor, no-replay boundary, and bounded background driver. M6.6 adds only the minimum live cognition and pilot surfaces needed to exercise those components as one daily-use loop.

## Pilot checkpoint cognition

`PilotCheckpointSource` receives the exact recovered supervisor snapshot, the current peer loop, and only the exact terminal `ChatPeerTurn` when the terminal durable event is a worker turn.

The cognition model does not choose `ADMIT`, `REVISE`, `REJECT`, or `ASK_HUMAN` directly. It returns bounded semantic judgments such as objective/scope/depth/evidence fit, material-human-choice status, reversibility, trajectory impact, alternatives, and one exact check per HUMAN attention constraint.

Those judgments are then passed through the existing authoritative constructors:

```text
semantic judgments
→ M6.2 ContinuationAdmission.evaluate(...)
→ M6.4 DynamicAttentionDecision.evaluate(...)
→ M6.5 supervisor/driver
```

A model preference therefore cannot bypass M6.2 admission derivation or M6.4 hard attention overrides.

When the terminal durable event is an exact worker turn, the next proposal is created only with `ChatPeerTurn.followup_proposal(...)`. The cognition response must use `proposal_text=null`; it cannot replace worker evidence with newly invented candidate text.

When there is no terminal worker turn, such as initial work or work resumed from exact external human activity, cognition may provide one bounded CODEXIA-authored proposal for the next worker step.

## Completion boundary

The driver accepts a `SupervisorCompletion` outcome, but the completion statement must be CODEXIA-authored and is committed through the existing `BackgroundWorkSupervisor.complete(...)` boundary.

For the M6.6 pilot, cognition may request completion only when the terminal durable event is the exact worker turn supplied as `latest_turn`.

```text
old worker evidence + newer human/external event != completion evidence
```

This deliberately prevents a previously successful worker answer from being reused to close work after the delegated state has changed.

## Human stop and exact resume

If M6.2 or M6.4 requires human judgment, the supervisor enters `WAITING_HUMAN` and the driver stops without sending another worker continuation.

A later account-side human message is captured through the existing M6.3 live-observation boundary and stored as an exact `EXTERNAL_OBSERVED` supervisor event. On the next READY cognition checkpoint, the exact terminal observation is included in the cognition state and prioritized in the bounded attention basis.

The original `WorkHandoff` is not rewritten. The new human answer is evidence about the suspended work, not a retroactive replacement for its original identity.

## Pilot runtime

The experimental runtime surface is intentionally small:

```text
python -m codexia_manual_agent.work.pilot_cli start ...
python -m codexia_manual_agent.work.pilot_cli drive <work_id> ...
python -m codexia_manual_agent.work.pilot_cli status <work_id> ...
```

`start` binds an existing ChatGPT conversation to an exact HUMAN `WorkHandoff`, CODEXIA interpretation, and exact current branch cursor.

`drive` invokes the existing bounded `BackgroundWorkDriver` with live `PilotCheckpointSource` cognition until one of the existing governed stop conditions is reached.

`status` performs durable recovery only.

This CLI is a pilot surface rather than a new main-product command family. It can be promoted or redesigned after real-use evidence instead of freezing speculative UX now.

## Two required real verticals

The harness is not sufficient to close M6.6. The milestone requires real evidence from both:

1. **ongoing software/research work** — multiple ordinary continuation/revision cycles advance without routine human `continue / check / fix / next` scheduling;
2. **standalone non-project knowledge work** — a substantial request is iterated, challenged, deepened, synthesized, and returned without human intervention between routine worker turns.

At least one real pilot should also demonstrate a genuine human-attention stop and exact resume from the human answer.

## Candidate exit criteria

The implementation candidate is ready for real pilot use when:

- exact-head CI and CodeQL are green;
- cognition output is strict-key decoded and authority-shaped extra fields fail closed;
- exact HUMAN attention constraints cannot be omitted, duplicated, or substituted;
- terminal exact worker evidence cannot be replaced by cognition;
- completion requires terminal exact worker evidence and CODEXIA authorship;
- human activity still invalidates stale prepared work through M6.5;
- provider crash/replay semantics remain unchanged from M6.5;
- the pilot CLI can start, drive, recover, stop for human judgment, and resume the same work.

M6.6 becomes **Complete** only after the two real verticals have been run and their evidence reviewed. Unit/integration tests prove the harness; they do not prove daily-use value.
