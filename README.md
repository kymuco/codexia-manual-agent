# Codexia Manual Agent

Codexia is a **governed AI work runtime and computational research environment** built around a strict separation between reasoning, work coordination, and authority to change the outside world.

The repository name reflects the project's origin as a manually governed coding agent. Its current scope is broader: Codexia now includes a natural delegated-work surface, durable work/session coordination, governed local execution and mutation, and a reproducible computational lab.

> **Status:** alpha (`0.5.0a0`). Security-sensitive interfaces are actively developed and should be treated as experimental.

## What Codexia is

Codexia is organized around one primary invariant:

```text
model intent != execution authority
```

A model may reason, plan, delegate, or propose an action. That does not itself grant permission to execute a process, mutate a workspace, create a commit, push a ref, or perform another side effect.

Today the repository contains five major layers:

- **Simple Work** — natural Codexia conversations, optional workers, continuation, human-attention boundaries, completion, recovery, and artifact intake;
- **provider/runtime boundary** — ChatGPT product-runtime transport through `chatgpt-web-adapter`, including saved conversations, Temporary Chat, canonical status/history, and artifact handoff;
- **governed local authority and execution** — digest-bound proposals, one-shot authorization, controlled process execution, workspace/patch mutation, and governed Git/network transports;
- **durable coordination** — persistent sessions, append-only event chronology, bounded delegation, recovery, integrity checks, and delegated-work supervision;
- **computational lab and bounded automation** — experiments, evidence, comparisons, conclusions, and reproducible research workflows that reuse the same authority boundaries.

Codexia is standalone. It does not require HDE or another host environment.

## Simple Work v0.3

Simple Work is the smallest current product loop.

A user gives Codexia a task. Codexia may solve it directly, delegate a separate branch of work, ask for a genuine human decision, or return a completed result.

```text
Human
  ↓
Codexia
  ├─ solve directly
  ├─ Temporary Worker
  ├─ Persistent Worker
  └─ ask Human only when needed
```

The human-readable control surface is intentionally small:

```text
ГОТОВО: <final result>
К ПОЛЬЗОВАТЕЛЮ: <one real question>
ВРЕМЕННЫЙ WORKER: <one-off separate work>
ПОСТОЯННЫЙ WORKER: <long-lived separate work>
```

Once a worker exists, Codexia can continue it with ordinary natural-language instructions instead of repeatedly emitting a worker-creation marker.

Simple Work currently supports:

- selectable long-lived saved Codexia conversations;
- process-local Temporary Codexia;
- temporary and persistent workers;
- multi-turn continuation in the same worker conversation;
- explicit human-attention boundaries;
- bounded cycle handling;
- fail-closed reconciliation after ambiguous provider writes;
- durable local transcripts;
- generated-artifact intake from explicit ChatGPT sandbox links;
- separate semantic-completion and Temporary-cleanup evidence.

Start a normal work item:

```powershell
python -m codexia_manual_agent.simple_work.cli start `
  "Придумай три нейтральных названия для тестового проекта заметок." `
  --auth-file auth_data.json
```

Start a disposable Temporary Codexia work item:

```powershell
python -m codexia_manual_agent.simple_work.cli start `
  "Одноразово исследуй этот вопрос и дай итог." `
  --temporary-codexia `
  --auth-file auth_data.json
```

Inspect or continue work:

```powershell
python -m codexia_manual_agent.simple_work.cli status <work_id>
python -m codexia_manual_agent.simple_work.cli history <work_id>
python -m codexia_manual_agent.simple_work.cli resume <work_id>
python -m codexia_manual_agent.simple_work.cli answer <work_id> "Мой ответ"
python -m codexia_manual_agent.simple_work.cli reconcile <work_id>
```

Saved Codexia contexts can be registered and selected explicitly:

```powershell
python -m codexia_manual_agent.simple_work.cli codexia-add `
  voice-engine <conversation_id>

python -m codexia_manual_agent.simple_work.cli start `
  "Продолжи Voice Engine." `
  --codexia voice-engine `
  --auth-file auth_data.json
```

See [`docs/simple_work_v0.md`](docs/simple_work_v0.md) for the complete Simple Work contract.

## Core authority model

For local side effects, Codexia uses an explicit authority spine:

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

- identity is not permission;
- context is not approval;
- model output is not authorization;
- delegation cannot mint authority;
- proposals and observations are digest-bound;
- authorization receipts are one-shot and capability-scoped;
- sensitive paths and repository control data are excluded from ordinary model-driven access;
- execution and mutation backends fail closed when required containment or atomicity primitives are unavailable;
- provider/browser state is transport state, not a security boundary;
- ambiguous side effects are not automatically retried;
- durable recovery reconstructs state without silently replaying side effects.

## Governed local runtime

The original Codexia runtime remains a substantial part of the project.

It includes:

- bounded read-only workspace inspection;
- human-governed local process execution;
- command admission and capability envelopes;
- explicit create/replace workspace mutation;
- governed multi-file patch application;
- preimage/postimage and drift binding;
- durable mutation recovery;
- independently governed Git commit and Git push;
- governed SSH and HTTPS Git transports;
- bounded delegation and human escalation;
- persistent session and authority chronology.

Example read-only task:

```powershell
codexia run "Inspect this repository and summarize its architecture" `
  --workspace W:\dev\some-repository `
  --auth-file W:\secrets\auth_data.json
