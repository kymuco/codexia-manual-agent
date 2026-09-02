# M4.2 — Durable Lab Registry Source Audit

## Scope

This audit covers M4.2 from exact merged M4.1 base:

```text
6759cf9ad2580139a0cf7333c91ed5e2f2670527
```

Reviewed surface:

- `codexia_manual_agent.lab.registry`;
- M4.2 persistence errors and public exports;
- restart and lifecycle recovery;
- corruption and identity recovery;
- concurrent mutation serialization;
- review-hardening regressions;
- M4.2 documentation and roadmap.

The milestone is registry-only. It adds no executor, provider integration,
filesystem or Git mutation, authorization capability, delegation launcher,
scheduler, or autonomous loop.

## Security and correctness target

Primary durable-existence invariant:

```text
standalone record claim != durable registered existence
```

The append-only per-experiment event chronology is authoritative. SQLite derived
indexes are navigation and uniqueness aids and must agree exactly with replay.

Lifecycle invariant:

```text
open run -> evidence additions -> run sealed
all runs sealed -> experiment sealed
```

No event can reopen a sealed run or experiment.

`SqliteLabRegistry` is deliberately filesystem-backed. Standard SQLite `:memory:`
is rejected with `LabPersistenceError` because its per-connection lifetime cannot
satisfy M4.2 durable-registry semantics.

## Repaired source findings

The implementation and review cycle repaired these classes before finalization:

1. raw persisted JSON is bounded before parsing and after canonicalization;
2. public event metadata is validated before payload digest allocation;
3. authoritative chronology is replayed before derived duplicate state is trusted;
4. root `registered_at` is rebound to the first event during recovery;
5. durable UUID-backed identity requires canonical `str(UUID(value))` text;
6. recovered event payloads are recursively immutable;
7. missing navigation rows are checked against authoritative chronology;
8. foreign global-ID collisions audit the conflicting authoritative owner;
9. missing-index recovery no longer depends on canonical JSON whitespace;
10. semantic UUID-equivalent persisted text is detected across representation drift;
11. metric/artifact scoped conflicts audit the conflicting owner;
12. persisted SHA-256 metadata is validated before constant-time comparison;
13. navigation rows must reference an existing experiment root;
14. caller UUIDs are validated before DB navigation;
15. missing-index identity discovery no longer uses raw UUID spelling as a filter;
16. authoritative fallback events with orphan owners fail as persistence integrity;
17. authoritative global identities are audited before append even if the derived
    row is missing;
18. parser `RecursionError` is normalized to `LabPersistenceIntegrityError`;
19. new experiment roots cannot adopt orphan namespace rows;
20. hypothesis admission replays every discovered authoritative owner;
21. derived global IDs reject uppercase/unhyphenated semantic aliases;
22. semantic UUID alias rejection is uniform across experiment, hypothesis, global
    record, and metric/artifact scoped-run identity surfaces;
23. malformed persisted owner/navigation UUIDs are normalized to
    `LabPersistenceIntegrityError`;
24. hypothesis owner membership is discovered from authoritative
    `EXPERIMENT_REGISTERED` chronology rather than mutable root metadata;
25. ordinary hypothesis-digest conflict is deferred until all authoritative owner
    integrity checks complete;
26. UUID-bearing persistence identity values must be SQLite text / Python `str`;
27. persisted event `kind` is validated before registration-kind selection;
28. metric `name` and artifact `logical_path` are validated as text before scoped
    equality filtering;
29. event sequence/chain/root-head/registered-at integrity is verified before event
    kind filtering can affect authoritative identity discovery;
30. malformed TEXT scoped `run_id` values fail closed instead of being skipped;
31. every TEXT UUID identity encountered by global/root scans must itself parse as
    UUID state;
32. experiment recovery audits replayed run/metric/artifact primary identities
    against the full derived namespace before experiment-scoped equality;
33. test rollback assertions remain observable after deliberate corruption by using
    direct SQL event counts instead of invoking recovery through the corruption;
34. standard SQLite `:memory:` is rejected fail-fast because M4.2 requires durable
    filesystem-backed persistence;
35. experiment and direct record recovery audit replayed metric `(run_id, name)` and
    artifact `(run_id, logical_path)` identities across the full derived namespace,
    so a foreign semantic `run_id` alias cannot remain hidden behind an
    `experiment_id` filter;
36. recovery cross-checks authoritative hypothesis-owner membership against the
    full derived experiment-root namespace, so a foreign exact or semantic-alias
    root rebind cannot remain invisible while the selected experiment still replays;
