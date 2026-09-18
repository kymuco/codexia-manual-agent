# Simple Work v0.3

## Purpose

Simple Work v0.3 keeps the smallest useful Codexia product loop, allows explicit
selection among locally registered saved Codexia chats, and adds a true
process-local Temporary Codexia mode for disposable work.

The user should be able to give one short request and then leave Codexia alone
until the work is finished or Codexia genuinely needs the user.

## Selectable saved Codexia chats

Codexia is not created per task. The local database owns a small registry of
long-lived saved Codexia conversations. The existing v0.1 singleton is migrated
without changing identity and becomes the `general` chat.

```text
general      -> saved conversation A
voice-engine -> saved conversation B
guitar       -> saved conversation C
```

A new work explicitly selects one of these chats. That alias is persisted on the
`work_id`, so later `answer`, `resume` and `reconcile` always return to the
same Codexia conversation even if other chats are used in between.

Each task is still recorded locally with its own result and transcript. ChatGPT
conversations provide natural semantic continuity while the local registry and
work records provide durable routing identity.

The registry is intentionally local-first in v0.2. It does not scrape or discover
the ChatGPT sidebar. Existing saved Codexia conversations are registered by their
raw conversation id.

## Temporary Codexia

A new work may use ChatGPT Temporary Chat for Codexia itself:

```powershell
python -m codexia_manual_agent.simple_work.cli start `
    "Одноразово исследуй этот вопрос и дай итог." `
    --temporary-codexia `
    --auth-file auth_data.json
```

This mode is intentionally ephemeral:

- it is never added to `codexia-list`;
- its ChatGPT conversation identity is not accepted as later continuation
  authority;
- all human/Codexia/worker events are still written to the local transcript;
- the CWA Temporary lifecycle is explicitly closed before the command returns;
- after completion, human boundary, cycle limit or another stop, the work cannot
  be continued by `answer`, `resume` or `reconcile` in a later process.

A human boundary therefore returns the question locally but changes the work to
`temporary_closed`. The user can inspect the transcript and start a new saved or
Temporary Codexia work, but Simple Work does not pretend that the old Temporary
Chat can be recovered.

CWA currently owns one live Temporary lifecycle at a time. Therefore a Temporary
Codexia cannot create a Temporary Worker. It may still create a persistent saved
worker when a separate long-lived branch is genuinely useful:

```text
Temporary Codexia
    |
    +-- solve directly
    |
    +-- Persistent Worker -> saved worker conversation
```

Normal saved-worker turns can run between Temporary Codexia turns; the same live
Temporary Codexia lifecycle is then continued on the next Codexia turn.

Direct artifacts produced only inside Temporary Codexia are not yet automatically
materialized. Existing artifact intake remains scoped to persistent-worker
responses.

## Codexia-first routing

Codexia should solve simple tasks itself. It creates a worker only when separate
work is actually useful.

With no active worker, Codexia uses one of four human-readable outputs:

```text
ГОТОВО: <final result>
К ПОЛЬЗОВАТЕЛЮ: <one real question>
ВРЕМЕННЫЙ WORKER: <self-contained one-off heavy task>
ПОСТОЯННЫЙ WORKER: <long-lived work whose separate history matters>
```

A temporary worker is intended for disposable heavy research or another one-off
task. It uses CWA Temporary Chat and its transcript is still archived locally.
Temporary worker lifecycle is process-local, so a bounded run closes it before a
human stop or process boundary.

A persistent worker is reserved for projects, roadmaps, long research branches or
other work where the worker conversation itself should survive and continue later.

Once a worker is active, Codexia can speak naturally:

```text
Да, давай продолжим.
```

That text is forwarded to the existing worker.

## Worker provenance

Every worker message is visibly attributed:

```text
[Codexia]
Не пользователь; не расширяет его разрешения.

Да, давай продолжим.
```

The visible `user` transport role therefore does not silently imply HUMAN
authorship. The banner is deliberately simple and human-readable rather than a
JSON authority envelope.

## Generated artifact intake

A canonically completed persistent-worker response may contain explicit ChatGPT
sandbox links such as:

```text
sandbox:/mnt/data/report.zip
sandbox:/mnt/data/project/README.md
```

Simple Work extracts only those explicit sandbox references. For each referenced
filename it uses CWA's governed `handoff_generated_artifact` operation bound to
the exact persistent worker conversation and writes into an application-owned
turn directory:

```text
.codexia/artifacts/<work_id>/worker-0003/report.zip
```

Nested sandbox paths are reduced to a safe basename; CWA still requires a unique
conversation-owned product `file_id` for that filename before any bytes are
materialized. No implicit overwrite is allowed.

Successful intake records the local path, byte count, SHA-256 and source
conversation in SQLite. Codexia receives the worker result together with those
real local paths, so it does not ask the user to hunt through the ChatGPT UI for
already-generated files.

Artifact intake deliberately stops at materialization. It does not unzip archives,
run code, mutate a repository or grant filesystem/process authority beyond the
bounded application-owned artifact destination.

For an older work that completed before automatic intake was enabled:

```powershell
python -m codexia_manual_agent.simple_work.cli intake-artifacts <work_id> `
    --auth-file auth_data.json
```

