# Changelog

## Unreleased

### Public baseline

- Current package version: `0.5.0a0`.
- The public repository begins from a clean source-available snapshot rather than exposing earlier private Git lineage.
- The baseline includes the development line through M4.2.

### Added

- M1 — bounded read-only agent runtime, provider abstraction, workspace inspection, and conversation continuation.
- M2.0 — digest-bound local action proposals, explicit policy/approval source, one-shot authorization receipts, and fail-closed lifecycle semantics.
- M2.1–M2.2 — bounded local-human process execution plus narrow command-family admission and capability envelopes.
- M2.3–M2.4 — governed workspace create/replace and multi-file patch application with exact pre/postimage evidence, drift checks, atomic commit semantics where supported, and durable recovery.
- M2.5–M2.5.1 — explicit Git commit/push authority and governed SSH/HTTPS transport with exact identity and lease binding.
- M2.6 — bounded delegation and human escalation under the rule that delegation cannot mint authority.
- M3.1 — durable session, provider, tool, and authority chronology with integrity-checked recovery.
- M3.2 — durable bounded-delegation recovery, budget accounting, request-claim replay protection, escalation state, and cancellation.
- M4.1 — immutable computational-lab contracts for hypotheses, manifests, runs, artifacts, metrics, and evidence-bounded conclusions.
- M4.2 — authoritative append-only experiment/run/evidence registry with exact lineage replay, sealing, concurrency control, recovery, and corruption detection.

### Security

- Remote-model tools remain read-only.
- Model/provider transport is not treated as a security boundary.
- Local side effects require explicit admitted capability and local authority.
- Authorization receipts are proposal-bound and single-use.
- Sensitive workspace/control paths are excluded from ordinary model-driven access.
- Process, workspace, patch, Git, delegation, persistence, and computational-evidence paths preserve explicit failure and unknown-outcome semantics.
- Platform-specific strong guarantees fail closed when their required primitive is unavailable.

For milestone-level details and current limitations, see [`docs/roadmap.md`](docs/roadmap.md), [`docs/architecture.md`](docs/architecture.md), and the focused documents under [`docs/`](docs/).
