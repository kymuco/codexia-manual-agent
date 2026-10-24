# DW5 — Existing Codexia controller chat decision, Gen2-bound review

Status: first integration checkpoint after PR #104. **No extra Command Interpreter
chat, no ModelProviderCognitionPort call, no automatic process execution.**

## Why this change exists

The operator has demonstrated a real `PLAN_READY_UNEXECUTED` interpretation of
a multiline PowerShell here-string. But spawning a separate interpreter Role
and external ChatGPT turn after every worker response is the wrong product
conversation topology. Codexia already has its own **saved controller chat**
with context; that regular decision turn should recognize commands.

Worker chat: continues to use ordinary prose, examples and GitHub connector.
Controller chat: evaluates that response once, in its existing context, then
chooses to answer, ask a question, delegate more work or produce a command plan.

## Contract

When the existing controller chooses to propose local commands, its final
answer contains the strict `CommandPlan` JSON already defined in
`chat_command_plan_v1.py` (optionally preceded by `RUN_COMMANDS:`).
The plan must copy `source_sha256` of the worker assistant's exact message.
The worker is **not** required to output JSON or tag execution blocks.

The read-only capture operator fetches the latest assistant reply from the
two **existing** conversations, verifies both message IDs and binds the
controller decision to the exact worker source, selected project/repo and
absolute workspace. It creates an exact Gen2 Work with one durable review
AttentionNeed for `commands`, and none for `no_commands`/`uncertain`.
A reviewed plan remains **unexecuted**. Recording human review is not a
host capability authorization.

There is no third ChatGPT conversation, provider send, WorkCompletion, or
duplicate GitHub adapter. The operator does not silently splice these
decisions into the legacy Simple Work runtime: it uses the current Gen2
StandaloneWorkSurface and hash-chained store.

## Windows CLI (read-only capture and review)

Use **real** saved Codexia and worker chat UUIDs. These must be different
conversations. Codexia's last reply must already contain the plan decision
with the exact worker `source_sha256`. This command does not create that
controller turn; the existing Codexia work loop does.

```powershell
Set-Location W:\dev\codexia-daily
git fetch origin
git switch --detach origin/main

$python = "W:\dev\codexia-m66-pilot\.venv-m66\Scripts\python.exe"
$cwaRoot = "W:\dev\chatgpt-web-adapter"
$env:PYTHONPATH = (Resolve-Path .\src).Path + ";" + (Resolve-Path "$cwaRoot\src").Path

& $python -m tools.dw_codexia_controller_command_v1 capture `
    --project player-intelligence `
    --repo kymuco/player-intelligence `
    --workspace W:\dev\chatgpt-web-adapter `
    --worker-conversation "<worker-UUID>" `
    --codexia-conversation "<saved-Codexia-UUID>" `
    --auth-file "$cwaRoot\auth_data.json"
```

Copy `work_id` from capture output:

```powershell
$workId = "<Work ID>"
& $python -m tools.dw_codexia_controller_command_v1 review $workId
& $python -m tools.dw_codexia_controller_command_v1 status $workId
```

For exact recorded human review only (without executing), use
`answer <Work ID> approve` or `answer <Work ID> reject`. The CLI asks
for the exact digest prefix before recording `AttentionResponse`.

## Critical discovered boundary: shells deliberately forbidden

The existing `execution/process.py` M2.1 host **explicitly rejects**
`powershell.exe`, `cmd.exe` and other shell interpreters in
`_resolve_executable`. Earlier PR #103's mocked tests did **not** establish
that it could execute an ordinary PowerShell invocation on a real Windows
host. Do not work around this by invoking a nested shell through Python.

A later host-policy milestone must explicitly define approved shell-command
execution, allowed effects and actual boundaries, without weakening the
existing M2.1 default policy or claiming network/secret sandboxing.
This PR intentionally stops at exact controller decision + human review;
it does not yet provide the complete automatic command-return loop.

## Checks

```text
python -m pytest -q tests/test_dw5_controller_command_v1.py
python -m ruff check src/codexia_manual_agent/workflow_runtime/controller_command_v1.py tools/dw_codexia_controller_command_v1.py tests/test_dw5_controller_command_v1.py
python -m ruff format --check src/codexia_manual_agent/workflow_runtime/controller_command_v1.py tools/dw_codexia_controller_command_v1.py tests/test_dw5_controller_command_v1.py
```

Regression uses five projects, two distinct conversation identities per
project, exact controller/source provenance, SQLite durable review and
restarts. It uses synthetic CWA replies and NO local process. The first
live operator proof is capture from the user's already-running saved
Codexia chat. Once supported, proceed to controlled execution and result
return to the right conversation.
