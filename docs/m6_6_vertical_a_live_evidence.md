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

M6.6 now pins that exact merged CWA revision rather than the pre-PR14.1 `b3e27cf...` source.

## Resume requirement

R1–R3 do not close Vertical A. The next live action is still to recover the **same durable pilot work** before any new provider send.

First require a read-only supervisor status. If it remains:

```text
READY
last_sequence = 0
pending_dispatch = null
last_admission = null
last_attention = null
```

then the same `work_id` may be driven again with the exact pinned CWA runtime after environment identity verification. If the durable state is `IN_FLIGHT`, contains a pending dispatch, or otherwise records an ambiguous worker effect, do not retry; reconcile M6.5 state first.

The successful resumed path still needs to demonstrate:

```text
same durable work
→ exact READY recovery
→ bounded cognition write through pinned CWA
→ confirmed canonical readback
→ normal M6.2/M6.4 checkpoint
→ governed worker continuation
```

If the current CWA public runtime is missing another consumer capability needed by Codexia, treat that as consumer-driven CWA hardening rather than reintroducing private ChatGPT backend behavior into Codexia.
