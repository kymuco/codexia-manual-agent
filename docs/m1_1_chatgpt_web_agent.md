# M1.1 — ChatGPT Web Provider and Read-Only Agent Loop

## Goal

Connect Codexia to an existing ChatGPT web session without granting the remote
model direct local authority.

## Transport boundary

M1.1 uses only the stable core of `chatgpt-web-adapter`:

- `ChatGPTWebClient.send()`;
- `send_to_conversation()`;
- `ChatResponse.text`;
- conversation metadata returned by the response.

Codexia does not call the SDK's experimental approval helpers. ChatGPT web tool
cards are not the Codexia permission boundary.

## Local protocol

The model may return exactly one JSON object per turn:

```json
{"type":"tool_request","request_id":"read-1","tool":"read_file","arguments":{"path":"README.md"}}
```

or:

```json
{"type":"final","text":"Evidence-bounded result."}
```

A single plain or `json` fenced object is accepted. Surrounding prose, unknown
keys, unknown tools, malformed request ids, and duplicate request ids stop the
run as protocol errors.

## Read-only tools

- `read_file` — maximum 65,536 bytes per model request;
- `list_files` — maximum 200 entries;
- `search_text` — maximum 100 matches;
- `git_status` — no model-controlled arguments.

All requests still pass through the M1.0 workspace adapter and canonical path
checks.

The model-facing policy also excludes common credential and secret surfaces,
including `auth_data.json`, `.env`, private-key suffixes, and directories such
as `.ssh` and `.aws`. Example/template variants such as `.env.example` remain
available. Recursive search skips symlink files and revalidates every candidate
after canonical resolution.

Direct user-invoked `inspect read` remains an explicit local operation; the
model-facing policy is stricter because a remote response must never be able to
request authentication material.

## Budgets

Defaults:

- 8 model turns;
- 8 tool calls;
- 32,768 characters per model response;
- 131,072 cumulative model characters;
- 131,072 characters per serialized tool observation.

Exhaustion stops the loop and is persisted as `budget_exhausted`. Codexia does
not silently increase a budget or continue in the background.

## Sessions

Session schema v2 adds:

- provider id;
- ChatGPT conversation and latest message ids;
- selected model and reasoning effort;
- cumulative turn and tool-call counts;
- last run status.

Schema-v1 M1.0 manifests remain readable.

This is minimal continuation state, not the full event/receipt journal planned
for M3.

## Install

```powershell
python -m pip install -e ".[web]"
```

`chatgpt-web-adapter` requires a valid existing web-session auth file and system
`curl`.

## Start

```powershell
codexia run "Inspect this repository and summarize its architecture" `
  --workspace W:\dev\some-repository `
  --auth-file W:\secrets\auth_data.json `
  --model thinking `
  --reasoning-effort high
```

Without a task, `codexia run` preserves the M1.0 offline behavior and creates a
manifest without contacting a model.

## Continue

```powershell
codexia resume <session-id> "Now inspect the test strategy" `
  --state-dir W:\dev\some-repository\.codexia\sessions `
  --auth-file W:\secrets\auth_data.json
```

The provider reattaches using `send_to_conversation()` and does not resend the
large runtime system prompt.

## Still forbidden

- arbitrary shell;
- file writes or patches;
- Git mutation;
- model-requested network access;
- browserless ChatGPT connector approvals;
- unattended automation;
- background continuation.
