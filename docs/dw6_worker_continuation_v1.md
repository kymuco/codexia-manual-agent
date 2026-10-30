# DW6.1 — Bounded Worker Continuation (first slice)

This extends **the existing** DW5/DW6 Worker ↔ Controller ↔ Gen2 workflow. No new
conversation or agent is created. Worker continuation is a **new model turn**,
not a command execution, project mutation, or an implied authorization to use
local tools. It does not remove DW5's human `APPROVE`, `EXECUTE`, or `POST`
boundaries.

## Why `no_commands` is not an automatic resume signal

`no_commands` means the latest Worker reply contains no local commands to
execute **now**. It says nothing authoritative about whether the project is
done or the next milestone is authorized. DW6 therefore stays `idle` until a
human explicitly supplies a non-secret, single-line continuation task,
restricted to one current Worker+Controller message pair, and confirms an
exact task-and-frontier digest.

DW6.1 adds a one-shot, project-scoped **Worker cognition grant** in the same
local registry database as DW5. An armed task changes the DW6 frontier from
`idle` to `continue_worker`. Only an explicit `advance --continue-worker`
performs one CWA send to the **existing Worker conversation**. The resulting
canonical assistant message must match the send receipt. The Worker then
becomes ahead of Controller and ordinary DW6 shows `interpret`. The next
commands, if any, pass through Controller, review and human execution approval
as before. No automatic execution, no resident service, no repeated sends.

## Durable phases and failure model

- `armed`: human-authorized, exact task + exact source Worker & Controller IDs
  and their assistant text SHA-256.
- `send_admitted`: committed **before** CWA send; never automatically retried.
- `worker_known`: CWA returned an assistant ID but readback is not yet proven;
  also blocks any automatic retry.
- `verified`: exact canonical readback matched the returned Worker message.
- `cancelled`: reserved for explicit pre-send cancellation; not a way to deny
  a possibly already-sent turn.

A non-verified external send remains a manual reconciliation event. Both
`send_admitted` and `worker_known` show
`blocked_ambiguous_worker_send`. The grant stores one human-provided task in
**plaintext local SQLite**. Do not put credentials or secrets in the task.
DW6.1 does not infer task completion from free-form Worker prose, give
permanent delegated authority, or start unlimited cognition loops.

## Safe operator pilot

Run from the Codexia DW6 branch with its existing `PYTHONPATH` and CWA auth.
First inspect:

```powershell
& $python -m tools.dw6_governed_work_loop_v1 status --project player-intelligence --auth-file "$cwaRoot\auth_data.json"
```

Only if status is exactly `idle` and the latest Controller decision is
`no_commands`, arm an explicit bounded task (example; replace with actual
operator-authorized scope):

```powershell
& $python -m tools.dw6_worker_continuation_v1 arm `
  --project player-intelligence --auth-file "$cwaRoot\auth_data.json" `
  --worker-message-id "<exact current Worker assistant UUID>" `
  --controller-message-id "<exact current Controller assistant UUID>" `
  --task "Perform R1d.1 admission audit only. Do not start R1d.2."
```

Review the exact task and type `ARM <digest-prefix>` only if intended. Then
`DW6 status` must show `continue_worker`, which is **not** a shell grant.

```powershell
& $python -m tools.dw6_governed_work_loop_v1 advance `
  --project player-intelligence --auth-file "$cwaRoot\auth_data.json" `
  --model gpt-6 --reasoning-effort high --continue-worker
```

On `WORKER_CONTINUED_READBACK_VERIFIED`, inspect `DW6 status`; the next action
is normally `interpret`. If CWA timed out or status says ambiguous, stop;
**never replay** the `--continue-worker` command automatically. Nothing
modifies files or executes terminal commands during the cognition step.

## Verified continuation history is not an active grant (2026-10-30)

After Controller observed-send reconciliation, a real DW6 status returned
`blocked_worker_frontier_not_advanced` even though the current Controller was
ready with a new `no_commands` decision and the Worker had not moved. The
Worker-continuation overlay had inspected the latest historical grant; it
blocked whenever phase was `verified` without comparing its Controller ID to
the new ready Controller frontier.

The overlay now blocks reuse of a verified grant only for the **same**
Controller message. A verified grant for an earlier Controller is historical:
with a different, verified current Controller, the overlay leaves the idle
frontier untouched. In contrast, old `send_admitted` or `worker_known`
entries continue to block because their delivery may be ambiguous. No task
is armed and no Worker model call is made by this read-only determination.

A new Worker continuation still requires a separate, explicit human task,
exact Worker/Controller identity and confirmation. This change does not grant
automatic sends or weaken the single-use database key.
