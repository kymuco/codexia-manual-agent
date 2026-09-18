# Simple Work v0

## Purpose

Simple Work v0 tests the smallest useful Codexia product loop before returning
to project automation.

The user gives one short request. One persistent Codexia conversation manages
one persistent worker conversation until the work is complete or a genuine
human decision is required.

The product question is intentionally simple:

> Can the user give Codexia one short task and then leave it alone until the
> task is finished or Codexia genuinely needs the user?

## Conversation model

Each work item owns exactly two ChatGPT conversations:

```text
Human request
    |
    v
persistent Codexia chat
    |
    | normal human-readable message
    v
persistent worker chat
    |
    | worker result
    v
same Codexia chat
```

Codexia talks to the worker the way a human normally would. A continuation may
be as short as:

```text
Да, давай продолжим.
```

or may contain a more detailed next instruction when the task needs it.

There is no model-facing JSON protocol and no M6 semantic-fit record.

Only two exact prefixes have runtime meaning:

```text
К ПОЛЬЗОВАТЕЛЮ: <question>
ГОТОВО: <final result>
```

Any other non-empty Codexia response is forwarded to the worker unchanged.

## Minimal durable state

The local SQLite row stores only operational continuity:

- work id and original user request;
- Codexia conversation id;
- worker conversation id;
- current status;
- last Codexia/worker text;
- the next worker message when a bounded run stops between cycles;
- pending human question or final result;
- worker-turn count.

The conversations remain the semantic memory of the work. Local state is not a
second semantic ontology for the conversation.

## CLI

Start one simple task and let it run for up to eight worker cycles:

```powershell
python -m codexia_manual_agent.simple_work.cli start `
    "Исследуй тему и дай мне хороший итог." `
    --auth-file auth_data.json
```

If the bounded cycle count is reached while Codexia still wants to continue:

```powershell
python -m codexia_manual_agent.simple_work.cli resume <work_id>
```

If Codexia genuinely needs the human:

```powershell
python -m codexia_manual_agent.simple_work.cli answer <work_id> "Мой ответ"
```

Inspect local state:

```powershell
python -m codexia_manual_agent.simple_work.cli status <work_id>
```

## v0 boundaries

Simple Work v0 deliberately does not attempt to solve:

- repository mutation, shell execution, Git write, merge, or other local authority;
- project-roadmap execution;
- multiple work items running concurrently;
- browser tab pooling;
- automatic recovery from an ambiguous provider write;
- replacement of the existing M2/M3 authority runtime;
- replacement or cleanup of the current M6 implementation.

Those are downstream questions. The first pilot is ordinary low-risk knowledge
work using only the two persistent ChatGPT conversations.

## Acceptance sequence

1. Run several ordinary simple tasks.
2. Observe whether Codexia naturally continues the worker without unnecessary
   human confirmation.
3. Exercise one real human-question boundary and resume the same two chats.
4. Only after the loop feels useful, try a small project with an existing
   roadmap.
5. Use the proven simple loop as the reference behavior for a later cleanup of
   the older M6 machinery.
