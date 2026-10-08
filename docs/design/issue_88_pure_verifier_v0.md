# Issue #88 — Pure reconciliation verifier v0

**Status:** implementation candidate, offline proof semantics only  
**Scope:** one existing cognition handoff; no provider interaction, authority, or WorkStore writes  
**Companion design:** [Issue #88 design PR #89](https://github.com/kymuco/codexia-manual-agent/pull/89)

## Purpose

The v0 implementation exposes a narrow, fully deterministic comparison of
caller-supplied expected metadata and a *previously obtained* readback
observation:

```python
from codexia_manual_agent.reconciliation import verify_readback

decision = verify_readback(pending_handoff, previously_read_observation)
```

The verifier does not import an external provider or the Codexia WorkStore.
It never calls `CognitionPort.complete`, sends model prompts, emits new
`CognitionOutcome`, admits Work events, or alters the Research Pack's
completion judgment.

The host remains responsible for recovering and validating the actual
`CognitionRequest` and `CognitionHandoff` from durable events, verifying
that the RoleRun is still `REQUESTED`, authenticating the provider read
source, and deriving the **exact original wire-input SHA-256**, which can
differ from the semantic request digest when an adapter applies prompt
prefixes or hot-conversation delta serialization.

## Typed inputs and verdicts

`PendingCognition` binds Work, WorkflowRun, RoleRun, request, handoff,
port, provider namespace, original wire hash, and expected Work frontier.
Its constructor checks canonical text/digest types but does **not** query
the WorkStore to prove those values.

`ReadbackObservation` captures the provider-read status, request/response
identities, observed wire hash, number of candidates, message order,
intervening user turns, branch-lineage assertion, finish signal, and full
raw response text plus its independently claimed SHA-256. Its fields
are **untrusted assertions** until verified by a higher-trust read adapter.

`ReconciliationDecision` is immutable and includes a verdict, reason code,
raw response digest, request/handoff identity, and deterministic report
digest. It intentionally carries no response text, admission permission,
provider receipt, or role outcome.

| Verdict | Meaning | Admission by v0 |
| --- | --- | --- |
| `CONTEXT_CORROBORATED` | Readback assertions are self-consistent with a unique completed turn | Forbidden |
| `UNVERIFIABLE` | Missing, pending, unavailable, or incomplete readback evidence | Forbidden |
| `CONFLICT` | Identity, digest, uniqueness, ordering, or lineage contradicts the scope | Forbidden |

**No `EXACT_PROVIDER_CORRELATED` verdict is emitted in v0.** A genuine
provider-issued receipt and a trusted way to authenticate it have not been
designed/implemented yet. The `eligible_for_automatic_admission` property
always returns `False`.

The raw output is intentionally **not normalized** in the generic verifier:
DW3's terminal `<escape>` handling remains pilot/domain-specific.
A future versioned canonicalization policy needs its own separate proof
and tests. The verifier never changes control booleans such as
`material_uncertainty_resolved`.

## Failure semantics

- `NOT_FOUND`, `PENDING`, `UNSUPPORTED`, `READ_UNAVAILABLE` and
  missing observations yield `UNVERIFIABLE`. None constitutes proof
  that the provider did not receive an effect.
- Changed port/provider namespace, request identity, wire SHA, multiple
  candidates, contradictory branch/message order, intervening user turn,
  bad response SHA, invalid UTF-8, or oversized response yield `CONFLICT`.
- Unknown branch, response order, message IDs, candidate count, or
  incomplete final signal yield `UNVERIFIABLE`.
- All reports are stable across repeated calls with identical inputs.
  The `report_digest` excludes raw response text while binding its
  SHA-256 and all observation metadata.

## Tests and non-claims

`tests/test_cognition_reconciliation_proof_v0.py` runs without credentials,
network, browser, GPU, real provider, or WorkStore. It exercises the
fail-closed cases and determinism from Issue #88's proposed matrix.

This slice **does not** implement all 61 proposed scenarios. In particular,
CAS, durable proof storage, role admission, cross-provider readback, original
provider receipt authentication, real branch traversal, and crash-safe
concurrency require later scoped work. No general exactly-once claim is
made, and no tests are reported as green until locally executed.

Recommended local checks:

```powershell
$files = @(
  "src/codexia_manual_agent/reconciliation/__init__.py"
  "src/codexia_manual_agent/reconciliation/proof_v0.py"
  "tests/test_cognition_reconciliation_proof_v0.py"
)
python -m ruff check $files
python -m ruff format --check $files
python -m pytest tests/test_cognition_reconciliation_proof_v0.py -q
```

The next design review must settle the durable provider receipt schema,
trust authority, outcome identity and admission policy **before** connecting
the verifier to a live host or the WorkStore.
