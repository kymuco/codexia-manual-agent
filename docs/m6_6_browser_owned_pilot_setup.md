# M6.6 Browser-Owned Pilot Setup

## Purpose

M6.6 live pilot writes use the production `chatgpt-web-adapter` product boundary rather than the historical browserless `ChatGPTWebClient.send()` path.

The exact CWA source revision selected by the M6.6 candidate is:

```text
21279260fbc344816e112393d40ee28e4355baaf
```

This is the squash-merge commit for CWA PR14.6. It includes the earlier PR14.1 request-bound ordinary-text conversation identity authority and browser-context canonical-read session-auth repair; PR14.2's exact committed-error preservation, request-text-shape compatibility, and safe correlation fingerprint; PR14.3's narrowly bounded browser-composer indentation compatibility; PR14.4's continuation Browser Authority Lease ordering repair plus exact prewrite `NOT_SUBMITTED` classification; PR14.5's stable browser-native deployment identity repair; and PR14.6's bounded throttle-safe retry for idempotent canonical GET pagination only.

PR14.4 live acceptance proved the original continuation lease mismatch was removed: an existing-conversation continuation reached the real product write boundary instead of failing during its prewrite canonical baseline read. That same acceptance then exposed a separate post-delegation `net::ERR_ABORTED` outcome where the user turn persisted but no terminal assistant answer was produced. That lifecycle finding is tracked separately in CWA issue #99 and must remain fail-closed; it does not authorize replay of the committed user turn.

PR14.5 fixed the deployment split-brain found by the resumed M6.6 pilot: the Python package, Native Messaging host, and Chrome unpacked extension could come from different CWA revisions while `cwa doctor` still appeared healthy. Browser-native installation now materializes one stable per-user extension deployment, binds it to the current Python environment, records deterministic deployment identity, and fails closed when package/installed-extension/host identity diverges.

PR14.6 fixes the later long-history canonical-read `HTTP 429` failure reproduced by the same Vertical A run. It retries only idempotent canonical GET reads with a bounded budget, honors bounded `Retry-After` when present, otherwise uses bounded backoff, retries the same page/cursor, paces successful pagination pages, never returns partial history as complete, and introduces no product-write retry path or authority.

The dependency is pinned to this exact Git revision. A moving CWA `main` is not part of the pilot identity.

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
cwa browser-native install
```

PR14.5 copies the packaged extension bytes into the stable per-user browser-native runtime directory and records a deployment manifest there. Print the stable unpacked-extension directory from that same environment:

```powershell
cwa browser-native extension-dir
```

On Windows the healthy installed path is expected to be under:

```text
%LOCALAPPDATA%\chatgpt-web-adapter\browser-native\extension
```

Load exactly that stable directory in Chrome/Chromium:

```text
chrome://extensions
→ Developer mode
→ remove any older checkout-bound unpacked CWA copy with the same frozen extension id
→ Load unpacked
→ select the stable directory printed by `cwa browser-native extension-dir`
```

Do not point Chrome back at a repository checkout, editable-install source tree, or `site-packages` directory. The stable runtime path is specifically intended to survive checkout relocation, virtual-environment replacement, and future CWA source changes.

The frozen extension id remains:

```text
kjfnkhajljnkbhikmfijcchenlfglaie
```

Verify deterministic deployment identity from the same pilot environment:

```powershell
python -c "import json; from chatgpt_web_adapter.browser_native_install import browser_native_deployment_status; print(json.dumps(browser_native_deployment_status(), indent=2))"

cwa doctor --json
```

`browser_native_deployment_status()` must report `healthy: true`. In particular, packaged/installed extension digest, source revision, package version, deployment schema/id, and current-environment Native Messaging host binding must agree. `cwa doctor` must also be healthy. A stale/mixed deployment is a fail-closed setup error, not permission to attempt a product write.

A live M6.6 drive should not be attempted until all of these checks are clean.

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

A CWA post-delegation failure with `write_may_have_been_submitted = true` is an ambiguity/reconciliation boundary, not retry permission. Only a structured exact `BROWSER_OWNED_WRITE_NOT_SUBMITTED` proof can support the M6.6 no-submit recovery path.

PR14.6 does not alter that rule: its retry loop is confined to canonical **reads** after or before a write boundary, never to the product write itself.

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

For the next clean Vertical A run, omit `--model` and `--reasoning-effort` unless an explicit profile is required. This preserves the product runtime's current/default model selection and minimizes unrelated variables.

## Resume or start a clean validation run

After a transport/setup failure, first recover status:

```powershell
python -m codexia_manual_agent.work.pilot_cli status <work_id> --database <db>
```

If the work is exactly `READY` with no pending dispatch, the same `work_id` may be resumed after the transport repair. If the work is `IN_FLIGHT` or contains a pending dispatch, do not retry blindly; reconcile the durable M6.5 state first.

For a historical `IN_FLIGHT` dispatch that predates machine-readable provider `NOT_SUBMITTED` evidence, M6.6 exposes an explicit HUMAN-authorized exact re-arm path. It must bind all of:

```text
work_id
current in_flight_claim_id
exact pending dispatch_digest
explicit HUMAN recovery reason
```

and may only perform:

```text
IN_FLIGHT
→ durable HUMAN_REARM evidence
→ same pending dispatch
→ PREPARED
```

The recovery command performs no provider write. A later normal `drive` invocation is a separate action and must acquire a fresh claim for the same exact pending dispatch.

The original Vertical A work item that exposed the completion/tool-tail/429 defects is retained as evidence and should not be forced to `COMPLETED` merely to obtain a green historical trace. After the repaired Codexia and merged PR14.6 pin are installed, validation should use a clean new Vertical A work item while preserving the historical database unchanged.

Never edit the SQLite state directly, fabricate a peer turn, or treat a CWA transport repair as automatic retry authority.
