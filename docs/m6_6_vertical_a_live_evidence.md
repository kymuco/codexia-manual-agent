# M6.6 Vertical A — Live Evidence Journal

## Purpose

Record product evidence from the first real M6.6 ongoing-work pilot. This file is an evidence journal, not a claim that Vertical A or M6.6 is complete.

## R1 — first live drive exposed obsolete transport

The first pilot was registered successfully against a real existing ChatGPT conversation from the M6.6 candidate branch. The first `drive` then failed at the live provider boundary with:

```text
codexia-pilot: chatgpt-web request failed: backend status=422: {"detail":"Invalid conversation body"}
```

The environment reported:

```text
chatgpt-web-adapter = 0.1.5
```

A read-only supervisor recovery immediately after the failure reported:

```text
status = ready
last_sequence = 0
last_proposal = null
last_admission = null
last_attention = null
pending_dispatch = null
completion = null
```

The private ChatGPT conversation id and local database path are intentionally not recorded in repository evidence.

## R1 interpretation

The failure occurred before any durable M6.2 admission, M6.4 attention decision, M6.5 dispatch, or worker peer turn existed. Therefore the failed live attempt did not create an ambiguous worker write or replay permission.

The default Codexia dependency was still pinned to the June `chatgpt-web-adapter==0.1.5` compatibility path. CWA has since moved its production write contract to `ChatGPTProductRuntime` with the browser-owned transport and has materially hardened canonical read, product finality, and identity behavior.

This is the first real Vertical A finding:

```text
M6.6 orchestration reached the live product boundary
→ legacy CWA 0.1.5 write contract failed with HTTP 422
→ supervisor remained exact READY at sequence 0
→ no worker dispatch / no ambiguous replay state
```

## R1 repair decision

Do not patch the obsolete backend conversation body in Codexia.

Instead, migrate the default live provider boundary to the current CWA production runtime. The first repaired pilot pin was:

```text
b3e27cf1f53323d946953b4d962994733482bbae
```

The intended ownership after repair is:

```text
ChatGPT product drift / protected write / canonical readback / finality
    owned by chatgpt-web-adapter

semantic provenance / admission / attention / durable delegated-work continuity
    owned by Codexia
```

The CWA revision is pinned by exact Git commit rather than a moving `main` reference. The published `0.3.0` version string remains present in that source tree, but the selected source revision contains substantial post-0.3 merged work.

## R2 — healthy browser-owned transport exposed an oversized cognition envelope

After R1 repair, browser-native status and CWA doctor were healthy. The same durable pilot work was driven again and reached the current browser-owned write boundary. The cognition turn then failed before any worker dispatch with:

```text
codexia-pilot: chatgpt product-runtime request failed: text is too large for browser-native turn
```

The pinned CWA source rejects browser-native turn text above 200,000 characters. The M6.6 cognition renderer was serializing the complete `ChatPeerCursor.to_dict()` into model input. A cursor intentionally retains up to 4,096 SHA-256 message fingerprints so the peer runtime can prove exact current-branch prefix identity. That transport verification material can itself exceed the browser-native text budget for a long-lived real conversation.

The model did not need the fingerprint array to judge continuation. It needed the semantic handoff/evidence plus the exact digest that binds the retained cursor.

This is the second Vertical A finding:

```text
browser-owned product transport healthy
→ real long-lived worker chat produces a large exact cursor
→ M6.6 leaked full transport-prefix proof into cognition prompt
→ cognition write rejected before worker dispatch
```

## R2 repair decision

Keep full cursor fingerprints durable and unchanged inside M6.3/M6.5 runtime verification, but project them out of cognition input.

The cognition projection now carries:

```text
conversation_id
message_count
cursor_digest
```

instead of the full `message_fingerprints[]` array. Exact peer turns and external observations likewise preserve their exact statement/digest evidence while projecting nested cursors through the same bounded representation.

The repair does **not** truncate semantic evidence silently. M6.6 now owns an explicit 120,000-character cognition-prompt budget. If human/work evidence itself exceeds that semantic budget, cognition fails closed before any product write with an explicit Codexia error.

This preserves the separation:

