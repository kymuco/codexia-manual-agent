# DW3 completed-synthesis recovery (pilot-only)

This is an explicit, evidence-gated recovery path for a **previously completed**
CWA synthesis turn whose `CognitionHandoff` is durable but whose
`CognitionOutcome` is absent. It never sends a prompt, retries the model, or
reopens the authority boundary. Gen2 Core is unchanged.

## Preconditions

- An existing DW3 SQLite WorkStore (do not recreate or delete it).
- An active Work with exactly four Research Pack roles; synthesizer is still
  `requested` and the **last** Work event is its admitted handoff.
- A complete UTF-8 synthesis answer saved with the read-only CWA client.
- The existing ChatGPT conversation and the exact IDs of its synthesis user
  message and completed assistant message.
- A separate CWA Python environment with read access; if the editable package
  is absent, provide the existing CWA checkout `src` path explicitly.

## Dry-run proof

Run from the Codexia repo root (fill real values; do **not** run a new
`work advance` before examining the handoff):

```powershell
python -m examples.dw3_reconcile_synthesis `
  --work-id "$workId" `
  --store "$store" `
  --expected-revision 17 `
  --expected-head-digest "<durable head digest>" `
  --handoff-id "<admitted synthesizer handoff id>" `
  --conversation "https://chatgpt.com/c/<conversation id>" `
  --request-message-id "<synthesizer user message id>" `
  --response-message-id "<completed assistant message id>" `
  --recovered-file ".codexia/dw3-synthesis-recovered.txt" `
  --expected-sha256 "<verified full-response SHA-256>" `
  --cwa-python "W:\dev\chatgpt-web-adapter\.venv\Scripts\python.exe" `
  --cwa-source-root "W:\dev\chatgpt-web-adapter\src" `
  --auth-file "W:\dev\codexia-m66-pilot\auth_data.json"
```

The probe invokes `examples/dw3_cwa_recovery_read.py` via the separate
CWA interpreter, using **only** `ChatGPTWebClient.get_messages`. When
`--cwa-source-root` is supplied, it validates that the checkout contains
`chatgpt_web_adapter/__init__.py` and prepends the `src` folder to the child
process's `PYTHONPATH`; it does not install, patch, or start CWA. It confirms:

1. Unique synthesis user/assistant IDs, correct order, no intervening user,
   `finish_reason=stop`, and one unique prior completed reviser answer;
2. exact CWA synthesis prompt reconstructed from durable `CognitionRequest`
   instructions plus the hot-conversation delta protocol;
3. prior CWA reviser answer equals durable Research reviser output;
4. exact Work revision/head, handoff ID, routing port, and rematerialized
   request digest;
5. exact UTF-8 file bytes equal the CWA response and expected SHA-256;
6. exactly one final `CODEXIA_CONTROL_V1` trailer with typed, exact fields,
   successfully parsed by Research Pack 1.1. If CWA's literal text encloses
   **only the final trailer** in standalone `<escape>` and `</escape>` lines,
   the pilot gate permits exactly that reversible, documented canonicalization.
   It rejects other trailing text, malformed wrappers, duplicated markers,
   altered booleans, and non-parseable JSON.

A successful dry run reports `verified=true`, `mode=dry-run`,
`workstore_modified=false`. It reports both `response_sha256` (the *raw,
unchanged CWA reply*) and `admitted_output_sha256` (the canonical plaintext
to be written), along with `normalization`. For a wrapped trailer these
digests intentionally differ. It does **not** attest a Work completion or
modify durable events.

## Explicit admission (separate human decision)

Only after reviewing a successful dry run, use **the same arguments** with:

```powershell
  --commit `
  --approve-sha256 "<exact response SHA-256>"
```

The tool **re-runs every verification** immediately before invoking the
existing `CognitionTransportBridge.record_outcome`. No CWA send occurs.
Any Work revision change, message drift, missing evidence, or bad approval
fails closed. The resulting `role.completed` is the recovered synthesis
outcome, **not** an automatic `WorkCompletion` or `ArtifactRef`. It stores
the canonical unwrapped Research Pack output, while the raw CWA response
identity remains independently SHA-bound in the forensic proof. Human
approval applies to the original raw SHA, not a newly invented model answer.

After admission, use the separate existing Research materialization and bounded
Work progression surfaces to produce evidence/artifacts. **Do not claim Work
completion merely because synthesis was recovered:** if the original control
states `material_uncertainty_resolved=false`, the materializer cannot admit
the `NO_MATERIAL_UNCERTAINTY_EVIDENCE_KIND` required for acceptance. Preserve
that finding instead of flipping the boolean or fabricating evidence.
The canonical event stores the validated response text; the CWA IDs,
head, and file SHA remain in the reconciliation report and should be archived
with pilot evidence. This is a pilot-level provenance approach, not a general
provider outcome recovery API.

## Boundary

This tool is limited to DW3 Research Work, `model-provider:cwa-subprocess`,
the exact hot-delta wire format, and the unique prior reviser constraint.
It must not be generalized into automatic model retry or inferred completion.
