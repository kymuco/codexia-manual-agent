# DW5: chat-to-local-command bridge v0

Status: one explicit, **noninteractive** command pilot. ChatGPT continues using
its existing GitHub plugin. Codexia binds a local action to one selected ChatGPT
conversation, exact assistant message, project, repository label and workspace.

The bridge reads the latest assistant message through CWA Direct HTTP.
It accepts exactly one PowerShell fenced block (with or without language tag),
containing a command shaped as an ampersand followed by a quoted absolute
script.ps1 path and optional literal -Name value pairs. It rejects pipelines,
multiple statements, variable expansion and multiple PowerShell blocks.
The script must already exist inside its project workspace. Its exact bytes
are SHA-256 pinned, and checked again before the host dispatch.

## Run from an updated Codexia checkout

PowerShell setup:

~~~powershell
Set-Location W:\dev\codexia-daily
$python = "W:\dev\codexia-m66-pilot\.venv-m66\Scripts\python.exe"
$cwaRoot = "W:\dev\chatgpt-web-adapter"
$env:PYTHONPATH = (Resolve-Path .\src).Path + ";" + (Resolve-Path "$cwaRoot\src").Path
~~~

For the fastest one-command pilot, use one invocation with **two** separate
interactive confirmations (EXECUTE then POST). The Work ID and local outcome
are printed before posting, so you can inspect what is about to be uploaded:

~~~powershell
& $python -m tools.dw_chat_command_v0 run --conversation "<conversation-UUID>" --project hde-home --repo kymuco/hde-home --workspace W:\dev\hde\hde-home --auth-file "$cwaRoot\auth_data.json"
~~~

This is intentionally noninteractive at the process level: it does **not**
yet work with the long-running Kaguya terminal. To inspect/approve each
phase on separate commands, use the three-stage route below instead.

Capture the latest assistant command. No execution or conversation POST:

~~~powershell
& $python -m tools.dw_chat_command_v0 capture --conversation "<conversation-UUID>" --project hde-home --repo kymuco/hde-home --workspace W:\dev\hde\hde-home --auth-file "$cwaRoot\auth_data.json"
~~~

The response prints a Work ID, exact command argv and command digest.
Use the real Work ID in the following commands:

~~~powershell
$workId = "<Work ID>"
& $python -m tools.dw_chat_command_v0 status $workId
& $python -m tools.dw_chat_command_v0 execute $workId
& $python -m tools.dw_chat_command_v0 status $workId
& $python -m tools.dw_chat_command_v0 post $workId --auth-file "$cwaRoot\auth_data.json"
~~~

Execute requires typing the exact EXECUTE plus digest-prefix confirmation.
The existing standalone Gen2 process host checks local authorization, runs
the command with a 60-second limit and captures bounded stdout/stderr.

POST is a **separate** action requiring typing POST plus digest-prefix.
The user must inspect the exact local result before uploading it: output may
contain local secrets. POST appends observed outcome text to the **same**
conversation with CWA HTTP, verifies exact assistant canonical readback and
records a receipt. A durable POST handoff blocks any automatic or manual
resend if the outcome is unknown. A command handoff likewise cannot be
redispatched just because a response is incomplete.

## Multi-project isolation

One shared WorkStore may hold five or more project Work items. Each immutable
Work objective pins project label, GitHub repository label, conversation ID,
assistant message ID and exact source SHA, workspace, script SHA and argv.
Output, host handoff, approval and chat POST are bound to that Work only.
There is no global current project. Existing GitHub permissions remain
controlled by ChatGPT site settings; this local bridge is not a GitHub client.

## Not yet supported

This pilot is manually triggered; it is not an unattended watcher. It supports
one bounded noninteractive .ps1 call, not arbitrary shell commands. Scripts
run with the Windows user's permissions; there is no promise of network or
secret isolation. The terminal Kaguya scenario with interactive stdin and a
long-lived process is deferred to a separately governed session feature.
It does not create local repositories, GitHub adapters, or a scheduler.

Offline acceptance:

~~~text
python -m pytest -q tests/test_dw_chat_command_v0.py
python -m ruff check tools/dw_chat_command_v0.py tests/test_dw_chat_command_v0.py
python -m ruff format --check tools/dw_chat_command_v0.py tests/test_dw_chat_command_v0.py
~~~