```text
transport identity proof != model cognition context

full fingerprints
    remain durable runtime verification material

cursor_digest + message_count
    become the model-visible binding to that exact retained runtime state
```

An adversarial regression constructs the maximum 4,096-message cursor and verifies that the cognition prompt remains bounded and contains no `message_fingerprints` array. A second regression verifies that genuinely oversized semantic evidence is rejected rather than truncated.

## R3 — cognition write succeeded but canonical readback lost authentication

After the bounded cognition projection repair, the same pilot reached a real browser-owned cognition write. ChatGPT created a separate cognition conversation, the assistant returned the requested JSON judgment, and the product write visibly completed. The provider then failed during post-write canonical reconciliation with:

```text
CANONICAL_READ_AUTHENTICATION_REQUIRED
HTTP 401
```

This was not a worker-chat identity mix-up. The new conversation was the expected Codexia cognition plane: the first checkpoint has no cognition conversation id yet, so `PilotCheckpointSource` sends `conversation=None`; only a confirmed provider response may establish the reusable cognition conversation identity.

The failure boundary was:

```text
browser-owned cognition write
→ product write succeeds
→ cognition chat exists and assistant JSON is visible
→ browser-context canonical readback returns 401
→ ProviderResponse is not confirmed
→ Codexia stops before M6.2/M6.4/worker dispatch can rely on that cognition result
```

CWA correctly treated the accepted-write/readback-failure state as ambiguous and did not authorize an automatic retry. Therefore the pilot was not blindly driven again while that CWA boundary remained unresolved.

## R3 consumer-driven CWA repair

The CWA investigation proved two independent live drifts and repaired them upstream rather than adding a Codexia fallback:

1. current ChatGPT empty-composer structure had changed, so CWA's browser-owned preflight needed a new bounded locale-neutral structural predicate;
2. the current canonical endpoint required a page-session Bearer access token in addition to cookies, so canonical auth and the current GET were moved into the same browser page evaluation without exporting token material.

The same investigation also proved that an older unpacked CWA directory can remain loaded in Chrome even when the Python checkout is newer. Reloading such an extension only reloads the old directory. For M6.6, Python CWA, Native Messaging host and loaded unpacked extension must therefore share one exact source identity.

CWA PR14.1 then passed a one-write live acceptance gate with:

```text
write_attempts = 1
automatic_write_retry = false
bare_conversation_id = true
assistant_exact_reply = true
canonical_snapshot_complete = true
canonical_read_scope = full_history
exact_user_marker_found = true
canonical_completion_proven = true
```

and merged as:

```text
d2ce811731898ae4b3bf04424bf02f047da0bcd9
```

M6.6 then pinned that exact merged CWA revision rather than the pre-PR14.1 `b3e27cf...` source.

## R4 — successful cognition response exposed false request-correlation failure

With PR14.1 installed from one exact pilot environment, CWA doctor was healthy and the same durable work remained:

```text
READY
last_sequence = 0
last_proposal = null
last_admission = null
last_attention = null
pending_dispatch = null
completion = null
```

The next real cognition turn visibly succeeded and returned the requested strict JSON judgment, but CWA surfaced:

```text
PR9_2_WRITE_COMPLETED_CONVERSATION_ID_UNRESOLVED:
SCHEMA29:identityParserNotReached=true:
submitCorrelationDiagnosticsUnavailable=true
```

The visible assistant JSON was valid cognition output, but Codexia correctly did not consume it because CWA did not return a confirmed `ProviderResponse`. No M6.2 admission, M6.4 attention decision, or worker dispatch was durably recorded.

Upstream diagnosis found that historical rich-input wrappers reused the same committed-write error prefix and could mask the outer ordinary-text failure suffix. CWA PR14.2 first preserved the exact ordinary failure, revealing the real class on a guarded reproduction:

```text
PR9_2_WRITE_COMPLETED_CONVERSATION_ID_UNRESOLVED:
ORDINARY_REQUEST_CORRELATION_UNRESOLVED:
requestCount=1:
unresolvedCount=0:
foreignCount=1:
requestOverflow=false
```

The ChatGPT UI nevertheless returned the exact requested response, proving that write and model execution had succeeded while request correlation alone was being rejected.

