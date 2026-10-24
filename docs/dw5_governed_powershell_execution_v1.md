# DW5 — Explicit PowerShell profile and governed step-by-step execution

Status: PR v1 adds a **separate opt-in host profile** and the Gen2 sequential
workflow for local shell plans proposed by Codexia in its **existing saved
controller conversation**. It does not add another model chat or worker and
does not affect ChatGPT's existing GitHub plugin.

## Execution contract and safety boundaries

1. Read latest completed assistant messages from the selected worker and
   Codexia controller conversations through CWA read-only access. Validate
   both canonical message IDs, worker source text SHA and exact JSON
   `CommandPlan` with at most eight ordered steps.
2. Create one immutable Gen2 Work scoped to project, repo, workspace, worker
   conversation, controller conversation, both assistant messages, plan digest.
   A SQLite append-only, hash-chained Work chronology records every transition; `inspect` prints exact per-event timestamps, event kinds, digests and payloads without spawning commands.
3. Show the complete exact commands, their hashes, cwd and workspace. Require
   an explicit human `APPROVE <plan-digest>` for the bounded plan. This
   AttentionResponse is **not** process authority.
4. Before the first dispatch, independently show all full commands again
   and require local `EXECUTE <plan-digest>`. The distinct
   `StandalonePowerShellCapabilityPort` grants the controlled PowerShell
   profile through the *existing* ExecuteProcessService: the normal M2.1
   process host remains deny-by-default for shell interpreters.
5. Every step has its own exact Gen2 `CapabilityNeed`, durable
   `CapabilityHandoff`, local one-shot approval receipt and `CapabilityOutcome`.
   A subsequent step is proposed only after the preceding outcome succeeds.
   Failure/unknown halts the plan. An admitted effect never auto-retries on
   a restart.
6. `status` recovers outcome evidence, including stdout/stderr preview,
   stream digests and byte counts, exit code, duration and termination reason.
   `post` separately requires `POST <result-sha>`, since local logs may
   contain secrets. It writes a **durable no-replay** outgoing handoff before
   sending to the exact original worker conversation and records receipt
   only after canonical assistant readback.

**PowerShell host policy:** On Windows, only `powershell.exe -NoProfile
-NonInteractive -Command "<exact literal text>"` is allowed in this new
profile. The command text is not reparsed or altered: PowerShell here-strings
piped to Python remain intact. Executing an approved script may still read
other files, use network and alter the host just as the signed-in Windows
user can. **This is NOT a sandbox**. Human review of the entire command
is essential. Standard M2.1 process invocations *still reject shells*.
No interactive terminal, stdin session, arbitrary shell, or unattended grant.

The shell environment is minimal, with the filtered system PATH inherited for
the explicit PowerShell profile. This does **not** guarantee an arbitrary
Python package, authentication file or relative path will resolve on the
user's machine. The first local pilot must prove that using actual logs.

## Windows operator commands

After this PR is merged, use the same Codexia checkout and CWA source already
used in the DW5 pilot. Set the same PYTHONPATH used in PR #104:

```powershell
Set-Location W:\dev\codexia-daily
git fetch origin
git switch --detach origin/main
$python = "W:\dev\codexia-m66-pilot\.venv-m66\Scripts\python.exe"
$cwaRoot = "W:\dev\chatgpt-web-adapter"
$env:PYTHONPATH = (Resolve-Path .\src).Path + ";" + (Resolve-Path "$cwaRoot\src").Path
```

Both source chats must have the expected last assistant messages; the saved
controller chat must already have produced an exact `RUN_COMMANDS` JSON
decision for that worker message. This operator does not create a new
Codexia cognition turn or convert ordinary worker prose by regex.

```powershell
& $python -m tools.dw_codexia_shell_plan_v1 capture `
    --project player-intelligence `
    --repo kymuco/player-intelligence `
    --workspace W:\dev\chatgpt-web-adapter `
    --worker-conversation "<worker-chat-UUID>" `
    --codexia-conversation "<saved-controller-chat-UUID>" `
    --auth-file "$cwaRoot\auth_data.json"
```

Copy `work_id` from capture output. No local effect has occurred.

```powershell
$workId = "<returned Work ID>"
& $python -m tools.dw_codexia_shell_plan_v1 review $workId
& $python -m tools.dw_codexia_shell_plan_v1 status $workId
& $python -m tools.dw_codexia_shell_plan_v1 approve $workId
& $python -m tools.dw_codexia_shell_plan_v1 run $workId --auth-file "$cwaRoot\auth_data.json"
& $python -m tools.dw_codexia_shell_plan_v1 status $workId
& $python -m tools.dw_codexia_shell_plan_v1 inspect $workId
```

The terminal prints exact plan and command digests **before** any PowerShell
launch, then outputs a complete Gen2 host outcome for every observed step.
After inspecting/redacting the stored output, optionally return bounded
observations to the *same worker chat*:

```powershell
& $python -m tools.dw_codexia_shell_plan_v1 post $workId --auth-file "$cwaRoot\auth_data.json"
```

If `run` or `post` reports an ambiguous attempt, stop and inspect.
Never make a second Work for the same controller message as a retry workaround.

## E2E evidence

Offline tests prove ordered CapabilityNeed/Outcome and no-replay,
failure-stop, five project/conversation Work isolation, separate approvals,
exact same-worker return, no effects before review and default shell denial.
Windows CI additionally executes one harmless real PowerShell command
through the **opt-in** ExecuteProcessService profile, collecting its real
exit code/stdout/receipt. User's `@' ... '@ | python -` and CWA auth/files
still require a separately approved local Windows E2E; CI does not send real
ChatGPT messages.

The product does not yet autonomously invoke the controller's decision
turn in response to all worker messages, and there is no resident service
or generic scheduler. This slice makes execution + logging reachable once
the existing controller conversation has produced its decision.
