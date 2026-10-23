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

## Try Codexia on real Research Work (opt-in alpha)

There is now an operator-facing command built on **existing Gen2 Work,
Research Work Pack v1, and CWA Direct HTTP**. It handles up to four bounded
research/critique/revision/synthesis model turns, returns for genuine human
attention, and can materialize the resulting Markdown report. It does **not**
edit a project, run tests, commit Git changes, or grant model-selected effects.

On Windows, from a Codexia checkout containing
`tools/dw_research_http.py`, using the existing CWA Python environment:

```powershell
$env:PYTHONPATH = (Resolve-Path .\src).Path + ";" + "W:\dev\chatgpt-web-adapter\src"
$cwaPython = "W:\dev\chatgpt-web-adapter\.venv\Scripts\python.exe"
$cwaRoot = "W:\dev\chatgpt-web-adapter"

& $cwaPython -m tools.dw_research_http run `
  "Review the project state and propose the next bounded improvement" `
  --file docs\gen2_product_baseline.md `
  --send --cwa-root $cwaRoot `
  --auth-file W:\dev\chatgpt-web-adapter\auth_data.json `
  --out .\artifacts\research-summary.md
```

The command prints a durable `WORK_ID` before any model dispatch. Typing
`SEND` authorizes **up to four** model messages for that one Research Work;
without `--send`, `run` only creates a local Work. Explicit Markdown/TXT
files are embedded as *untrusted reference content* within the immutable
Work objective (16,384-character total limit), kept in the local SQLite
WorkStore, and sent to ChatGPT when you authorize the run. Choose files
without secrets you do not want to send.

If it yields `attention`, answer the displayed `human_question` and
explicitly resume the **same** Work:

```powershell
$workId = "<UUID printed as WORK_ID>"
& $cwaPython -m tools.dw_research_http answer $workId "Prioritize reliability"
& $cwaPython -m tools.dw_research_http resume $workId `
  --send --cwa-root $cwaRoot `
  --auth-file W:\dev\chatgpt-web-adapter\auth_data.json `
  --out .\artifacts\research-summary.md
```

Read-only status and explicit report export are also available:

```powershell
& $cwaPython -m tools.dw_research_http status $workId
& $cwaPython -m tools.dw_research_http export $workId `
  --out .\artifacts\summary-copy.md
```

Use `--store <sqlite-path>` **before** the subcommand for a custom local
WorkStore. Output creation is explicit and will never overwrite an existing
file. No HTTP POST is retried when a durable CognitionHandoff exists, even
after a crash. The CLI requires exact canonical assistant-message readback
before a successful cognition result can be admitted. Unknown outcomes
remain unresolved under Issue #88; neither the readback nor the model's
completion claim grants execution authority.

If the first response was a **plain report instead of exact Research Role
JSON**, Codexia preserves the original `role.completed` event and does not
retry the admitted handoff. The operator now reports
`INVALID_ROLE_OUTPUT_PRESERVED` and offers a read-only extraction of the
exact original text:

```powershell
& $cwaPython -m tools.dw_research_http status $workId
& $cwaPython -m tools.dw_research_http export-role $workId `
  --stage initial --out .\artifacts\first-role-raw.md
```

`export-role` creates a **new** file (never overwrites) and prints the
raw SHA-256. This is **not** a valid synthesis artifact, a repaired RoleRun,
or WorkCompletion; it is a salvageable human-readable model response.
Ordinary `resume --send` detects this state **before confirmation** and
does not make another HTTP request. The operator also supplies the full
nine-key JSON serialization schema to subsequent new Research Role prompts
without modifying the pinned role/Pack semantics. These instructions
improve compliance but cannot guarantee it; noncompliant outputs still fail
closed and are exportable. Existing Work chronology is never rewritten.

This is the first useful **research/reporting workflow**, not yet the
DW5 software-maintenance automation product. Local code editing, tests and
Git changes continue to require their separate human-governed authority.

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

On Windows, the governed M2.5.1 HTTPS credential transport requires patched CPython with private `tempfile.mkdtemp()` directory semantics: CPython **3.11.10+**, **3.12.4+**, or **3.13+**. Older 3.11/3.12 patch releases and unproven alternative Python implementations fail closed for that transport rather than materializing a credential response in a namespace whose confidentiality is not established.

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

Internal professional and business use is not prohibited merely because it is commercial. Separate written **commercial licenses** may be offered for competing products, OEM or white-label distribution, or other uses that require rights beyond the public license.

Because the public license restricts competing use, Codexia is not OSI open-source software. See [`LICENSING.md`](LICENSING.md) for licensing and commercial-use details.