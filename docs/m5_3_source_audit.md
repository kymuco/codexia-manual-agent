# M5.3 — First Real Bounded Automation Source Audit

## Audit question

M5.3 is allowed to finish the scientific closure of one already-complete M5.2 run set, but it must not turn post-execution orchestration into a new authority path, a free budget zone, or a way to rewrite the frozen scientific criterion after evidence is visible.

Primary boundary:

```text
bounded automation progress != execution authority
```

Budget boundary:

```text
scientific closure consumes the same frozen M5 budget
```

Scientific-policy boundary:

```text
automation identity != permission to choose a new comparison or conclusion
```

## Threat-to-evidence map

| Threat | M5.3 evidence |
| --- | --- |
| Post-run automation executes a process without another HUMAN receipt | `GovernedAutomationClosure` has no receipt or process-execution API. All four real process executions happen earlier through M5.2 `continue_authorized()` with externally supplied exact receipts. |
| M5.3 introduces a second executor/evidence path | Closure uses only existing `SqliteLabRegistry.seal_experiment`, `SqliteComparisonResultRegistry.evaluate`, and `SqliteAdjudicatedConclusionRegistry.publish`; run execution/evidence remain M4.3. |
| Caller chooses a favorable policy after evidence | Public closure API accepts only `automation_id`; `policy_id` is recovered from the already-frozen M5.1 plan, which itself was frozen before runs. |
| Caller swaps baseline/candidate during closure | Experiment ids and manifest digests come only from the frozen M4.4 policy recovered through the frozen M5 plan. |
| Closure gets free work after the M5.2 run budget | Four post-run durable stages are added to the same `max_steps`; real closure freezes `16` total steps before work begins. |
| Restart resets closure budget | `closure_steps_used` is derived from irreversible experiment seals/result/conclusion state; no independent M5.3 counter is persisted. |
| External/manual durable work bypasses the budget | Recovery counts whatever authoritative closure state already exists. State beyond the frozen budget fails integrity; exact budget exhaustion becomes `STOPPED_BUDGET`. |
| Budget exhaustion is only a label after conclusion publication | A regression freezes a budget that reaches a durable comparison at the exact limit; recovery returns `STOPPED_BUDGET`, `conclusion is None`, and repeated `advance()` is inert. |
| Crash between baseline and candidate seals bricks recovery | Baseline-only sealing is a named derived phase and recovers in a fresh coordinator; M5.2 already validates that a sealed arm contains its exact complete run set. |
| Crash after comparison persistence causes another comparison choice | M4.4 result identity is deterministic for the frozen policy; recovery revalidates the existing result and M5.3 derives `COMPARISON_COMPLETE`. |
| Crash after conclusion persistence republishes or rewrites semantics | M4.5 conclusion is deterministic derived state; M5.3 derives terminal `STOPPED_CONCLUSION` and repeated `advance()` is inert. |
| Automation weakens the threshold because candidate is visibly better | Real fixture preserves threshold `180`; observed effect remains `176`, therefore result is still `REFUTED`. |
| Automation turns policy-local REFUTED into universal scientific falsity | Conclusion publication is delegated to existing M4.5 bounded adjudication with fixed `frozen_comparison_policy.v1` scope and canonical wording. |
| Fresh-process recovery secretly reruns experiments | Child receives only SQLite path + `automation_id`; no receipt is supplied or execution continuation called, and all four physical files retain byte-identical content and unchanged `mtime_ns`. |
| Conclusion does not actually stop automation | Terminal state is `STOPPED_CONCLUSION`; another closure `advance()` returns exactly the same state. |

## Public API review

The M5.3 coordinator has only two public orchestration methods:

```text
recover(automation_id)
advance(automation_id)
```

It does not accept:

- `policy_id`;
- experiment ids;
- run ids;
- metric values;
- thresholds;
- direction;
- seeds;
- a result/outcome;
- a verdict/summary;
- an approval decision;
- an authorization receipt;
- a process proposal or argv.

The exact scientific closure target is therefore recovered rather than caller-selected.

## Derived closure state

M5.3 creates no mutable closure ledger.

After M5.2 has proven `RUN_SET_COMPLETE`, the post-run stage is reconstructed as:

