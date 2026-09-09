# M4.5 — Conclusion Adjudication Source Audit

## Audit question

M4.5 must prevent a valid comparison from being silently strengthened, rewritten, or persisted as a broader scientific claim than the exact frozen policy and verified evidence support.

Primary boundary:

```text
comparison outcome != unrestricted scientific conclusion
```

Authority boundary:

```text
human/model wording != conclusion adjudication authority
```

Persistence boundary:

```text
persisted conclusion != conclusion authority
```

## Threat-to-evidence map

| Threat | M4.5 evidence |
| --- | --- |
| Caller chooses `SUPPORTED` / `REFUTED` / `INCONCLUSIVE` directly | M4.5.1 `AdjudicatedConclusion.create()` receives exact M4 objects, not a caller verdict; verdict maps deterministically from `ComparisonOutcome`. |
| Caller writes stronger/free-form scientific prose | M4.5.1 generates one canonical bounded summary for each outcome; the public constructor is disabled and the normal adjudication API accepts no summary text. |
| Caller substitutes conclusion identity | `conclusion_id` is deterministic from the exact M4.4 `result_digest`; strict validation rejects another identity. |
| Caller swaps baseline/candidate or another manifest | M4.5.1 binds exact hypothesis, both experiment ids, both manifest digests, frozen policy, and result arm identities. |
| Caller attaches another policy/result | Exact `policy_id`, `policy_digest`, `freeze_digest`, `result_id`, and `result_digest` are jointly bound and checked. |
| Structurally valid JSON is treated as provenance authority | M4.5.1 decoder is structure-only; M4.5.2 recovery independently re-recovers M4.4 state and recomputes the expected conclusion. |
| Attacker forges a self-consistent alternate verdict with a fresh valid SHA-256 | M4.5.2 adversarial regression rewrites a real `REFUTED` conclusion into internally consistent `SUPPORTED` state with a freshly recomputed digest; authoritative recomputation rejects it. |
| Persisted payload/index tamper | M4.5.2 validates canonical JSON, payload digest, policy/result/conclusion indexes, and exact derived equality. |
| Database row becomes semantic source of truth | `SqliteAdjudicatedConclusionRegistry.recover()` returns freshly derived state after validating the persisted cache; the row remains derived cache only. |
| Underlying comparison/evidence changes after conclusion publication | Conclusion recovery transitively invokes M4.4 result recovery; M4.3 physical mutation therefore invalidates M4.4 and M4.5 recovery fail-closed. |
| Recovery secretly reruns experiments | M4.5.3 child process constructs only recovery registries, not `GovernedPythonJsonRunner` or authorization machinery; all four physical result files remain byte-identical with unchanged `mtime_ns`. |
| Recovery silently changes the comparison criterion | M4.5.3 receives only the existing stable `policy_id` and recovers the already-frozen policy/result; no replacement policy creation path exists in the child process. |
| `REFUTED` is upgraded to universal scientific falsity | M4.5.1 fixed scope is `frozen_comparison_policy.v1`; canonical wording explicitly limits refutation/support to the declared policy and evidence. |
| Conclusion automatically grants action authority | No M4.5 object or registry introduces M2 capability, process, filesystem, Git, network, delegation, scheduler, or recommendation execution authority. |

## M4.5.1 — Semantic authority boundary

`AdjudicatedConclusion` is a distinct cross-experiment record rather than a retroactive widening of the M4.1 single-manifest `Conclusion`.

Authoritative construction binds:

```text
Hypothesis
+ baseline ExperimentManifest
+ candidate ExperimentManifest
+ FrozenComparisonPolicy
+ ComparisonResult
→ one deterministic policy-scoped conclusion
```

The normal path does not accept semantic output fields from the caller.

The direct dataclass constructor is disabled (`init=False` plus rejecting `__init__`) so callers cannot bypass `create()` by filling `verdict`, `summary`, or `conclusion_id` manually.

The strict decoder verifies structural/canonical consistency only. It is deliberately not advertised as provenance authority by itself.

## M4.5.2 — Durable derived-state boundary

`SqliteAdjudicatedConclusionRegistry.publish(policy_id)` accepts only one stable policy identity and derives every conclusion field through authoritative recovery.

Publication path:

```text
policy_id
→ recover frozen comparison policy
→ recover durable comparison result
→ recover exact baseline/candidate experiments
→ AdjudicatedConclusion.create(...)
→ persist canonical derived cache
```

Recovery path:

```text
persisted row
→ structural/canonical/index validation
→ fresh M4.4 recovery
→ fresh M4.5.1 recomputation
→ exact equality required
→ return freshly derived conclusion
```

This specifically separates digest integrity from adjudication provenance.

## M4.5.3 — End-to-end closure

M4.5.3 reuses the M4.4.3 integration-error comparison. The frozen claim requires an improvement of at least `180`; verified effect is `176`, so the result is `REFUTED`.

The durable conclusion must remain:

```text
scope   = frozen_comparison_policy.v1
verdict = REFUTED
```

with the exact bounded summary generated by M4.5.1.

A fresh Python process receives only the SQLite path, existing `policy_id`, and expected deterministic conclusion identity. It reconstructs the policy, result, and conclusion through recovery registries. It has no experiment runner, authorization authority, replacement policy construction, or caller-provided conclusion text.

The parent verifies all four governed physical result files have unchanged bytes and `mtime_ns` after the child exits.

## Composition matters

No single M4.5 layer is sufficient alone:

- M4.5.1 proves canonical bounded semantics but not durable provenance;
- M4.5.2 proves durable recovery but relies on M4.4/M4.3 dependency recovery;
- M4.5.3 proves the complete restart path on a real inconvenient `REFUTED` case.

The milestone guarantee therefore depends on composition with earlier M4 guarantees:

```text
M4.3 verified physical evidence
+ M4.4 frozen comparison and deterministic result
+ M4.5 canonical bounded adjudication and recovery
```

## Explicit limits

This audit does not claim:

- universal scientific truth/falsity;
- correctness of the hypothesis design itself;
- generalization outside the declared experiment scope;
- external replication;
- statistical significance machinery not declared by the policy;
- hostile-machine or kernel attestation;
- interactive identity proof for the HUMAN-source test receipts;
- automatic recommendation/action authority;
- autonomous experiment scheduling.

## Closure criterion

M4.5 is complete only if the exact reviewed candidate demonstrates:

> A policy-scoped conclusion is derived only from exact recovered M4.4 state, survives restart through deterministic recomputation, rejects even self-consistent persisted semantic forgery, and cannot be strengthened into a broader scientific claim through the governed adjudication path.
