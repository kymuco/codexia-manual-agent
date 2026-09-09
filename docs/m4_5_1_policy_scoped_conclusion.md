# M4.5.1 — Deterministic Policy-Scoped Conclusion

## Purpose

M4.4 produces a durable `ComparisonResult` whose outcome is local to one exact frozen comparison policy.

M4.5.1 establishes the next semantic boundary:

```text
comparison outcome != caller-authored scientific conclusion
```

and, more specifically:

```text
human/model wording != conclusion adjudication authority
```

The slice introduces a deterministic `AdjudicatedConclusion` for the one scope currently proven by Codexia:

```text
frozen_comparison_policy.v1
```

## Why the M4.1 `Conclusion` is not reused directly

The M4.1 `Conclusion` remains a valid single-experiment provenance record. Its invariant is that evidence cannot cross the exact manifest lineage supplied to that conclusion.

An M4.4 comparison necessarily uses two manifests: baseline and candidate. Pretending that this cross-arm evidence belongs to one M4.1 manifest would weaken the original invariant.

M4.5.1 therefore adds a distinct comparison-adjudication contract rather than widening the old record retroactively.

## Normal creation authority

`AdjudicatedConclusion.create(...)` accepts only:

- exact `Hypothesis`;
- exact baseline `ExperimentManifest`;
- exact candidate `ExperimentManifest`;
- exact `FrozenComparisonPolicy`;
- exact `ComparisonResult`.

It does **not** accept:

- `verdict`;
- `summary`;
- `conclusion_id`;
- caller-selected evidence records;
- alternate policy/result digests.

The normal dataclass constructor is disabled as an authoring path as well. `AdjudicatedConclusion(...)` cannot be used to bypass `create()` by supplying semantic fields directly. Valid construction is routed through the exact-object creation path or the structure-only strict decoder.

The constructor verifies the full relationship:

```text
hypothesis
  -> baseline manifest
  -> candidate manifest
  -> frozen comparison policy
  -> exact comparison result
```

A baseline/candidate reversal, foreign result, foreign hypothesis, manifest rebind, policy digest mismatch, or freeze digest mismatch fails closed.

## Deterministic verdict

The only admitted mapping is:

```text
SUPPORTED    -> supported
REFUTED      -> refuted
INCONCLUSIVE -> inconclusive
```

The `comparison_outcome` is retained explicitly in the adjudicated payload and the corresponding M4.1 `ConclusionVerdict` value must match it exactly.

This is not an upgrade from a comparison result to unrestricted scientific truth. The record carries an explicit `scope = frozen_comparison_policy.v1`.

## Deterministic wording

The runtime generates one bounded canonical summary per outcome.

For example, a refuted comparison is summarized as refutation **limited to the declared frozen policy and evidence**. A caller cannot replace that wording with a stronger statement such as “the hypothesis is universally false” through the normal creation path.

Direct decoding also requires the canonical summary associated with the embedded comparison outcome. Changing the wording or verdict while keeping the old digest fails before the record is admitted.

The decoder is a structural/canonical decoder, not a provenance authority. A fully rewritten standalone payload with freshly recomputed identities is not considered scientifically authoritative merely because it decodes. M4.5.2 must re-derive durable validity from authoritative M4.4 state.

## Deterministic identity

`conclusion_id` is UUIDv5-derived from the exact `ComparisonResult.result_digest` under the M4.5.1 namespace.

Therefore one exact comparison result has one canonical conclusion identity in this scope.

The conclusion digest additionally binds:

- exact hypothesis id/digest;
- exact baseline experiment id/manifest digest;
- exact candidate experiment id/manifest digest;
- exact policy id/digest;
- exact freeze digest;
- exact result id/digest;
- exact comparison outcome;
- exact mapped verdict;
- exact fixed scope;
- exact canonical summary.

## What this proves

M4.5.1 proves a construction boundary:

> Given one exact M4.4 policy/result object graph, the normal Codexia adjudication path has no parameter through which a caller can choose a different verdict, stronger wording, identity, arm ordering, policy, or result while still receiving the same adjudicated conclusion.

The decoder also rejects stale-digest payload tamper and internally inconsistent outcome/verdict/summary combinations.

## What this does not yet prove

M4.5.1 intentionally does not persist conclusions.

A completely rewritten standalone JSON object with a freshly recomputed digest is not treated as authoritative merely because its fields are internally consistent. M4.5.2 must make durable validity depend on fresh recovery of the exact M4.4 policy and comparison result, then deterministic recomputation of the conclusion.

M4.5.1 also does not claim:

- scientific truth outside the frozen policy and evidence;
- statistical significance or uncertainty estimates;
- automatic conflict resolution across studies;
- model-generated scientific prose as governed adjudication;
- recommendation/action authority;
- conclusion supersession or “latest wins” semantics;
- persistence, scheduling, or execution authority.

## Regression surface

`tests/test_lab_conclusion_adjudication.py` covers:

- absence of caller verdict/summary/id parameters on normal creation;
- disabled direct semantic dataclass construction;
- exact supported/refuted/inconclusive mapping;
- deterministic identity/digest for one exact result;
- baseline/candidate reversal rejection;
- foreign comparison-result rebinding rejection;
- caller-authored summary rejection;
- outcome/verdict disagreement rejection;
- result-digest stale-identity rejection;
- exact encode/decode round trip.

## Exit gate

> For one exact M4.4 policy/result pair there is one canonical policy-scoped conclusion identity, verdict, summary, and digest; the caller cannot substitute another verdict, wording, arm, policy, or result through the normal adjudication path.

M4.5.2 is responsible for durable publication and restart recovery that re-derives this record from authoritative M4.4 state rather than trusting stored conclusion text.
