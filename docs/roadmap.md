# Roadmap

## Public baseline

The public repository begins from a clean source-available snapshot. Earlier private development history is not part of this repository. Milestone status below describes the implementation present in the public baseline without depending on private pull requests or commit lineage.

## M1 — Read-only agent runtime

Complete.

- bounded model/tool turns and observations;
- read-only workspace inspection and fixed Git status access;
- strict structured model protocol;
- provider abstraction and conversation continuation;
- optional `chatgpt-web-adapter` transport;
- no remote-model side effects.

## M2 — Governed local authority and execution

Complete through M2.6.

### M2.0 — Local authority contracts

Complete.

- digest-bound action proposals and receipts;
- locally derived capability/risk;
- explicit human-vs-policy decision source;
- `always`, `risky`, and `never` approval modes;
- atomic single-use receipt consumption;
- canonical `PROPOSED → AUTHORIZED → EXECUTED → OBSERVED` lifecycle;
- terminal denial.

### M2.1–M2.2 — Process execution and command admission

Complete.

- structured argv and `shell=False`;
- workspace-bound canonical cwd;
- minimal child environment;
- executable identity binding and revalidation;
- timeout/output budgets and process-tree containment;
- narrow command-family admission and capability envelopes;
- generic model process execution remains disabled.

M2.1 process containment is not presented as a general filesystem or network sandbox for arbitrary approved child code.

### M2.3–M2.4 — Workspace mutation and governed patches

Complete.

- explicit create/replace workspace actions;
- traversal, sensitive-path, symlink/junction, and control-tree guards;
- exact preimage/postimage binding;
- no-clobber create and strict replace;
- bounded multi-file patch proposals;
- pre-authority drift checks;
- atomic multi-file application where the required platform primitive is available;
- exact terminal observations and durable recovery;
- bounded model patch requests remain separate from local authorization.

Linux workspace mutation remains intentionally fail-closed until a commit boundary satisfying the required ancestry and atomicity properties is available.

### M2.5–M2.5.1 — Git mutation and governed network transport

Complete.

- Git commit and Git push are independent authorities;
- exact repository/ref/index/tree/commit identity binding;
- compare-and-swap ref mutation;
- governed SSH and HTTPS push transport;
- route, host-key/TLS, credential-source, proxy/helper, and lease binding;
- no generic Git argv or ambient credential authority.

### M2.6 — Bounded delegation and human escalation

Complete.

Primary invariant:

```text
delegation cannot mint authority
```

- immutable root/parent lineage;
- child capability can only stay the same or shrink;
- atomic budget reservation;
- root depth/node limits;
- explicit human escalation;
- continuation resumes orchestration only and does not grant the escalated action.

## M3 — Durable coordination

Complete through M3.2.

### M3.1 — Persistent sessions and authority chronology

Complete and included in the clean public baseline.

- append-only SQLite session chronology;
- hash-chained exact event receipts;
- durable provider/tool/authority evidence;
- explicit unknown-provider-outcome state;
- recovery of conversation identity and cumulative counters;
- consumed authority never becomes fresh after restart;
- no automatic side-effect replay.

See `docs/m3_persistent_sessions_event_receipts.md` and `docs/m3_1_source_audit.md`.

### M3.2 — Durable bounded-delegation recovery

Complete and included in the clean public baseline.

- root-scoped append-only orchestration chronology;
- durable child budget reservations and consumption;
- non-replayable request claims;
- durable escalation/continuation state;
- durable cancellation;
- exact recovery and derived-index verification;
- recovery reconstructs state and never launches autonomous child work or grants M2.x authority.

See `docs/m3_2_durable_delegation_recovery.md` and `docs/m3_2_source_audit.md`.

## M4 — Computational Lab

M4.1 through M4.5 are complete.

The M4 series turns Codexia's governed runtime into a reproducible computational research surface without widening execution authority.

### M4.1 — Computational Lab Core Contracts

Complete.

