# M6.6 Vertical A — Transport Recovery Evidence R6–R8

## Purpose

Continue the first real M6.6 Vertical A evidence journal after R1–R5 without rewriting the earlier transport findings.

This supplement records the continuation lease-order repair, historical durable dispatch recovery, the browser-native deployment split-brain discovered on the resumed work item, the stable deployment repair accepted through CWA PR14.5, and the long-history canonical-read throttling reproduced later in the same live run and repaired by CWA PR14.6.

It is evidence for transport/runtime hardening. It is **not** evidence that Vertical A or M6.6 is complete.

## R6 — continuation prewrite lease ordering and historical exact re-arm

After R5, the same historical Vertical A work progressed far enough to create a real durable worker dispatch. Under the then-current CWA revision, the continuation failed with:

```text
CANONICAL_READ_AUTHORITY_LEASE_MISMATCH
```

The failure occurred during the existing-conversation continuation prewrite canonical baseline read, before the fresh Browser Authority Lease reached the actual product write boundary.

CWA PR14.4 repaired that ordering. Its guarded live continuation acceptance proved that the old lease mismatch was removed and that the continuation reached the real product write boundary.

PR14.4 merged to CWA `main` as:

```text
429746893ea107fc2a09655be5bc071f645dad0a
```

The same acceptance then exposed a separate post-delegation lifecycle finding:

```text
BrowserOwnedWriteRuntimeError
CHATGPT_CONVERSATION_REQUEST_FAILED:net::ERR_ABORTED
failure_kind = BROWSER_OWNED_WRITE_OUTCOME_UNKNOWN
write_may_have_been_submitted = true
reconciliation_required = true
automatic_retry_allowed = false
manual_retry_safe_after_repair = false
```

After browser reload, the continuation user turn was durable but no terminal assistant answer existed. That separate lifecycle issue is tracked in CWA #99. It does not grant replay authority and is not addressed by the deployment/read repairs below.

The historical Vertical A dispatch had failed under an older CWA revision before PR14.4 emitted machine-readable `BROWSER_OWNED_WRITE_NOT_SUBMITTED` evidence. Its durable supervisor state therefore remained fail-closed:

```text
CHECKPOINT_DECIDED
→ DISPATCH_STARTED
→ IN_FLIGHT
```

Generic provider-proven no-submit recovery could not consume retroactive evidence that the old provider had never serialized.

Codexia therefore added a distinct explicit HUMAN-authorized recovery for exactly one historical `IN_FLIGHT` claim. The recovery binds:

```text
work_id
current in_flight_claim_id
expected pending dispatch_digest
explicit HUMAN recovery statement/reason
```

and only permits:

```text
exact IN_FLIGHT claim
→ durable HUMAN_REARM event
→ same exact pending dispatch
→ PREPARED
```

The recovery command itself constructs no provider and performs zero product writes.

Live acceptance against the real historical Vertical A work succeeded. A separate subsequent `status` process reconstructed:

```text
status = PREPARED
in_flight_claim_id = null
same dispatch_id
same dispatch_digest
same cursor
same proposal/admission/attention identity
```

This established restart-durable historical recovery without replaying the worker turn.

## R7 — resumed drive exposed browser-native deployment split-brain

After the historical re-arm was merged into the M6.6 candidate, the same work item was resumed. The provider failed again with:

```text
browser-owned write commit check failed: canonical read unavailable
```

Read-only inspection proved this was not a new product semantic drift. The pilot environment had three different browser-native CWA identities at once:

```text
Codexia Python package
→ older exact CWA VCS revision

Native Messaging host
→ executable from a separate CWA development virtual environment

Chrome unpacked extension
→ directory inside a separate CWA source checkout
```

At the same time, the Codexia consumer expected the newer merged CWA revision.

`cwa doctor --json` still appeared healthy because the old doctor checked extension id, registration, connectivity, and package presence but did not prove that Python, the registered native host, and the extension bytes loaded by Chrome formed one exact deployment identity.

This reproduced the old prewrite canonical-read behavior even though the underlying PR14.4 source repair already existed.

The failed drive had already created a fresh durable claim for the same pending dispatch before provider failure, so the supervisor correctly remained `IN_FLIGHT`. No blind retry was attempted.

## R7 repair — CWA PR14.5 stable browser-native deployment identity

CWA issue #100 captured the deployment defect. PR14.5 implemented a stable per-user deployment contract:

```text
current Python CWA package
→ browser-native install
→ deterministic extension-tree digest
→ stable per-user extension directory
→ Native Messaging host from the current Python environment
→ deployment manifest
→ fail-closed deployment identity checks
```

On Windows the stable unpacked extension path is:

```text
%LOCALAPPDATA%\chatgpt-web-adapter\browser-native\extension
```

The extension id remains frozen:

```text
kjfnkhajljnkbhikmfijcchenlfglaie
```

