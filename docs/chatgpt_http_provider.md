# Codexia experimental ChatGPT HTTP provider

This is a separate explicit ModelProvider built on the CWA PR #212
BrowserlessHTTPClient. It is NOT the older browserless-request transport or
the browser-owned ChatGPTProductRuntime. It does not start Chrome or use
Native Messaging.

Production implementation:
- src/codexia_manual_agent/providers/chatgpt_http.py
- Existing CognitionPort adapter: src/codexia_manual_agent/providers/model_provider_cognition.py
- Independent browser-owned provider remains unchanged.

Required source and runtime:
- CWA HTTP worktree: W:\dev\chatgpt-web-adapter-http
- CWA source branch: codex/browserless-http
- CWA PR: https://github.com/kymuco/chatgpt-web-adapter/pull/212
- Node.js 24+, curl, authenticated ChatGPT auth_data.json
- Import CWA from the matching experimental source checkout, not another
  browser-owned editable installation. Qwen is not involved.

Example Python use, **only after the host has durably admitted a new handoff**:

    from codexia_manual_agent.providers.chatgpt_http import ChatGPTHttpProvider
    from codexia_manual_agent.providers.model_provider_cognition import ModelProviderCognitionPort

    provider = ChatGPTHttpProvider(
        auth_file=r"W:\dev\chatgpt-web-adapter\auth_data.json",
        model="gpt-6-instant",
    )
    port = ModelProviderCognitionPort(provider)

The caller's system role instructions are included as visible request text,
not claimed to be privileged server-side system messages.

The provider name is chatgpt-http, not chatgpt-web. A previously submitted
browser-owned CognitionHandoff cannot be migrated by altering its provider name.
Conversation continuation uses the exact server conversation ID and fails if
the new response unexpectedly changes it.

Provider errors after calling the HTTP send are considered potentially
ambiguous. There are no automatic retries or browser fallbacks. The provider
inherits read-only status and history APIs for future reconciliation, but
the proof/index/admission obligations of Codexia Issue #88 remain separate
and are not implemented by this initial transport adapter.

## Human-visible HTTP E2E observability (initial implementation)

The new one-shot HTTP bootstrap entry point can display ordered,
human-readable, sanitized progress events, with optional streaming of model
text into stdout:

  [01] WORK       new Work ID, canonical revision
  [02] INPUT      file count, total bytes and hashes, no private file content
  [03] CLAIM      one durable bootstrap claim admitted (not CognitionHandoff)
  [04] HTTP       one direct send attempt started, no Chrome
  [05] STREAM     first token received, live output if explicitly requested
  [06] READBACK   canonical conversation/message IDs and evidence checked
  [07] ADMIT      exact Work frontier checked, or yield for human review
  [08] RESULT     CAPTURED / YIELDED / RECONCILIATION_REQUIRED

Progress must never mislabel local request dispatch as remote acceptance.
Do not print auth tokens, cookies, unredacted HTTP packets or all handoff
content by default. A possible write followed by failure is an uncertain
state, not permission to retry; older claimed Work IDs must remain untouched.

No Codexia Work was created and no external ChatGPT request was sent
to prepare this code.

### Pre-claim HTTP session gate

For an explicitly confirmed `--commit`, Codexia now checks the selected CWA
HTTP client's Node.js (24+) and curl executables using bounded local
`--version` commands. It then requires CWA's
`refresh_auth(persist=False)`: a session **GET** at
`/api/auth/session` that updates the client's bearer in memory but does not
persist credentials. Only an explicit status-200, token-present,
not-persisted receipt permits the existing SQLite CAS `claimed.v0` event.

Failure is reported as `HTTP_PREFLIGHT_FAILED` and must leave Work unclaimed:
no conversation POST, no file upload and no browser-owned fallback.
The dry-run remains offline and never performs this network gate.

This is session evidence, **not** proof that the eventual conversation POST will
succeed or that the short-lived server state cannot change between checks.
Once claimed, any ambiguous POST/stream/readback outcome still forbids a retry.
An operator can separately synchronize an expired `auth_data.json` through
the existing CWA auth tools **before** trying a new Work. Codexia never
implicitly launches a browser or performs browser auth sync.

### Running the new HTTP bootstrap

