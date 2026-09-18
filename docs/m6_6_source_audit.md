# M6.6 Source Audit — First General Daily-Use Pilot

## Audit target

M6.6 connects live cognition and one minimal human-answer surface to the already-governed M6.1–M6.5 work loop. The audit asks whether these convenience layers accidentally create a new semantic authority, execution authority, completion authority, human-impersonation path, replay path, or transport retry authority.

## Cognition authority audit

`PilotCheckpointSource` does not accept a model-authored admission decision. The model supplies bounded semantic judgments only. `ContinuationAdmission.evaluate(...)` derives the M6.2 decision from those judgments, and `DynamicAttentionDecision.evaluate(...)` derives the M6.4 attention outcome plus hard overrides.

```text
model judgment != admission authority
model attention preference != explicit human attention constraint
```

The cognition response is strict-key decoded. Additional fields such as `execute`, `authorized`, `approved`, or another undeclared control field fail before an M6 record can be created.

## Exact logical worker-evidence audit

The frozen M6.3 `ChatPeerTurn` schema still records one exact Codexia message and one assistant message. Vertical A proved that a real ChatGPT product turn can expose additional assistant-only tool/progress artifacts after that v1 turn.

M6.6 therefore derives current logical worker evidence from an exact contiguous chain:

```text
PEER_TURN_RECORDED
→ zero or more exact EXTERNAL_OBSERVED events
   containing assistant-only messages
→ current cursor
```

Every link must bind the exact previous/next cursor and the same handoff/interpretation identity. The most recent assistant statement in that exact tail becomes current logical worker evidence. Any pilot HUMAN answer, provider-side user message, mixed-role observation, or unrelated durable event breaks the chain and prevents stale completion evidence from being reused.

The live peer-loop hardening is installed on the existing `ChatGPTPeerLoop` surface before M6.5 provenance wrapping. This preserves the original fresh-live-turn ticket mechanism instead of bypassing it with a new subclass authority path.

When current logical worker evidence exists, cognition must use `proposal_text=null`; it cannot replace captured worker evidence with a more convenient candidate.

## Completion audit

`SupervisorCompletion` accepts only a CODEXIA-authored `WorkStatement`.

Vertical A proved that the historical two-field `mode=complete` shortcut bypassed explicit HUMAN attention-constraint evaluation. The hardened completion path now derives a normal proposal from current logical worker evidence, runs M6.2 admission, constructs exact M6.4 attention context/checks, and evaluates M6.4 before a completion outcome can be returned.

```text
current logical worker evidence
→ completion semantic judgment
→ exact M6.2 admission
→ exact one-check-per-HUMAN-constraint M6.4 context
→ TRIGGERED / UNCERTAIN => WAITING_HUMAN
→ all CLEAR + KEEP_MOVING => SupervisorCompletion candidate
```

A legacy two-field completion response is accepted only when the handoff has no explicit attention constraints; otherwise it fails closed.

The pre-cognition evidence check is still not sufficient by itself because cognition is a live provider call. Human or worker activity may arrive while Codexia is deciding whether the current result is complete. M6.6 therefore retains the second boundary immediately before the durable terminal transition:

```text
current logical worker evidence
→ completion cognition
→ final exact live-peer reread   # external-state linearization point
→ no visible intervening activity
→ CAS against the pre-cognition supervisor sequence/digest
→ durable COMPLETED
```

If the final reread sees any new peer activity, that activity is durably recorded first and the completion judgment is discarded; the next loop requires fresh cognition over the new state. If another supervisor process advances the durable event chain after cognition, the compare-and-swap append loses and completion can happen only after fresh recovery/cognition.

This preserves:

```text
worker says done != work complete
assistant-only artifacts from the same exact provider turn != stale worker evidence
old worker result + newer HUMAN activity != completion evidence
concurrent durable transition != stale completion permission
```

## Human precedence and answer audit

M6.5 continues to synchronize live peer state before claiming a prepared dispatch. M6.6 does not bypass that path.

M6.6 additionally allows an explicit human answer outside the worker ChatGPT conversation. The pilot answer transition requires exact `WAITING_HUMAN` state, a HUMAN-authored `WorkStatement`, the exact waiting sequence/event digest, and the exact M6.4 attention-decision digest. It is appended to the same supervisor hash chain as an external-observed pilot payload.

The transition performs only:

```text
WAITING_HUMAN → READY
```

It does not move the ChatGPT cursor, create a `ContinuationAdmission`, construct a dispatch, mint a provider lease, or grant local authority. A second or unsolicited answer in `READY` fails closed.

Vertical A proved that requiring the pilot answer to remain the terminal durable event was too brittle: a newer ordinary external observation could shadow the answer before the first fresh judgment. The hardened checkpoint source scans backward only until the next `CHECKPOINT_DECIDED`, retaining the latest unresolved exact pilot HUMAN answer across newer external observations. Fresh cognition can therefore receive both the answer and the newer provider evidence.