- immutable digest-bound `Hypothesis`;
- exact `ExperimentManifest`;
- `ExperimentRun`, `ArtifactRecord`, and `MetricRecord` lineage;
- evidence-bounded `Conclusion` records;
- strict decoders and structural/numeric/evidence budgets;
- provenance is not treated as scientific truth, actor authenticity, or physical artifact verification;
- no new execution or mutation authority.

See `docs/m4_1_computational_lab_core_contracts.md` and `docs/m4_1_source_audit.md`.

### M4.2 — Durable experiment/run/evidence registry

Complete.

- authoritative per-experiment append-only event chronology;
- durable experiment/run/metric/artifact registration;
- exact lineage replay and scoped uniqueness constraints;
- irreversible evidence/experiment sealing;
- serialized concurrent mutations;
- atomic event/head/index publication;
- deterministic writer/recovery transition logic;
- corruption detection and exact derived-index verification;
- registry closure does not claim execution success;
- artifact registration records metadata, not physical-byte verification;
- no new capability, process, filesystem, provider, Git, delegation, or scheduler authority.

See `docs/m4_2_durable_experiment_registry.md` and `docs/m4_2_source_audit.md`.

### M4.3 — Governed Experiment Execution

**Complete.**

Primary invariant:

```text
declared run != executed run
```

M4.3 closes the first real end-to-end scientific execution/evidence loop without widening M2 authority.

Demonstrated vertical slice:

- a registered M4 run is bound before authorization to one exact M2 process proposal;
- exact HUMAN authorization and M3 authority chronology are part of the durable execution provenance;
- a forged/rebound observation cannot make an arbitrary run execution-backed;
- one admitted local Python profile derives exact execution from manifest state and remains bound to the current exact Python executable;
- final result bytes must equal the exact retained stdout bytes of the governed execution;
- physical artifact size/SHA-256 and one declared numeric metric are derived locally from those bytes;
- missing, mutated, redirected, mismatched, malformed, or ambiguous evidence fails closed;
- run/experiment evidence can be irreversibly sealed;
- a fresh Python process can reconstruct the exact hypothesis → manifest → run → governed execution → verified physical evidence chain from durable state without conversational history and without rerunning the experiment;
- an already-sealed run cannot be prepared for replay.

The M4.3.3 closure fixture uses the frozen hypothesis that, for `n=37`, the sum of the first 37 positive odd integers equals `37²`, with `absolute_error != 0` declared as the falsification criterion before execution.

See `docs/m4_3_governed_experiment_execution_plan.md`, `docs/m4_3_3_first_real_experiment.md`, and `docs/m4_3_source_audit.md`.

### M4.4 — Comparison and Falsification

**Complete.**

Primary invariant:

```text
verified evidence != precommitted comparison
```

M4.4 freezes one exact comparison policy before either arm has run evidence, admits only the final complete verified evidence set, and recomputes the same policy-local result after restart without rerunning experiments.

Demonstrated vertical slice:

- exact hypothesis/baseline/candidate manifest identity is frozen before either arm registers a run;
- metric, direction, minimum effect, ordered repetition/seed identities, mean aggregation, and missing/failure policy are precommitted;
- one unordered exact manifest pair admits only one deterministic v1 policy identity, preventing policy shopping and baseline/candidate reversal shopping;
- freeze-vs-run ordering is serialized in the same SQLite trust domain rather than inferred from wall-clock timestamps;
- both comparison arms must be irreversibly sealed, making the final run set complete before evaluation;
- caller-supplied runs, metric values, subsets, thresholds, or alternate directions are not part of the evaluation API;
- every admitted value is recovered through M4.3 physical evidence;
- extra runs, seed/ordinal substitution, metric mismatch, evidence mutation, result tamper, and favorable partial aggregation fail closed;
- complete means/effect use exact rational arithmetic and bind the original metric evidence identities;
- a fresh Python process can reconstruct the exact policy → verified evidence → comparison result chain from durable state and physical files without rerunning experiments.

