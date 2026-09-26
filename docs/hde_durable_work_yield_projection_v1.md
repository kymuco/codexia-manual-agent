# Durable Work Yield Projection v1

## Purpose

Expose one read-only Codexia-owned projection for the durable states accepted by
the current HDE Worker-return boundary, without importing or implementing HDE or
IRR semantics.

The projection observes one existing Work and returns exactly one ephemeral
classification:

\`\`\`text
COMPLETION
ATTENTION
NONE
\`\`\`

## Completion frontier

\`COMPLETION\` requires:

\`\`\`text
Work.state == COMPLETED
+
one exact projected WorkCompletion
+
work.completed is the exact current terminal chronology head
+
snapshot terminal_event_id == WorkCompletion.completion_id
\`\`\`

The projector does not create WorkCompletion and does not interpret it as
parent-HDE completion.

## Attention frontier

\`ATTENTION\` requires:

\`\`\`text
Work.state == ACTIVE
+
current Work chronology head == attention.need-declared
+
projected AttentionNeed exactly matches that head
\`\`\`

Any later durable Work event makes the older AttentionNeed non-current and the
projection becomes \`NONE\`.

The projector does not classify the AttentionNeed into any host/IRR need kind.

## NONE

\`NONE\` is not a durable Work state.

It means only:

\`\`\`text
the exact observed Work chronology does not currently expose
a guarded WorkCompletion or current-head AttentionNeed
\`\`\`

It does not mean:

- progress is safe;
- progress is required;
- progress is possible;
- a retry is permitted;
- the Work failed;
- the Work is blocked;
- the parent host should continue automatically.

Cancelled Work also projects \`NONE\` because cancellation is not one of the
Codexia durable Worker-return frontiers accepted by the current integration
contract.

## Restart

Projection is derived only from \`WorkStore.snapshot()\` and
\`WorkStore.events()\`.

Restart therefore performs no semantic step:

\`\`\`text
same durable Work
→ same exact chronology
→ same durable yield projection
\`\`\`

## Ownership

This surface imports only Codexia Work, Attention, and Completion semantics.

It owns no:

- HDE or IRR type;
- WorkerResult / WorkerNeed;
- authority or executor;
- provider selection;
- Workflow progression;
- capability progression;
- cognition progression;
- scheduler;
- retry policy;
- Work mutation.

The next bounded-progression slice may inspect this projection before and after
one existing Codexia progression transition. It must not weaken these ownership
boundaries.
