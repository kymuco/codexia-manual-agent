# Contributing

Codexia is an authority-sensitive project. Changes are evaluated for functionality, permissions, preserved evidence, and failure modes.

## Principles

- Keep transport, reasoning, execution, and approval authority separate.
- Never give a remote model uncontrolled shell, filesystem, Git, network, or credential authority.
- Preserve exact evidence, durable chronology, and tool/authorization receipts.
- Prefer fail-closed behavior when a required security or atomicity primitive is unavailable.
- Prefer narrow real-workflow milestones over broad speculative frameworks.
- Historical prompt/evaluation artifacts are provenance: do not edit them in place merely to make current behavior look cleaner.

## Development setup

```bash
python -m pip install -e ".[test]"
python -m pytest -q
```

If your change touches the optional web provider contract:

```bash
python -m pip install -e ".[web,test]"
python -m pytest -q tests/test_web_adapter_contract.py
```

Platform-specific execution or mutation changes should also be exercised on every affected operating system.

## Change checklist

Before proposing a change, verify that:

- scope and non-goals are explicit;
- trust and capability boundaries are explicit;
- new authority has an admission and authorization story rather than being implicit;
- tests cover denial, corruption/recovery, rollback, or failure paths where relevant, not only success;
- no secret, session authentication material, or private credential is committed;
- persistence changes distinguish authoritative state from derived/navigation state;
- documentation describes the actual implementation rather than an intended future state;
- the full test suite passes on the exact candidate being proposed.

For vulnerabilities, follow [`SECURITY.md`](SECURITY.md) rather than posting exploit details publicly.

## External contributions

Codexia's source-available edition uses the PolyForm Perimeter License 1.0.1, and separate commercial licensing may also be offered. Keeping that option available requires explicit contributor terms for accepted third-party work.

Until a contributor-agreement process is published, unsolicited code or documentation contributions are not accepted for inclusion in the canonical distribution. Issues, design proposals, reproducible test cases, and discussion remain welcome. A maintainer may invite a contribution under separate written terms.

Submitting a pull request does not by itself grant additional relicensing rights beyond the terms explicitly agreed for that contribution.

## License

The canonical Codexia distribution is source-available under the [PolyForm Perimeter License 1.0.1](LICENSE), except where material explicitly states different terms. The public license restricts providing competing products as defined by its terms; separate written commercial licenses may be offered for uses that require different rights. See [`LICENSING.md`](LICENSING.md) for the licensing and contribution policy.