Use `tools/dw_chatgpt_http_bootstrap_v0.py` **on a new, previously unclaimed
activated Gen2 Work**. It is a separate entry point from the old browser
bootstrap. It reuses the same one-shot claim event kind; an old browser
claim can never be resent through this new HTTP script.

From a separate Codexia worktree (illustrative path), use:

```powershell
Set-Location W:\dev\codexia-http-observable
$env:PYTHONPATH = "W:\dev\codexia-http-observable\src;W:\dev\chatgpt-web-adapter-http\src"
& "W:\dev\chatgpt-web-adapter\.venv\Scripts\python.exe" `
  -m tools.dw_chatgpt_http_bootstrap_v0 `
  --store .codexia\dw-chat-v0.sqlite3 `
  --work-id "<new-unclaimed-work-id>" `
  --file docs\gen2_product_baseline.md
```

This is a zero-write dry-run. The script prints Work identity and revision,
file count, total bytes, aggregate digest and the exact preview manifest.
Do not run on any older Work already carrying a bootstrap claim.

Only for a separately authorized real E2E, additionally supply
`--commit --stream --cwa-root W:\dev\chatgpt-web-adapter-http` and
`--auth-file W:\dev\chatgpt-web-adapter\auth_data.json`.
It checks the imported CWA module is from this exact source tree, creates
the client **before** the claim, and requires typing `SEND` at the terminal.
Anything else cancels before the Work is claimed.

Before claiming, it freezes byte-accurate Markdown/TXT attachment snapshots
(1..12 files, total 6 MB, unique basenames) so the HTTP upload does not
re-read a changing filesystem file after the claim.

The terminal stages are WORK, INPUT, CLAIM, HTTP_CALL, STREAM_OPEN,
FIRST_TOKEN, HTTP_RETURN, HTTP_IDENTITY, READBACK, ADMISSION and RESULT.
`HTTP_CALL` is a local attempt, not a remote receipt.
`FIRST_TOKEN` is provisional, not completion. READBACK checks the exact
assistant message ID and text against canonical read-only history.
ADMISSION always says `not_admitted` for this bootstrap; it does not
pretend to complete a Gen2 RoleRun or Work. A successful capture is
`CAPTURED_UNADMITTED`.

For an HTTP error, Ctrl+C after the claim, mismatched canonical history,
or failure to append the capture, the Work remains claimed and the progress
reports `OUTCOME_UNKNOWN`, `retry=forbidden`. Reconcile read-only;
never resend the same Work. Progress is printed to stderr, streamed
model text to stdout only with explicit `--stream`.

Developers can reuse `HttpProgressConsole(stream_text=True)` by injecting
`progress=observer.emit, on_token=observer.token` into
`ChatGPTHttpProvider`. No new Gen2 Core runtime or permission authority
has been added, and Qwen/browser-owned remain unchanged.

## Model selection and thinking effort

The browserless SDK accepts an explicit model slug and a thinking effort.
Codexia forwards them without silent browser-owned fallback:

```powershell
# First run with a NEW, never-claimed Work (no external write):
python -m tools.dw_chatgpt_http_bootstrap_v0 `
  --work-id "<new-work-id>" `
  --file docs\gen2_product_baseline.md `
  --model gpt-6 `
  --reasoning-effort high

# On a separately approved real E2E, additionally supply:
# --commit --stream --cwa-root W:\dev\chatgpt-web-adapter-http
# --auth-file W:\dev\chatgpt-web-adapter\auth_data.json
```

The CLI defaults remain `gpt-6-instant` and unspecified reasoning effort
for compatibility. It accepts `instant`, `medium`, `high`,
`standard`, or `extended`. The CWA legacy request serializer maps
`high` to the web protocol's `extended`; this is **not proof** that
the remote model honored the requested mode.

The `MODEL_ROUTE` progress event distinguishes `requested_model` /
`requested_reasoning_effort`, `sent_model` / `sent_reasoning_effort`,
and `observed_model` / `observed_reasoning_effort` when CWA exposes
these diagnostics. Missing observed metadata must remain visibly unknown,
never be filled in with requested values.

The durable one-shot claim records the requested `model` and
`reasoning_effort` alongside the existing SHA and Work binding.
**Never reuse** a Work that reached a claim or capture event to compare
models. Run separate Work IDs for Instant and High, preferably on identical
input documents/tasks. High thinking can change both first-token latency
and total runtime; quality gains must be measured on the same evaluation
set, not assumed.
