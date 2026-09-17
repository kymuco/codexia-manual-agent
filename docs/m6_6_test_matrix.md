# M6.6 Test Matrix — First General Daily-Use Pilot

## Harness gates

The candidate must prove the live cognition bridge without weakening existing M6 boundaries.

| Gate | Expected result |
|---|---|
| Two routine worker turns in one driver call | advances without human `continue` and ends only at a governed stop/completion |
| Live cognition revision | insufficient worker depth derives `REVISE`, emits Codexia revision, and continues without human interruption |
| Genuine material choice | enters `WAITING_HUMAN` before provider send |
| Pilot human answer | explicit HUMAN answer outside worker chat binds exact waiting state and returns only to `READY` |
| Human answer cursor isolation | recording the answer does not modify the worker ChatGPT cursor/history |
| Human answer retention | a newer external observation does not hide the unresolved pilot HUMAN answer before the first fresh governed judgment |
| Unsolicited human answer | answer outside exact `WAITING_HUMAN` fails closed |
| Account-side human activity | existing M6.3 `EXTERNAL_USER` observation path remains valid and distinct from pilot answer evidence |
| Worker follow-up provenance | cognition cannot replace terminal exact worker text |
| Tool-using logical worker turn | assistant-only tool/progress artifacts remain one exact logical worker evidence chain without bypassing M6.5 provenance |
| Completion authorship | WORKER-authored completion is rejected |
| Completion evidence | completion before current terminal logical worker evidence is rejected |
| Completion attention coverage | completion evaluates every exact HUMAN attention constraint exactly once |
| Completion hard attention override | `TRIGGERED / UNCERTAIN` at completion reaches `WAITING_HUMAN`, never `COMPLETED` |
| Completion all-clear | all exact completion-time constraints `CLEAR` may proceed only through final reread/CAS |
| Stale completion evidence | older worker result cannot close work after newer human activity |
| Completion cognition race | human activity arriving during completion cognition is caught by the final live-peer reread and invalidates stale completion |
| Concurrent completion state | durable append CAS prevents completion if another supervisor transition wins after cognition |
| Authority-shaped cognition field | undeclared field such as `execute=true` fails strict decoding |
| Attention constraints | every exact HUMAN attention constraint must be checked exactly once |
| Hard attention override | `TRIGGERED / UNCERTAIN` explicit HUMAN rule overrides cognition `KEEP_MOVING` |
| Invalid attention shape | human-required attention paired with `urgency=none` fails closed instead of being normalized |
| Cognition continuity | ephemeral cognition conversation may be reused, but each prompt contains exact durable state |
| Pilot CLI smoke | `start / drive / answer / status / rearm-dispatch` parse through the experimental CLI surface |
| Driver bound | repeated continuation/revision remains bounded by `max_steps` |
| M6.5 replay contract | no automatic retry after ambiguous provider effect |
| Exact CWA identity | optional web dependency and contract gate bind merged PR14.6 commit exactly |
| Canonical 429 handling | bounded retry applies only to idempotent canonical GET pagination; partial history and product-write retry remain forbidden |

## Existing regression gates

The full repository CI remains required because M6.6 composes M6.2–M6.5 rather than replacing them:

- Ubuntu Python 3.11 / 3.12 / 3.13 tests;
- Windows Python 3.12.4 / 3.13 tests;
- Ubuntu and Windows ChatGPT web-provider contract checks;
- CLI smoke;
- CodeQL for Python and Actions.

The upstream CWA PR14.6 candidate CI #1040 completed successfully before squash merge. Codexia must still rerun its own full matrix after repinning the exact merged CWA revision.

## Real pilot gates

Automated tests are necessary but not sufficient. M6.6 remains incomplete until two actual delegated-work runs are reviewed.

### Vertical A — ongoing software/research work

The historical Vertical A is retained as evidence because it found the semantic and transport defects. The repaired implementation should be validated on a clean new work item.

Evidence should show:

- at least two routine worker cycles without human scheduling turns;
- worker-side `REVISE` remains background when no human judgment is required;
- a tool-using product turn does not create a completion/provenance livelock;
- no stale dispatch or stale completion overwrites intervening human activity;
- bounded canonical-read throttling does not create product-write retry authority;
- final stop is completion, genuine human attention, bounded step budget, or fail-closed ambiguity — not a fabricated success.

### Vertical B — standalone non-project knowledge work

Evidence should show:

- an initial substantial request can begin without a prewritten project plan;
- the worker result is iterated/challenged/deepened through ordinary turns without the human between those turns;
- Codexia decides completion only from current terminal logical worker evidence after exact completion-time HUMAN-attention evaluation and the final live-peer reread;
- the returned result is materially more complete than a single worker turn and satisfies the original handoff interpretation.

### Human-attention resume

Across either vertical, demonstrate at least one real case where:

```text
WAITING_HUMAN
→ human answers through the Codexia pilot surface, not worker chat
→ answer binds exact waiting event + attention decision
→ same work_id becomes READY with unchanged worker cursor
→ fresh M6.2/M6.4 judgment sees the answer even if a newer observation also exists
→ next governed worker turn
```

The evidence review should record work id, stop reason, provider turn count, relevant supervisor sequence/digests, and a concise qualitative assessment of whether the interruption was genuinely necessary.