That retention ends at the first fresh governed checkpoint; it is evidence continuity, not persistent authority.

The existing M6.3 `EXTERNAL_USER` route remains valid for real account-side human chat activity. Pilot cognition distinguishes the two evidence sources.

Fresh human evidence is prioritized near the front of the bounded M6.4 attention basis so large static handoff context cannot silently displace the answer that resumed the work. The original handoff identity remains unchanged.

This preserves:

```text
human answer != arbitrary execution authority
```

## Explicit attention-constraint audit

Continuation and governed completion responses must contain exactly one judgment for every exact HUMAN attention-constraint statement digest. Missing, duplicate, substituted, or foreign checks fail before `DynamicAttentionContext` is created, and M6.4 independently validates exact coverage again.

A `TRIGGERED` or `UNCERTAIN` explicit rule remains a hard M6.4 override even when cognition returns `KEEP_MOVING`. The pilot does not normalize an inconsistent attention payload: for example, an override that requires human attention together with `urgency=none` fails closed rather than silently rewriting the model judgment.

## Provider, canonical-read, and replay audit

The pilot does not call ChatGPT worker transport directly. Worker continuation/revision still passes through M6.5 durable `DISPATCH_STARTED`, the ephemeral live lease, M6.3 before/send/after reconciliation, provenance tickets, and crash reconciliation.

Routine insufficient worker output can derive M6.2 `REVISE` and remain background work when M6.4 says `KEEP_MOVING`; the revision still uses the existing governed provider-dispatch path rather than creating a second transport authority.

The cognition provider call is a reasoning input, not a delegated-work side effect. Its response cannot itself mutate local files, Git, processes, network targets, or the worker conversation. A cognition failure therefore fails the current drive call without creating provider-dispatch retry permission.

Vertical A also reproduced two canonical-read transport failures. First, `HTTP 429` occurred while CWA was paginating full canonical history; PR14.6 owns that bounded idempotent GET retry. The later clean R2 run then reproduced a post-write `CANONICAL_READ_TIMEOUT` after the worker answer was visibly complete. PR14.7 owns that second repair and the exact merged CWA transport revision is:

```text
df8435ee46bb0f1a5c9e07ee8070fe00d096c686
```

The 429 retry budget remains limited to idempotent canonical GET reads, preserving the same page/cursor and never returning partial history as complete. The timeout recovery performs at most one fresh canonical-read operation only for `CANONICAL_READ_TIMEOUT`, preserving the exact conversation id and captured Browser Authority Lease; a repeated timeout fails closed as `CANONICAL_READ_TIMEOUT_EXHAUSTED`. Non-timeout canonical failures and product writes are never retried by PR14.7.

Critically:

```text
canonical read retry != product write retry
ambiguous provider effect != retry permission
```

The M6.5 no-replay contract remains authoritative.

The pilot may reuse an ephemeral cognition conversation within one process. That conversation is not authoritative or durable; every cognition request includes the exact durable delegated-work state required to recompute the judgment after restart.

## Historical dispatch recovery audit

The explicit `rearm-dispatch` surface exists only for one exact historical `IN_FLIGHT` claim that cannot be recovered from machine-readable provider no-submit proof. It requires HUMAN authorship and binds the current claim id plus exact pending dispatch digest.

It performs only:

```text
IN_FLIGHT → PREPARED
```

while preserving the same dispatch and performing zero provider writes. A later `drive` is a distinct action that must acquire a new claim. The re-arm surface is manual recovery authority, not evidence that a previous ambiguous write was safe to replay.

## Runtime surface audit

`pilot_runtime.py` is thin composition over existing M6 components. `pilot_cli.py` exposes only:

- register a handoff against an existing conversation;
- drive a registered work with bounded `max_steps`;
- record one exact human answer for `WAITING_HUMAN` work;
- recover durable status;
- explicitly re-arm one exact historical dispatch from HUMAN authority without a provider write.

It introduces no resident daemon, timer, notification channel, arbitrary shell, filesystem mutation, Git mutation, merge authority, or generic local-machine control.

## Audit conclusion

The M6.6 candidate adds the missing live cognition/product bridge without replacing the authority roots established in M6.1–M6.5:

```text
HUMAN handoff
→ live CODEXIA semantic judgment
→ existing M6.2 admission
→ existing M6.4 attention
→ existing M6.5 durable driver
→ exact M6.3 + same-turn assistant-tail worker evidence
→ repeat / WAITING_HUMAN / governed completion attention / final-live-reread + CAS

WAITING_HUMAN
→ exact pilot HUMAN answer
→ READY only
→ answer retained until first fresh M6.2/M6.4 judgment
→ fresh governed continuation before any next dispatch
```

The repaired candidate still needs clean real-pilot validation. The historical Vertical A remains evidence of the defects that drove the repairs; it is not forced to `COMPLETED`. M6.6 remains incomplete until both required real daily-use verticals are demonstrated and reviewed.
