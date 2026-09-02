# M4.1 Candidate 2 Post-Ready Audit

## Why Candidate 2 exists

Candidate 1 (`a5cbdd79f12aa0360c8d213d499ec57e5e6ab38b`, tree `4f6c3dd299932a87232faf9136051351a7bbed12`) passed exact-head Windows validation, but the required pre-merge metadata/review check surfaced two new P2 findings after Ready.

Because both findings require source changes, Candidate 1 is retained only as historical validation evidence and is no longer a merge candidate.

## Finding 6 — boolean schema versions aliased integer version 1

Python defines `True == 1`. The original equality-only `schema_version != LAB_SCHEMA_VERSION` checks therefore admitted a boolean schema version if a caller supplied a correspondingly recomputed unkeyed record digest.

Repair:

- add one shared `_validate_schema_version` helper;
- require `type(value) is int` in addition to exact version equality;
- route every M4.1 record `__post_init__` through the helper;
- add regression coverage for all six public record decoders with `schema_version=True`.

## Finding 7 — timestamps were parsed before any size/canonical-form budget

`datetime.fromisoformat` accepts long fractional-second strings. The original validator therefore allowed an arbitrarily large timestamp string to reach parsing and later canonical-digest processing.

Repair:

- add `MAX_TIMESTAMP_CHARS = 64`;
- reject non-string, empty, NUL-containing or oversized values before parsing;
- retain timezone-awareness validation;
- require `parsed.isoformat()` to equal the persisted timestamp exactly;
- add regressions for oversized timestamps and parseable-but-noncanonical timestamp forms.

## Scope check

The repairs do not add execution, filesystem, provider, Git, delegation, scheduler or authority surfaces. They only narrow record metadata admission.

The source diff from Candidate 1 is limited to:

- `src/codexia_manual_agent/lab/models.py`: strict schema/timestamp validation;
- `tests/test_lab_review_findings.py`: focused regressions;
- this audit record.

Both review threads were answered with the exact repair commits and resolved before Candidate 2 freeze.

## Candidate policy

Candidate 2 must be frozen from the reviewed repair tree over the same exact merged M3.2 base `d633a1047e1c2607849c5ccdde1c7213734dc462`.

No PASS from Candidate 1 transfers to Candidate 2. Candidate 2 requires fresh exact-head Windows gates:

```text
python -m pytest -q tests/test_lab_contracts.py tests/test_lab_contract_hardening.py tests/test_lab_serialization.py tests/test_lab_review_findings.py
python -m pytest -q
```

Any later source/tree mutation invalidates Candidate 2 and requires another frozen candidate.
