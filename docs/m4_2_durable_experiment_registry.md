# M4.2 — Durable Experiment / Run / Evidence Registry

## Purpose

M4.1 made computational-lab records immutable and digest-bound, but a standalone
record proved only its own payload integrity. It did not prove that the referenced
hypothesis, manifest, run, metric, or artifact had ever been accepted into one
durable registry.

M4.2 closes that gap with an authoritative SQLite registry whose source of truth is
an append-only per-experiment event chronology.

Primary invariant:

```text
standalone record claim != durable registered existence
```

Registered evidence exists only when the exact record is present in the durable
experiment chronology and the derived indexes agree with that chronology.

Lifecycle invariant:

```text
open run -> evidence additions -> run sealed
all runs sealed -> experiment sealed
```

These are registry lifecycle claims only. They are not execution receipts.

## Storage boundary

`SqliteLabRegistry` is a durable filesystem-backed registry. Standard SQLite
`:memory:` is rejected at construction with `LabPersistenceError`.

The implementation initializes schema on one connection and performs later
operations on separate connections. Standard `:memory:` databases are scoped to a
single connection, so supporting that mode would contradict the durable-existence
contract unless the runtime architecture changed to a shared/long-lived connection.
M4.2 deliberately does not introduce such a second storage mode.

## Durable root

Each experiment has one root chronology. The first event is exactly:

```text
EXPERIMENT_REGISTERED(hypothesis, manifest)
```

The manifest must bind the exact hypothesis id and digest. Its `experiment_id` is
the root identity for the chronology.

The same exact hypothesis may anchor multiple experiments. A `hypothesis_id` may
never be rebound to a different hypothesis digest anywhere in the registry.

A new experiment root is admitted only after the writer verifies, inside the same
`BEGIN IMMEDIATE` transaction, that the semantic `experiment_id` is not already
occupied by an exact row or a noncanonical UUID alias in the root, event, run,
metric, or artifact namespace.

Hypothesis ownership is discovered from authoritative `EXPERIMENT_REGISTERED`
chronology, not mutable root/index metadata. Every authoritative owner of the same
semantic `hypothesis_id` is replayed and corroborated against derived ownership.
An ordinary digest conflict is reported only after those integrity checks complete.

## Canonical durable UUID identity

M4.2 persists UUID identities as SQLite `TEXT` keys. Every admitted UUID-backed
identity must equal exact `str(UUID(value))`: lowercase hexadecimal with standard
hyphens.

This applies to event ids, experiment ids, hypothesis ids, run ids, metric ids,
artifact ids, and linked UUID fields inside registered records.

Caller noncanonical UUID text is rejected rather than silently normalized because
normalization would rewrite identity text outside an existing digest-bound M4.1
record.

Persistence identity scans are representation-neutral for the requested semantic
UUID:

- exact canonical text may continue to owner/replay verification;
- different text that parses to the same UUID is `LabPersistenceIntegrityError`;
- non-text SQLite storage is `LabPersistenceIntegrityError`;
- malformed TEXT that cannot parse as UUID is `LabPersistenceIntegrityError`;
- only a different valid UUID may remain unrelated.

The same rule applies to experiment/hypothesis identity, global run/metric/artifact
identity, and metric/artifact scoped `run_id` identity.

A lookup identity originating from persisted owner/navigation state is also
untrusted. Parser failures are translated into the persistence-integrity error
domain rather than leaking built-in UUID exceptions.

## Event chronology

M4.2 v1 defines six event kinds:

- `experiment_registered`;
- `run_registered`;
- `metric_registered`;
- `artifact_registered`;
- `run_sealed`;
- `experiment_sealed`.

Every event binds:

- schema version;
- unique canonical event UUID;
- exact canonical experiment UUID;
- contiguous sequence number;
- bounded canonical timezone-aware timestamp;
- exact event kind;
- exact bounded canonical payload;
- previous event digest;
- current SHA-256 event digest.

Sequence zero has no previous digest. Every later event binds the previous digest.
Recovery verifies the complete chain and root head before returning public state.

Persisted event rows are not trusted through SQL `kind` equality. Registration
identity discovery first validates each persisted receipt and event-kind storage,
groups events by experiment, verifies contiguous sequence, previous-digest links,
root head/count, and first-event `registered_at`, and only then selects the relevant
registration kind.

