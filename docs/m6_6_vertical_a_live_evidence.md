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

## Interpretation

The failure occurred before any durable M6.2 admission, M6.4 attention decision, M6.5 dispatch, or worker peer turn existed. Therefore the failed live attempt did not create an ambiguous worker write or replay permission.

The default Codexia dependency was still pinned to the June `chatgpt-web-adapter==0.1.5` compatibility path. CWA has since moved its production write contract to `ChatGPTProductRuntime` with the browser-owned transport and has materially hardened canonical read, product finality, and identity behavior.

This is the first real Vertical A finding:

```text
M6.6 orchestration reached the live product boundary
→ legacy CWA 0.1.5 write contract failed with HTTP 422
→ supervisor remained exact READY at sequence 0
→ no worker dispatch / no ambiguous replay state
```

## Repair decision

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

## Resume requirement

R1 does not close Vertical A. After deterministic CI validates the migration, the same durable pilot work should be recovered and driven again rather than registering a replacement handoff merely to obtain a clean run.

A successful repair therefore needs to demonstrate:

```text
same durable work
→ READY recovery
→ current product-runtime cognition write
→ normal M6.2/M6.4 checkpoint
→ governed worker continuation
```

If the current CWA public runtime is missing a consumer capability needed by Codexia, treat that as consumer-driven CWA hardening rather than reintroducing private ChatGPT backend behavior into Codexia.
