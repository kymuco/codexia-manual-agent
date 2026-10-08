# DW3 Research Pilot — Closure

Date: 2026-10-08

## Verdict

**Execution and recovery: supported. Formal Research Work completion: not met.**

This closes the bounded DW3 experiment, not the persisted Work. Its state remains
`active` under the Research Pack completion criterion.

## Observed evidence

- Researcher, critic, reviser, and synthesizer produced durable completed roles.
- The CWA synthesizer response completed externally, but its outcome was initially
  absent from Codexia's WorkStore after an admitted cognition handoff.
- The existing conversation was inspected read-only. A forensic reconciliation
  verified the request/handoff binding, response identity, exact prompt, prior
  reviser output, and raw answer checksum.
- The recovered response required one narrowly defined normalization: remove
  a standalone final `<escape>` wrapper around the control trailer, without
  changing the assessment flags.
- A human-authorized `record_outcome` admitted a single recovered
  `role.completed` at Work revision **18** without another model request.
- Research materialization produced **six EvidenceRefs** and **one Markdown
  ArtifactRef**; the Work reached revision **25** and remained `active`.
- The final synthesis assessed objective coverage and evidence sufficiency as
  true, but `material_uncertainty_resolved=false`. The required evidence for
  absence of material uncertainty was therefore not recorded, and the Research
  Pack's completion criterion was not satisfied.

## Boundaries

This pilot supports *manual, evidence-bound recovery* for the observed CWA
handoff, not generalized exactly-once execution across external providers,
automatic recovery after every crash boundary, or formal WorkCompletion.

Retain the WorkStore and readback proof files. Do not retry synthesis merely to
change the final assessment. Follow-up work on generic outcome reconciliation
belongs to a **separate** project scope, not an extension of DW3.

Implementation/protocol details: [DW3 CWA reconciliation](dw3_cwa_reconciliation.md).
