# M5.2 — Governed Automation State Machine Source Audit

## Audit question

M5.2 is allowed to advance a frozen automation plan, but it must not turn orchestration progress into execution authority or replay permission.

Primary boundary:

```text
automation progress != authorization authority
```

Recovery boundary:

```text
recovered progress != side-effect replay permission
```

Budget boundary:

```text
durable work determines budget consumption
!=
rollback-prone automation counters
```

## Threat-to-evidence map

| Threat | M5.2 evidence |
| --- | --- |
| Automation silently approves its own process | Public execution continuation requires an externally supplied `AuthorizationReceipt`; the state machine never calls an approval-decision API to create one. |
| Caller bypasses the frozen run set | Public progression accepts only `automation_id`; slots are derived from the exact frozen M4.4 policy. |
| Caller chooses a favorable arm/seed/run ordering | v1 slot order is deterministic baseline-first then candidate, each in frozen seed order. |
| A foreign run is adopted into a declared slot | Recovery checks exact deterministic run UUID, arm experiment id, manifest digest, ordinal, and seed; mismatch fails integrity. |
| Restart resets a mutable step counter | `steps_used` and run counts are derived from irreversible M4/M3 facts rather than stored as an independent mutable counter. |
| Budget is checked only after work happens | Recovery exposes `STOPPED_BUDGET` before the next run registration/preparation/execution stage is admitted. |
| Receipt is supplied after the frozen step budget is already exhausted | `continue_authorized()` first recovers state; only exact `PAUSED_AUTHORIZATION_REQUIRED` can consume a receipt, so `STOPPED_BUDGET` cannot execute. |
| Repeated `advance()` at an approval boundary runs the process anyway | `advance()` has no execution branch for `PAUSED_AUTHORIZATION_REQUIRED`; state/output remain unchanged. |
| Restart creates a new proposal and invites double approval | Exact M3/M4.3 binding recovery returns the already-durable proposal; a partial unbound-proposal state fails closed rather than minting a replacement target. |
| Crash after authority chronology has advanced causes automatic retry | `AUTHORIZED` or otherwise inconsistent post-authority recovery becomes `STOPPED_ERROR`; process replay is forbidden. |
| M5 invents a second executor/evidence path | Execution uses `GovernedPythonJsonRunner`, `SqliteRunExecutionRegistry`, and `SqlitePhysicalEvidenceRegistry` unchanged. |
| Physical output is accepted without governed execution provenance | Completion requires M4.3 physical recovery, which transitively verifies exact execution/observation provenance and actual output bytes. |
| Run counted complete before immutable evidence closure | `runs_completed` requires stage 3, successful verified physical evidence, and an irreversible M4 run seal. |
| Downstream M5.3 seals one experiment and a restart bricks the handoff | M5.2 permits sequential arm sealing after the complete run set while validating that any sealed arm contains only complete exact automated slots. |
| Downstream sealing hides an incomplete arm | `_derived()` rejects a sealed experiment if any declared arm slot lacks terminal verified sealed evidence. |
| Automation evaluates or rewrites the frozen scientific criterion | M5.2 has no comparison-result or conclusion publication path; M4.4/M4.5 remain downstream. |

## Authority path review

The only method that can trigger process execution is:

```text
continue_authorized(automation_id, receipt=...)
```

Before calling the M4.3 runner it:

1. recovers the exact frozen automation state;
2. requires `PAUSED_AUTHORIZATION_REQUIRED`;
3. reconstructs the exact previously durable `PreparedPythonJsonRun` from M4/M3 state;
4. passes the externally supplied receipt into the existing `GovernedPythonJsonRunner.execute_authorized()` path.

That existing path performs M2 receipt verification, durable M3 authorization recording/one-shot consumption, governed process execution, M3 execution/observation recording, M4.3 execution evidence, and physical evidence finalization.

M5.2 therefore coordinates an authority path; it does not become authority.

## Derived budget review

For each frozen slot M5.2 derives one stage from authoritative durable state:

```text
0 = absent
1 = run registered
2 = exact execution proposal/binding durable
3 = authority/execution chronology terminal
```

`steps_used = Σ(stage)`.

No M5.2 SQL table stores an independently editable step balance. The frozen `AutomationBudget` remains in the M5.1 plan and the runtime compares derived work against that precommitted maximum.

This means database edits that merely try to reduce a hypothetical automation counter do not exist as an attack surface in v1.

## Replay review

Safe pre-authority recovery is intentionally different from unknown post-authority recovery.

Safe case:

```text
run + exact proposal/binding durable
+ M3 action still PROPOSED
+ no output
→ recover same PAUSED_AUTHORIZATION_REQUIRED
```

Unsafe/ambiguous cases include:

```text
M4 execution chronology says AUTHORIZED but no terminal evidence
```

or:

```text
M3 action advanced beyond PROPOSED while matching M4 evidence is absent
```

These become `STOPPED_ERROR` rather than another execution attempt.

The audit deliberately prefers lost liveness over duplicated side effects at this boundary.

## M5.3 handoff review

M5.2 owns run-set advancement only. Its terminal scientific-execution state is:

```text
RUN_SET_COMPLETE
```

At that point every declared slot is independently sealed and physically verified, but the two experiment roots may still be open.

M5.3 must be free to perform:

```text
seal baseline experiment
→ possible restart
→ seal candidate experiment
→ evaluate exact M4.4 comparison
→ publish exact M4.5 conclusion
```

Therefore M5.2 recovery cannot require the two experiment-seal events to be atomic. It instead validates any already-sealed arm against the complete automated run set and preserves `RUN_SET_COMPLETE` throughout the downstream sealing handoff.

## Composition matters

M5.2 does not prove its safety in isolation. The guarantee is the composition:

```text
M5.1 frozen target/budget/stop policy
+ M5.2 deterministic state machine
+ M3 durable one-shot authority chronology
+ M4.3 governed execution and physical evidence
```

Removing any one of these changes the claim materially.

## Explicit limits

This audit does not claim:

- hostile local-machine/kernel attestation;
- interactive proof that a HUMAN-source receipt actor is physically the intended human;
- automatic recovery from every crash point while preserving liveness;
- autonomous scheduling/background operation;
- automatic experiment design;
- statistical or scientific correctness beyond the already-frozen M4 policy;
- automatic comparison/conclusion closure;
- generic sandboxing beyond the existing M2 execution guarantees.

## Closure criterion

M5.2 is complete only if the exact reviewed candidate demonstrates:

> The coordinator can advance only the exact frozen run set, derives monotonic budget use from authoritative durable facts, pauses before each external authorization boundary, resumes only through an externally supplied exact receipt, never replays an ambiguous post-authority side effect during recovery, and hands a complete verified sealed run set to M5.3 without creating another authority or execution architecture.
