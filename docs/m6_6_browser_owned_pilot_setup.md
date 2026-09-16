# M6.6 Browser-Owned Pilot Setup

## Purpose

M6.6 live pilot writes use the production `chatgpt-web-adapter` product boundary rather than the historical browserless `ChatGPTWebClient.send()` path.

The exact CWA source revision selected by the M6.6 candidate is:

```text
75c8f359b113b1db2a03bc85b074d0cfa91cf361
```

This is the merge commit for CWA PR14.3. It includes the PR14.1 request-bound ordinary-text conversation identity authority and browser-context canonical-read session-auth repair; PR14.2's exact committed-error preservation, request-text-shape compatibility, and safe correlation fingerprint; and PR14.3's narrowly bounded browser-composer indentation compatibility for ordinary zero-attachment text. PR14.3 was acceptance-proven by a guarded cognition-style pretty-JSON live turn where the prior strict inspector rejected the browser representation, the browser-indent layer identified 568 line-leading ASCII-space ↔ NBSP/NNBSP substitutions at equal total length, and the existing schema-29 authority then confirmed the exact logical request. The dependency is pinned to this Git revision. A moving CWA `main` is not part of the pilot identity.

## Install the exact pilot dependency

From the M6.6 Codexia checkout and its pilot virtual environment:

```powershell
python -m pip install -U pip
python -m pip uninstall -y chatgpt-web-adapter
python -m pip install -e ".[web]"
```

The explicit uninstall is part of the exact-source procedure. CWA source revisions can change while the package version remains `0.3.0`; in that situation pip may clone a newer VCS revision for dependency resolution but still leave an already-installed `0.3.0` distribution in place as satisfying the dependency. Removing the existing distribution first guarantees that the subsequent editable install materializes the exact Git revision pinned by the `web` extra.

Verify the installed source identity:

```powershell
python -c "import importlib.metadata as m; print(m.version('chatgpt-web-adapter')); print(m.distribution('chatgpt-web-adapter').read_text('direct_url.json'))"
```

The package version may still report `0.3.0`; the authoritative pilot dependency identity is the Git commit in `direct_url.json`. Do not continue the live pilot if that commit differs from the exact revision documented above.

## Install the browser-owned bridge

The production write path is browser-owned. Register the Native Messaging host **from the same pilot virtual environment that imports the pinned CWA revision**:

```powershell
chatgpt-web-adapter browser-native install
```

Print the unpacked extension directory from that same environment:

```powershell
chatgpt-web-adapter browser-native extension-dir
```

Load exactly that directory in Chrome/Chromium:

```text
chrome://extensions
→ Developer mode
→ remove any older unpacked CWA copy with the same frozen extension id if necessary
→ Load unpacked
→ select the printed directory
```

A Chrome extension reload only reloads the directory Chrome already owns. It does not prove that directory matches the Python package currently imported by Codexia. For a pilot run, the Python package, Native Messaging host and loaded unpacked extension must all come from the same exact CWA revision.

Then verify the local bridge:

```powershell
chatgpt-web-adapter browser-native status
cwa doctor --json
```

A live M6.6 drive should not be attempted until the browser-owned runtime reports ready/connected.

## Authority boundary

The browser bridge owns only the ChatGPT product write mechanics and canonical product reconciliation. It does not grant Codexia local filesystem, Git, process, connector, approval, merge, or retry authority.

```text
browser-owned product write authority
!= Codexia execution authority
```

The M6.3/M6.5 boundaries remain authoritative:

```text
exact durable cursor
→ M6.2 admission
→ M6.4 attention
→ M6.5 durable dispatch/no-replay
→ one browser-owned product write
→ canonical readback
→ exact M6.3 captured turn
```

## System-context behavior

The modern browser-owned product runtime does not claim the legacy hidden `system=` backend field as part of its production text-turn contract.

For Codexia cognition only, a `ProviderRequest.system` value is therefore rendered explicitly into the visible cognition request envelope. It is not silently dropped and is not represented as a hidden product-system message.

Worker peer-loop continuations use `system=None`; their exact Codexia continuation text remains unchanged for M6.3 before/send/after reconciliation.

## Model selection

The product runtime accepts evidence-backed semantic profiles:

```text
FAST
BALANCED
DEEP
```

The existing pilot `--reasoning-effort` values are mapped conservatively onto those profiles when supplied. Raw historical model slugs are rejected before a product write rather than routed through an obsolete backend payload.

For the first resumed Vertical A run, omit `--model` and `--reasoning-effort` unless an explicit profile is required. This preserves the product runtime's current/default model selection and minimizes unrelated variables.

## Resume the same durable work

After a transport/setup failure, first recover status:

```powershell
python -m codexia_manual_agent.work.pilot_cli status <work_id> --database <db>
```

If the work is still exactly:

```text
READY
last_sequence = 0
pending_dispatch = null
```

then resume the same `work_id` after the transport repair. Do not register a replacement handoff merely to obtain a clean run.

If the work is `IN_FLIGHT` or contains a pending dispatch, do not retry blindly; reconcile the durable M6.5 state first.
