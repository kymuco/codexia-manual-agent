# DW ChatGPT bootstrap v0 — actual local pilot runbook

This runbook is the **first isolated Gen2 Work + CWA ChatGPT bootstrap**, not unattended development. There is no automatic second turn, local execution, or completion authority. The product pilot is deliberately separate from Issue #88.

## Prerequisites

- Keep `W:\dev\codexia-m66-pilot` unchanged. Use the existing `W:\dev\codexia-dw-chat-bootstrap` worktree.
- The checkout includes `examples/dw_chat_bootstrap_host.py`; this is an explicit *bootstrap-only* Pack provider. It pins `codexia:delegated-chat-work@0.1.0` into a real Work; there is intentionally no WorkflowImplementation for auto-advance.
- Run source/tests with the existing Codexia Python environment. Do not reinstall or replace the independently managed CWA native host.

## Gate 1 — create exact Gen2 Work, without a product write

From PowerShell:

```powershell
Set-Location W:\dev\codexia-dw-chat-bootstrap
$env:PYTHONPATH = (Resolve-Path .\src).Path

$python = 'W:\dev\codexia-m66-pilot\.venv-m66\Scripts\python.exe'
$work = (& $python -m codexia_manual_agent work start `
  'Restore Codexia project context; propose the next narrow PR and Windows validation, with no HDE integration' `
  --store .codexia\dw-chat-v0.sqlite3 `
  --source-id codexia-dw-chat-v0-first-pilot `
  --host-factory examples.dw_chat_bootstrap_host:make_host `
  --provider-ref codexia:delegated-chat-pilot-provider@0.1.0 `
  --workflow-id codexia:delegated-chat-work `
  --workflow-version 0.1.0 `
  --json) | ConvertFrom-Json

$workId = $work.work.work_id
$work | ConvertTo-Json -Depth 10
```

Required checks: `state=active`, one workflow with a pinned Pack, `yield.kind=none`, and no unresolved roles, capabilities or live children.

If `$workId` is empty, do not proceed; inspect the CLI error. Using the same `--source-id` and the exact same selector/objective is idempotent; it does not start another Work.

## Gate 2 — dry-run with *real* local Codexia documentation

These source files are present in the repository. Replace them with actual handoff files later if preferred, but keep .md/.txt.

```powershell
& $python tools\dw_chatgpt_bootstrap_v0.py `
  --store .codexia\dw-chat-v0.sqlite3 `
  --work-id $workId `
  --file docs\gen2_product_baseline.md `
  --file docs\dw_chatgpt_bootstrap_v0.md `
  --file docs\dw1_standalone_gen2_work_surface.md
```

Expected: `DRY_RUN` plus exact `work_id`, Work revision, manifest file names, byte sizes, SHA-256 values; zero CWA product writes and zero bootstrap claim/capture events. The Work should contain only the activation events.

## Gate 3 — independent CWA preflight (no product write)

Only after PR merge and exact local code gate, from the proven CWA installation:

```powershell
& W:\dev\chatgpt-web-adapter\.venv\Scripts\cwa.exe doctor --json
```

CWA and Codexia environments must remain independent; do not install a second CWA bridge or change the browser-native host registration. CWA live rich input requires authenticated Chrome/Chromium, not merely this PowerShell process or Firefox.

## Gate 4 — first external send, **separate explicit decision**

Do not perform this step as part of Gate 1–3. After validating the gate outputs and CWA identity, run the **same** `tools/dw_chatgpt_bootstrap_v0.py` file with the **same** Work ID and same files, using the independently installed CWA Python interpreter, and append `--commit`:

```powershell
$env:PYTHONPATH = (Resolve-Path .\src).Path
& W:\dev\chatgpt-web-adapter\.venv\Scripts\python.exe `
  tools\dw_chatgpt_bootstrap_v0.py `
  --store .codexia\dw-chat-v0.sqlite3 `
  --work-id $workId `
  --file docs\gen2_product_baseline.md `
  --file docs\dw_chatgpt_bootstrap_v0.md `
  --file docs\dw1_standalone_gen2_work_surface.md `
  --auth-file W:\dev\chatgpt-web-adapter\auth_data.json `
  --commit
```

The auth file path above is **illustrative only**; resolve the actual path from your existing working CWA setup. The script intentionally does not set `model_profile`, because rich attachments plus an explicit mode are unsupported on the proven CWA path.

If Gate 4 raises, **do not rerun** for the same Work ID: the dispatch claim may already be durable while the product response was lost. Inspect with:

```powershell
& $python -m codexia_manual_agent work inspect $workId `
  --store .codexia\dw-chat-v0.sqlite3 --json
```

A `codexia.chat.bootstrap.claimed.v0` event without the captured event means **UNKNOWN**, not permission to retry. A `codexia.chat.bootstrap.captured.v0` event records ChatGPT conversation/message ids and a response digest, but still not a Gen2 CognitionOutcome or WorkCompletion.

## Explicit nonclaims

This v0 only proves bootstrap and captured metadata. No autonomous response/decision/continue cycle exists yet. There is no generically trusted generated-artifact download and no HDE/Runplane integration.
