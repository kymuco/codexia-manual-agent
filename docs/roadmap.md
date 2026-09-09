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

M4.1 through M4.4 are complete.

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

**Next active milestone.**

Turn frozen comparison policy and verified evidence into reproducible conclusion adjudication. The next proof must preserve the distinction between a policy-local `SUPPORTED` / `REFUTED` / `INCONCLUSIVE` comparison result and an evidence-bounded scientific `Conclusion`; it must not silently upgrade either into scientific truth beyond the declared policy and evidence lineage.

## Near-term anti-drift rule

The next work should increase Codexia's ability to conduct and falsify real computational experiments. Security, persistence, and transport hardening remain important, but should interrupt the M4 research line only for a concrete blocker, demonstrated vulnerability, or invariant violation rather than becoming the product goal themselves.

## M5 — Bounded automation

Planned after the manual experiment loop is proven.

Support limited autonomous exploration with explicit budgets, stop policies, and the existing authority boundaries. No unattended destructive or external write authority. M5 should automate an already-proven M4 research loop rather than invent a second execution architecture.

## M6 — Optional TUI

Planned only after CLI/runtime contracts are stable and the computational-research loop has demonstrated practical value.
