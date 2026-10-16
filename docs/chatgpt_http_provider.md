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

## Planned interactive E2E observability (not implemented in this PR)

The future single live E2E entry point should optionally display ordered,
human-readable, sanitized progress events, with optional streaming of model
text into stdout:

  [01] WORK       new Work ID, canonical revision
  [02] INPUT      file count, total bytes and hashes, no private file content
  [03] HANDOFF    one durable claim/handoff admitted
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