The M4.4.3 closure fixture compares left-Riemann and trapezoid approximations of `∫₀¹x²dx` at `n=8`. The policy freezes a required lower-is-better improvement of at least `180` common-denominator error-numerator units. Verified evidence yields baseline `184`, candidate `8`, and effect `176`, so the precommitted claim is correctly **REFUTED** even though the candidate is substantially better.

See `docs/m4_4_comparison_falsification_plan.md`, `docs/m4_4_2_verified_evidence_comparator.md`, `docs/m4_4_3_first_real_comparison.md`, and `docs/m4_4_source_audit.md`.

### M4.5 — Conclusion Adjudication

**Complete.**

Primary invariants:

```text
comparison outcome != unrestricted scientific conclusion
human/model wording != conclusion adjudication authority
persisted conclusion != conclusion authority
```

M4.5 turns one exact recovered M4.4 result into one deterministic policy-scoped conclusion without allowing caller/model prose or database text to become adjudication authority.

Demonstrated vertical slice:

- M4.4 comparison outcomes map deterministically to bounded `SUPPORTED` / `REFUTED` / `INCONCLUSIVE` conclusion verdicts;
- the comparison conclusion has fixed scope `frozen_comparison_policy.v1` and one canonical bounded summary per outcome;
- exact hypothesis, baseline/candidate manifests, policy/freeze, and comparison result identities/digests are jointly bound;
- callers cannot provide verdict, summary, conclusion id, evidence subset, replacement policy/result, or alternate arm ordering through the authoritative creation path;
- direct dataclass semantic construction is disabled; the strict decoder validates structure but is not provenance authority by itself;
- durable conclusion rows are derived cache only: recovery re-recovers exact M4.4 dependencies and recomputes the expected conclusion;
- a structurally self-consistent forged verdict with a freshly valid SHA-256 is rejected when authoritative M4.4 recovery reproduces a different conclusion;
- physical evidence mutation invalidates conclusion recovery transitively through M4.3 and M4.4;
- the M4.4.3 real `REFUTED` integration comparison is adjudicated without weakening the threshold or upgrading policy-local refutation into universal scientific falsity;
- a fresh Python process reconstructs the exact policy → result → bounded conclusion chain from durable state without an experiment runner, new authorization, replacement criterion, or caller-authored conclusion text;
- all four governed physical result files remain byte-identical with unchanged `mtime_ns` across fresh-process conclusion recovery.

The M4.5.3 closure therefore completes the manual Computational Lab loop from hypothesis through governed execution, verified evidence, precommitted comparison, bounded adjudication, and durable restart recovery without replay.

See `docs/m4_5_conclusion_adjudication_plan.md`, `docs/m4_5_2_durable_conclusion_recovery.md`, `docs/m4_5_3_first_real_conclusion.md`, and `docs/m4_5_source_audit.md`.

## Near-term anti-drift rule

M5 closes bounded automation of the already-proven manual M4 loop. M6 must now prove practical delegated-work continuity rather than adding UI or generic autonomy for its own sake. Each M6 step should directly reduce the amount of routine human scheduling needed to keep real work moving. Security, persistence, and transport hardening should interrupt only for a concrete blocker, demonstrated vulnerability, or invariant violation.

## M5 — Bounded automation

**Complete through M5.3.**

Primary invariant:

```text
automation intent != execution authority
```

M5 automates the already-proven M4 research loop under precommitted budgets and stop policies while preserving the existing M2/M3 authority and replay boundaries.

### M5.1 — Frozen Automation Plan

**Complete.**

- one exact frozen M4.4 policy maps to one deterministic v1 automation identity;
- target policy/freeze, exact arm manifests, required run count, budget, and stop policy are immutable and digest-bound;
- `max_runs` cannot exceed the exact frozen comparison run set;
- strict v1 stop rules require pause on authorization, stop on error, stop on budget exhaustion, and stop on conclusion;
- the plan must freeze before either experiment advances beyond the M4.4 policy-freeze anchor;
- freeze-vs-run ordering is serialized by the shared SQLite writer domain rather than wall-clock comparison;
- both arms must use the already-proven `python-inline-json-result.v1` M4.3 execution profile;
- one policy cannot acquire alternate budgets after the first plan is frozen;
- durable recovery validates exact M4 lineage and freeze anchors but does not execute or replay work;
- no M2 proposal, approval decision, authorization receipt, process execution, mutation, Git, network, or scheduler authority is introduced.