37. recovery requires every authoritative and derived owner of one semantic
    `hypothesis_id` to bind the same hypothesis digest as the replayed experiment,
    so coherent foreign owner digest drift cannot survive owner-set equality;
38. recovery resolves every replayed run/metric/artifact primary ID against
    authoritative registration chronology as well as derived indexes, so coherent
    foreign chronology reuse of one global ID cannot remain hidden behind a stale
    foreign derived row;
39. authoritative registration discovery semantically replays each hash-consistent
    experiment chronology before kind filtering, so an owner cannot retain matching
    hypothesis identity/digest while its manifest or later lifecycle transitions are
    semantically invalid for that root;
40. every event-bearing root discovered by authoritative replay must corroborate the
    replayed hypothesis id/digest and manifest digest, so root-only identity metadata
    drift cannot remain invisible while another shared-hypothesis owner recovers;
41. one immutable validated registration-discovery snapshot is reused across a load
    or admission boundary, so replayed run/metric/artifact owner resolution no longer
    re-reads, decodes, validates, and semantically replays the full event registry for
    every record;
42. one immutable validated derived-index snapshot is built per recovery load, so
    run/metric/artifact primary and metric/artifact scoped identity verification
    preserves full-namespace fail-closed semantics without rescanning an entire
    derived table once per replayed record;
43. authoritative registration discovery indexes event-bearing experiment roots from
    one validated root-table snapshot, preserving root UUID corruption taxonomy
    without rescanning and reparsing the full experiment-root table once per
    chronology;
44. complete SQLite registry operations normalize unhandled `sqlite3.Error` failures
    from connection setup, transaction begin, statements, commit, rollback, or close
    into the exported `LabPersistenceError` domain after the existing rollback path,
    rather than leaking driver exceptions to callers;
45. filesystem parent-directory creation failures are normalized to
    `LabPersistenceError` before SQLite initialization, preserving the persistence
    error taxonomy for constructor failures outside the SQLite driver;
46. shared-hypothesis admission reuses one union derived-index snapshot across all
    authoritative owners after discovery has replayed them, avoiding one full set of
    derived-table scans per owner while preserving the same per-owner integrity
    checks.

Key late-stage repair provenance:

```text
malformed persisted owner UUID taxonomy:
  production: 84fecfebd9efbe1b2958d5967d094acbabd20681
  regression: c01aa81df2d0563d3b1589850d5ddc75def61492

authoritative hypothesis owner discovery:
  production: 757b2ccb9724b4e9144cf808c3c2c7c909e2a47d
  regression: b0886752be8275bbe4782fde6f1c4b409e8812ae

hypothesis conflict precedence after full owner audit:
  production: ecc21bd0ec4e73f3c199b9a92403c1add4793447
  regression: 419624b600ccf318e6e16c8cc2d763f1553b5363

persisted UUID SQLite text-domain enforcement:
  production: cb6be9abf8f1ce6ea09ba4289f759b3fdefde5ca
  regression: 4ddbf49d62ef1d12b8ee3245c9d6cf1ddd399299

pre-filter event-kind + scoped-label validation:
  production: 83f06a5bfa81d82b7510ffbc743ed11dbb3cc254
  regression: 8d6432904a6de84b2759f87cf06c16c707f3c807

chain/head verification before event-kind filtering:
  production: 5bd89fc688e2174e4555774e464e2be200fa7b92
  regression: 8db4422707817148ac71e78fc6754104325e9524

malformed scoped UUID uniqueness state:
  production: 0bb327fc8ce1d05509157c6bf076b17163301e77
  regression: 886fc142ac1d85c4c4586539a581a68ba030e26a

malformed global/root UUID identity scans:
  production: 6f08ee9326401686a5278579aa8ddc8f7ff0d3d1
  regression: c325d746254928d3e6ca1799a28ac733a0e7a333

experiment-recovery global identity audit:
  production: cfb2c5e0b169b1b22e4581bdaa0386e5acaf1fd1
  regression: cee6237757b59dd2b7f3d550647413bfa99650a0

test-only rollback-count repair:
  repair: 171aeace12e4f131bc6f3ec3144ac11497cfafa6

filesystem durability / :memory: rejection:
  production: 04d04b3125d7926012d9f985f63bd90d7fec68e1
  regression: 9e5e3a6c078c228ca13e27cee7605203994e1752

recovery scoped-identity full-namespace audit:
  production: a402005c3f57198db9fc9e0ee2641c44340e2ff2
  regression: e2b17b5c6377ffe685ebf1409aaa59847fad2116

recovery hypothesis-owner full-namespace audit:
  production: df11539c4da538627cb106f1a32edad0010eb027
  regression: 3beceec32e4051a3c73d49c052f16031ce778471

recovery hypothesis-owner digest consistency:
  production: 50481774157585941e9acdde561b3ad43ae0a1dc
  regression: c4f03352dfd6ebeb80ceba37462e788728f70152

recovery authoritative global-record identity audit:
  production: 632d977d673f1e6b66bae469139dd1039374dee5
  regression: 57db75af813bb44dd65fa0dcbfbe0751b9c846a8

authoritative chronology semantic replay before discovery filtering:
  production: d3acd30adb0f0a149d3f35743358e74c94cfe05f
  regression: e84c43a164987c309196037a048120f30c30b247

authoritative root corroboration + single discovery snapshot:
  regression: 9fb40ad201311748c36977bbc30e6728ac0ba40e
  production: 0d2954cdfa72f3d36f6e0643ca8811cd9870346e

batched derived recovery identity scans:
  regression: 183a46a28047754ff673e3d33ff1c0058105e9b7
  production: a6a0c5fd58d3831532e925cd6ea60ca850095889

batched authoritative experiment-root discovery:
  regression: 86a1f1699c475a4ca193f3ed25d7afb782bf9085
  production: 05fca79c41890a9c5e23bf024e9dfb52fb8751ac

SQLite operation error normalization:
  regression: 0fcb468be7b84ab135a9a517fcc036e0732213e6
  production: e78002e7fa2eca6a9890961b27937a55fe0d18bf

parent-directory error normalization + shared-owner derived batching:
  regression: 6bb8cd3f58b486e892b6d560af7984212fe19f98
  production: 6ab2c01a24ea3479ef7b1889f8e6dee2788203af
```

