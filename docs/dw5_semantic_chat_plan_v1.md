# DW5 — Semantic command interpreter v1

This is the next narrow milestone after PR #103. Codexia reads the assistant's ordinary reply, reasons about which local commands are actually requested, and stores the ordered steps in durable Gen2 Work history. ChatGPT does not need to change its reply format. This feature does **not** execute the interpreted commands.

## Behavior

- CWA read-only get_messages fetches the latest completed assistant message, with its canonical conversation and message ID.
- A Gen2 Work pins project/repo label, absolute intended local workspace, exact source message and SHA-256. One shared WorkStore supports five or more independent project Works.
- The pinned Command Interpreter Role uses ModelProviderCognitionPort / ChatGPT Direct HTTP and keeps the one-shot cognition handoff durable. It sees the **whole** assistant reply, not code blocks selected by regex.
- The strict JSON result has source_sha256, decision (commands / no_commands / uncertain), 1..8 ordered steps with shell, complete command (multiline preserved), safe workspace-relative cwd and purpose, and reason.
- It must distinguish requested execution from examples, quotes, historical instructions, and expected output. For uncertainty it returns an empty plan rather than guessing.
- A parsed plan is only proposed intent. No automatic shell commands, GitHub adapter, capabilities, local effects, chat posting, or WorkCompletion are created by this interpreter.
- Invalid role output stays byte-for-byte in durable RoleRun history. export-raw recovers it without retry; a successful result is exportable as JSON. File exports use exclusive create.

## Windows pilot (interpretation only)

Update the Codexia checkout, set Python PATH to include Codexia src and CWA src, and run:

    python -m tools.dw_chat_plan_v1 plan --conversation "<conversation-UUID>" --project player-intelligence --repo kymuco/player-intelligence --workspace "W:\dev\player-intelligence" --cwa-root "W:\dev\chatgpt-web-adapter" --auth-file "W:\dev\chatgpt-web-adapter\auth_data.json" --send

Type INTERPRET to authorize one GPT-6 High model call. The program prints Work ID and PLAN_READY_UNEXECUTED, with the exact JSON plan. It will not run the commands.

Read-only resume/inspect/export:

    python -m tools.dw_chat_plan_v1 status "<Work ID>"
    python -m tools.dw_chat_plan_v1 export "<Work ID>" --out "artifacts\command-plan.json"
    python -m tools.dw_chat_plan_v1 export-raw "<Work ID>" --out "artifacts\raw-role.txt"

After uncertain transport outcomes, do not resend an admitted handoff. The source chat remains unmodified during this milestone.

## Acceptance

    python -m pytest -q tests/test_dw5_semantic_chat_plan_v1.py
    python -m ruff check src/codexia_manual_agent/workflow_runtime/chat_command_plan_v1.py tools/dw_chat_plan_v1.py tests/test_dw5_semantic_chat_plan_v1.py
    python -m ruff format --check src/codexia_manual_agent/workflow_runtime/chat_command_plan_v1.py tools/dw_chat_plan_v1.py tests/test_dw5_semantic_chat_plan_v1.py

Offline tests assert Gen2 durability, full message context with PowerShell here-string piped to Python and irrelevant demonstration block, source binding, strict ordered steps, five independent Works, invalid role recovery, and zero effect capabilities. They do not prove live semantic extraction accuracy. Host-execution of a separately approved plan and same-chat outcome continuation belong to the next bounded milestone, reusing PR #103's host path.