`browser-native install` materializes the packaged extension bytes into the stable path, records package/source revision, extension digest/id, and current host executable, and prefers the native-host console script adjacent to the active Python interpreter before ambient `PATH`.

A mixed/stale deployment now fails closed instead of allowing the old false-green doctor state.

PR14.5 exact candidate:

```text
00d0f122f58d15286d56df5111c345e3c339ca37
```

CI #1035 passed on that exact head, including engineering quality and the full Windows/Linux Python matrix.

Live Windows acceptance from the actual M6.6 pilot environment then proved:

```text
package source revision = 00d0f122f58d15286d56df5111c345e3c339ca37
stable extension path = %LOCALAPPDATA%\chatgpt-web-adapter\browser-native\extension
Native Messaging host = current M6.6 pilot virtual environment
browser_native_deployment_status.healthy = true
extension_digest_matches = true
source_revision_matches = true
package_version_matches = true
host_matches_current_environment = true
cwa doctor = healthy
```

The packaged and installed extension digests matched exactly during acceptance. Chrome was moved once from the checkout-bound unpacked extension to the stable runtime path.

No Codexia dispatch or retry was performed during PR14.5 acceptance.

PR14.5 squash-merged to CWA `main` as:

```text
a061e3f799915a8549b5449f41ae11eddae233dd
```

CWA issue #100 was then closed as completed. Post-delegation `net::ERR_ABORTED` remains separate in CWA #99.

## R8 — long-history canonical pagination hit HTTP 429

With the stable PR14.5 deployment active, the historical Vertical A finally advanced through multiple real worker/cognition cycles. Long model turns could exceed the caller timeout yet still finish in ChatGPT; M6.5 reconciliation correctly recovered already-existing exact turns without blind resend.

Later, during a closure worker turn, the model response completed visibly in ChatGPT but CWA failed the post-write canonical read with:

```text
browser-context canonical read failed:
reason=CANONICAL_READ_HTTP_ERROR
status=429
content_type=application/json
```

This was a read-plane failure, not proof that the write had failed. The visible terminal assistant response existed, while the durable supervisor remained fail-closed until canonical reconciliation could prove the exact turn.

The same historical conversation had accumulated enough messages/tool artifacts that full-history pagination became operationally expensive. The existing read implementation retried no throttled page and therefore allowed a transient `429` on any page to fail the entire canonical read.

This live reproduction promoted CWA issue #101 from a scaling concern to a concrete M6.6 transport blocker for long-lived product conversations.

## R8 repair — CWA PR14.6 throttle-safe canonical reads

CWA PR14.6 keeps write semantics unchanged and hardens only idempotent canonical GET reads:

```text
canonical GET page
→ HTTP 429
→ honor bounded Retry-After when valid
   or bounded exponential backoff
→ retry the same page URL/cursor only
→ pace successful pagination pages
→ bounded retry budget
→ explicit CANONICAL_READ_THROTTLE_EXHAUSTED if still throttled
```

The repair intentionally does **not**:

```text
retry a product write
reinterpret an ambiguous write outcome
return partial history as complete
skip a failed cursor/page
widen Codexia authority
```

PR14.6 candidate head:

```text
4c3a955d6b900cdbbac7005f2be3a6fbc1d5714d
```

CWA CI #1040 completed successfully on that candidate. After review, PR14.6 was marked ready and squash-merged to `main` as:

```text
21279260fbc344816e112393d40ee28e4355baaf
```

Issue #101 remains separately trackable; merging the bounded repair does not silently declare every future long-history scaling concern solved.

## Current M6.6 transport identity

The M6.6 `web` dependency and its exact-source contract gate are now pinned to:

```text
21279260fbc344816e112393d40ee28e4355baaf
```

The package version string remains `0.3.0`; the Git source revision in `direct_url.json` is the authoritative pilot identity.

Before the next live validation, the local M6.6 environment must reinstall the merged PR14.6 revision, run `cwa browser-native install` from that same environment, and verify healthy deployment identity. The stable Chrome extension path remains unchanged; the deployment manifest must still bind the exact merged source revision.

## Next validation requirement

The historical Vertical A work item is retained as evidence. It has already demonstrated real routine continuation, timeout reconciliation, semantic completion/attention defects, logical tool-turn provenance pressure, completion livelock, and canonical-read throttling. It should not be forced to `COMPLETED` merely to create a clean historical trace.

The next validation should therefore use a clean Vertical A work item after both repaired Codexia and exact merged PR14.6 are installed:

```text
exact merged PR14.6 installed
→ stable browser-native deployment healthy
→ new clean Vertical A work item
→ multiple routine worker/revision cycles
→ tool-using logical worker evidence remains current
→ completion passes exact HUMAN-attention evaluation
→ canonical full-history read survives bounded throttling
→ no product-write retry authority introduced
```

The transport repair is not itself provider proof, human authorization, or replay permission. Codexia's durable authority boundaries remain authoritative.