See `docs/m5_1_frozen_automation_plan.md`.

### M5.2 — Governed Automation State Machine

**Complete.**

Primary invariant:

```text
automation progress != authorization authority
```

- the exact baseline/candidate run slots are derived only from the frozen M5.1/M4.4 lineage;
- deterministic UUIDv5 run/session identities prevent caller-selected slot substitution;
- state and budget use are reconstructed from authoritative durable M3/M4 facts rather than a rollback-prone automation counter;
- `advance()` performs only bounded non-authority progression and becomes inert at the authorization boundary;
- the exact pending M2 proposal survives fresh-process recovery without execution or proposal replacement;
- execution continuation requires an externally supplied exact `AuthorizationReceipt` and reuses the existing M2/M3/M4.3 authority path;
- frozen step/run budgets stop work before the next irreversible stage;
- ambiguous post-authority recovery stops fail-closed rather than replaying a process;
- foreign runs, non-contiguous progression, incomplete sealed arms, and authority/evidence mismatch fail integrity;
- every completed slot requires governed execution provenance, verified physical evidence, and an irreversible M4 run seal;
- the complete sealed run set remains recoverable while downstream M5.3 sequentially seals the two experiment arms;
- no scheduler, second executor, alternate evidence path, automatic comparison, or conclusion authority is introduced.

See `docs/m5_2_governed_automation_state_machine.md` and `docs/m5_2_source_audit.md`.

### M5.3 — First Real Bounded Automation Closure

**Complete.**

Primary boundaries:

```text
bounded automation progress != execution authority
scientific closure consumes the same frozen M5 budget
```

- the existing M4.4.3/M4.5.3 inconvenient `REFUTED` integration-error case is reused unchanged;
- the M5.1 plan freezes an exact `16`-step / `4`-run budget before evidence: `12` governed run stages plus `4` scientific closure stages;
- every process pauses at the M5.2 authorization boundary and proceeds only with an externally supplied exact HUMAN-source test receipt;
- `GovernedAutomationClosure` accepts only `automation_id` and derives the exact policy, arm identities, and post-run targets from frozen durable state;
- closure advances one non-authority durable stage at a time: baseline experiment seal → candidate experiment seal → exact M4.4 comparison → exact M4.5 bounded conclusion;
- closure budget use is derived from irreversible M4 state rather than a new mutable M5 counter;
- a budget-limited regression proves automation can stop after a durable comparison but before conclusion publication;
- the precommitted threshold remains `180`; recovered effect remains `176`, so full automation preserves `REFUTED` rather than moving the criterion;
- conclusion publication preserves the bounded `frozen_comparison_policy.v1` scope and reaches terminal `STOPPED_CONCLUSION`;
- repeated advance after budget/conclusion stop is inert;
- fresh-process recovery receives only SQLite path + stable `automation_id` and reconstructs the exact terminal plan/result/conclusion state;
- all four governed physical result files remain byte-identical with unchanged `mtime_ns` across terminal fresh-process recovery;
- no scheduler, second executor, alternate evidence path, policy selector, conclusion author, or new execution authority is introduced.

The demonstrated complete bounded loop is:

```text
frozen M4.4 policy
→ frozen M5.1 plan/workspace/budget/stop rules
→ deterministic M5.2 run progression
→ external HUMAN authorization at each process boundary
→ existing M2/M3/M4.3 governed execution and physical evidence
→ complete sealed run set at 12/16 steps
→ deterministic M5.3 experiment seals
→ exact M4.4 REFUTED comparison
→ exact bounded M4.5 REFUTED conclusion
→ STOPPED_CONCLUSION at 16/16 steps
→ fresh-process recovery without replay
```

