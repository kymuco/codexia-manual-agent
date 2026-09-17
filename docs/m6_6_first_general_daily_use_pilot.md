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
human answer != arbitrary execution authority
background progress != autonomous authority
ambiguous provider effect != retry permission
```

M6.1–M6.5 already define the handoff, admission, attention, peer transport, durable supervisor, no-replay boundary, and bounded background driver. M6.6 adds only the minimum live cognition and pilot surfaces needed to exercise those components as one daily-use loop.

## Pilot checkpoint cognition

`PilotCheckpointSource` receives the exact recovered supervisor snapshot and derives the current exact logical worker evidence without weakening the durable M6.3/M6.5 record schemas.

A simple worker turn is still represented by the exact `ChatPeerTurn`. A real ChatGPT tool-using product turn may expose additional assistant-only canonical artifacts after that recorded turn. M6.6 treats a contiguous assistant-only tail, chained exactly to the current cursor and the same delegated-work identity, as part of the same logical worker evidence. Any newer HUMAN/pilot-answer event or provider-side user message invalidates that worker evidence for completion.

The cognition model does not choose `ADMIT`, `REVISE`, `REJECT`, or `ASK_HUMAN` directly. It returns bounded semantic judgments such as objective/scope/depth/evidence fit, material-human-choice status, reversibility, trajectory impact, alternatives, and one exact check per HUMAN attention constraint.

Those judgments are passed through the existing authoritative constructors:

```text
semantic judgments
→ M6.2 ContinuationAdmission.evaluate(...)
→ M6.4 DynamicAttentionDecision.evaluate(...)
→ M6.5 supervisor/driver
```

A model preference therefore cannot bypass M6.2 admission derivation or M6.4 hard attention overrides.

When current exact logical worker evidence exists, cognition must use `proposal_text=null`; it cannot replace worker evidence with newly invented candidate text. When no current worker evidence exists, such as initial work or work resumed from exact human evidence, cognition may provide one bounded CODEXIA-authored proposal for the next worker step.

## Completion boundary

The driver accepts a `SupervisorCompletion` outcome, but the completion statement must be CODEXIA-authored and is committed through the existing supervisor completion boundary.

Vertical A exposed that a separate two-field `mode=complete` shortcut could bypass the M6.2/M6.4 attention pipeline. The hardened pilot therefore evaluates a completion candidate through the same exact HUMAN-attention coverage before returning `SupervisorCompletion`.

```text
completion candidate
+ every exact HUMAN attention constraint evaluated once

TRIGGERED / UNCERTAIN
→ WAITING_HUMAN
→ never COMPLETED