The detailed review history is recorded in `docs/m4_2_post_review_audit.md`.

## Candidate history

### Candidate 1 — invalidated after validation

```text
head: 91abc912bbe52347bb8dfc1fcafa1c5d64bf219f
tree: aa0497b860b609909a0806ba8ea12698df1fbe79
```

Historical Windows evidence only:

```text
focused: 38 passed, 3 subtests passed in 1.62s
full:    613 passed, 30 skipped, 272 subtests passed in 70.27s (0:01:10)
```

### Candidate 2 — invalidated before validation

```text
head: 6ab817ee365b8e4609cf53df424d4a8a8b46219b
tree: 9d3e911bb5ab785becff06a6a95837f675426985
```

No Windows release validation.

### Candidate 3 — invalidated by focused gate

```text
head: e2a9e01b2cd83ed1c5302f0d3eed73612c5529eb
tree: a1420681297c12063317b25c903ff9f43d78d450
```

The failure was a test-fixture defect: an all-numeric UUID made `.upper()` identical
to canonical text. Test-only repair:

```text
94ddb35cbfc2466e0a6146c97a03a9631f3faa36
```

### Candidate 4 — invalidated post-Ready by documentation drift

```text
head: 288ed69eeb3eb83b4148c3540e9b5bd72a9a3a51
tree: 76346cbe603e6375ff9afe81caa6ced3e98dfa86
```

Historical Windows evidence only:

```text
focused: 67 passed, 40 subtests passed in 3.46s
full:    642 passed, 30 skipped, 309 subtests passed in 73.54s (0:01:13)
```

### Candidate 5 — invalidated post-Ready

```text
head: 5cf76bb7554776c35e0778f1f87a4e7e6b661cc6
tree: 5475de9c6c97e54634ec9272747828c74b84e867
```

Historical Windows evidence only:

```text
focused: 69 passed, 40 subtests passed in 3.71s
full:    644 passed, 30 skipped, 309 subtests passed in 77.84s (0:01:17)
```

Candidate 5 was invalidated by roadmap live-state drift and a later non-text SQLite
UUID identity finding. No invalidated candidate's evidence is reused.

## Event source of truth and write ordering

Every experiment starts with exactly one `EXPERIMENT_REGISTERED` event containing
the exact M4.1 hypothesis and manifest. Later state is reconstructed by
`apply_lab_registry_event`.

For an existing experiment the serialized mutation boundary is:

```text
BEGIN IMMEDIATE
-> build one validated authoritative registration-discovery snapshot
-> recover and verify chronology + derived indexes using that snapshot
-> construct candidate event
-> audit authoritative global identity against the same snapshot
-> apply deterministic transition in memory
-> append event + CAS root head
-> audit derived global/scoped ownership
-> publish derived index
-> COMMIT
```

