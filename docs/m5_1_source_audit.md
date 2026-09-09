# M5.1 — Frozen Automation Plan Source Audit

## Audit question

M5.1 must make the future automation envelope immutable before any automated M4 run work while preserving the existing M2 authority boundary.

Primary invariant:

```text
automation intent != execution authority
```

Temporal invariant:

```text
frozen automation context/budget/stop policy before automated work
!=
context/budget/stop policy selected after work has started
```

## Threat-to-evidence map

| Threat | M5.1 evidence |
| --- | --- |
| Automation starts before its budget is fixed | `SqliteAutomationPlanRegistry.register_plan()` requires both arm chronologies to remain exactly at the M4.4 policy-freeze anchors and contain no runs. |
| Run registration races plan freeze | Plan registration and M4.2 run registration serialize through `BEGIN IMMEDIATE` in the same SQLite writer domain. One operation wins; the other observes the resulting chronology. |
| A larger or more favorable budget is selected later | One deterministic `automation_id` is derived from one frozen policy, and `policy_id` is unique in the durable plan table. A different budget conflicts with the already-frozen identity. |
| Automation changes cwd/workspace after seeing intermediate state | Canonical existing `workspace_root` is part of `AutomationPlan` and `plan_digest`; a different workspace under the same policy conflicts with the one v1 automation identity. |
| Frozen workspace disappears or is moved | Recovery strictly resolves the persisted canonical path and fails closed rather than substituting another workspace. |
| Automation declares more runs than the scientific comparison permits | `max_runs` cannot exceed `2 * len(policy.seeds)`, the exact baseline+candidate run set declared by M4.4. |
| Automation disables the external authorization stop | M5.1 v1 requires `pause_on_authorization_required=true`; `false` is structurally invalid. |
| Automation ignores failures, budget exhaustion, or an existing conclusion | M5.1 v1 requires `stop_on_error`, `stop_on_budget_exhaustion`, and `stop_on_conclusion` all be `true`. |
| Automation targets an execution profile Codexia has not proven | Both manifests must pass `PythonJsonExperimentSpec.from_manifest()`, admitting only the existing `python-inline-json-result.v1` M4.3 profile. |
| Persisted plan text is altered | Canonical JSON, exact row indexes, `plan_digest`, `freeze_digest`, policy recovery, profile admission, workspace recovery, and event anchors are all revalidated. |
| Plan itself mints authority | `SqliteAutomationPlanRegistry` exposes plan registration/recovery only. M5.1 contains no process proposal, approval decision, authorization receipt, receipt consumption, executor, mutation, Git, network, or scheduler API. |

## Workspace finding

The first M5.1 draft bound policy, manifests, budgets, and stop rules but not workspace.

That was insufficient because M4.3 `GovernedPythonJsonRunner.prepare()` takes an explicit workspace and the resulting M2 process proposal binds that cwd. Inline Python can also observe relative filesystem state. Therefore:

```text
same manifest + same policy + different workspace
!=
necessarily the same execution context
```

The final M5.1 design freezes the canonical existing workspace path before automated work. This closes automation-side workspace selection after intermediate evidence is visible.

This does **not** claim that M5.1 snapshots or attests every byte already present in that directory. Ambient workspace-content dependence remains governed by the existing M4.3 execution/evidence model and is not silently upgraded into a stronger scientific reproducibility claim here.

## Python executable boundary

M5.1 does not add a caller-selectable Python executable field.

M4.3 already admits only the current exact `sys.executable` path through `_admitted_python_executable()`, and the resulting M2 process proposal binds/revalidates the executable identity before execution. M5.2 should reuse that path by calling the existing runner rather than adding a second interpreter-selection surface.

Therefore no new automation choice is needed at M5.1.

## Budget semantics

`max_runs` is a ceiling, not a promise that a complete M4.4 comparison will always be reached.

A plan may deliberately set `max_runs` below the complete comparison requirement. That future execution must stop as `BUDGET_EXHAUSTED`; it must not silently compare a favorable partial subset because M4.4 already requires complete sealed evidence under its own policy.

This distinction is intentional:

```text
bounded automation may stop early
!=
partial evidence may be promoted to a complete comparison
```

## Durable ordering

At successful registration the latest event in each arm must be exactly the event already anchored by `FrozenComparisonPolicy`. Since M4.4 itself freezes only before any run exists, M5.1 thereby proves its control envelope existed before the first run-registration event.

After registration, valid later run events are allowed. Recovery checks that the old freeze anchor still exists at the same sequence with the same id/digest rather than requiring the arm chronology to remain frozen forever.

This separates:

```text
plan was frozen before work
```

from:

```text
work is forbidden after plan freeze
```

The former is required; the latter would defeat automation.

## Explicit limits

M5.1 does not prove or provide:

- autonomous process execution;
- automatic approval or policy-generated `ALLOW` receipts;
- a scheduler or background worker;
- automatic experiment/run creation;
- automatic comparison or conclusion publication;
- workspace-content snapshotting or hostile-machine attestation;
- general arbitrary-Python sandboxing beyond the existing M2/M4 boundaries;
- automatic hypothesis or comparison-policy selection;
- external write, Git, network, or delegation authority.

## Closure criterion

M5.1 is complete only if the exact reviewed candidate demonstrates:

> The exact automation target, canonical workspace, finite run/step budget, and mandatory stop rules are frozen before any run work, remain bound to one exact M4.4 policy, reject post-hoc plan shopping, and introduce no path by which automation can mint execution authority.
