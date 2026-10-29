# DW5 project bootstrap — first saved Worker/Controller pair

This is a **first-use binding**, not a new agent, scheduler, or general chat
manager. The existing Simple Work SQLite database stores persistent Codexia
chat aliases; DW5 adds one project-to-worker/controller binding to that same
SQLite file. The existing WorkStore remains the authority for each individual
approved command plan.

## Contract

- `bootstrap` reads the latest completed assistant reply in an **existing saved
  Worker** chat via CWA. It does not write to the Worker.
- If the project is not registered, it durably records the Worker, repository,
  workspace, message ID, and SHA-256. A one-way `send_admitted` state is saved
  **before** creating any Controller chat.
- The first CWA `send` (no `conversation`, `temporary=False`) creates one
  persistent Controller chat using GPT-6 / high reasoning and supplies the
  standard bounded DW5 interpretation instructions. No terminal effect is
  authorized by the Controller reply.
- Upon returning a conversation identity, DW5 atomically registers that
  Controller under its project alias in the existing `simple_codexia_chats_v1`
  table and records the project pairing. A canonical Controller readback and
  strict JSON CommandPlan validation must pass before the pairing becomes
  `ready`. The next `capture` still checks the exact current Worker/Controller
  message and SHA through its existing authority boundary.
- Existing `ready` pairing is reused, **never duplicated**. An ambiguous CWA
  send remains `send_admitted`; a created chat with invalid or unconfirmed
  readback remains `controller_known`. Neither state automatically resends.
  Inspect and reconcile manually instead.
- This first slice does **not yet** automatically refresh the Controller for a
  *newer Worker turn*. If the Worker has advanced since bootstrap, it reports
  `EXISTING_PROJECT_WORKER_ADVANCED_NO_SEND`; a separately admitted continuation
  protocol is required before a new `capture`. Do not treat a previous plan as
  authority for a newer message.

## First run (from Codexia DW5 branch)

Set `PYTHONPATH` to the current `codexia-daily/src` plus CWA `src`, as in DW5
preflight. Verify the project workspace exists before running.

```powershell
$python = 'W:\dev\codexia-m66-pilot\.venv-m66\Scripts\python.exe'
$cwaRoot = 'W:\dev\chatgpt-web-adapter'
$workspace = 'W:\dev\player-intelligence'
$authFile = Join-Path $cwaRoot 'auth_data.json'
$env:PYTHONPATH = @((Resolve-Path .\src).Path, (Resolve-Path "$cwaRoot\src").Path) -join [IO.Path]::PathSeparator

# Read-only: inspect whether the pairing already exists.
& $python -m tools.dw_codexia_project_bootstrap_v1 status --project player-intelligence

# This operation admits exactly ONE saved-Controller CWA send if unregistered.
# Never rerun automatically after an error.
& $python -m tools.dw_codexia_project_bootstrap_v1 bootstrap `
    --project player-intelligence `
    --repo kymuco/player-intelligence `
    --workspace $workspace `
    --worker-conversation 6adb4dd7-9ea0-83ed-bb9f-285ce4706b06 `
    --auth-file $authFile

# Only after CONTROLLER_CREATED_AND_BOUND_NO_LOCAL_EFFECT / phase=ready:
& $python -m tools.dw_codexia_shell_plan_v1 capture `
    --project player-intelligence `
    --repo kymuco/player-intelligence `
    --workspace $workspace `
    --auth-file $authFile
```

`capture` resolves missing Worker/Controller IDs from the registered project
pairing. Explicit IDs retain backward-compatible DW5 behavior. No `approve`,
`run`, or `post` is performed by bootstrap/capture. Run them separately after
human review of the exact approved plan and stdout/stderr disclosure risk.

Both CLIs default to `.codexia/simple_work_v0.sqlite3` for the project pairing.
The existing per-work DW5 event store remains `.codexia/controller-shell-work.sqlite3`.

## Non-retry rules

If bootstrap returns `send_admitted`, the remote chat creation may have
succeeded without a local conversation ID. Stop and check ChatGPT's saved
conversations; do not rebootstrap or send the same prompt a second time.
If it returns `controller_known`, the ID was saved but validation/readback
was incomplete. Inspect that saved conversation; no automatic re-send.
