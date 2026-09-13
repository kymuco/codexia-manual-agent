# M6.6 Source Audit — First General Daily-Use Pilot

## Audit target

M6.6 connects live cognition to the already-governed M6.1–M6.5 work loop. The audit asks whether this convenience layer accidentally creates a new semantic authority, execution authority, completion authority, human-impersonation path, or replay path.

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

`SupervisorCompletion` accepts only a CODEXIA-authored `WorkStatement`. The driver forwards it through the existing `BackgroundWorkSupervisor.complete(...)` boundary.

The pilot adds a stronger evidence rule: `mode=complete` is accepted only when the driver supplied a non-null `latest_turn`, which by construction means the terminal durable event is the exact current worker turn.

```text
worker says done != work complete
old worker result + newer external activity != completion evidence
```

A completion claim before worker evidence, or after a newer human/external event, fails closed.

## Human precedence and resume audit

M6.5 continues to synchronize live peer state before claiming a prepared dispatch. M6.6 does not bypass that path.

When work is `WAITING_HUMAN`, only a fresh M6.3 `EXTERNAL_USER` observation can resume it. That exact observation is durably recorded and recovered for the next pilot cognition checkpoint. The observation is included in the exact prompt and is prioritized near the front of the bounded M6.4 attention basis so large static handoff context cannot silently displace the fresh human answer.

The original handoff identity remains unchanged.

## Explicit attention-constraint audit

The cognition response must contain exactly one judgment for every exact HUMAN attention-constraint statement digest. Missing, duplicate, substituted, or foreign checks fail before `DynamicAttentionContext` is created, and M6.4 independently validates exact coverage again.

## Provider and replay audit

The pilot does not call ChatGPT worker transport directly. Worker continuation/revision still passes through M6.5 durable `DISPATCH_STARTED`, the ephemeral live lease, M6.3 before/send/after reconciliation, provenance tickets, and crash reconciliation.

The cognition provider call is a reasoning input, not a delegated-work side effect. Its response cannot itself mutate local files, Git, processes, network targets, or the worker conversation. A cognition failure therefore fails the current drive call without creating provider-dispatch retry permission.

The pilot may reuse an ephemeral cognition conversation within one process. That conversation is not authoritative or durable; every cognition request includes the exact durable delegated-work state required to recompute the judgment after restart.

## Runtime surface audit

`pilot_runtime.py` is thin composition over existing M6 components. `pilot_cli.py` exposes only:

- register a handoff against an existing conversation;
- drive a registered work with bounded `max_steps`;
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
→ repeat or CODEXIA completion
```

The remaining milestone risk is product quality, not an intentionally widened authority boundary. M6.6 should remain incomplete until both real daily-use verticals are demonstrated.
