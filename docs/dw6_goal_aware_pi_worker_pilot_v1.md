# DW6 goal-aware Worker pilot — Player Intelligence

## Purpose

The human wants Codexia to **develop Player Intelligence**, not merely
report on it, while real PI development supplies feedback to improve Codexia.
The goal is a useful Genshin account-level progression advisor, using existing
PI code and UI. The old DW6 R1d.1 task scope made Codexia repeat small read-only
audits; a separate A/B run showed that supplying explicit product intent led
to an arguably more product-oriented decision. The result was not a reliable
proof of superior engineering judgment.

## Smallest existing-runtime change

The existing DW6.1 Worker continuation reads ONE optional, local, UTF-8 file
from the registered project's workspace:

`.codexia/PROJECT_BRIEF.md`

Its contents accompany the next **already human-authorized Worker cognition
turn** as *project-goal reference*, separated from the current bounded task.
The text is bounded to 8192 bytes, not loaded from arbitrary user-selected
paths, and symlinks are rejected. An absent file is backward-compatible.

No other long historical handoffs are automatically injected. Keep the
document short and stable; consult source/historical files only when needed.

## Set up the real PI pilot (operator action, not an automatic effect)

Use the already drafted `PLAYER_INTELLIGENCE_PROJECT_BRIEF.md` (a 2026-10-31
context document), review it and place a copy at:

`W:\\dev\\player-intelligence\\.codexia\\PROJECT_BRIEF.md`

For example, from PowerShell after downloading the brief:

```powershell
$targetDir = "W:\\dev\\player-intelligence\\.codexia"
New-Item -ItemType Directory -Force -Path $targetDir | Out-Null
Copy-Item -LiteralPath "$HOME\\Downloads\\PLAYER_INTELLIGENCE_PROJECT_BRIEF.md" `
  -Destination (Join-Path $targetDir "PROJECT_BRIEF.md") -ErrorAction Stop
```

This is an intentional human-managed file copy; Codexia does not silently
create or mutate PI workspace files. Treat this draft as goal context, not as
evidence of latest repository HEAD or completed account features.

## First real Worker task (scope example)

The operator may authorize exactly one bounded cognition turn with a
product-oriented task, for example:

> Продолжи разработку Player Intelligence к работающему Progression Advisor.
> Учитывай PROJECT_BRIEF. Используй текущие факты и существующий код.
> Выбери одно полезное реализуемое улучшение продукта вместо повторного
> аудита, предложи необходимые конкретные команды и проверку результата.
> Не начинай R1d.2 и не выполняй действия без отдельного разрешения.

Do **not** copy this task directly into a terminal without matching the
exact current DW6 idle frontier and completing the existing `arm` /
`continue_worker` grant. A Worker instruction to implement is not approval
for shell, Git, network changes, or access to a real account. The existing
Controller still only interprets commands; DW5/DW6 human approvals remain.

## What counts as useful real-world feedback

Judge the Worker response and subsequent governed implementation by:

- whether it selects a user-visible product improvement, not another
  standalone no-op audit;
- whether it reuses current pipelines and avoids duplicate architecture;
- whether it names one falsifiable completion criterion and proposes
  proportionate implementation/tests;
- whether it avoids inventing support for unverified character mechanics;
- whether it reduces unnecessary questions and can proceed through the
  already proven Worker → Controller → approval → execution → Worker loop.

Neither a proposed step nor a successful approval/command constitutes
product completion. For actual PI progress, inspect the resulting code
change, Web/API behavior and valid source evidence.

## Current limits

- This PR does **not** add automatic task selection, a self-running scheduler,
  a new decision-maker, goal version pinning across an already armed turn,
  or a new authority to edit PI.
- Because the Workspace brief is read at model send, avoid editing it between
  arming and continuation. The bounded task/approval itself is still
  unchanged. Goal pinning may be justified later by a proven conflict.
- A local brief is presently re-sent on each approved Worker continuation
  (CWA has no separate supported persistent system-prompt channel).
  Keep it concise. Do not duplicate historical handoffs or Work UUIDs.
- Local CI validates formatting, exception paths and no-send fences; a live PI
  continuation requires separate operator approval and is not claimed here.

This PR is stacked on the concise-chat pilot PR #108. Evaluate/merge the
base before the goal-aware layer, without changing Controller semantics.