```text
0 = both experiments open, no result/conclusion
1 = exact baseline experiment sealed
2 = exact baseline + candidate experiments sealed
3 = exact M4.4 comparison result durable/recoverable
4 = exact M4.5 conclusion durable/recoverable
```

Total budget use is:

```text
M5.2 run steps + M5.3 derived closure stage
```

For the real two-seed comparison:

```text
12 run steps + 4 closure steps = 16 total
```

If derived durable work exceeds `max_steps`, recovery raises `LabPersistenceIntegrityError`. If the current exact stage consumes the remaining budget but a later closure transition is still pending, state becomes `STOPPED_BUDGET`.

This preserves the M5.1 promise that budget is precommitted rather than selected after intermediate evidence.

## Scientific transition review

### Experiment sealing

M5.3 seals the exact baseline first, then the exact candidate, using ids/digests from the recovered frozen policy.

The order is deterministic. A candidate-sealed/baseline-open state is rejected as an invalid M5.3 closure lineage rather than normalized into a different order.

### Comparison

Once both arms are sealed, M5.3 calls only:

```text
SqliteComparisonResultRegistry.evaluate(frozen_policy_id)
```

M4.4 therefore remains responsible for:

- exact final sealed run sets;
- exact declared seeds/ordinals;
- physical evidence recovery;
- metric/unit matching;
- exact rational aggregation/effect;
- frozen threshold/direction;
- deterministic `SUPPORTED` / `REFUTED` / `INCONCLUSIVE` result.

M5.3 does not duplicate or weaken those checks.

### Conclusion

After the exact result is durable, M5.3 calls only:

```text
SqliteAdjudicatedConclusionRegistry.publish(frozen_policy_id)
```

M4.5 therefore remains responsible for bounded semantic scope, deterministic verdict/summary, and authoritative rederivation from M4.4 state.

M5.3 does not add free-form conclusion text.

## Authority composition review

M5.3 itself performs no M2 action. The complete authority-sensitive path remains:

```text
M5.2 exact pending proposal
→ external AuthorizationReceipt
→ existing M2 verification/consumption
→ durable M3 chronology
→ existing M4.3 governed execution
→ physical evidence
```

Only after every exact run is terminal, physically verified, and sealed can M5.3 begin.

This separation is intentional:

```text
run authority boundary = M5.2 + M2/M3/M4.3
scientific closure      = M5.3 + existing M4.2/M4.4/M4.5
```

The latter does not become permission for the former.

## Restart/replay review

Fresh-process M5.3 recovery receives only:

```text
SQLite path
+ automation_id
```

`GovernedAutomationClosure.recover()` reconstructs the frozen plan, M5.2 terminal run state, frozen policy, experiment seal state, comparison result, and conclusion.

Although the composed runtime contains the existing M5.2/M4.3 recovery machinery, the child supplies no receipt and calls no execution-continuation method. The regression additionally verifies every governed physical output keeps identical bytes and `mtime_ns`, ruling out a hidden rerun that happens to reproduce the same numeric result.

## Real inconvenient outcome

The real M4.4.3/M4.5.3 integration-error case is retained unchanged.

Frozen requirement:

```text
candidate lower-is-better improvement >= 180
```

Recovered physical evidence:

```text
baseline mean = 184
candidate mean = 8
effect         = 176
```

Therefore:

```text
176 < 180
→ REFUTED
```

The full automated closure must preserve this inconvenient result and the bounded REFUTED conclusion. A system that changed the policy after seeing `176` would fail the purpose of M4.4 and M5.

## Explicit limits

This audit does not claim:

- unattended background scheduling;
- self-issued HUMAN authorization;
- automatic experiment or hypothesis design;
- automatic choice of the next scientific question;
- statistical correctness beyond the frozen declared policy;
- universal truth/falsity from one bounded comparison;
- hostile-machine/kernel attestation;
- liveness after every ambiguous post-authority crash;
- arbitrary filesystem, Git, network, or external-service authority.

## Closure criterion

M5 is complete only if the exact reviewed candidate demonstrates:

> A single pre-frozen automation plan can progress the exact governed run set and the exact scientific closure under one immutable budget, requiring external authority at every process boundary, deriving all post-run targets from frozen state, preserving an inconvenient precommitted REFUTED result, stopping on budget/conclusion, and recovering the same terminal state after restart without replay or a second executor/evidence/authority architecture.