```

Direct workspace inspection:

```powershell
codexia inspect --workspace W:\dev\some-repository list
codexia inspect --workspace W:\dev\some-repository read README.md
codexia inspect --workspace W:\dev\some-repository search "TODO" src
codexia inspect --workspace W:\dev\some-repository git-status
```

Human-authorized bounded process execution:

```powershell
codexia exec `
  --workspace W:\dev\some-repository `
  --approve `
  --timeout 60 `
  -- `
  python -m pytest -q
```

These examples are not a complete capability reference. Security-sensitive behavior is defined by the runtime contracts and milestone documentation.

## Computational lab and automation

Codexia also contains a reproducible computational-research stack.

The M4 line provides durable, typed records for:

```text
Hypothesis
→ ExperimentManifest
→ ExperimentRun
→ Artifact / Metric evidence
→ Comparison
→ Conclusion
```

The M5 line adds bounded automation over already-governed research workflows while preserving the same execution-authority boundary.

This layer is designed for reproducibility and evidence lineage. Provenance establishes declared lineage; it is not treated as scientific truth, actor authenticity, or execution authority.

## Delegated Work foundations

The current `main` branch also contains the M6.1–M6.5 delegated-work foundations:

- general work handoff and provenance;
- continuation admission;
- Chat/Codexia peer-loop identity;
- dynamic human-attention assessment;
- a durable background work supervisor candidate.

The central continuity property is:

```text
human absence != work suspension
```

These stricter research-oriented contracts are retained as a governed substrate and reference model. Simple Work intentionally provides a much smaller natural-language product surface rather than exposing that internal formality directly to the user.

## Current development line

The current `main` branch contains:

- **M1** — read-only model/runtime foundation;
- **M2 through M2.6** — governed local authority, process execution, workspace/patch/Git mutation, and bounded delegation;
- **M3 through M3.2** — durable sessions, chronology, and delegation recovery;
- **M4 through M4.5** — computational-lab contracts, evidence, comparison, and bounded conclusions;
- **M5 through M5.3** — bounded governed automation;
- **M6.1 through M6.5** — delegated-work continuity foundations;
- **Simple Work v0.3** — the current natural Codexia product loop.

See [`docs/roadmap.md`](docs/roadmap.md) for milestone-level detail.

## Requirements

- Python **3.11+**
- Windows or Linux, depending on the capability being exercised
- Bubblewrap for Linux process containment where required

On Windows, the governed M2.5.1 HTTPS credential transport requires a CPython version with the expected private temporary-directory semantics: CPython **3.11.10+**, **3.12.4+**, or **3.13+**.

Some high-assurance mutation paths are intentionally platform-constrained. Codexia does not silently fall back to a weaker backend while claiming a stronger guarantee.

## Install

Core development install:

```bash
python -m pip install -e .
```

With the ChatGPT web/product transport:

```bash
python -m pip install -e ".[web]"
```

With test tooling:

```bash
python -m pip install -e ".[test]"
```

## Test

```bash
python -m pytest -q
```

The test suite exercises denial, corruption, rollback, crash/recovery, replay boundaries, transport ambiguity, authority invariants, and successful paths.

## Documentation

Useful entry points:

- [`docs/simple_work_v0.md`](docs/simple_work_v0.md) — current Simple Work product loop;
- [`docs/architecture.md`](docs/architecture.md) — governed runtime architecture;
- [`docs/governance.md`](docs/governance.md) — authority and project principles;
- [`docs/roadmap.md`](docs/roadmap.md) — milestone history and current development line;
- [`docs/m3_persistent_sessions_event_receipts.md`](docs/m3_persistent_sessions_event_receipts.md) — durable session chronology;
- [`docs/m4_1_computational_lab_core_contracts.md`](docs/m4_1_computational_lab_core_contracts.md) — computational-lab contracts;
- [`docs/m5_3_first_real_bounded_automation.md`](docs/m5_3_first_real_bounded_automation.md) — first complete bounded automation loop;
- [`docs/m6_5_background_work_supervisor.md`](docs/m6_5_background_work_supervisor.md) — delegated-work supervisor candidate.

Historical design and source-audit documents are retained for provenance.

## Security

Do not commit authentication state, cookies, tokens, private keys, or live credentials. See [`SECURITY.md`](SECURITY.md) for vulnerability reporting and the supported security posture.

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md).

Changes that expand authority, weaken fail-closed behavior, alter durable evidence semantics, or introduce automatic replay require explicit tests and review.

## License

Codexia is **source-available** under the [PolyForm Perimeter License 1.0.1](LICENSE).

The public license permits use, modification, and distribution for permitted purposes, but it does not permit providing others with a competing product as defined by the license. Internal professional and business use is not prohibited merely because it is commercial.

Because the public license restricts competing use, Codexia is not OSI open-source software. See [`LICENSING.md`](LICENSING.md) for the licensing, contribution, and commercial-licensing policy.
