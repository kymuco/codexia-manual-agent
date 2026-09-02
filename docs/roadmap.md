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

M4.1 and M4.2 are complete in the clean public baseline.

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

## Next work

Later M4.x work may add explicit run comparison, metric aggregation/statistical policy, stronger artifact verification, and additional evidence-bounded conclusion policy without silently widening execution authority.

## M5 — Bounded automation

Planned.

Support limited autonomous exploration with explicit budgets, stop policies, and the existing authority boundaries. No unattended destructive or external write authority.

## M6 — Optional TUI

Planned only after CLI/runtime contracts are stable.
