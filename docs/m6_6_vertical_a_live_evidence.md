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

Instead, migrate the default live provider boundary to the current CWA production runtime and pin the pilot to the exact merged CWA source revision:

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

## Resume requirement

Neither R1 nor R2 closes Vertical A. After deterministic CI validates the latest repair, the same durable pilot work should be recovered and driven again rather than registering a replacement handoff merely to obtain a clean run.

A successful repair therefore needs to demonstrate:

```text
same durable work
→ READY recovery
→ bounded current product-runtime cognition write
→ normal M6.2/M6.4 checkpoint
→ governed worker continuation
```

If the current CWA public runtime is missing a consumer capability needed by Codexia, treat that as consumer-driven CWA hardening rather than reintroducing private ChatGPT backend behavior into Codexia.
