# M4.2 — Post-Review Audit

## Scope

This note records the review-hardening history for M4.2 — Durable Experiment /
Run / Evidence Registry.

Exact merged M4.1 base:

```text
6759cf9ad2580139a0cf7333c91ed5e2f2670527
```

M4.2 remains registry-only. No process execution, provider call, filesystem/Git
mutation, delegation launch, scheduler, or autonomous loop is introduced.

## Candidate history

### Candidate 1 — invalidated after Windows validation

```text
head: 91abc912bbe52347bb8dfc1fcafa1c5d64bf219f
tree: aa0497b860b609909a0806ba8ea12698df1fbe79
parent: 6759cf9ad2580139a0cf7333c91ed5e2f2670527
```

Historical exact-head Windows evidence only:

```text
focused: 38 passed, 3 subtests passed in 1.62s
full:    613 passed, 30 skipped, 272 subtests passed in 70.27s (0:01:10)
```

A Ready-triggered UUID identity P2 invalidated Candidate 1 before merge.

### Candidate 2 — invalidated before Windows validation

```text
head: 6ab817ee365b8e4609cf53df424d4a8a8b46219b
tree: 9d3e911bb5ab785becff06a6a95837f675426985
parent: 6759cf9ad2580139a0cf7333c91ed5e2f2670527
```

No Windows release validation.

### Candidate 3 — invalidated by focused gate

```text
head: e2a9e01b2cd83ed1c5302f0d3eed73612c5529eb
tree: a1420681297c12063317b25c903ff9f43d78d450
parent: 6759cf9ad2580139a0cf7333c91ed5e2f2670527
```

The focused gate exposed a test-fixture defect, not a production defect: the
uppercase-alias fixture used an all-numeric UUID. Test-only repair:

```text
94ddb35cbfc2466e0a6146c97a03a9631f3faa36
```

### Candidate 4 — invalidated post-Ready by audit-document drift

```text
head: 288ed69eeb3eb83b4148c3540e9b5bd72a9a3a51
tree: 76346cbe603e6375ff9afe81caa6ced3e98dfa86
parent: 6759cf9ad2580139a0cf7333c91ed5e2f2670527
```

Historical exact-head Windows evidence only:

```text
focused: 67 passed, 40 subtests passed in 3.46s
full:    642 passed, 30 skipped, 309 subtests passed in 73.54s (0:01:13)
```

### Candidate 5 — invalidated post-Ready

```text
head: 5cf76bb7554776c35e0778f1f87a4e7e6b661cc6
tree: 5475de9c6c97e54634ec9272747828c74b84e867
parent: 6759cf9ad2580139a0cf7333c91ed5e2f2670527
```

Historical exact-head Windows evidence only:

```text
focused: 69 passed, 40 subtests passed in 3.71s
full:    644 passed, 30 skipped, 309 subtests passed in 77.84s (0:01:17)
```

Candidate 5 was invalidated by roadmap live-state drift and a later non-text SQLite
UUID identity finding. No invalidated candidate's validation is reused.

## Review findings and repairs

### 1. Recovered event payloads were recursively mutable

```text
production: f7e8c66c5a6fba87e14f7ff615321a9b338ce711
regression: b833901c0fc4673c5fd492f7efb9da9cf033c294
```

Validated event mappings are recursively frozen.

### 2. Missing navigation rows could look like ordinary unknown IDs

```text
production: f7e8c66c5a6fba87e14f7ff615321a9b338ce711
regression: b833901c0fc4673c5fd492f7efb9da9cf033c294
```

Run/metric/artifact lookup checks authoritative chronology before `Unknown`.

### 3. Equivalent UUID text could bypass durable identity uniqueness

```text
production: fed9cb406265b9998f0c1041eb3d22978e90b59a
regression: d2ec3d7c48b91c0e7a5ca87bccd41045f9e1586c
```

M4.2 durable UUID-backed identity requires exact canonical UUID text.

### 4. Stale foreign global-index rows could masquerade as genuine duplicates

```text
production: ebe1069f693c70ee31a06467bc7a6102ff383c59
regression: 5b2218186126f69e996daa47e552721e555c8539
```

Conflicting global derived rows audit their authoritative owner before an ordinary
identity conflict may be returned.

