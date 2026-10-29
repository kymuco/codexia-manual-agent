# DW5 — explicit Sentinel pre-send reconciliation (v1)

This is a **narrow, human-attested manual recovery** for an already admitted
Controller send that failed at **`browserless Sentinel prepare rejected`**.
It does not send any ChatGPT messages, run commands, or modify a Gen2 Work.
It is deliberately not valid for HTTP timeouts, ambiguous post-submit errors,
unknown send results, or failed canonical readback after a successful send.

## Why

`continue_controller()` commits a `dw5_controller_turn_v1` row with phase
`send_admitted` *before* `client.send()` starts. The CWA direct HTTP client's
Sentinel prepare exception is thrown before a chat submission, but the durable
fence still correctly prevents a blind retry. An operator can explicitly
reconcile that exact pre-send failure without disabling no-replay for other
errors.

## Scope, proofs, and invariant

- A prior Controller turn with phase `ready` must exist and directly precede
  the `send_admitted` row. No initial-bootstrap recovery in this version.
- The newest Worker ID and text SHA-256 must match the fenced source exactly.
- The latest Controller assistant ID and text SHA-256 must match operator-
  supplied expected values. The Controller cannot have advanced.
- Human attests that the *observed CWA error* was exactly
  `browserless Sentinel prepare rejected`. CWA does not persist that error in
  DW5 SQLite; this is a human-verified evidence boundary, not an automatically
  certified transport proof.
- The operator must enter `RECONCILE <digest-prefix>` after seeing the exact
  project, worker ID, controller ID, hashes, and error kind. Canonical reads
  are performed again **after** that confirmation.
- One SQLite transaction copies the old fence to an immutable audit table
  `dw5_controller_presend_recovery_v1`, then removes it from the **active turn
  table only**. If either write or the restored verified frontier fails,
  the transaction rolls back. Original fence fields remain in the audit table.
- One recovery maximum per project+Controller predecessor. It cannot be used
  to clear a second ambiguous send for the same predecessor.
- A separate operator request is required to execute `DW6 advance --interpret`
  after recovery. Reconciliation is **not** authorization to send that request.

## Windows operator example

From the repository root, using the existing Codexia/CWA `PYTHONPATH`:

```powershell
$python = 'W:\dev\codexia-m66-pilot\.venv-m66\Scripts\python.exe'
$authFile = 'W:\dev\chatgpt-web-adapter\auth_data.json'
$common = @(
  '--project', 'player-intelligence',
  '--auth-file', $authFile,
  '--worker-message-id', 'a57787c5-91b3-4fa8-814d-c81fda54a8e4',
  '--controller-message-id', '938a6213-2a6a-4657-b886-3fee5aba2bea',
  '--controller-sha256', 'd0f6e497c97423bd9ebb441df7a6ca34d5907f4b1809b8e6c5b3cfd8d7684f54'
)
& $python -m tools.dw_codexia_presend_recovery_v1 inspect @common
# Review the transcript, original failure type, and hash-based preflight.
& $python -m tools.dw_codexia_presend_recovery_v1 reconcile-presend `
  @common --error-kind sentinel_prepare_rejected
```

After `PRESEND_FENCE_ARCHIVED_NO_SEND`, inspect `DW5 Controller status` and
`DW6 status`. Only if the latter returns `interpret` with the original
Worker/Controller IDs may the human issue the **separate** explicit
`DW6 advance --interpret`. Do not run that automatically inside a recovery
script. Never edit SQLite by hand to bypass another kind of blocked send.
