# M2.0 — Local Authority and Approval Contracts

## Goal

Establish the local trust contract that every future side effect must pass
through before Codexia gains shell, patch, Git mutation, network, or
outside-workspace execution.

M2.0 does **not** add any side-effect executor.

## Canonical lifecycle

```text
PROPOSED
   ↓ allow receipt
AUTHORIZED
   ↓ single-use authorization consumed immediately before execution
EXECUTED
   ↓ exact result captured
OBSERVED
```

A denied proposal takes the terminal path:

```text
PROPOSED → DENIED
```

The lifecycle object records state transitions only. It cannot itself run a
process, modify a file, call the network, or mutate Git.

## Proposal binding

Every action is represented by an immutable `ActionProposal` containing:

- proposal id;
- creation timestamp;
- required capability;
- local action name;
- workspace root;
- JSON-compatible parameters;
- optional human-readable summary;
- SHA-256 proposal digest.

Parameters are deep-snapshotted into immutable structures before the digest is
computed. Mapping key order does not affect the digest. NaN, infinity,
non-string object keys, and non-JSON values are rejected.

Risk is **not** supplied by the remote model. `ApprovalPolicy` derives it from
the locally selected capability.

## Capability-level risk

```text
read_workspace      → read_only
write_workspace     → workspace_mutation
execute_process     → process_execution
network_access      → network_access
git_commit/push     → external_git
delete_files        → destructive
outside_workspace   → outside_workspace
```

M2.0 is deliberately conservative. At capability level, both `always` and
`risky` require a human decision for every side effect. M2.1 may introduce a
narrower command classifier for additional safe subcases without weakening this
baseline contract.

## Approval modes

### `always`

- read-only action: local policy auto-authorizes;
- any side effect: explicit local human decision.

### `risky`

- read-only action: local policy auto-authorizes;
- write/process/network/Git/destructive/outside-workspace: explicit local human
  decision.

### `never`

- read-only action: local policy auto-authorizes;
- every side effect: local policy denies;
- passing `approved=True` cannot override this mode.

Changing the session mode is a separate future control-plane action. A single
proposal cannot override `never`.

## Authorization receipts

Every decision produces a digest-bound `AuthorizationReceipt`.

A receipt is bound to:

- proposal id;
- exact proposal digest;
- approval mode;
- decision;
- source (`policy` or `human`);
- actor;
- optional reason.

All M2.0 receipts are single-use.

For a side effect that requires a human decision, a policy-sourced allow receipt
is rejected even if its proposal digest matches. This prevents source
confusion between local policy and explicit human authority.

## One-shot consumption

Authorization is consumed **before** the future executor is invoked.

This creates the intended M2.1 sequence:

```text
proposal
→ approval decision
→ AUTHORIZED
→ consume receipt
→ invoke executor
→ record EXECUTED
→ capture exact observation
→ OBSERVED
```

If a process crashes after consumption, the same receipt is not reusable in the
same runtime process.

Durable consumption state, crash recovery, and replay-safe persisted receipt
journaling remain M3 responsibilities.

## Threat model

Digest binding protects proposal/receipt identity and detects accidental or
unsynchronized payload changes. It is not a signature against a malicious local
administrator. The trusted boundary is the local Codexia runtime and human
operator; the remote model is not an authority.

## Deferred to M2.1+

- actual process execution;
- command parsing/classification;
- environment and working-directory policy;
- patch/write execution;
- pre-execution human prompt UI;
- Git commit/push execution;
- network execution;
- durable receipt/event journal.
