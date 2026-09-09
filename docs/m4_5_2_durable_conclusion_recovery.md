# M4.5.2 — Durable Conclusion Registry and Restart Recovery

## Purpose

M4.5.1 proved that one exact recovered M4.4 comparison object graph maps to one deterministic policy-scoped `AdjudicatedConclusion` and that the caller does not choose its verdict, wording, or identity.

M4.5.2 closes the next authority hole:

```text
persisted conclusion != conclusion authority
```

A database row is only a durable cache of derived state. It is valid only if fresh authoritative M4.4 recovery deterministically reproduces it.

## Public authority surface

`SqliteAdjudicatedConclusionRegistry.publish(policy_id)` accepts only one stable identity:

- the exact frozen M4.4 `policy_id`.

It does not accept:

- an `AdjudicatedConclusion` object;
- a verdict;
- summary text;
- conclusion/result digests;
- baseline/candidate manifests;
- evidence subsets.

Publication performs the authoritative derivation itself:

```text
policy_id
  -> SqliteComparisonRegistry.recover_policy()
  -> SqliteComparisonResultRegistry.recover_result()
  -> SqliteLabRegistry.recover_experiment(baseline)
  -> SqliteLabRegistry.recover_experiment(candidate)
  -> AdjudicatedConclusion.create(...)
  -> canonical durable cache row
```

Therefore caller-supplied database text is never the semantic input to publication.

## Existing trust domain

The conclusion registry requires the comparison-policy registry, comparison-result registry, and lab registry to resolve to one exact SQLite database path.

The durable table is:

```text
lab_adjudicated_conclusions
```

It indexes:

- `policy_id` as the one durable conclusion root for that frozen policy;
- deterministic `conclusion_id`;
- exact `conclusion_digest`;
- exact M4.4 `result_id`;
- exact M4.4 `result_digest`;
- canonical conclusion payload JSON.

The row has foreign-key lineage to the frozen comparison policy and durable comparison result already in the same trust domain.

## Recovery rule

Recovery deliberately has two independent stages.

First, persisted cache validation:

```text
stored JSON
-> strict duplicate-free JSON decode
-> M4.5.1 structural decoder
-> canonical JSON equality
-> persisted indexes == payload indexes
```

Second, authoritative recomputation:

```text
fresh frozen-policy recovery
+ fresh M4.4 result recovery
+ fresh baseline/candidate experiment recovery
-> AdjudicatedConclusion.create(...)
-> exact object equality with persisted conclusion
```

The registry returns the freshly derived conclusion after equality succeeds. The database payload is not returned as an independent authority source.

## Why a valid digest is insufficient

M4.5.1 intentionally documented that a standalone self-consistent JSON object with a recomputed digest is only structurally valid.

M4.5.2 directly tests this distinction.

An adversarial regression rewrites a real `REFUTED` persisted conclusion into a structurally self-consistent `SUPPORTED` conclusion, including:

- matching supported verdict;
- canonical supported summary;
- a newly recomputed valid conclusion digest;
- unchanged valid result identity/digest indexes.

The structural decoder can accept that internally consistent payload. Recovery still rejects it because fresh M4.4 result recovery deterministically derives `REFUTED`.

This is the core M4.5.2 proof:

```text
digest integrity != adjudication provenance
```

## Transitive evidence validity

Fresh conclusion recovery calls M4.4 `recover_result()`, which itself recomputes the comparison from sealed M4.3 physical evidence.

Therefore mutation of underlying accepted physical evidence after conclusion publication invalidates conclusion recovery transitively:

```text
physical evidence mutation
-> M4.3 recovery failure
-> M4.4 result recovery failure
-> M4.5 conclusion recovery failure
```

The conclusion layer does not create a shortcut around lower evidence guarantees.

## Idempotence

Publishing the same exact policy again derives the same deterministic conclusion. An existing durable row is accepted only if every index and canonical payload field already equals that derivation, after which normal recovery is performed again.

A conflicting row is not overwritten.

## What this proves

M4.5.2 proves:

> A persisted policy-scoped conclusion is valid only when its exact durable M4.4 policy/result dependencies and experiment lineage freshly reproduce the same deterministic M4.5.1 conclusion. Database text or a freshly recomputed standalone conclusion digest cannot mint a different governed adjudication.

## Explicit non-claims

M4.5.2 still does not provide:

- unrestricted scientific truth;
- statistics beyond the frozen M4.4 comparison;
- model-authored scientific prose as authority;
- cross-study conflict resolution;
- conclusion supersession or latest-wins semantics;
- automatic recommendation/action authority;
- experiment scheduling or execution authority.

It also does not claim hostile local-machine or kernel/database attestation. The guarantee is inside the existing local SQLite/application trust model.

## Regression surface

`tests/test_lab_conclusion_registry.py` covers:

- real governed M4.3 evidence -> durable M4.4 `REFUTED` result -> published M4.5 conclusion;
- fresh-runtime restart recovery;
- refusal to publish before a durable comparison result exists;
- persisted summary corruption;
- persisted index corruption;
- fully self-consistent forged verdict + recomputed digest rejected by authoritative recomputation;
- underlying physical evidence mutation invalidates conclusion recovery transitively.

## Exit gate

> A persisted conclusion is valid only when fresh recovery of its exact M4.4 dependencies deterministically reproduces the same adjudication; database text alone cannot mint or alter a conclusion.

M4.5.3 should now use the already-existing real M4.4.3 `REFUTED` closure experiment end to end and prove fresh-process conclusion recovery without adding another runtime subsystem.
