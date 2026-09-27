# Codexia Manual Agent

Codexia is a **portable governed runtime for durable delegated intellectual work**.

The repository name reflects the project's origin as a manually governed coding agent. The current Gen2 architecture is broader: Codexia owns the continuity and semantic lifecycle of delegated Work while keeping external execution authority separate.

> **Status:** alpha (`0.5.0a0`). Gen2 is the current product-development baseline. The standalone daily-use product is not yet considered proven; product proof is tracked through DW0–DW6.

## Core idea

Codexia is built around a strict separation:

```text
model intent != execution authority
```

A model may reason, progress Work, delegate child Work, request capabilities, produce artifacts/evidence, ask for human judgment, or make a completion claim. None of those acts automatically grants permission to execute a process, mutate a workspace, change Git state, use the network, or perform another external effect.

Important invariants include:

```text
worker output != WorkCompletion
AttentionNeed != approval
CapabilityNeed != host authority
CapabilityOutcome.UNKNOWN != retry permission
restart != replay
```

## Current Gen2 runtime

The current Gen2 baseline includes:

- durable `Work` with exact ingress identity and append-only chronology;
- `Workflow` / `WorkflowRun` progression;
- `Role` / `RoleRun` bounded cognition;
- durable child-Work delegation;
- `ContextProjection`;
- `CapabilityNeed`, durable capability handoff, and `CapabilityOutcome`;
- `AttentionNeed` / `AttentionResponse`;
- `ArtifactRef` / `EvidenceRef` relationships;
- `CompletionClaim` and guarded `WorkCompletion`;
- semantic `Pack` membership and exact Pack pinning;
- restart-safe durable Work-yield projection;
- finite bounded progression of one existing Work without a generic scheduler.

The unit Codexia owns is **Work continuity**. Host environments remain responsible for concrete external authority and effects.

## Durable Work lifecycle

A simplified Gen2 flow is:

```text
human / host delegates Work
        ↓
Work
        ↓
Workflow / Role cognition
        ↓
optional child Work
optional CapabilityNeed
optional ArtifactRef / EvidenceRef
        ↓
AttentionNeed when genuine human judgment is required
        ↓
CompletionClaim
        ↓
guarded WorkCompletion
```

Current durable host-return projection recognizes only:

```text
COMPLETION
    exact guarded WorkCompletion

ATTENTION
    exact AttentionNeed at the current Work chronology head

NONE
    no current durable return frontier
```

`NONE` is not a Work state and does not mean that progress is automatically safe or required.

## Bounded progression

One existing Work can be progressed under an explicit finite budget.

Mechanism-level results include:

```text
YIELDED
BOUND_EXHAUSTED
QUIESCENT
TERMINAL_NON_YIELD
```

These are not canonical Work states. They describe only the result of one bounded progression call.

Each bounded semantic step is tied to the exact observed Work frontier. Concurrent durable changes invalidate stale progression decisions before they can continue from the old frontier.

## Standalone viability

The SV1–SV4 line proves increasingly strong standalone properties:

- **SV1** — real standalone process Work from ingress to guarded WorkCompletion;
- **SV2** — restart-safe continuation of the same Work without redispatching already durable handoffs;
- **SV3** — durable process attempts, one-shot authority consumption, and an independent runner;
- **SV4** — exact runner-liveness ownership and safe hard-kill reconciliation to `UNKNOWN` without replay.

These proofs establish that Gen2 can survive real external-effect boundaries without making Codexia itself a general authority root.

## Historical runtime and research substrate

The repository also retains the earlier governed runtime and computational-lab lines:

- **M1** — bounded read-only model/runtime foundation;
- **M2** — governed process/workspace/patch/Git authority and execution;
- **M3** — durable sessions, authority chronology, and delegation recovery;
- **M4** — computational-lab contracts, governed execution, evidence, comparison, and bounded conclusion;
- **M5** — bounded governed automation;
- **M6 / Simple Work** — historical delegated-work continuity and product experiments.

Those lines remain valuable implementation history and evidence, but new product development should not treat M6.x / Simple Work internals as the normative Gen2 architecture.

See [`docs/roadmap.md`](docs/roadmap.md) for milestone history and [`docs/gen2_product_baseline.md`](docs/gen2_product_baseline.md) for the current product baseline.

## Current product roadmap

Gen2 product development is now tracked by the DW roadmap:

```text
DW0  freeze Gen2 baseline / retire legacy product branches
DW1  standalone Gen2 Work surface
DW2  Research Work Pack v1
DW3  real Research Work vertical
DW4  restart + genuine human-attention continuation
DW5  Software Work Pack v1
DW6  general daily-use delegated-work pilot
```

The roadmap is tracked in GitHub issue **#84**.

The development rule is intentionally failure-driven:

```text
real vertical
→ concrete failure
→ smallest missing invariant
→ bounded repair
→ rerun vertical
```

Major integrations such as HDE or a Runplane-backed technical host are intentionally deferred until the standalone Gen2 product proof is complete.

## Existing governed local execution surfaces

The older local-runtime surfaces remain available for bounded, explicitly governed work.

Read-only inspection:

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

These surfaces are not the future DW1 product UI and should not be read as a complete capability reference.

## Requirements

- Python **3.11+**
- Windows or Linux, depending on the capability being exercised
- Bubblewrap for Linux process containment where required

Some high-assurance mutation primitives are intentionally platform-constrained. Codexia fails closed rather than silently substituting weaker guarantees.

## Install

Core development install:

```bash
python -m pip install -e .
```

With optional ChatGPT web transport:

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

The test suite exercises successful paths together with denial, corruption, concurrency, replay, restart, ambiguity, and authority-boundary behavior.

## Documentation

Useful entry points:

- [`docs/gen2_product_baseline.md`](docs/gen2_product_baseline.md) — current Gen2 product baseline and DW policy;
- [`docs/roadmap.md`](docs/roadmap.md) — historical milestone roadmap;
- [`docs/architecture.md`](docs/architecture.md) — governed runtime architecture;
- [`docs/governance.md`](docs/governance.md) — project and authority principles;
- [`docs/hde_durable_work_yield_projection_v1.md`](docs/hde_durable_work_yield_projection_v1.md) — durable Work-yield projection contract;
- [`docs/hde_bounded_existing_work_progression_v1.md`](docs/hde_bounded_existing_work_progression_v1.md) — bounded existing-Work progression contract;
- [`docs/sv4_process_runner_ownership_reconciliation.md`](docs/sv4_process_runner_ownership_reconciliation.md) — hard-kill runner reconciliation.

Historical M1–M6 documents are retained for provenance.

## Security

Do not commit authentication state, cookies, tokens, private keys, or live credentials. See [`SECURITY.md`](SECURITY.md) for vulnerability reporting and the supported security posture.

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md).

Changes that expand authority, weaken fail-closed behavior, alter durable evidence semantics, introduce replay, or add new canonical Work semantics require explicit tests and review.

## License

Codexia is **source-available** under the [PolyForm Perimeter License 1.0.1](LICENSE).

The public license permits use, modification, and distribution for permitted purposes, but it does not permit providing others with a competing product as defined by the license.

Because the public license restricts competing use, Codexia is not OSI open-source software. See [`LICENSING.md`](LICENSING.md) for licensing and commercial-use details.