### 5. Missing-index fallback depended on raw JSON formatting

```text
production: 50f5a2b04d69c45d50f1dcb6fa268b8c0c2b68f6
hardening:  a9668ed7519067395cfb0455f02be76699d47a01
regression: 0fe9deb8c4275c5a1c6b3d02b4733c7b4cbae83a
regression: 5dcbe299d743b8c6616d3a08a89d7b39bd448d73
```

Registration payloads are parsed before identity relevance is decided.

### 6. Stale foreign scoped indexes could become generic SQLite errors

```text
production: 0661dd721957147e27580c5d01c90a3d2d36ee0b
regression: dbef74280288105bb2139316d2a5c9aadfe87e55
```

Metric `(run_id, name)` and artifact `(run_id, logical_path)` conflicts audit the
conflicting owner inside the same writer transaction.

### 7. Malformed persisted digests could leak comparison errors

```text
production: 0661dd721957147e27580c5d01c90a3d2d36ee0b
regression: dbef74280288105bb2139316d2a5c9aadfe87e55
```

Persisted digests are validated before constant-time comparison.

### 8. Unhyphenated UUID aliases could hide missing-index corruption

```text
production: 2e75739d61da18635bf1a9ddba14eeadb4bb126d
regression: 4de760613f41d1d6de6c32ecc872fa5ff57af4c8
```

Missing-index recovery detects semantic UUID aliases.

### 9. Orphaned navigation owners could look like ordinary unknown experiments

```text
production: 9b145f89466ad306948e4af90d174b02d31f2978
regression: d8306bf11d8e9de4d133bce6fd66c0d4679b1c1d
```

Navigation rows must reference durable experiment roots.

### 10. Linked caller run UUIDs were validated after navigation

```text
production: 9b145f89466ad306948e4af90d174b02d31f2978
regression: d8306bf11d8e9de4d133bce6fd66c0d4679b1c1d
```

Caller-domain malformed UUID input is rejected before DB navigation.

### 11. JSON Unicode escapes could defeat raw UUID candidate filtering

```text
production: f87f5d73876e1083a3b42dec25324bfdd688f699
regression: e0c38427b25a64bb6534b2c1a9ea617b9011585b
```

Missing-navigation recovery no longer uses raw UUID representation filters.

### 12. An authoritative registration could itself have an orphan owner

```text
production: 9f01157b5128fae203de7f3e317da60d8dfe4792
regression: 8d4aa95d7b76cbd5b915cdda5e09a8f0df6d1e9f
```

An authoritative registration with a missing root is persistence corruption.

### 13. An authoritative global identity could be reused when its derived row was missing

```text
production: ce39900a66b89ecead799c49e503a13f5406b5d1
regression: af8317ca50cbc8066d105836b2710fb27a3d0b07
```

Global record registration audits authoritative chronology before append.

### 14. Deeply nested persisted JSON could leak `RecursionError`

```text
production: ce39900a66b89ecead799c49e503a13f5406b5d1
regression: af8317ca50cbc8066d105836b2710fb27a3d0b07
```

Parser recursion failure is translated to `LabPersistenceIntegrityError`.

### 15. A new experiment could adopt orphan namespace rows

```text
production: e73597b56a6f6c724bf4a51e46952f069f428be9
regression: 35a9e4f1a4df59c74827df61e18bb052ab3680b4
```

New roots check namespace occupancy before publication.

### 16. Hypothesis admission checked only the first claiming root

```text
production: e73597b56a6f6c724bf4a51e46952f069f428be9
regression: 35a9e4f1a4df59c74827df61e18bb052ab3680b4
```

All then-discovered owners are replayed. Finding 20 later hardened discovery itself.

### 17. Noncanonical UUID aliases in derived global indexes could admit duplicates

```text
production: c2dee1852c34506204a8f8ffb86eb3c68eb4733f
regression: 9e693d0e3fd7c25e8929000a072600a03880738f
```

Global derived identity discovery is semantic-UUID aware.

### 18. Canonical identity enforcement was not uniform across persistence keys

```text
production: 268a7ae2823a4a8b88c3507545d810a5be2028ef
regression: 54b5674b82037a81b16380c347d17a8141085618
```

