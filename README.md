# Codexia Manual Agent

Codexia is a human-governed coding agent and local computational laboratory built around explicit authority, durable evidence, and fail-closed execution boundaries.

> **Status:** alpha (`0.5.0a0`). Codexia is actively developed and its security-sensitive interfaces should be treated as experimental.

## What Codexia is

Codexia separates model reasoning from the authority to change a machine or repository. A model may propose work, but local policy, explicit authorization, bounded executors, and durable receipts determine what can actually happen.

The project currently spans four layers:

- **agent runtime** — structured model interaction, bounded workspace inspection, prompts, and provider adapters;
- **local authority and execution** — digest-bound proposals, one-shot authorization, controlled process/workspace/patch/Git mutation, and platform-specific containment;
- **durable coordination** — persistent sessions, event receipts, bounded delegation, recovery, and integrity checks;
- **computational lab** — typed experiment/run/evidence contracts plus an append-only SQLite registry for durable experiment, run, metric, and artifact existence.

Codexia is developed and distributed as a standalone project. It does not require HDE, while remaining compatible with broader HDE integration.

## Core safety model

The central rule is simple:

```text
model intent != execution authority
```

For side effects, Codexia uses an explicit authority spine:

```text
PROPOSED
→ AUTHORIZED
→ authorization consumed once
→ EXECUTED
→ OBSERVED
```

A denial is terminal:

```text
PROPOSED → DENIED
```

Important properties include:

- proposal and observation data are digest-bound;
- authorization receipts are one-shot and capability-scoped;
- sensitive paths and repository control data are excluded from ordinary model-driven access;
- execution and mutation backends fail closed when their required containment/atomicity primitives are unavailable;
- provider or web-session transport is not treated as a security boundary;
- durable registries distinguish authoritative chronology from derived navigation indexes and detect disagreement as persistence corruption.

Specific capabilities and platform guarantees are documented milestone-by-milestone under [`docs/`](docs/).

## Current development line

The repository currently contains the development line through **M4.2 — Durable Experiment / Run / Evidence Registry**.

Highlights include:

- M1 — read-only runtime and bounded model-tool loop;
- M2 — local authority, controlled process/workspace mutation, patch application, Git mutation governance, and bounded delegation;
- M3 — persistent sessions/event receipts and durable delegation recovery;
- M4.1 — computational-lab core contracts;
- M4.2 — authoritative append-only SQLite experiment/run/evidence registry with integrity, recovery, concurrency, and corruption hardening.

See [`docs/roadmap.md`](docs/roadmap.md) and [`CHANGELOG.md`](CHANGELOG.md) for the detailed history.

## Requirements

- Python **3.11+**
- Windows or Linux, depending on the capability being exercised
- Bubblewrap for Linux process containment where required

Some mutation primitives are intentionally platform-constrained. Codexia does not silently fall back to a weaker backend when a required security primitive is unavailable.

## Install

Core development install:

```bash
python -m pip install -e .
```

With the optional ChatGPT web transport:

```bash
python -m pip install -e ".[web]"
```

With test tooling:

```bash
python -m pip install -e ".[test]"
```

## Run the test suite

```bash
python -m pytest -q
```

The project test suite is designed to exercise denial, corruption, rollback, recovery, and authority-boundary behavior in addition to successful paths.

## Basic usage

Read-only model task:

```powershell
codexia run "Inspect this repository and summarize its architecture" `
  --workspace W:\dev\some-repository `
  --auth-file W:\secrets\auth_data.json `
  --model thinking `
  --reasoning-effort high
```

Direct read-only inspection:

```powershell
codexia inspect --workspace W:\dev\some-repository list
codexia inspect --workspace W:\dev\some-repository read README.md
codexia inspect --workspace W:\dev\some-repository search "TODO" src
codexia inspect --workspace W:\dev\some-repository git-status
```

Human-authorized local process:

```powershell
codexia exec `
  --workspace W:\dev\some-repository `
  --approve `
  --timeout 60 `
  -- `
  python -m pytest -q
```

Do not treat these examples as a complete capability reference. Security-sensitive execution and mutation behavior is defined by the contracts and milestone documentation, not by README examples.

## Documentation

Useful entry points:

- [`docs/architecture.md`](docs/architecture.md) — architecture overview;
- [`docs/governance.md`](docs/governance.md) — project and authority principles;
- [`docs/roadmap.md`](docs/roadmap.md) — milestone history and planned work;
- [`docs/m4_1_computational_lab_core_contracts.md`](docs/m4_1_computational_lab_core_contracts.md) — computational-lab contracts;
- [`docs/m4_2_durable_experiment_registry.md`](docs/m4_2_durable_experiment_registry.md) — durable experiment/run/evidence registry.

Historical prompts and evaluation material are retained for provenance where appropriate.

## Security

Do not commit authentication state, cookies, tokens, private keys, or live credentials. See [`SECURITY.md`](SECURITY.md) for vulnerability reporting and the supported security posture.

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md). Changes that expand authority, weaken fail-closed behavior, or alter durable evidence semantics require explicit tests and review.

## License

Codexia is **source-available** under the [PolyForm Perimeter License 1.0.1](LICENSE). The public license permits use, modification, and distribution for permitted purposes, but it does not permit providing others with a product that competes with Codexia as defined by the license.

Internal professional and business use is not prohibited merely because it is commercial. Separate written commercial licenses may be offered for competing products, OEM or white-label distribution, or other uses that require rights beyond the public license.

Because the public license restricts competing use, Codexia is not OSI open-source software. See [`LICENSING.md`](LICENSING.md) for the licensing, contribution, and commercial-licensing policy.
