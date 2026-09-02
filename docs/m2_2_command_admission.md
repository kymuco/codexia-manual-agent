# M2.2 — Command Admission and Model-to-Process Governance

## Purpose

M2.2 defines a local admission boundary for narrowly specified process intents.
It does **not** give the remote model arbitrary process execution and it does not
weaken M2.1 human authorization.

The model may name a known command family. Local code constructs the exact argv,
derives risk and required capabilities, and decides whether the request can become
an M2.1 action proposal. Canonical executable identity remains owned by M2.1 when
a family uses a bare host command token such as `git`.

```text
model process intent
→ strict process_request parser
→ known command family
→ exact family argument schema
→ local argv construction
→ local capability envelope
→ local command risk
→ admission verdict
→ approval preview
→ optional M2.1 ActionProposal
→ STOP at human authorization boundary
```

The existing M1.1 `ReadOnlyAgentLoop` remains unchanged and still accepts only
`read_file`, `list_files`, `search_text`, and `git_status`.

## Process-request protocol

M2.2 defines a separate parser for future model-process requests:

```json
{"type":"process_request","request_id":"proc-1","family":"python_version","arguments":{}}
```

The object accepts exactly four fields. A model cannot provide any of the
following authoritative material:

- executable path;
- raw argv;
- command risk;
- required capabilities;
- approval mode;
- approval decision;
- proposal or receipt digests/identifiers.

Unknown families fail closed. Family-specific arguments are validated locally.
The initial M2.2 families accept no model-controlled arguments.

## Capability envelope

A command name is not sufficient evidence of safety. Each family has a locally
constructed `CapabilityEnvelope` describing the minimum capabilities needed and
whether downstream authority is actually bounded.

The default future model-process admission surface contains only
`execute_process`.

### `python_version`

Local argv:

```text
<current absolute Python executable> --version
```

Envelope:

```text
execute_process
bounded=true
risk=diagnostic
```

Verdict: `admit_requires_human`.

### `git_version`

Local argv:

```text
git --version
```

M2.2 intentionally does **not** call an unfiltered `shutil.which("git")` or turn
that token into an absolute executable path. The M2.1 proposal builder owns
canonical host-executable resolution. Its search PATH excludes workspace entries,
and every canonical result of **bare-name** resolution is rejected if it lands
inside the workspace anyway. The second check is independent of why the OS found
the executable, so Windows current-directory search semantics cannot turn a
repository-controlled `git.exe` into the executable behind a bounded
`git_version` admission.

Explicit executable paths remain a separate human-visible semantic. A command
such as `./tool` or an absolute workspace path is not silently converted from a
bare host command and may still be proposed under the existing M2.1 explicit-path
rules.

Envelope:

```text
execute_process
bounded=true
risk=diagnostic
```

Verdict: `admit_requires_human`.

### `python_compileall`

Local argv:

```text
<current absolute Python executable> -m compileall -q .
```

Envelope:

```text
execute_process
write_workspace
bounded=true
risk=workspace_mutation
```

`compileall` can create or replace `__pycache__` bytecode. The default M2.2
bridge therefore rejects it with `reject_capability_envelope` rather than
pretending that process authority implies workspace-write authority.

Even if a future policy instance knows about `write_workspace`, the current
M2.2→M2.1 proposal bridge still accepts only the exact `execute_process`-only
envelope. Capability-policy expansion cannot silently expand the bridge.

### `python_unittest_discover`

Local argv:

```text
<current absolute Python executable> -m unittest discover -s tests -v
```

The command is recognized but its envelope is `bounded=false` and its risk is
`unbounded_child_code`.

Test discovery imports and executes repository-controlled Python. Without a real
filesystem/network permission boundary, tests may write files, access the
network, start processes, or perform other actions. Listing more nominal
capabilities would not prove a complete envelope, so M2.2 rejects this family as
`reject_unbounded_child_code`.

## Approval preview

Admission produces a preview containing:

- request id and family;
- exact locally constructed argv;
- cwd;
- local risk classification;
- required capability envelope;
- bounded/unbounded status;
- admission verdict and reason;
- whether a human authorization step is applicable.

For a bare host command such as `git`, the preview shows the exact argv token
(`git --version`); the later M2.1 proposal contains the canonical executable path,
size, and SHA-256 produced by workspace-filtered, workspace-excluding resolution.

The preview deliberately contains no `approved`, approval-mode, proposal-digest,
receipt-id, or receipt-digest field. It communicates a local decision boundary;
it cannot grant authority.

## M2.1 proposal bridge

Only `admit_requires_human` commands with the exact capability envelope:

```text
(execute_process,)
```

may become M2.1 `ActionProposal` objects. M2.1 then performs its existing
workspace-filtered executable resolution and identity binding, rejects bare-name
results inside the workspace, and applies cwd, environment, containment,
one-shot authorization, and execution-observation checks.

Admission is not authorization:

```text
M2.2 admitted
≠ M2.0 authorized
≠ M2.1 executed
```

## Deliberate non-goals

M2.2 does not:

- add `execute_process` to `ToolName`;
- change the M1.1 runtime system prompt;
- change `ReadOnlyAgentLoop`;
- auto-authorize diagnostic commands under `risky`;
- admit shell interpreters, `python -c`, arbitrary scripts, arbitrary binaries,
  wrappers, or model-supplied argv;
- provide filesystem or network sandboxing;
- add workspace mutation or Git mutation authority.

These remain separate later gates.