A failure at any stage rolls the transaction back. Unhandled SQLite driver failures
at connection setup, `BEGIN`, statements, `COMMIT`, rollback, or close are normalized
to `LabPersistenceError` at the complete-operation boundary; existing lab-domain
errors keep their more specific taxonomy. Parent-directory creation `OSError` is
normalized to the same persistence domain before SQLite initialization begins.

For new experiments whose hypothesis already has multiple authoritative owners,
registration discovery's already-validated replay states are reused. One union
`_DerivedIndexSnapshot` reads the experiment, hypothesis, run, metric, and artifact
derived tables once for all those owners, and the ordinary per-owner integrity checks
then resolve against that same immutable snapshot instead of rebuilding it per owner.

## Identity boundary

Global durable identity rules:

- one `hypothesis_id` cannot bind two hypothesis digests;
- one semantic `experiment_id` identifies one root chronology;
- `run_id`, `metric_id`, and `artifact_id` are globally unique by semantic UUID;
- admitted UUID-backed identities use canonical lowercase hyphenated UUID text;
- persisted UUID-bearing identities encountered by scans must be SQLite text and
  valid UUID state;
- semantic aliases, malformed UUID text, and non-text storage fail closed;
- persisted event `kind`, metric `name`, and artifact `logical_path` are validated
  before equality filtering can affect identity availability;
- authoritative event chain/head consistency, replayed root identity metadata, and
  semantic lifecycle replay are verified before registration discovery can affect
  identity availability.

Caller noncanonical UUIDs are rejected rather than normalized because normalization
would rewrite digest-bound M4.1 payload identity text.

## Derived indexes and scoped recovery

Derived indexes exist for:

- hypothesis identity;
- run identity and `(experiment_id, ordinal)`;
- metric identity and `(run_id, name)`;
- artifact identity and `(run_id, logical_path)`.

They are not independent authority. Recovery compares them with authoritative replay.
Missing, extra, rebound, altered, malformed, semantic-alias, or non-text state fails
closed.

Authoritative registration discovery first validates every persisted receipt, exact
per-root sequence/hash chain/head/registration timestamp, validates persisted root
hypothesis/manifest digests, and then semantically replays each complete chronology
through `apply_lab_registry_event`. Before the resulting ownership maps are trusted,
each replayed hypothesis id/digest and manifest digest must agree with the exact
persisted event-bearing root. Thus a hash-consistent owner with a manifest bound to
another experiment root, an invalid run/evidence binding, an invalid lifecycle
transition, or root-only manifest/hypothesis metadata drift cannot remain eligible.

One immutable `_RegistrationDiscoverySnapshot` is built for the current transaction
or recovery load and reused for authoritative hypothesis and run/metric/artifact
owner resolution. Its event-bearing experiment roots are resolved from one validated
experiment-root table snapshot rather than a full root-table scan per chronology. A
multi-record or multi-experiment recovery therefore does not repeat either the full
event read/decode/receipt validation/hash-chain validation/semantic replay per record
or the full experiment-root scan per event-bearing chronology. The snapshot is not
process-global or persistent; it is scoped to the unchanged transaction state in
which it was constructed. It also retains the private replay states produced by that
validation so shared-hypothesis admission does not need to replay those owners again.

Derived verification is batched separately. One immutable `_DerivedIndexSnapshot` is
built for each recovered experiment load by reading the experiment, hypothesis, run,
metric, and artifact derived tables once. For shared-hypothesis admission, one union
snapshot is built for all authoritative owners and reused across every owner's
verification. It retains the exact hypothesis owner/index rows, requested primary-ID
matches, requested metric/artifact scoped matches, and each requested experiment's
derived rows. The same representation-neutral UUID rules remain in force: non-text
or malformed state fails closed when the corresponding full scan was previously
required, semantic aliases of requested identities fail closed, and a different valid
UUID remains unrelated. `_verify_derived_indexes` then resolves every replayed record
from the supplied snapshot rather than rescanning a full derived table per record or
per shared-hypothesis owner.