PR14.2 added bounded request-text shape compatibility without fuzzy matching or widened authority. The existing schema-29 authority still requires exact text equality, `action=next`, exact new-chat/continuation semantics, one logical user-message id, and exact attachment evidence. It also added a durable safe correlation fingerprint containing only booleans/counts/lengths rather than prompt text, ids, or request bodies.

A guarded 100,000-character synthetic acceptance turn then passed exact request correlation and canonical full-history readback. PR14.2 merged to CWA `main` as:

```text
a90fc56d66f67ed8557120da4ca8c49a52b333e5
```

However, that synthetic prompt did not reproduce the pretty-printed JSON indentation used by the real M6.6 cognition envelope.

## R5 — real cognition envelope isolated browser indentation canonicalization

After repinning Codexia to merged PR14.2 and resuming the same durable pilot work, another real cognition turn visibly succeeded but CWA again refused request correlation. The durable safe fingerprint was decisive:

```text
actionNext = true
conversationIdentityMatches = true
userMessageCount = 1
userMessageIdCount = 1
userMessageIdentityClassified = true
expectedTextLength = 8761
observedTextLength = 8761
commonPrefixLength = 1835
commonSuffixLength = 42
stringPartCount = 1
objectTextPartCount = 0
unknownPartCount = 0
exactObservedTextEqualsExpected = false
crlfNormalizedEqualsExpected = false
nfcNormalizedEqualsExpected = false
trimEqualsExpected = false
```

The same read-only supervisor status remained exact:

```text
READY
last_sequence = 0
last_proposal = null
last_admission = null
last_attention = null
pending_dispatch = null
completion = null
```

Reconstructing the current cognition envelope placed offset 1835 exactly at the first two-space indentation immediately after the opening `{` of a pretty-printed checkpoint JSON block. Equal observed/expected lengths, one parsed user message, and the mismatch geometry isolated the drift to contenteditable preservation of line-leading indentation using non-breaking Unicode-space representation.

CWA PR14.3 therefore introduced only a bounded browser-composer indentation equivalence for ordinary zero-attachment turns:

```text
expected ASCII space
↔ observed U+00A0 NBSP or U+202F NNBSP
only while still inside line-leading indentation
```

It does not perform global whitespace normalization, trimming, Unicode normalization, fuzzy matching, retry, route inference, or request mutation. The network request remains untouched; a local correlation copy is canonicalized only when total length is identical and every other code unit matches exactly, then the existing schema-29 authority is run again.

PR14.3 exact candidate:

```text
41b00c1ee64a45e7e8dbba33fe966cd64a88d377
```

Deterministic CI #1016 passed engineering quality/architecture, JavaScript syntax, Windows and Ubuntu on Python 3.10–3.14, release artifact build, and installed-wheel smoke.

A guarded cognition-style pretty-JSON live acceptance then returned:

```text
write_attempts = 1
automatic_write_retry = false
transport = browser-owned
assistant_text = PR14_3_INDENT_OK
bare_conversation_id = true
canonical_snapshot_complete = true
canonical_message_count = 6
canonical_read_scope = full_history
exact_user_marker_found = true
PASS = true
```

Most importantly, the durable browser-indent fingerprint proved that the new path was actually exercised:

```text
priorMatched = false
eligibleOrdinaryRequest = true
expectedTextLength = 9378
observedTextLength = 9378
observedTextCandidateCount = 1
browserIndentEquivalent = true
normalizedIndentSpaceCount = 568
normalizedMatch = true
```

This is the required causal proof: the prior strict inspector rejected the browser-emitted representation; PR14.3 recognized only the bounded line-leading indentation representation; and the existing request-bound schema-29 authority then accepted the exact logical request.

PR14.3 merged to CWA `main` as:

```text
75c8f359b113b1db2a03bc85b074d0cfa91cf361
```

CWA issue #95 closes with that merge. M6.6 now pins this exact merged revision.

## Resume requirement

R1–R5 do not close Vertical A. They establish that the product transport boundary which repeatedly blocked confirmed cognition is now acceptance-proven against the same structural form as the real cognition prompt.

Before any new provider send, recover status again. If it remains:

```text
READY
last_sequence = 0
pending_dispatch = null
last_admission = null
last_attention = null
```

