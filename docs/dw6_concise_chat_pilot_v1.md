# DW6 concise conversation pilot v1 (2026-10-31)

**Scope: interaction only.** This is a draft opt-in evaluation of the chat
surface after DW6.1 engineering closure, not a new execution authority.

## Problem verified in the player-intelligence transcript

- The continuing Worker already has an existing long project conversation, yet
  every continuation repeated repo path, model message IDs, and task SHA.
- Work-result posts repeated Work ID, source ID, plan hash, per-stream hashes and
  byte counts even though those are already tracked by the local Work events.
- The Worker often restated already-known results. The original human exchange
  is in Russian, but Codexia's English wrapper can contaminate response language.

## Exact changes

1. DW6 Worker continuation sends the **bounded human task**, reminders not to
   repeat completed checks, conversational language and concise-result guidance.
   It retains the explicit restriction on local effects, but omits duplicate
   project path, repository, message ID and SHA. All checks happen against
   unchanged durable registry fields **before** the call.
2. DW5 result handoff sends **state + exit code + actual nonempty terminal
   preview**. No Work ID, hashes, byte counters, or empty stderr lines are
   inserted into the chat. Unusual termination is still shown. Large text is
   clipped at the preexisting 8k characters per stream, explicitly marked
   as shortened; messages exceeding the preexisting 13k overall guard are
   rejected. Local Work evidence and the post's full-message digest remain.
3. Both messages instruct the model to answer in the human conversation's
   language, and focus on the result and next necessary decision.

## Nonclaims / unchanged boundaries

- This is **prompt guidance**, not an independently verified automatic
  language detector. A durable per-project language override is future work.
- Removing technical audit fields does **not** mean raw output can always be
  omitted. Until evidence retrieval is available to the Worker, meaningful
  stdout and stderr must still reach it (within current safety limits).
- This does **not** improve independent strategic decision making; Controller
  remains a command interpreter.
- No new model sends, shell executions, SQLite schema mutations, Work posts
  or changes to human approval, stale-frontier fences, or canonical readback.
- `_result_message` is shared by the existing DW5 post path; verify its
  changed text in tests before live opt-in, including receipt/recovery behavior.

## Concrete contrast: R1d.1

Legacy continuation: task plus repo/workspace/previous assistant UUID and SHA.
New continuation: the task and short behavioral boundaries only.

Legacy result: Work UUID, SHA-256 plan, stdout byte/hash, stderr byte/hash,
step digest, then stdout.
New result: `Step 1: succeeded; exit_code=0`, actual nonempty stdout, and
one short instruction to avoid restating it.

**Acceptance before shipping:** offline regression on all DW5/DW6 boundary
tests, entire CI, and then one separately human-authorized live roundtrip in
a **pilot** conversation. Judge language, answer length, information loss,
decision quality and need for human attention. Do not change main or auto-run
the live pilot without explicit permission.