Experiment recovery cross-checks authoritative `EXPERIMENT_REGISTERED` owners for the
replayed `hypothesis_id` against the full derived experiment-root namespace and
requires every authoritative registration digest plus every derived claiming root
digest to equal the replayed hypothesis digest before returning state. For each
replayed run, metric, and artifact primary ID, recovery also resolves authoritative
registration chronology and requires the sole authoritative owner to be the recovered
experiment before trusting the full-namespace derived primary-ID audit. It then
performs full-namespace scoped audits for every replayed metric `(run_id, name)` and
artifact `(run_id, logical_path)`. The scoped audit uses semantic UUID comparison for
`run_id`, validates storage types and UUID parseability, and requires the exact scoped
row to resolve back to the replayed primary ID. A foreign experiment can therefore
hide neither authoritative nor derived global-record reuse merely by keeping a stale
derived row or by being excluded from an experiment-scoped SQL query.

Only a fully valid existing owner may produce ordinary `LabIdentityConflictError`.
Corrupt or orphaned owner state is `LabPersistenceIntegrityError`.

## Recovery and corruption boundary

Recovery verifies at least:

- canonical semantic experiment identity and namespace;
- every authoritative chronology considered by registration discovery is
  semantically replayable for its own root before ownership indexing;
- every event-bearing root's persisted hypothesis id/digest and manifest digest
  corroborate the replayed state;
- authoritative and derived hypothesis-owner membership agree for the replayed
  semantic `hypothesis_id`, and every authoritative/derived owner binds the same
  hypothesis digest as the replayed hypothesis;
- UUID-bearing persistence values use text storage and parse as UUID state;
- persisted event kind is text and receipt-valid before kind-specific discovery;
- per-experiment contiguous sequence, previous-digest chain, exact root head/count,
  and first-event registration timestamp;
- metric/artifact scope labels are text and matching scoped `run_id` is valid UUID
  text;
- root existence and non-empty chronology;
- raw and canonical JSON budgets;
- parser failures remain in persistence-integrity taxonomy;
- canonical persisted JSON and UUID identity text;
- strict typed-record decoding;
- event digest and previous-digest chain;
- canonical persisted SHA-256 metadata;
- exact root tail sequence/digest and registration metadata;
- authoritative chronology ownership plus batched full-namespace derived
  global/scoped identity integrity for every replayed run/metric/artifact;
- hypothesis/run/metric/artifact experiment-scoped derived indexes;
- semantic lifecycle replay.

## Authority boundary and explicit non-claims

`SqliteLabRegistry` cannot start processes, call providers, mutate workspace/Git,
mint authority, launch delegated work, or schedule autonomous exploration.

M4.2 does not provide external anti-rollback anchoring, actor signatures, execution
success receipts, physical artifact-byte verification, statistical inference,
baseline comparison, conclusion adjudication, or autonomous execution.

The SQLite hash chain and root head are same-trust-domain consistency metadata, not
protection against an attacker able to rewrite the entire database and recompute
unkeyed hashes.

## Validation and finalization policy

Development tests are not release evidence. A merge candidate must be a single
commit directly over exact merged M4.1 base and point at the exact reviewed tree.

Exact frozen-candidate Windows gates are:

```text
python -m pytest -q tests/test_lab_registry_recovery.py tests/test_lab_registry_integrity.py tests/test_lab_registry_concurrency.py tests/test_lab_registry_hardening.py tests/test_lab_registry_review_findings.py tests/test_lab_registry_final_review_findings.py tests/test_lab_registry_candidate3_findings.py tests/test_lab_registry_candidate3_taxonomy.py tests/test_lab_registry_candidate5_findings.py tests/test_lab_registry_candidate6_findings.py tests/test_lab_registry_candidate6_global_uuid_findings.py tests/test_lab_registry_candidate6_recovery_identity_findings.py tests/test_lab_registry_candidate6_storage_findings.py tests/test_lab_registry_candidate6_recovery_scoped_findings.py
python -m pytest -q
```

A release candidate is eligible for merge only when:

1. its sole parent is exact merged M4.1 base;
2. its tree exactly matches the reviewed tree;
3. it is `0 behind` the base and all review threads are resolved;
4. exact-head Windows focused and full gates pass;
5. the PR is marked Ready only after that evidence is recorded;
6. a post-Ready whole-diff/source-security review finds no blocking issue;
7. final head/tree/thread/base checks still match the tested candidate;
8. merge uses the exact tested SHA and requires explicit authorization.

Any source/tree mutation after exact-head validation invalidates the candidate and
requires a new freeze plus fresh gates.

Hosted GitHub Actions runs that terminate with no job steps are infrastructure-only
and are not release test evidence.

Live candidate number, PR Draft/Ready state, and current release evidence belong in
the PR metadata. This source audit intentionally contains no mutable `Current
candidate` section.