One semantic-UUID persistence rule covers root, hypothesis, global record, and
scoped-run identity surfaces.

### 19. Malformed persisted owner UUIDs could leak built-in parser errors

```text
production: 84fecfebd9efbe1b2958d5967d094acbabd20681
regression: c01aa81df2d0563d3b1589850d5ddc75def61492
```

Malformed persisted lookup identities remain in persistence-integrity taxonomy.

### 20. Root metadata rebinding could hide an authoritative hypothesis owner

```text
production: 757b2ccb9724b4e9144cf808c3c2c7c909e2a47d
regression: b0886752be8275bbe4782fde6f1c4b409e8812ae
```

Hypothesis ownership is discovered from authoritative experiment-registration
chronology, not mutable root metadata.

### 21. An early digest conflict could mask corruption in a later hypothesis owner

```text
production: ecc21bd0ec4e73f3c199b9a92403c1add4793447
regression: 419624b600ccf318e6e16c8cc2d763f1553b5363
```

Ordinary digest conflict is deferred until every authoritative owner passes
integrity checks.

### 22. Non-text SQLite UUID identities could coexist with canonical TEXT identities

```text
production: cb6be9abf8f1ce6ea09ba4289f759b3fdefde5ca
regression: 4ddbf49d62ef1d12b8ee3245c9d6cf1ddd399299
```

UUID identity scans require SQLite text storage.

### 23. SQL event-kind filtering could hide corrupted authoritative registrations

```text
production: 83f06a5bfa81d82b7510ffbc743ed11dbb3cc254
regression: 8d6432904a6de84b2759f87cf06c16c707f3c807
```

Event discovery validates persisted receipt/kind before kind selection.

### 24. Non-text scoped labels could bypass metric/artifact uniqueness

```text
production: 83f06a5bfa81d82b7510ffbc743ed11dbb3cc254
regression: 8d6432904a6de84b2759f87cf06c16c707f3c807
```

Scoped audits require metric names and artifact logical paths to be text.

### 25. Individually valid event rewrites could be filtered before chain/head verification

```text
production: 5bd89fc688e2174e4555774e464e2be200fa7b92
regression: 8db4422707817148ac71e78fc6754104325e9524
```

Registration discovery verifies contiguous sequence, previous digest, root head,
count, and registration timestamp before kind filtering.

### 26. Malformed TEXT scoped run UUIDs could be skipped as unrelated keys

```text
production: 0bb327fc8ce1d05509157c6bf076b17163301e77
regression: 886fc142ac1d85c4c4586539a581a68ba030e26a
```

Matching scoped rows with malformed TEXT `run_id` fail as persistence corruption.

### 27. Malformed TEXT UUID rows could be skipped by global/root scans

```text
production: 6f08ee9326401686a5278579aa8ddc8f7ff0d3d1
regression: c325d746254928d3e6ca1799a28ac733a0e7a333
```

Every TEXT UUID identity encountered by the full scan must parse as UUID state.

### 28. Experiment recovery could miss a foreign alias of a replayed global identity

```text
production: cfb2c5e0b169b1b22e4581bdaa0386e5acaf1fd1
regression: cee6237757b59dd2b7f3d550647413bfa99650a0
```

`recover_experiment()` now applies full-table semantic UUID audits to every replayed
run, metric, and artifact primary ID before experiment-scoped derived-index equality.

### 29. Corruption regression event counting invoked the integrity path under test

```text
repair: 171aeace12e4f131bc6f3ec3144ac11497cfafa6
```

This was test-only. The rollback assertion now uses direct SQL event counting so an
intentional corrupt index does not make the test fail before the intended assertion.
No production runtime behavior changed.

### 30. Standard SQLite `:memory:` is incompatible with this durable registry

```text
production: 04d04b3125d7926012d9f985f63bd90d7fec68e1
regression: 9e5e3a6c078c228ca13e27cee7605203994e1752
```

The registry opens separate SQLite connections for schema initialization and later
operations. Standard `:memory:` therefore cannot satisfy durable existence semantics.
Construction now fails immediately with `LabPersistenceError`; no shared/long-lived
connection mode was introduced.

### 31. Recovery could miss foreign aliases of replayed scoped evidence identities