Persisted JSON rejects duplicate keys, non-finite constants, noncanonical encoding,
excessive nesting, excessive raw character size, and excessive canonical byte size.
Parser recursion failure is normalized to `LabPersistenceIntegrityError`.

## Registration semantics

### Run

A run may be registered only while the experiment is open. It binds the durable
experiment id and exact manifest digest.

`run_id` is globally unique by semantic UUID. `ordinal` is unique inside one
experiment.

Run registration does not claim that a process started or completed.

### Metric

A metric may be registered only for an existing open run in an open experiment. It
binds the exact run digest and manifest digest.

`metric_id` is globally unique by semantic UUID. M4.2 v1 permits one metric name per
semantic run identity.

The scoped identity is therefore:

```text
(run_id, name)
```

The persisted `name` must be SQLite text, and the persisted linked `run_id` must be
canonical/parseable UUID text under the persistence identity rules.

### Artifact

An artifact record may be registered only for an existing open run in an open
experiment. It binds the exact run digest and manifest digest.

`artifact_id` is globally unique by semantic UUID. Logical path is unique per
semantic run identity:

```text
(run_id, logical_path)
```

The persisted logical path must be SQLite text and the linked `run_id` must satisfy
the same persistence UUID rules.

Artifact registration records metadata only. M4.2 does not prove that declared
physical bytes currently exist on disk.

## Global identity admission

For `run_id`, `metric_id`, and `artifact_id`, absence of a derived row does not prove
that the identity is free. Before append, the writer searches authoritative
registration chronology for the decoded identity.

If a prior authoritative registration exists:

- a fully valid owner chronology yields `LabIdentityConflictError`;
- a missing/corrupt owner root or derived-state disagreement yields
  `LabPersistenceIntegrityError`.

During derived-index publication, the inverse direction is also audited: an
existing derived row is not accepted as a real duplicate until its authoritative
owner passes replay.

## Scoped identity admission and recovery

Metric/artifact scoped uniqueness is not merely an insert-time SQLite constraint.
The registry scans the full corresponding derived table and validates both scope
components before classifying a conflict.

For a candidate metric `(run_id, name)` or artifact `(run_id, logical_path)`:

- every encountered scope label must be text before equality filtering;
- a matching-scope linked `run_id` must be text and parseable UUID state;
- a semantic alias of the requested `run_id` is persistence corruption;
- malformed/non-text linked `run_id` is persistence corruption;
- a different valid UUID remains unrelated;
- only an exact scoped row with a fully valid authoritative owner may become an
  ordinary identity conflict.

Recovery enforces the same boundary. For every replayed metric and artifact,
`_verify_derived_indexes` audits the corresponding scoped identity across the full
derived namespace before experiment-scoped equality is trusted. The exact scoped
row must exist once and its primary `metric_id` / `artifact_id` must equal the
replayed record.

Therefore a foreign experiment row using an uppercase or unhyphenated alias of the
target run plus the same metric name or artifact logical path cannot remain hidden
behind `WHERE experiment_id = ?`.

Direct `recover_for_metric` / `recover_for_artifact` and `recover_experiment` converge
on the same full-namespace integrity semantics because all paths load and verify the
owning experiment before returning public recovery state.

## Seal semantics

`RUN_SEALED` binds exact `run_id` and `run_digest`. After commit, no metric or
artifact may be added to that run and it cannot be sealed twice.

A run may be sealed with zero evidence. Seal means only that its M4.2 evidence set
is closed.

`EXPERIMENT_SEALED` binds the exact manifest digest and is admitted only when every
registered run is sealed. After commit, no new run/evidence or second experiment
seal is admitted.

An experiment with zero runs can be sealed. This is registry closure, not scientific
or execution success.

## SQLite transaction boundary

All mutations use `BEGIN IMMEDIATE`.

For a new experiment:

```text
BEGIN IMMEDIATE
-> audit semantic experiment namespace
-> validate authoritative registration-discovery chain/head boundary
-> discover hypothesis owners from authoritative chronology
-> replay every owner
-> verify derived hypothesis ownership
-> defer ordinary digest conflict until integrity checks pass
-> publish root
-> apply experiment_registered transition
-> append first event + advance root head
-> COMMIT
```