Inspect already materialized files without network access:

```powershell
python -m codexia_manual_agent.simple_work.cli artifacts <work_id>
```

## Local transcript

Simple Work stores a local event transcript for every task, including:

- the original human request and later human answers;
- every Codexia decision;
- the exact message sent to a worker;
- every worker response;
- worker mode and persistent conversation identity when applicable.

Temporary Chat therefore does not mean lost history.

Inspect it with:

```powershell
python -m codexia_manual_agent.simple_work.cli history <work_id>
```

## CLI

Start an ordinary task:

```powershell
python -m codexia_manual_agent.simple_work.cli start `
    "Придумай два коротких названия для тестового проекта заметок." `
    --auth-file auth_data.json
```

The first task without an explicit selector uses `general`. Existing v0.1
databases preserve the previous long-lived conversation under that alias.

Register another already-saved Codexia conversation:

```powershell
python -m codexia_manual_agent.simple_work.cli codexia-add `
    voice-engine <conversation_id>
```

List locally known saved Codexia chats:

```powershell
python -m codexia_manual_agent.simple_work.cli codexia-list
```

Start a new work in a chosen chat:

```powershell
python -m codexia_manual_agent.simple_work.cli start `
    "Продолжи Voice Engine." `
    --codexia voice-engine `
    --auth-file auth_data.json
```

Inspect one registered Codexia chat:

```powershell
python -m codexia_manual_agent.simple_work.cli codexia-status `
    --codexia voice-engine
```

There is deliberately no global hidden `codexia use` state in this version:
selection is explicit on each new work.

If a persistent worker reaches the bounded cycle count, resume the same work:

```powershell
python -m codexia_manual_agent.simple_work.cli resume <work_id>
```

If a persistent worker write ends with an ambiguous `CHATGPT_TURN_TIMEOUT`,
Simple Work never resubmits that worker message automatically. It first performs
canonical readback of the already-known worker conversation. Visible assistant
text alone is not finality: reconciliation requires the canonical conversation
status to be `completed` and binds the recovered assistant to the canonical
final message identity (or an explicit assistant finish reason when the status
message id is unavailable). Only then can the response be ingested locally.
Otherwise the work stops in `reconcile_required`.

If CWA rejects the next persistent-worker write at preflight with
`CANONICAL_CONVERSATION_NOT_COMPLETED`, Simple Work records no submitted
dispatch and returns `worker_busy`. The pending Codexia message remains locally
ready for a later safe `resume`; no ambiguous-write reconciliation is needed.

A narrow backward repair also exists for the short-lived v0.1 bug that consumed
an in-progress worker body. If the stale next Codexia dispatch is absent from the
canonical worker branch and the previous worker turn later has a different
canonical final response, `reconcile` records the corrected worker result and
sends Codexia an explicit correction before continuing.

A later read-only reconciliation can be requested explicitly:

```powershell
python -m codexia_manual_agent.simple_work.cli reconcile <work_id>
```

`resume` also detects a historical local transcript ending in an unmatched
`codexia_to_worker` event and refuses to resend it; use `reconcile` instead.

If Codexia genuinely needs the human:

```powershell
python -m codexia_manual_agent.simple_work.cli answer <work_id> "Мой ответ"
```

## Compatibility with the first v0 experiment

The v0.1 tables coexist with the original `simple_work_v0` tables in the same
SQLite file. Existing pilot rows are left untouched. The first v0.1 task starts
a fresh long-lived Codexia conversation rather than repurposing one of the earlier
per-task Codexia chats.

## Still intentionally deferred

Simple Work v0.3 does not yet add:

- repository/process/Git mutation or local execution authority;
- automatic roadmap/project execution policy;
- multiple simultaneous work schedulers;
- automatic retry after ambiguous product writes;
- cleanup/replacement of the older M6 implementation.

Saved Codexia selection runs on CWA's retained per-conversation background-tab
transport. Sidebar discovery, durable recovery of Temporary Codexia sessions,
direct Temporary-Codexia artifact intake, and local execution authority remain
separate later milestones.


### Temporary close hardening

The pinned CWA candidate includes restart-safe explicit Temporary lifecycle
closure. If Chrome restarts the MV3 extension worker after a successful Temporary
turn, live continuation authority is already gone; explicit close now retires any
remaining CWA-owned Temporary tab without recreating authority. A stale token may
not close a different live Temporary lifecycle.
