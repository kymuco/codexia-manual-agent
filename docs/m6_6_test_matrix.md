# M6.6 Test Matrix — First General Daily-Use Pilot

## Harness gates

The candidate must prove the live cognition bridge without weakening existing M6 boundaries.

| Gate | Expected result |
|---|---|
| Two routine worker turns in one driver call | advances without human `continue` and ends only at a governed stop/completion |
| Genuine material choice | enters `WAITING_HUMAN` before provider send |
| Pilot human answer | explicit HUMAN answer outside worker chat binds exact waiting state and returns only to `READY` |
| Human answer cursor isolation | recording the answer does not modify the worker ChatGPT cursor/history |
| Unsolicited human answer | answer outside exact `WAITING_HUMAN` fails closed |
| Account-side human activity | existing M6.3 `EXTERNAL_USER` observation path remains valid and distinct from pilot answer evidence |
| Worker follow-up provenance | cognition cannot replace terminal exact worker text |
| Completion authorship | WORKER-authored completion is rejected |
| Completion evidence | completion before terminal worker evidence is rejected |
| Stale completion evidence | older worker result cannot close work after newer human/external activity |
| Authority-shaped cognition field | undeclared field such as `execute=true` fails strict decoding |
| Attention constraints | every exact HUMAN attention constraint must be checked exactly once |
| Cognition continuity | ephemeral cognition conversation may be reused, but each prompt contains exact durable state |
| Driver bound | repeated continuation/revision remains bounded by `max_steps` |
| M6.5 replay contract | no automatic retry after ambiguous provider effect |

## Existing regression gates

The full repository CI remains required because M6.6 composes M6.2–M6.5 rather than replacing them:

- Ubuntu Python 3.11 / 3.12 / 3.13 tests;
- Windows Python 3.12.4 / 3.13 tests;
- Ubuntu and Windows ChatGPT web-provider contract checks;
- CLI smoke;
- CodeQL for Python and Actions.

## Real pilot gates

Automated tests are necessary but not sufficient. M6.6 remains incomplete until two actual delegated-work runs are reviewed.

### Vertical A — ongoing software/research work

Evidence should show:

- at least two routine worker cycles without human scheduling turns;
- any worker-side `REVISE` remains background when no human judgment is required;
- no stale dispatch overwrites intervening human activity;
- final stop is completion, genuine human attention, bounded step budget, or fail-closed ambiguity — not a fabricated success.

### Vertical B — standalone non-project knowledge work

Evidence should show:

- an initial substantial request can begin without a prewritten project plan;
- the worker result is iterated/challenged/deepened through ordinary turns without the human between those turns;
- Codexia decides completion only from terminal exact worker evidence;
- the returned result is materially more complete than a single worker turn and satisfies the original handoff interpretation.

### Human-attention resume

Across either vertical, demonstrate at least one real case where:

```text
WAITING_HUMAN
→ human answers through the Codexia pilot surface, not worker chat
→ answer binds exact waiting event + attention decision
→ same work_id becomes READY with unchanged worker cursor
→ fresh M6.2/M6.4 judgment
→ next governed worker turn
```

The evidence review should record work id, stop reason, provider turn count, relevant supervisor sequence/digests, and a concise qualitative assessment of whether the interruption was genuinely necessary.