all CLEAR
→ existing final live-peer reread
→ existing CAS
→ COMPLETED
```

For historical no-attention test compatibility only, the legacy two-field completion shape remains accepted when the handoff contains no attention constraints. A handoff with any explicit HUMAN attention constraint cannot use that shortcut.

Completion additionally requires current exact logical worker evidence. Assistant-only artifacts from the same exact provider turn do not erase that evidence merely because they are recorded as later durable observations; newer HUMAN activity still does.

```text
same logical assistant-only tail != stale worker evidence
new HUMAN/provider-user activity     = stale worker evidence
```

## Human stop and exact resume

If M6.2 or M6.4 requires human judgment, the supervisor enters `WAITING_HUMAN` and the driver stops without sending another worker continuation.

M6.6 adds an explicit pilot human-answer boundary that does **not** require the human to write into the worker ChatGPT conversation:

```text
WAITING_HUMAN
→ exact HUMAN WorkStatement
→ bind waiting sequence + waiting event digest + attention decision digest
→ durable external-observed event
→ READY
→ fresh cognition / M6.2 / M6.4
```

The answer changes no ChatGPT cursor and creates no pending dispatch. It is evidence for the next cognition checkpoint only. The next worker send still requires a newly derived M6.2/M6.4 checkpoint and the normal M6.5 dispatch path.

Vertical A also exposed that a newer account-side observation could make the answer cease to be the terminal event before the fresh judgment. The hardened pilot therefore retains the latest unresolved pilot HUMAN answer across newer ordinary external observations until the first fresh `CHECKPOINT_DECIDED` consumes it. This preserves evidence; it does not turn the answer into admission or execution authority.

The existing M6.3 account-side human observation path remains valid, and fresh cognition may receive both the retained pilot answer and newer provider observation. Fresh human evidence remains prioritized in the bounded M6.4 attention basis.

The original `WorkHandoff` is not rewritten. The answer is evidence about the suspended work, not a retroactive replacement for its identity or authority.

## Pilot runtime

The experimental runtime surface is intentionally small:

```text
python -m codexia_manual_agent.work.pilot_cli start ...
python -m codexia_manual_agent.work.pilot_cli drive <work_id> ...
python -m codexia_manual_agent.work.pilot_cli answer <work_id> "..."
python -m codexia_manual_agent.work.pilot_cli status <work_id> ...
python -m codexia_manual_agent.work.pilot_cli rearm-dispatch <work_id> ...
```

`start` binds an existing ChatGPT conversation to an exact HUMAN `WorkHandoff`, CODEXIA interpretation, and exact current branch cursor.

`drive` invokes the existing bounded `BackgroundWorkDriver` with M6.6 live checkpoint cognition and the pilot logical-turn peer loop until a governed stop boundary is reached.

`answer` is accepted only for exact `WAITING_HUMAN` work. It records one explicit HUMAN answer outside the worker chat and returns the same work to `READY`; it cannot admit or execute the next step.

`rearm-dispatch` is a separate explicit HUMAN historical-recovery surface bound to the exact claim and dispatch. It performs no provider write itself.

`status` performs durable recovery only.

This CLI is a pilot surface rather than a new main-product command family. It can be promoted or redesigned after real-use evidence instead of freezing speculative UX now.

## Vertical A semantic findings

The first real Vertical A adversarial audit found three implementation gaps at the daily-use boundary:

1. completion could bypass exact HUMAN-attention evaluation;
2. a pilot HUMAN answer could be shadowed by a newer external observation before fresh cognition;
3. logical terminal worker evidence could be lost when a tool-using ChatGPT turn emitted assistant-only artifacts after the v1 recorded peer turn, producing a procedural completion livelock.

The fixes are intentionally bounded. They do not change the durable M6.3/M6.5 schema, add execution authority, add provider-write retry authority, or alter the M6.6 exit criteria.

The same live run also reproduced upstream long-history canonical-read HTTP 429 throttling. CWA PR14.6 repaired that owning boundary with bounded retries only for idempotent canonical GET pages, exact same-page/cursor retry, bounded `Retry-After`/backoff, pagination pacing, and fail-closed exhaustion. It introduced no product-write retry authority and squash-merged as:

```text
21279260fbc344816e112393d40ee28e4355baaf
```

## Two required real verticals

The harness is not sufficient to close M6.6. The milestone requires real evidence from both:

1. **ongoing software/research work** — multiple ordinary continuation/revision cycles advance without routine human `continue / check / fix / next` scheduling;
2. **standalone non-project knowledge work** — a substantial request is iterated, challenged, deepened, synthesized, and returned without human intervention between routine worker turns.

At least one real pilot must also demonstrate a genuine human-attention stop and exact resume through the pilot human-answer surface.

## Candidate exit criteria

The implementation candidate is ready for the next clean real pilot when:

- exact-head CI and CodeQL are green;
- the exact merged CWA PR14.6 revision is installed and the stable browser-native deployment identity is healthy;
- cognition output is strict-key decoded and authority-shaped extra fields fail closed;
- every explicit HUMAN attention constraint is evaluated exactly once for continuation **and completion**;
- completion `TRIGGERED / UNCERTAIN` reaches `WAITING_HUMAN`, while all-`CLEAR` may proceed only through existing final reread/CAS;
- exact logical worker evidence cannot be replaced by cognition and survives only same-turn assistant-only artifacts;
- newer HUMAN activity invalidates stale worker completion evidence;
- the pilot HUMAN-answer event is exact-state bound, HUMAN-authored, cursor-preserving, creates no dispatch, and remains visible through the first fresh governed judgment even if a newer external observation arrives first;
- canonical full-history pagination handles bounded transient 429 throttling without returning partial history and without retrying product writes;
- provider crash/replay semantics remain unchanged from M6.5;
- routine REVISE remains background-capable without human scheduling;
- the pilot CLI can start, drive, recover, stop for human judgment, answer outside the worker chat, and resume the same `work_id`.

The original historical Vertical A database remains evidence and is not forced to `COMPLETED`; the repaired implementation should be exercised on a clean new Vertical A work item.

M6.6 becomes **Complete** only after the two real verticals have been run and their evidence reviewed. Unit/integration tests prove the harness and repaired boundaries; they do not prove daily-use value.