then install the new exact CWA pin from the Codexia pilot environment, register the Native Messaging host from that environment, load the matching unpacked extension, verify `cwa doctor --json`, and drive the **same** `work_id` once under the governed M6.6 pilot surface.

The successful resumed path now needs to demonstrate:

```text
same durable work
→ exact READY recovery
→ real cognition write through merged CWA PR14.3
→ confirmed canonical readback
→ normal M6.2/M6.4 checkpoint
→ governed worker continuation
```

If the durable work is `IN_FLIGHT`, contains a pending dispatch, or otherwise records an ambiguous worker effect, do not retry; reconcile M6.5 state first.

If the current CWA public runtime exposes another real consumer gap, treat it as consumer-driven CWA hardening rather than reintroducing private ChatGPT backend behavior into Codexia.


## R6–R9 — later Vertical A transport and semantic findings

The R1–R5 journal above records the early transport bring-up. Later governed Vertical A work continued to expose additional consumer-driven boundaries rather than being rewritten into a clean historical trace.

R6/R7 established two separate transport/deployment findings:

- continuation prewrite Browser Authority Lease ordering had to be repaired without converting an ambiguous post-delegation effect into retry authority;
- the Python package, Native Messaging host, and unpacked Chrome extension could drift across revisions, requiring the stable browser-native deployment identity introduced by CWA PR14.5.

The first full adversarial Vertical A then reproduced three Codexia semantic gaps: completion could bypass explicit HUMAN attention evaluation; a pilot HUMAN answer could be shadowed before the first fresh governed judgment; and a tool-using ChatGPT turn could lose terminal logical-worker provenance behind assistant-only artifacts and enter a completion livelock. Those gaps are retained as historical evidence and are covered by deterministic M6.6 regressions rather than being hidden by restarting the run.

That same run also reproduced long-history canonical pagination throttling:

```text
HTTP 429
```

CWA PR14.6 bounded retry to idempotent canonical GET pagination only, preserved the exact page/cursor, honored bounded `Retry-After`/backoff, paced successful pages, never returned partial history as complete, and introduced no product-write retry path. It squash-merged as:

```text
21279260fbc344816e112393d40ee28e4355baaf
```

A later clean Vertical A R2 exposed a cognition-contract ambiguity before its first worker dispatch: an initial bounded step with no prior worker evidence was rendered as `evidence_fit=unsupported` while `revision_request=null`, which deterministically derived M6.2 `REVISE` and then failed the bounded-revision invariant. The contract was clarified so absence of prior worker evidence does not itself mean unsupported evidence; malformed `unsupported + revision_request=null` remains fail-closed.

R2 was then continued without rewriting its immutable handoff, so it remained intentionally pinned to historical Codexia candidate:

```text
b3ad5464f09ed3dbce7316827b7e2fa1ea551c08
```

It autonomously completed a first worker cycle and admitted a second bounded read-only adversarial revision without HUMAN scheduling. The second worker answer became visibly complete in ChatGPT, but the post-write canonical readback failed with:

```text
CANONICAL_READ_TIMEOUT
```

Durable supervisor recovery remained fail-closed:

```text
status = IN_FLIGHT
completion = null
last_sequence = 6
pending second-cycle dispatch retained
```

No re-arm or replay was authorized. Because the immutable R2 objective was pinned to the older Codexia SHA, this run is evidence of runtime behavior and transport failure only; it is not final validation of the current PR #20 candidate.

CWA PR14.7 therefore adds one bounded fresh canonical-read retry only for `CANONICAL_READ_TIMEOUT`. The retry preserves the exact conversation id and captured Browser Authority Lease, creates a fresh native read request/budget, applies to both short and full paginated reads, and fails closed as `CANONICAL_READ_TIMEOUT_EXHAUSTED` if the second read also times out. Non-timeout canonical failures and every product write remain non-retried.

PR14.7 candidate CI #1043 completed successfully and it squash-merged to CWA `main` as:

```text
df8435ee46bb0f1a5c9e07ee8070fe00d096c686
```

The next qualifying Vertical A must therefore be a new clean work item whose immutable objective is pinned to the post-PR14.7 Codexia candidate head. Historical databases remain evidence and must not be forced to completion merely to obtain a green trace.