```text
production: a402005c3f57198db9fc9e0ee2641c44340e2ff2
regression: e2b17b5c6377ffe685ebf1409aaa59847fad2116
```

A foreign metric/artifact row could previously use a semantic alias of the target
`run_id` plus the same metric name/logical path and remain hidden by a later
`experiment_id` filter. Recovery now reuses one full-table representation-neutral
scoped scan for registration and replay verification. The exact scoped row must exist
once and bind back to the replayed metric/artifact primary ID. Uppercase,
unhyphenated, malformed, or non-text linked run identities fail closed.

### 32. Recovery could miss a foreign root claiming the replayed hypothesis identity

```text
production: df11539c4da538627cb106f1a32edad0010eb027
regression: 3beceec32e4051a3c73d49c052f16031ce778471
```

A foreign experiment root could previously be rebound to the selected recovery's
`hypothesis_id` while its authoritative `EXPERIMENT_REGISTERED` chronology still
bound a different hypothesis. The selected experiment and shared hypothesis index
could remain individually valid, making recovery asymmetric with admission. Recovery
now discovers authoritative hypothesis owners through validated registration
chronology and compares that exact owner set against representation-neutral derived
root ownership before returning state. Exact and unhyphenated semantic-alias foreign
root rebinds therefore fail as `LabPersistenceIntegrityError`. The shared discovery
helper does not recursively invoke `_load_experiment` during replay verification.

### 33. Recovery owner-set equality could still hide hypothesis digest drift

```text
production: 50481774157585941e9acdde561b3ad43ae0a1dc
regression: c4f03352dfd6ebeb80ceba37462e788728f70152
```

Two experiments may legitimately share one exact hypothesis. A coherently rewritten
foreign sequence-zero registration could keep the same `hypothesis_id` while binding
a different valid hypothesis digest and matching manifest/root head, leaving the
owner set unchanged. Recovery now retains each authoritative owner's validated
hypothesis digest, requires every authoritative digest to equal the replayed digest,
and separately validates every derived claiming root's persisted `hypothesis_digest`
against the same value. Thus owner membership and owner digest consistency must both
hold before recovery returns.

### 34. Recovery could miss authoritative global-ID reuse hidden by stale derived rows

```text
production: 632d977d673f1e6b66bae469139dd1039374dee5
regression: 57db75af813bb44dd65fa0dcbfbe0751b9c846a8
```

A foreign chronology could be coherently rewritten so its tail run/metric/artifact
registration reuses a primary ID already owned by the target experiment while the
foreign derived row keeps its former primary ID. Full-table derived UUID scans alone
therefore still see exactly one target row. Recovery now resolves every replayed
global record ID through authoritative registration chronology and requires the sole
authoritative owner to equal the recovered experiment before trusting derived-index
agreement. The regression covers run, metric, and artifact reuse with canonical
payload JSON, recomputed tail event digest, and matching foreign root head.

### 35. Hash-consistent authoritative owners could remain semantically invalid

```text
production: d3acd30adb0f0a149d3f35743358e74c94cfe05f
regression: e84c43a164987c309196037a048120f30c30b247
```

A second experiment could retain the same hypothesis ID and digest as the recovered
experiment while its hash-consistent sequence-zero registration carried a manifest
bound to another `experiment_id`. Owner membership and digest checks therefore both
passed even though direct recovery of that owner failed semantic replay. Registration
discovery now replays every validated per-experiment chronology through
`apply_lab_registry_event` after exact sequence/hash/root-head/registered-at checks
and before any event-kind filtering. Invalid root binding, run/evidence binding, or
lifecycle transitions are normalized to `LabPersistenceIntegrityError` at the same
authoritative discovery boundary, with no recursive `_load_experiment` traversal.

### 36. Replayed authoritative owners were not corroborated against all root identity metadata

```text
regression: 9fb40ad201311748c36977bbc30e6728ac0ba40e
production: 0d2954cdfa72f3d36f6e0643ca8811cd9870346e
```

Semantic chronology replay alone did not prove that the persisted root still bound
the replayed manifest. With two valid owners sharing one hypothesis, changing only
the foreign root `manifest_digest` to another valid SHA-256 value left that owner's
event chronology fully replayable, so recovery of the healthy owner could miss the
corruption. Registration discovery now validates each root hypothesis/manifest
digest and requires the replayed hypothesis id/digest and manifest digest to agree
exactly with that event-bearing root before authoritative ownership is indexed.

