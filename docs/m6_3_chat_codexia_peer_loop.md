# M6.3 — Chat / Codexia Peer Loop

## Goal

M6.1 made delegated work and semantic authorship explicit. M6.2 separated a worker proposal from Codexia admission. M6.3 connects those records to the real ChatGPT conversation surface without turning Codexia-authored continuation into a HUMAN message.

Primary boundary:

```text
transport role "user" != semantic human authorship
```

Additional boundaries:

```text
Codexia-authored continuation != human instruction
live worker output != admitted continuation
conversation identity != authorship proof
human chat activity invalidates stale continuation
worker output from work A != follow-up for work B
```

## Existing transport

M6.3 deliberately reuses the existing `ChatGPTWebProvider` and the already-pinned `chatgpt-web-adapter==0.1.5` stable core. The pinned adapter already exposes `ChatGPTWebClient.get_messages()`, which reads visible messages from the current conversation branch. No provider-version bump or alternate browser transport is introduced.

`ChatGPTWebProvider.read_messages()` normalizes only visible `user` and `assistant` messages and requires exact provider node/message identity. Missing or duplicate identities fail closed because M6.3 cannot safely reconcile provenance without them.

## Cursor-first attachment

`ChatGPTPeerLoop.attach()` reads the current branch and creates a digest-bound `ChatPeerCursor` from ordered provider message fingerprints.

The cursor does **not** retroactively label old `user` messages as human. M6.3 has no evidence about which client produced arbitrary historical account-side messages before attachment.

```text
existing conversation history
→ exact baseline cursor
→ no retrospective semantic authorship claim
```

## New external turns

`observe(cursor)` requires the old branch to remain an exact prefix. Only the delta after the cursor is attributed.

A newly observed `user` turn that did not pass through the tracked Codexia send path is captured as `EXTERNAL_USER` and represented with HUMAN semantic authorship for the delegated-work layer. This means an external account-side user turn, not cryptographic proof of the physical person's identity. A new `assistant` turn is captured as WORKER.

This lets the human enter the same chat directly without a manual-mode takeover ceremony.

## Codexia continuation provenance

`continue_admitted()` accepts only an exact M6.2 `ADMIT` whose proposal checkpoint equals the current `ChatPeerCursor` digest.

Before sending, M6.3 rereads the conversation. Any unseen human/worker delta makes the admission stale and the send fails closed.

For a valid continuation:

```text
exact cursor
+ exact admitted proposal
→ reread exact same branch
→ send explicit Codexia continuation envelope
→ reread branch
→ require exact old prefix
→ require exactly one new user message + one assistant response
→ require observed user text == sent Codexia envelope
→ require observed assistant text/id == provider response
→ capture CODEXIA + WORKER semantic provenance
→ new exact cursor
```

The visible `[Codexia delegated-work continuation]` label is for human/worker readability. It is not the security mechanism. Provenance comes from exact before/send/after reconciliation against provider node/message identities.

## Human precedence

If the human writes in the chat after a proposal was admitted but before Codexia sends it, current conversation state no longer equals the proposal checkpoint.

```text
human intervention
→ stale admission
→ no Codexia send
→ observe the new turn first
```

This is the first concrete implementation of the desired property that the human can simply enter an active chat and change direction without toggling manual/autonomous mode.

## Worker response bridge

A completed peer turn exposes `followup_proposal()`, which binds the exact captured WORKER statement to the new cursor digest as a fresh M6.2 `ContinuationProposal` candidate.

The peer turn also retains the exact handoff and interpretation identities/digests inherited from the admission. `followup_proposal()` requires those exact bindings again, so worker output from one delegated work cannot be transplanted into another work merely by passing different arguments to the convenience bridge.

This is provenance wiring only:

```text
captured worker response from work A
→ candidate proposal for exact work A
!= proposal for work B
!= admitted continuation
```

M6.3 does not claim that arbitrary worker prose always contains a useful next action, nor does it let the worker admit itself. Semantic fit remains a Codexia/M6.2 judgment.

## Authority boundary

M6.3 intentionally performs one remote cognitive side effect: sending a Codexia-authored continuation into the already selected ChatGPT conversation when `continue_admitted()` is explicitly invoked.

It does not grant or invoke local process, filesystem, Git, or local-network authority. M6.2 `ADMIT` still does not authorize those effects.

```text
peer-loop continuation
!= local execution authority
```

No background scheduler automatically calls `continue_admitted()` in M6.3; that remains M6.5 work.

## Explicit limits

M6.3 does not yet provide:

- a background supervisor or durable work queue;
- learned/dynamic attention policy beyond stale-turn precedence;
- notification delivery;
- local worker execution;
- cryptographic proof of a physical human behind an external account-side turn;
- attribution of pre-attachment historical user messages;
- automatic semantic extraction of a clean next action from arbitrary prose;
- automatic execution of an admitted worker proposal.

## Exit gate

M6.3 is complete when an existing ChatGPT conversation can be attached, a fresh M6.2-admitted continuation can be sent as explicitly CODEXIA-authored work, the resulting ChatGPT answer is captured as WORKER output, a human-entered turn supersedes stale continuation without a mode switch, and the captured worker answer can become a new exact-work proposal candidate without being rebound to another work, promoted to human authorship, or treated as execution authority.
