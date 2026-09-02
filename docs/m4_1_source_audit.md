# M4.1 — Computational Lab Core Contracts Source Audit

## Scope

This audit covers the M4.1 computational-lab contract slice introduced on branch
`m4/computational-lab-core-contracts` from exact merged M3.2 base
`d633a1047e1c2607849c5ccdde1c7213734dc462`.

The reviewed surface is intentionally contract-only:

- `codexia_manual_agent.lab` record models;
- strict record serialization/deserialization;
- focused contract regressions;
- M4.1 design documentation.

No experiment executor, provider integration, process launch, filesystem mutation,
Git action, delegation scheduler, or autonomous loop is part of M4.1.

## Security / correctness target

Primary provenance goal:

```text
normal M4.1 construction may not silently mix evidence from another experiment manifest
```

Equally important non-claim:

```text
digest-bound provenance is integrity metadata, not scientific truth or durable existence proof
```

Until M4.2 introduces a durable registry, a standalone record proves only that its
own canonical payload matches its digest. It does not independently prove that a
referenced run/artifact was durably registered or physically existed.

## Findings repaired during development review

### Finding 1 — canonical logical artifact path admitted `.`

The first `PurePosixPath` check rejected absolute/traversal/backslash/noncanonical
paths but could admit the single-dot path because `PurePosixPath('.')` normalizes
to an empty parts sequence.

Repair:

- require a non-empty normalized parts sequence;
- retain absolute, `.`/`..`, backslash and canonical-string checks;
- regression added in `test_lab_contract_hardening.py`.

### Finding 2 — manifest parameter nesting had a byte budget but no structural depth budget

A byte cap alone is not a sufficient pre-digest structural bound for adversarially
nested Python containers.

Repair:

- add `MAX_PARAMETER_DEPTH = 32`;
- reject deeper parameter trees during recursive freezing before JSON encoding;
- regression constructs a deeply nested parameter value and verifies fail-closed
  rejection.

### Finding 3 — Python arbitrary-precision integers escaped intended bounded-record semantics

Run ordinals/seeds, artifact sizes, integer metrics and integer manifest parameters
were type-checked but initially allowed arbitrarily large Python integers.

Repair:

- bounded integer-bearing fields to signed 64-bit ranges;
- artifact size/run ordinal remain non-negative;
- regression covers oversized ordinal, metric value, artifact size and parameter
  integer.

### Finding 4 — evidence-count limit was checked only after consuming the iterable

The initial conclusion evidence collector accumulated the full iterable and then
checked the count. A generator could therefore run without bound before the
contract rejected it.

Repair:

- enforce `MAX_EVIDENCE_RECORDS` during iteration;
- stop as soon as the 257th record would be admitted;
- regression uses an unbounded generator and verifies bounded termination.

### Finding 5 — public `to_dict()` records had no strict supported inverse

M4.1 exposes JSON-compatible dictionaries, and `Conclusion.to_dict()` correctly
serializes digest tuples as arrays. Without explicit decoders, future persistence
code would be forced into ad-hoc reconstruction, especially for tuple/list
normalization and exact-key handling.

Repair:

- add strict `*_from_dict` decoders for every public M4.1 record;
- require exact key sets;
- normalize conclusion evidence arrays to canonical tuples;
- rely on record `__post_init__` for complete schema/value/digest revalidation;
- regressions cover round-trip, missing/extra keys, stale digest after payload
  tamper and invalid evidence field shape.

## Whole-diff observations

### Digest semantics

All public records are frozen and validate an exact SHA-256 digest over their
canonical payload. Canonical parameter values are deeply detached from caller
containers using immutable mapping/tuple structure before the manifest digest is
accepted.

This protects record identity against accidental mutation and stale-payload
reconstruction. It is not a signature and does not establish actor authenticity.

### Hypothesis boundary

A hypothesis requires both a bounded statement and a bounded falsification
criterion. M4.1 therefore does not admit a normal factory-created hypothesis that
contains only an unfalsifiable free-form claim.

The implementation cannot determine whether the supplied falsification criterion
is scientifically adequate; that remains semantic review, not a cryptographic
property.

### Run boundary

`ExperimentRun` is deliberately an identity record. Its creation does not imply
that execution began or completed. Treating it as an execution receipt would be
an invalid M4.1 claim.

### Evidence boundary

`MetricRecord` and `ArtifactRecord` created through the public factories inherit
exact run/manifest lineage from an `ExperimentRun` object. `Conclusion.create()`
requires evidence from the exact manifest it is concluding over and rejects
cross-manifest or duplicate evidence.

However, direct reconstruction of any standalone record can only validate that
record's own payload and digest. Cross-record existence/uniqueness cannot be
proved without a registry. M4.2 must make the registry authoritative before it
claims durable evidence lineage.

### Conclusion boundary

`SUPPORTED` and `REFUTED` require at least one bound evidence record in the normal
factory path. `INCONCLUSIVE` may contain no evidence.

This prevents an evidence-free supported/refuted conclusion from being produced
through the normal constructor API. It does not validate that the conclusion text
or verdict is the uniquely correct scientific interpretation of the evidence.

### Authority boundary

The M4.1 package imports no M2.x authority implementation and exposes no method
that can:

- issue/consume authorization;
- execute a process;
- mutate a workspace;
- read physical artifact bytes;
- call a remote provider;
- commit/push Git state;
- launch delegated work;
- schedule autonomous exploration.

M4.1 is therefore descriptive/provenance state only.

## Validation status

Development-time isolated sanity execution has exercised the new module and
focused regressions, but it is not repository validation and is not merge
evidence.

Before Ready/merge the reviewed tree must be frozen into one exact candidate over
merged M3.2 base and validated on the user's real Windows checkout with concise
pytest gates:

```text
pytest -q tests/test_lab_*.py
pytest -q
```

No exact-head PASS is claimed in this audit until those commands are run against
the frozen candidate.

## Pre-freeze checklist

- synchronize `docs/roadmap.md` with merged M3.2 and current M4.1;
- keep M4.1 execution/authority-free;
- re-read final changed-file set after documentation synchronization;
- confirm review threads are empty or resolved;
- freeze the exact reviewed tree over `d633a1047e1c2607849c5ccdde1c7213734dc462`;
- validate focused and full `pytest -q` gates on that exact head;
- then mark Ready and perform one final post-Ready source/security review.