### 37. Recovery replayed the full authoritative registry once per durable record

```text
regression: 9fb40ad201311748c36977bbc30e6728ac0ba40e
production: 0d2954cdfa72f3d36f6e0643ca8811cd9870346e
```

`_verify_derived_indexes` previously resolved each replayed run, metric, and artifact
through `_chronology_root_for_record`, and each resolution rebuilt the full
registration scan/replay. Recovery therefore performed `O(kE)` authoritative replay
work for `k` target records and `E` registry events. The registry now builds one
validated `_RegistrationDiscoverySnapshot` per load/admission boundary: one full
event read validates receipts, per-root chain/head/registration metadata, root
identity metadata and semantic replay, then records hypothesis and run/metric/artifact
authoritative ownership maps. Recovery reuses that same immutable snapshot for every
record lookup; hypothesis admission and pre-append authoritative identity audit reuse
it as well. The structural regression uses a multi-record experiment and requires
exactly one snapshot build during `recover_experiment`, avoiding timing-based tests.

### 38. Recovery rescanned full derived tables once per durable record

```text
regression: 183a46a28047754ff673e3d33ff1c0058105e9b7
production: a6a0c5fd58d3831532e925cd6ea60ca850095889
```

After authoritative event discovery was batched, recovery still called
`_uuid_identity_row` and `_scoped_identity_rows` for each replayed run, metric, and
artifact. Those helpers each performed a full derived-table scan, making evidence
recovery quadratic in the target record count and compounding sequential append work
toward cubic behavior. Recovery now builds one immutable `_DerivedIndexSnapshot` per
load. The experiment, hypothesis, run, metric, and artifact derived tables are read
once; requested primary UUID and metric/artifact scoped identities are validated with
the same text-domain, UUID-parseability, semantic-alias and exact-row rules; and
experiment-scoped exact-index equality is derived from those same reads. The verifier
then performs map lookups for every replayed record instead of another full table
scan. The structural regression registers multiple metrics/artifacts and requires
exactly one derived snapshot build during `recover_experiment`.

### 39. Registration discovery rescanned the experiment-root table once per chronology

```text
regression: 86a1f1699c475a4ca193f3ed25d7afb782bf9085
production: 05fca79c41890a9c5e23bf024e9dfb52fb8751ac
```

Even after authoritative event replay and derived verification were batched,
registration discovery still resolved each event-bearing experiment through
`_uuid_identity_row`, which rescanned and reparsed the full experiment-root table.
With `R` experiment chronologies this produced quadratic root processing before
semantic replay. Discovery now builds one validated immutable experiment-root map
from a single root-table read and resolves every event-bearing chronology from it.
The prior fail-closed text-storage, UUID-parseability, requested semantic-alias, and
exact-row rules remain intact. The structural regression creates four independent
chronologies and requires exactly one root-snapshot build during recovery.

### 40. Raw SQLite operational failures could escape the public persistence taxonomy

```text
regression: 0fcb468be7b84ab135a9a517fcc036e0732213e6
production: e78002e7fa2eca6a9890961b27937a55fe0d18bf
```

Transaction entry points previously used `with closing(self._connect())` outside the
inner rollback `try`, while `BEGIN`, statements, and `COMMIT` were followed by a
rollback-and-reraise block. A lock timeout, connection/open failure, read-only or disk
I/O error could therefore expose raw `sqlite3.Error` subclasses to callers. The
registry now wraps each complete SQLite operation, including initialization, in one
private `_sqlite_connection` boundary. Unhandled SQLite driver errors from connect,
PRAGMA setup, transaction begin, statements, commit, rollback, or close are
translated to `LabPersistenceError`; existing lab-domain and caller-validation errors
are not caught by that boundary. Regressions cover `BEGIN IMMEDIATE`, `_connect`, and
`COMMIT`; the commit-failure case additionally proves rollback leaves the chronology
and derived run index unchanged.

### 41. Parent-directory creation could escape the persistence-error taxonomy

```text
regression: 6bb8cd3f58b486e892b6d560af7984212fe19f98
production: 6ab2c01a24ea3479ef7b1889f8e6dee2788203af
```

