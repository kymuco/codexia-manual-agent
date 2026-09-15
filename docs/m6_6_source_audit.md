# M6.6 Source Audit — First General Daily-Use Pilot

## Audit target

M6.6 connects live cognition and one minimal human-answer surface to the already-governed M6.1–M6.5 work loop. The audit asks whether these convenience layers accidentally create a new semantic authority, execution authority, completion authority, human-impersonation path, or replay path.

## Cognition authority audit

`PilotCheckpointSource` does not accept a model-authored admission decision. The model supplies bounded semantic judgments only. `ContinuationAdmission.evaluate(...)` derives the M6.2 decision from those judgments, and `DynamicAttentionDecision.evaluate(...)` derives the M6.4 attention outcome plus hard overrides.

```text
model judgment != admission authority
model attention preference != explicit human attention constraint
```

The cognition response is strict-key decoded. Additional fields such as `execute`, `authorized`, `approved`, or another undeclared control field fail before an M6 record can be created.

## Exact worker-evidence audit

When the supervisor terminal event is `PEER_TURN_RECORDED`, `BackgroundWorkDriver` supplies only that exact decoded `ChatPeerTurn` to cognition. The pilot requires `proposal_text=null` in that state and derives the next proposal with `ChatPeerTurn.followup_proposal(...)`.

The cognition model therefore cannot replace a captured worker response with a more convenient candidate while retaining the original provenance.

## Completion audit

`SupervisorCompletion` accepts only a CODEXIA-authored `WorkStatement`. The pilot additionally accepts `mode=complete` only when the checkpoint source received a non-null `latest_turn`, which by construction means the pre-cognition terminal durable event is the exact current worker turn.

That pre-cognition check is not sufficient by itself because cognition is a live provider call. Human or worker activity may arrive while Codexia is deciding whether the terminal worker result is complete. M6.6 therefore adds a second boundary immediately before the durable terminal transition:

```text
terminal exact worker turn
→ completion cognition
→ final exact live-peer reread   # external-state linearization point
→ no visible intervening activity
→ CAS against the pre-cognition supervisor sequence/digest
→ durable COMPLETED
```

If the final reread sees any new peer activity, that activity is durably recorded first and the completion judgment is discarded; the next loop requires fresh cognition over the new state. If another supervisor process advances the durable event chain after cognition, the compare-and-swap append loses and completion is retried only through fresh recovery/cognition.

This preserves:

```text
worker says done != work complete
old worker result + newer visible human/external activity != completion evidence
concurrent durable transition != stale completion permission
```

The final provider reread is the explicit cross-system linearization point: activity visible by that read precedes completion and invalidates it; activity that occurs after that read is logically after the completion boundary. This avoids pretending that SQLite and the remote conversation can be atomically committed together.

## Human precedence and answer audit

M6.5 continues to synchronize live peer state before claiming a prepared dispatch. M6.6 does not bypass that path.

M6.6 additionally allows an explicit human answer outside the worker ChatGPT conversation. The pilot answer transition requires exact `WAITING_HUMAN` state, a HUMAN-authored `WorkStatement`, the exact waiting sequence/event digest, and the exact M6.4 attention-decision digest. It is appended to the same supervisor hash chain as an external-observed pilot payload.

The transition performs only:

```text
WAITING_HUMAN → READY
```

It does not move the ChatGPT cursor, create a `ContinuationAdmission`, construct a dispatch, mint a provider lease, or grant local authority. A second or unsolicited answer in `READY` fails closed.

On recovery, the pilot extension revalidates the HUMAN statement and all waiting/attention bindings before accepting the transition. The exact terminal answer is then supplied separately to cognition as `latest_exact_pilot_human_answer`; it is not mislabeled as an M6.3 provider observation.

The existing M6.3 `EXTERNAL_USER` route also remains valid for real account-side human chat activity. Pilot cognition distinguishes the two evidence sources.

Fresh human evidence is prioritized near the front of the bounded M6.4 attention basis so large static handoff context cannot silently displace the answer that resumed the work. The original handoff identity remains unchanged.

This preserves:

```text
human answer != arbitrary execution authority
```

## Explicit attention-constraint audit

The cognition response must contain exactly one judgment for every exact HUMAN attention-constraint statement digest. Missing, duplicate, substituted, or foreign checks fail before `DynamicAttentionContext` is created, and M6.4 independently validates exact coverage again.

A `TRIGGERED` or `UNCERTAIN` explicit rule remains a hard M6.4 override even when cognition returns `KEEP_MOVING`. The pilot does not normalize an inconsistent attention payload: for example, an override that requires human attention together with `urgency=none` fails closed rather than silently rewriting the model judgment.

## Provider and replay audit

The pilot does not call ChatGPT worker transport directly. Worker continuation/revision still passes through M6.5 durable `DISPATCH_STARTED`, the ephemeral live lease, M6.3 before/send/after reconciliation, provenance tickets, and crash reconciliation.

Routine insufficient worker output can derive M6.2 `REVISE` and remain background work when M6.4 says `KEEP_MOVING`; the revision still uses the existing governed provider-dispatch path rather than creating a second transport authority.

The cognition provider call is a reasoning input, not a delegated-work side effect. Its response cannot itself mutate local files, Git, processes, network targets, or the worker conversation. A cognition failure therefore fails the current drive call without creating provider-dispatch retry permission.

The pilot may reuse an ephemeral cognition conversation within one process. That conversation is not authoritative or durable; every cognition request includes the exact durable delegated-work state required to recompute the judgment after restart.

## Runtime surface audit

`pilot_runtime.py` is thin composition over existing M6 components. `pilot_cli.py` exposes only:

- register a handoff against an existing conversation;
- drive a registered work with bounded `max_steps`;
- record one exact human answer for `WAITING_HUMAN` work;
- recover status.

It introduces no resident daemon, timer, notification channel, arbitrary shell, filesystem mutation, Git mutation, merge authority, or generic local-machine control.

## Audit conclusion

The M6.6 candidate adds the missing live cognition/product bridge without replacing the authority roots established in M6.1–M6.5:

```text
HUMAN handoff
→ live CODEXIA semantic judgment
→ existing M6.2 admission
→ existing M6.4 attention
→ existing M6.5 durable driver
→ exact M6.3 worker evidence
→ repeat / WAITING_HUMAN / final-live-reread + CODEXIA completion

WAITING_HUMAN
→ exact pilot HUMAN answer
→ READY only
→ fresh M6.2/M6.4 judgment before any next dispatch
```

The remaining milestone risk is product quality, not an intentionally widened authority boundary. M6.6 should remain incomplete until both real daily-use verticals are demonstrated.
