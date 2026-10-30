# DW6 — governed Work loop v1 (first slice)

DW6 does **not** introduce another reasoning agent, interpreter, process host,
controller chat, or separate execution authority. This first increment provides
one project-scoped operator that **reconstructs the next permissible transition**
from DW5's existing persisted project binding, Controller turn fence, Gen2 Work
chronology, and the two latest CWA assistant messages.

It performs **at most one transition per invocation**. It does not run in the
background or watch chats; that is later scope. This makes restart/resume
explicit and audit-friendly rather than adding a hidden agent loop.

## Commands

In the repo root, with `PYTHONPATH` set to the checkout `src` and the connected
CWA `src` as in DW5, run:

```powershell
$python = "W:\dev\codexia-m66-pilot\.venv-m66\Scripts\python.exe"
$auth = "W:\dev\chatgpt-web-adapter\auth_data.json"
& $python -m tools.dw6_governed_work_loop_v1 status --project player-intelligence --auth-file $auth
& $python -m tools.dw6_governed_work_loop_v1 advance --project player-intelligence --auth-file $auth
```

`status` only observes; `advance` without a privileged flag can **capture** a
new immutable Gen2 Work or create its **human review request**. After each
transition rerun `status` or `advance`. No `run`, `post`, or model send occurs
merely because the user chose `advance`.

When `next_action` is `await_approval`, `await_execution`, `await_post`, or
`interpret`, an unflagged `advance` returns `WAITING_FOR_EXPLICIT_HUMAN_ACTION`.
Only then, and only for that exact frontier, use the matching flag:

```powershell
& $python -m tools.dw6_governed_work_loop_v1 advance --project player-intelligence --auth-file $auth --approve
& $python -m tools.dw6_governed_work_loop_v1 advance --project player-intelligence --auth-file $auth --execute
& $python -m tools.dw6_governed_work_loop_v1 advance --project player-intelligence --auth-file $auth --post
& $python -m tools.dw6_governed_work_loop_v1 advance --project player-intelligence --auth-file $auth --interpret
```

For local execution and posting, these flags are **not sufficient approval**.
The DW5 prompts remain mandatory: exact `APPROVE <digest>`,
`EXECUTE <digest>` and `POST <digest>` must be entered separately.
`--reject` also requires explicit confirmation; `--post` must be a
**separate invocation after inspecting execution observations**.
By contrast, `--interpret` itself is the explicit Controller model-send
action: there is **no second built-in Read-Host confirmation** inside that
command. The operator must authorize it as one exact turn, preferably
through a guarded outer script. The durable pre-send fence prevents
blind retries, not the initial send.

## Recovery and stop rules

- If a Controller send has phase `send_admitted` or `controller_known`, or its
  assistant message no longer matches the durable registry: **stop**.
- If the Worker advances before a captured Work is approved, observed and
  posted: **stop**. Never execute/post a stale command from that Work.
- A human `REJECT` always blocks execution. New work needs a new Controller
  interpretation, not a changed command inside the rejected Work.
- Any non-success or unknown capability outcome blocks repeat execution.
- `external.shell-result-handoff` without its readback receipt is **ambiguous**:
  do not repost.
- A recorded verified `post_receipt` allows interpretation of a **new** Worker
  reply only when the latest assistant message differs from the previously
  interpreted message.
- Identity lookup uses DW5's exact `shell:<controller conversation>:<message>`
  ingress key, with read-only SQLite; it never guesses a Work by recency.

Only CWA's latest assistant message is read; this first slice is deliberately
not a generic polling daemon, scheduler or multi-ChatGPT-account monitor.

## Evidence and limitations

The frontier classifier and fail-closed cases are covered by CI. On
2026-10-30, a human-approved live `player-intelligence` cycle verified one
Worker continuation with canonical readback, then one Controller
interpretation (decision=`no_commands`, steps=0), and finally
`controller_phase=ready`, `worker_advanced=false`, `work_id=null`,
`next_action=idle`. No local execution, Work post, or automatic model retry
occurred. The CWA Direct HTTP transport fix is independently covered by CI;
the live turns were not duration-instrumented, so there is **no claim of a
measured stream longer than 60 seconds**.

Known boundaries remain: `send_admitted` or `controller_known` after an
ambiguous Controller write, `send_admitted` or `worker_known` after an
ambiguous Worker write, and an unreceipted Work post all **block** subsequent
sends. There is no generic automatic reconciliation mechanism. The narrow
2026-10-30 observed Controller-send operator requires the exact previous
two presend recovery archives and exact reconstructed prompt; it cannot
reconcile arbitrary future send timeouts or `controller_known` states.
Likewise, Worker `worker_known` is not automatically cleared merely because
a later GET seems promising. These are deliberate stop states requiring
new operator-reviewed tooling/evidence, never a cleared fence or blind retry.

The DW6 v1 dispatch reuses existing DW5 APIs and does not create a second
execution authority. DW6.1 separately adds a durable one-time Worker grant.
Known plaintext non-secret local input caching and single-literal
`Read-Host` constraints remain DW5 limitations.
