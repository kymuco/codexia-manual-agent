# DW ChatGPT bootstrap v0 — one bounded product write

First **pilot-only** vertical toward Delegated ChatGPT Work. The implementation reuses an already activated Gen2 Work and the independently managed CWA browser-owned product runtime. It is **not** an autonomous peer loop, a scheduler, an outcome reconciler or a WorkCompletion path.

## Product flow

1. `codexia work start` activates one real Work with a pinned workflow/Pack (the existing DW1 CLI; a trusted host factory is required).
2. Bootstrap reads the current Work status, checks the objective/work digest/revision, pinned workflow and no active roles, effects or live child Work.
3. The operator chooses 1–12 regular handoff / decisions / project-plan files, with a total preflight read budget of 6 MB. The default is read-only `DRY_RUN`.
4. Opt-in `--commit` writes `codexia.chat.bootstrap.claimed.v0` into the **same Gen2 WorkStore** using `append(expected_revision=...)` (SQLite `BEGIN IMMEDIATE`). This event is the durable, atomic **dispatch-attempt fence**, not a record that the model executed.
5. Only after the atomic claim, make **one** CWA `send_text_observed(..., media=[...])` to a *new* ChatGPT conversation. Exact return requires `browser-owned` transport and nonempty response message/conversation ids and text.
6. On successful CWA return, append `codexia.chat.bootstrap.captured.v0` to the exact claimed head. This is captured transport metadata only (conversation id, message id and response SHA-256). It does not admit a Gen2 CognitionOutcome, prove semantic completion, or grant actions.

**No automatic retry** after claim under any circumstance, including crash after claim but before send, unknown CWA result, failed capture, or concurrent Work advancement. Future recovery must read already-completed provider evidence. The user must not use the script a second time with the same Work.

This avoids the original file-based receipt's POSIX directory durability hole and reuses the canonical SQLite transaction. It does **not** turn SQLite durability into a guarantee against every disk/power failure, nor prove the exact bytes the browser uploaded. In particular, the file manifest is a pre-send observation; file content could change between hashing and CWA's upload.

## Windows dry run

In `W:\dev\codexia-m66-pilot` (or another verified Codexia checkout), first activate the Work through the existing DW1 CLI using its pinned workflow and host factory, then replace the example paths:

```powershell
python tools/dw_chatgpt_bootstrap_v0.py `
  --store .codexia/work.sqlite3 `
  --work-id "<existing-work-id>" `
  --file W:\dev\project\decisions.md `
  --file W:\dev\project\new_chat_handoff.md
```

After inspecting Work status and the dry-run manifest, explicitly opt in to the external message:

```powershell
python tools/dw_chatgpt_bootstrap_v0.py `
  --store .codexia/work.sqlite3 `
  --work-id "<existing-work-id>" `
  --file W:\dev\project\decisions.md `
  --file W:\dev\project\new_chat_handoff.md `
  --auth-file auth_data.json --profile DEEP --commit
```

Do **not** execute the commit command until the final branch revision has passed independent review and local tests. CWA must remain independently managed (do not reinstall its native host from Codexia's venv). The default browser-owned rich-input path requires an authenticated Chrome/Chromium ChatGPT session.

## Future verticals

- Read back the newly created conversation and reconcile exact message/provenance before admitting a cognition outcome via Issue #88.
- Implement bounded Codexia-side continuation decisions using canonical Work and chat evidence.
- Add a Work AttentionNeed path for Windows-only local test gates.
- Add handoff/context rollover only after the first real multi-turn product proof.

This slice deliberately makes **no** HDE, IRR, Runplane, generic scheduler, UI, or Gen2 Core modification. The two application-specific WorkEvents only record this pilot's transport attempt and observed result.
