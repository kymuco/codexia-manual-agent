# DW ChatGPT bootstrap v0 — one bounded product write

First **pilot-only** vertical toward delegated ChatGPT Work. This is **not** an implemented autonomous peer loop, a Gen2 workflow, a scheduling service, or a completion pathway. It reuses one **already activated** Gen2 Work and the independently managed CWA product runtime.

## Behavior

- Check exact `StandaloneWorkSurface.status()` of an existing Work; reject terminal, attention-yielded, unpinned, or unresolved Work.
- Explicitly select 1–12 local regular non-symlink context files; read them for an SHA-256/size manifest; fail over a total 6 MB byte budget.
- Dry-run by default. `--commit` makes at most one `send_text_observed()` call on the supported CWA browser-owned transport, with attachment paths in `media` and a scoped project-continuation prompt.
- Persist a unique host-owned **transport attempt** receipt before calling CWA. The `IN_FLIGHT_UNKNOWN` receipt is a no-retry fence across process restart. A successful transport return changes it to `CAPTURED_UNADMITTED` and records provider conversation/message ids plus answer digest.
- Do not promote a CWA text answer, generated PR, or the receipt to Gen2 evidence, authority or completion. No automatic retry on exceptions or even when a later read suggests no visible response. This slice deliberately does not wire Issue #88 reconciliation.

## Usage (Windows PowerShell)

First activate one real Work using the existing `codexia work start` product surface and a trusted local `--host-factory`/Pack. Then:

```powershell
python tools/dw_chatgpt_bootstrap_v0.py \`
  --store .codexia/work.sqlite3 \`
  --work-id "<existing-work-id>" \`
  --file W:\dev\project\decisions.md \`
  --file W:\dev\project\new_chat_handoff.md
```

Review the dry-run manifest. Only when ready to create one ChatGPT conversation:

```powershell
python tools/dw_chatgpt_bootstrap_v0.py \`
  --store .codexia/work.sqlite3 \`
  --work-id "<existing-work-id>" \`
  --file W:\dev\project\decisions.md \`
  --file W:\dev\project\new_chat_handoff.md \`
  --auth-file auth_data.json --profile DEEP --commit
```

CWA must already be installed and authenticated independently in the Python environment. Do not reinstall or overwrite the globally installed CWA browser-native host. One Work id is intentionally limited to one initial bootstrap attempt in this pilot.

## Known gaps / next useful slice

1. Canonical CWA readback/correlation and Issue #88 admission before treating the result as durable Work evidence.
2. One bounded continuation-decision loop based on exact chat/Work frontier, not free-form automatic `continue`.
3. Local-gate AttentionNeed and response handling; then only later a context rollover.
4. Concurrent file modification and Windows power-loss durability are **not** proven by the hash manifest or this host-only receipt. The manifest is pre-submit observation, not a cryptographic attestation of the bytes uploaded by the browser.

No HDE, IRR, Runplane, generic scheduler, or Gen2 Core change is involved.
