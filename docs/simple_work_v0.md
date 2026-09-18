# Simple Work v0.1

## Purpose

Simple Work v0.1 tests the smallest useful Codexia product loop before returning
to project automation.

The user should be able to give one short request and then leave Codexia alone
until the work is finished or Codexia genuinely needs the user.

## One long-lived Codexia chat

Codexia is no longer created per task. The local database owns one long-lived
Codexia session and reuses the same ChatGPT conversation across ordinary tasks.

```text
Task A -> same Codexia chat
Task B -> same Codexia chat
Task C -> same Codexia chat
```

Each task is still recorded locally with its own `work_id`, result and transcript.
The ChatGPT conversation provides natural semantic continuity; the local store
provides durable task history.

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

Это сообщение написано Codexia, а не пользователем. ...

Да, давай продолжим.
```

The visible `user` transport role therefore does not silently imply HUMAN
authorship. The banner is deliberately simple and human-readable rather than a
JSON authority envelope.

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

The first v0.1 task creates the long-lived Codexia conversation. Later `start`
commands reuse it automatically.

Inspect the shared Codexia session:

```powershell
python -m codexia_manual_agent.simple_work.cli codexia-status
```

If a persistent worker reaches the bounded cycle count, resume the same work:

```powershell
python -m codexia_manual_agent.simple_work.cli resume <work_id>
```

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

Simple Work v0.1 does not yet add:

- repository/process/Git mutation or local execution authority;
- automatic roadmap/project execution policy;
- multiple simultaneous work schedulers;
- a conversation-bound browser tab pool;
- automatic retry after ambiguous product writes;
- cleanup/replacement of the older M6 implementation.

The next validation target remains ordinary low-risk tasks. Only after this shape
feels natural should a small project with an existing roadmap be used.
