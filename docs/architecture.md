# Runtime Architecture

## Core separation

Codexia is organized around one primary rule:

```text
model intent != execution authority
```

Reasoning, provider transport, local authority, durable coordination, and computational evidence are separate concerns. A model may propose work or emit a structured request, but that does not by itself grant permission to launch a process, mutate a workspace, change Git state, or use an external transport.

The current public baseline has four layers:

```text
model/provider transport
        ↓
agent runtime
        ↓
local authority + governed execution
        ↓
durable coordination
        ↓
computational-lab records and registries
```

These layers share identities and evidence, but authority does not flow implicitly between them.

## 1. Agent runtime

The agent runtime owns structured model interaction, bounded workspace inspection, prompts, provider adapters, and model/tool budgets.

The optional `chatgpt-web-adapter` is transport only. Provider or browser-session state is not a security boundary.

The remote model-facing tool surface remains read-only. Process requests, patch requests, and orchestration requests use separate parsers and do not themselves authorize local side effects.

## 2. Local authority and governed execution

Side effects use an explicit authority spine:

```text
PROPOSED
→ AUTHORIZED
→ authorization consumed once
→ EXECUTED
→ OBSERVED
```

A denial is terminal:

```text
PROPOSED → DENIED
```

Core properties:

- proposals are canonical and digest-bound;
- capability/risk is derived locally rather than trusted from model labels;
- approval source is explicit;
- authorization receipts are one-shot and proposal-bound;
- execution revalidates approved identity before receipt consumption;
- terminal observations record what actually happened rather than what was intended.

### Process execution

M2.1 provides local-human bounded process execution with structured argv, workspace-bound cwd, a minimal environment, executable identity binding, timeouts, output budgets, and process-tree containment.

`execute_process` is not treated as a generic OS sandbox. Approved child code may have powers granted by the host OS, so generic process execution is not a remote-model capability.

### Command admission

M2.2 adds narrow command-family admission and capability envelopes. Model-owned request fields remain distinct from locally constructed argv, workspace, capability, proposal, and authorization state.

### Workspace mutation and patches

M2.3 provides explicit create/replace workspace mutation with exact preimage and postimage binding.

M2.4 builds governed multi-file patch application on the same authority model: proposal binding, pre-authority drift checks, atomic application where supported, exact terminal observations, durable recovery, and a bounded model request bridge that cannot mint approval.

### Git mutation and transport

M2.5 treats Git commit and Git push as independent authorities. Workspace mutation, process execution, model intent, or a previous Git action never implicitly authorizes another Git action.

M2.5.1 extends governed push to explicitly bound SSH and HTTPS transports while removing ambient credential, proxy, and helper influence from the admitted path.

### Delegation

M2.6 adds bounded delegation and human escalation under the invariant:

```text
delegation cannot mint authority
```

Delegated capability and budget can only stay the same or shrink. Continuation after human escalation resumes orchestration; it does not grant the action named by the escalation.

## 3. Durable coordination

M3 moves session and delegation state from process-local coordination into durable, integrity-checked chronology.

M3.1 records persistent sessions, provider requests/responses, tool observations, and authority evidence in an append-only SQLite chronology with hash chaining and exact recovery checks. Recovery reconstructs state; it does not silently replay side effects or convert an unknown provider outcome into a known result.

M3.2 applies the same discipline to bounded delegation: root-scoped event chronology, durable budget consumption, non-replayable request claims, escalation/continuation state, cancellation, and exact derived-index verification.

Authoritative chronology and derived navigation state are intentionally different. Disagreement is treated as corruption rather than silently repaired from the less-authoritative representation.

## 4. Computational lab

M4 adds reproducible research records without widening execution authority.

M4.1 defines immutable, digest-bound hypotheses, experiment manifests, runs, artifacts, metrics, and conclusions. Provenance establishes declared lineage; it does not by itself prove scientific truth, actor authenticity, or physical artifact bytes.

M4.2 adds an authoritative append-only SQLite experiment/run/evidence registry with deterministic transition logic, exact lineage replay, scoped uniqueness, evidence sealing, recovery, and corruption detection.

Registering evidence is not an execution receipt. Experiment code that launches processes or mutates state must still pass through the ordinary governed authority path.

## Capability vocabulary

Current policy concepts include:

- `read_workspace`
- `write_workspace`
- `execute_process`
- `network_access`
- `git_commit`
- `git_push`
- `delete_files`
- `outside_workspace`

Capabilities are independent policy concepts. A capability label never creates an OS security boundary that the underlying platform does not enforce.

## Platform boundaries

Some high-assurance mutation paths are intentionally platform-constrained.

On supported Windows/local-NTFS configurations, strict workspace and Git mutation use capability-gated transactional primitives and exact identity checks.

Linux process containment uses its own supported primitives where required. Linux workspace mutation remains fail-closed where the project does not yet have a commit boundary satisfying the required ancestry and atomicity properties.

Codexia does not silently fall back from an unavailable security primitive to a weaker semantic while claiming the stronger guarantee.

## Design invariants

Across the runtime:

- identity is not permission;
- model intent is not authority;
- context is not approval;
- evidence is not execution;
- provenance is not truth;
- delegation cannot mint authority;
- unknown outcomes remain unknown;
- durable recovery does not silently replay side effects;
- unsupported security primitives fail closed.

Milestone-specific contracts and failure modes are documented under `docs/`.
