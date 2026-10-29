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

These flags are **not approvals by themselves**. The DW5 human prompts are
retained: exact `APPROVE <digest>`, `EXECUTE <digest>`, or `POST <digest>` must
still be typed. `--reject` records a human rejection in the same way. `--post`
must be a **separate invocation after inspecting execution observations**.
`--interpret` uses the same saved Controller conversation and DW5's durable
pre-send fence, never an unrelated chat or an automatic retry.

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

The pure frontier classifier and its deny-by-default cases are tested locally
and in CI. Project-level Windows/CWA live validation is still required for this
new one-command operator; DW5's earlier live E2E does not automatically prove
DW6's new orchestration. The operator reuses existing DW5 APIs; there is no new
schema or authority bypass. Known plaintext non-secret local input caching and
single-literal `Read-Host` constraints remain DW5 limits.
