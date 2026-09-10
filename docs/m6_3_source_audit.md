# M6.3 Source Audit — Chat / Codexia Peer Loop

## Audit target

M6.3 connects the delegated-work semantics from M6.1/M6.2 to the existing ChatGPT web provider. The audit asks whether transport-side `user` roles can be confused with human authorship, whether stale live conversation state can be overwritten, whether worker output can admit itself, whether peer records can be rebound after capture, or whether the peer loop widens local execution authority.

## Transport-role audit

ChatGPT represents both a physical user's account-side turn and a Codexia-sent continuation as transport role `user`. Therefore M6.3 never derives CODEXIA provenance from that role or from a textual label alone.

For Codexia sends, semantic provenance is established by an exact sequence:

```text
before current-branch snapshot
→ exact Codexia envelope sent
→ after current-branch snapshot
→ old snapshot must remain an exact prefix
→ exactly one new user node must equal the sent envelope
→ exactly one new assistant node must equal provider response
```

The visible Codexia label is explanatory metadata, not the provenance primitive.

## Historical-attribution audit

`attach()` records only ordered fingerprints in a `ChatPeerCursor`. It does not turn any pre-existing `user` message into a HUMAN `WorkStatement`.

Only new delta messages observed after attachment are semantically captured. This avoids inventing provenance for historical account activity that Codexia did not witness.

## Human-precedence audit

Before any admitted continuation is sent, `continue_admitted()` rereads the current branch and requires no unseen delta beyond the exact M6.2 checkpoint.

A human-entered turn after admission therefore makes the proposal stale before Codexia sends anything. The caller must observe and incorporate the new state first.

Current-branch rewrites/edits that invalidate the old fingerprint prefix also fail closed.

## Admission audit

The peer send path accepts only `ContinuationDecision.ADMIT`, revalidates exact M6.1/M6.2 binding, and requires `proposal.checkpoint_digest == cursor.cursor_digest`.

A rejected/revision/human-attention admission cannot reach the send path. A once-admitted proposal cannot be replayed against a later conversation checkpoint.

The returned ChatGPT worker answer may be wrapped as a new `ContinuationProposal`, but remains WORKER-authored and must pass M6.2 admission again.

```text
worker response
!= admitted follow-up
```

The completed `ChatPeerTurn` also retains the exact handoff and interpretation identities/digests inherited from the admission. `followup_proposal()` revalidates those bindings before wrapping worker output, so a real response from delegated work A cannot be transplanted as the next-step proposal for unrelated delegated work B.

```text
worker response from work A
!= proposal evidence for work B
```

## Provider-response reconciliation audit

M6.3 does not trust the response object alone. After the remote send it rereads the current branch and requires the observed assistant text to equal `ProviderResponse.text`; when the provider supplies a message id, the observed assistant message id must match it too.

Concurrent account-side activity during the send produces a delta other than exactly `user → assistant` and fails closed.

## Peer-record integrity audit

`ChatPeerCursor`, `CapturedChatPeerMessage`, `ChatPeerObservation`, and `ChatPeerTurn` all validate their schema and exact content digest on construction. A caller cannot mutate an already-captured observation or turn while retaining its old digest.

Factory construction additionally verifies the exact cursor transition:

```text
before message fingerprints
+ fingerprints(exact captured delta)
==
after message fingerprints
```

Conversation identity must remain identical across the before cursor, every captured message, and the after cursor. This prevents a caller-supplied after cursor or cross-conversation capture from being silently rebound into an otherwise well-formed peer record.

These records remain provenance records, not execution authority.

## Provider dependency audit

Codexia continues to pin `chatgpt-web-adapter==0.1.5`. That exact stable-core release already exposes `ChatGPTWebClient.get_messages()` and `ChatMessage`; M6.3 therefore does not upgrade the transport dependency merely to obtain history reads.

The CI web-provider contract now checks the current-branch history surface in addition to send/continue.

## Authority audit

M6.3 does create one intentional remote conversation effect when the caller explicitly invokes `continue_admitted()`: a Codexia-authored message is sent to the selected ChatGPT conversation.

The new work surface imports no M2 process executor, local workspace mutation, Git mutation, AuthorizationReceipt, or local network transport. An M6.2 semantic ADMIT remains insufficient for those effects.

No background supervisor invokes the peer loop automatically in M6.3.

## Known trust limits

An `EXTERNAL_USER` delta means a new account-side user-role message not emitted by the tracked Codexia send transaction. It is represented as HUMAN for M6 delegated-work semantics, but M6.3 does not cryptographically authenticate the physical person behind that account-side event.

M6.3 also assumes one tracked Codexia peer-loop writer for the attached cursor. Independent automation using the same account/conversation is treated as external activity and may invalidate or supersede the cursor rather than being silently attributed to Codexia.

M6.3 does not yet persist a peer cursor or recover it across supervisor restarts; durable multi-work recovery belongs to the later supervisor milestone. A fresh attach therefore makes no authorship claims about pre-attachment history.

## Audit conclusion

The M6.3 surface preserves the intended peer relationship:

```text
Human may enter the same live chat
Codexia may continue already-admitted delegated work
ChatGPT remains the cognitive worker
```

while enforcing:

```text
transport user role != semantic human authorship
Codexia continuation != human instruction
human intervention invalidates stale continuation
worker output != admitted continuation
worker output from work A != follow-up for work B
captured peer record != caller-rebindable provenance
peer-loop continuation != local execution authority
```