See `docs/m5_3_first_real_bounded_automation.md` and `docs/m5_3_source_audit.md`.

## M6 — Delegated Work Continuity

**In progress through M6.1.**

North-star property:

```text
human absence != work suspension
```

M6 generalizes Codexia from one bounded scientific automation vertical into a governed work runtime. The delegated unit is work, not necessarily a software project: ongoing coding, research, information gathering, analysis, artifact preparation, or another human objective may all use the same continuity layer.

### M6.1 — General Work Handoff and Attention Boundary

**Complete candidate.**

Primary boundaries:

```text
human handoff != inferred work interpretation
worker statement != human instruction
resource reference != access authority
dynamic attention judgment != execution authority
plan != prerequisite for delegation
```

- `WorkStatement` preserves explicit `human / codexia / worker / system` provenance;
- `WorkResourceRef` can point at chats, repositories, files, URLs, or other context without granting access;
- `WorkHandoff` stores the exact HUMAN-authored objective, human constraints, optional plan reference, context resources, and human attention constraints;
- a non-project research request is valid with no prewritten plan;
- `WorkIntentInterpretation` is a separate derived record, so changing inferred completion/depth/scope never rewrites human handoff identity;
- depth is free attributed interpretation rather than a fixed probe/MVP/production mode;
- `AttentionAssessment` binds the exact handoff, interpretation, and work checkpoint while remaining non-authoritative;
- strict decoding/digests reject statement tamper, cross-handoff rebinding, and smuggled authority-shaped fields;
- no worker orchestration, continuation admission, scheduler, notification transport, persistence, local execution, or new authority is introduced.

See `docs/m6_1_general_work_handoff_attention.md` and `docs/m6_1_source_audit.md`.

### M6.2 — Continuation Admission

**Next.**

Decide whether a worker-proposed next action actually follows from the current handoff, exact interpretation, evidence, constraints, and delegation scope.

Primary boundary:

```text
worker proposal != admitted continuation
```

Expected decisions are bounded equivalents of continue, revise, reject, or require human judgment; the admission layer must not mint execution authority.

### M6.3 — Chat / Codexia Peer Loop

Planned.

Connect the real ChatGPT conversation surface so Codexia and the cognitive worker remain distinct peers. Codexia-authored continuation must never be recorded semantically as a HUMAN message, and the human must be able to enter the same chat directly without a manual-mode takeover ceremony.

### M6.4 — Dynamic Attention

Planned.

Use exact work state, human constraints, current evidence, alternatives, reversibility, and cognition to decide whether work can continue without interruption. Attention preferences may later influence this judgment but must never become execution authority.

### M6.5 — Background Work Supervisor

Planned.

Maintain multiple delegated works durably, react to worker/tool/external state, and keep ready work moving independently of whether the human is currently viewing the chat. Recovery must preserve exact pending work and must not replay ambiguous effects.

### M6.6 — First General Daily-Use Pilot

Planned.

Prove two real verticals:

1. an ongoing coding/research project advances through multiple continuation cycles without routine `continue / check CI / fix / next PR` human scheduling;
2. a substantial non-project research request is iterated, challenged, deepened, and returned as a completed result without requiring the human between ordinary worker turns.

The pilot should stop or notify only at a genuine human-attention boundary and allow a human answer outside the original worker chat to resume the exact suspended work.

### M6.7 — Governed Local Worker

Planned after the web/GitHub continuity vertical is proven.

Expose narrow local gate capabilities such as test, lint, type-check, and Git inspection inside one fixed workspace without generic shell authority. Local mutation/network/Git write authority remains separate and governed.

## Later human surfaces

TUI, companion integration, mobile notifications, additional worker providers, and richer attention profiles remain downstream surfaces. They should be added in response to demonstrated daily-use friction rather than becoming prerequisites for the M6 continuity proof.