Registry construction creates the filesystem parent before opening SQLite. If a
parent component is a regular file, permissions deny creation, or the filesystem is
read-only, `Path.mkdir` could therefore expose a raw `OSError` before the SQLite
operation boundary existed. Construction now translates parent-directory creation
`OSError` into `LabPersistenceError` while retaining the original exception as the
cause. The regression uses a regular file as a deterministic blocked parent.

### 42. Shared-hypothesis admission rebuilt derived tables once per owner

```text
regression: 6bb8cd3f58b486e892b6d560af7984212fe19f98
production: 6ab2c01a24ea3479ef7b1889f8e6dee2788203af
```

When many experiments legitimately shared one hypothesis, admission iterated the
authoritative owners and called `_load_experiment` for each. Each owner load rebuilt a
full `_DerivedIndexSnapshot`, repeatedly reading experiment, hypothesis, run, metric,
and artifact derived tables. Authoritative discovery already performs one validated
semantic replay of every event-bearing experiment, so it now retains those private
owner replay states. Admission builds one union derived snapshot for all relevant
owners and reuses it while applying the existing per-owner integrity checks. The
structural regression creates four existing shared-hypothesis owners with runs and
requires exactly one derived snapshot build while admitting a fifth owner.

## Corruption and trust boundary

Direct-SQL mutations in regressions model out-of-band damage that the supported
writer cannot normally create. Recovery and write admission must still fail closed.

M4.2 does not claim protection against an attacker able to rewrite the entire
same-trust-domain SQLite database and recompute unkeyed hashes. Event hashes, root
heads, and derived indexes are consistency metadata, not actor signatures or an
external anti-rollback anchor.

## Review-state record

All currently recorded inline findings are repaired before the next freeze; their
threads must be resolved before the candidate is created. The late cycle included
one test-only P1, the filesystem `:memory:` durability P2, scoped/global recovery
identity P2s, hypothesis-owner membership/digest/semantic-replay/root-metadata P2s,
authoritative global-record recovery P2, authoritative discovery replay-cost P2,
derived-index recovery scan-cost P2, experiment-root discovery scan-cost P2, SQLite
complete-operation and parent-directory persistence-error taxonomy P2s, and the
shared-hypothesis owner derived-snapshot batching P2 in addition to the earlier
production hardening findings.

No unavailable or incomplete automated review is represented as clean. Hosted CI
runs that terminate with zero job steps are infrastructure failures and are not test
PASS evidence.

## Durable finalization policy

Live candidate identity, PR Draft/Ready state, and current validation evidence belong
in PR metadata. This audit intentionally contains no mutable current-release-state
section.

For every final merge candidate:

1. freeze the exact reviewed tree as one commit directly over exact merged M4.1;
2. verify sole parent, exact tree, `1 ahead / 0 behind / 1 commit`, expected changed
   files, and zero unresolved review threads;
3. run exact-head Windows focused and full gates;
4. record only that candidate's results;
5. mark Ready only after both gates pass;
6. perform a post-Ready whole-diff/source-security review without changing the tree;
7. reconfirm head, tree, threads, and base;
8. merge only the exact tested SHA and only after explicit authorization.

The focused Windows gate uses explicit filenames because PowerShell does not expand
POSIX shell globs:

```text
python -m pytest -q tests/test_lab_registry_recovery.py tests/test_lab_registry_integrity.py tests/test_lab_registry_concurrency.py tests/test_lab_registry_hardening.py tests/test_lab_registry_review_findings.py tests/test_lab_registry_final_review_findings.py tests/test_lab_registry_candidate3_findings.py tests/test_lab_registry_candidate3_taxonomy.py tests/test_lab_registry_candidate5_findings.py tests/test_lab_registry_candidate6_findings.py tests/test_lab_registry_candidate6_global_uuid_findings.py tests/test_lab_registry_candidate6_recovery_identity_findings.py tests/test_lab_registry_candidate6_storage_findings.py tests/test_lab_registry_candidate6_recovery_scoped_findings.py
python -m pytest -q
```

Any source/tree mutation after validation invalidates the candidate and requires a
new freeze plus fresh gates. Historical PASS results are never reused for a different
tree.