For an existing experiment:

```text
BEGIN IMMEDIATE
-> recover and verify target chronology + derived indexes
-> construct candidate event
-> validate registration-discovery chain/head boundary
-> audit authoritative global identity before append
-> apply deterministic transition in memory
-> append event
-> CAS root head from exact previous sequence/digest
-> audit derived global/scoped identity across the full namespace
-> publish derived index
-> COMMIT
```

Failure at any stage rolls back the transaction.

Competing registrations and seals serialize through SQLite. Two runs cannot both
claim one ordinal, two metrics cannot both claim one semantic run/name, evidence
cannot cross a run seal, and a new run cannot cross an experiment seal.

## Derived indexes

Derived indexes exist for navigation and uniqueness:

- hypothesis identity;
- run identity and `(experiment_id, ordinal)`;
- metric identity and `(run_id, name)`;
- artifact identity and `(run_id, logical_path)`.

They are not independent authority. Recovery reconstructs state from chronology and
requires derived state to agree exactly.

Experiment recovery performs full-namespace primary-ID audits for every replayed
run, metric, and artifact before experiment-scoped equality. It also performs
full-namespace scoped audits for every replayed metric and artifact as described
above.

Missing, extra, altered, rebound, semantic UUID alias, non-text storage, malformed
UUID state, or scoped ambiguity fails as `LabPersistenceIntegrityError`.

## Missing-navigation recovery

When direct run/metric/artifact navigation is absent, M4.2 validates persisted event
receipts and owner chain/head state before registration-kind selection, parses the
bounded payload, and decides relevance from the decoded primary UUID.

Raw JSON representation and SQL kind equality are not identity boundaries.
Whitespace, case, hyphen placement, JSON Unicode escapes, BLOB event kinds,
payload-inconsistent kinds, or root-inconsistent event rewrites cannot make a
durable registration look like an ordinary unknown ID.

## Cross-record lineage

Replay requires:

- manifest -> exact hypothesis id/digest;
- run -> exact experiment id + manifest digest;
- metric/artifact -> existing run + exact run digest + exact manifest digest;
- run seal -> exact run id/digest;
- experiment seal -> exact manifest digest.

A standalone M4.1 record is never proof of durable registered existence.

## Authority boundary

`SqliteLabRegistry` cannot:

- start a process;
- call a model/provider;
- perform workspace execution mutation;
- mutate Git;
- mint or consume new authority;
- launch delegated work;
- schedule autonomous exploration.

Existing M2.x authority remains the only path for later side effects.

## Explicit non-claims

M4.2 does not provide:

- external anti-rollback anchoring;
- actor signatures/authenticity;
- execution receipts or success/failure semantics;
- physical artifact-byte verification;
- metric aggregation/statistical inference;
- baseline comparison;
- conclusion adjudication;
- autonomous experiment execution.

The SQLite root head and hash chain are same-trust-domain consistency metadata, not
protection against an attacker able to rewrite the whole database and recompute
unkeyed hashes.

## Release validation

The exact frozen-candidate focused Windows gate is:

```text
python -m pytest -q tests/test_lab_registry_recovery.py tests/test_lab_registry_integrity.py tests/test_lab_registry_concurrency.py tests/test_lab_registry_hardening.py tests/test_lab_registry_review_findings.py tests/test_lab_registry_final_review_findings.py tests/test_lab_registry_candidate3_findings.py tests/test_lab_registry_candidate3_taxonomy.py tests/test_lab_registry_candidate5_findings.py tests/test_lab_registry_candidate6_findings.py tests/test_lab_registry_candidate6_global_uuid_findings.py tests/test_lab_registry_candidate6_recovery_identity_findings.py tests/test_lab_registry_candidate6_storage_findings.py tests/test_lab_registry_candidate6_recovery_scoped_findings.py
```

The full Windows gate is:

```text
python -m pytest -q
```

Development tests and historical candidate results are not release evidence for a
new tree. Hosted CI runs that terminate with zero job steps are infrastructure-only
and are not test PASS evidence.

Any source/tree mutation after exact-head validation invalidates that candidate and
requires a new freeze plus fresh focused/full gates.