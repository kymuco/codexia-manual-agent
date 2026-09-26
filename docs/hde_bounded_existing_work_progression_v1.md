# Bounded Existing-Work Progression v1

## Purpose

Compose existing Gen2 progression boundaries into one finite interpreter for one
already-existing Work.

The service exists so a host can ask Codexia:

\`\`\`text
progress this exact existing Work
through at most N bounded Codexia transitions
until a durable return frontier appears
\`\`\`

The accepted durable return frontiers remain owned by the separate read-only
Durable Work Yield Projection:

\`\`\`text
guarded WorkCompletion
OR
current-head AttentionNeed
\`\`\`

## Not a scheduler

The service operates on exactly one caller-selected \`work_id\`.

It does not:

- choose between Work items;
- maintain a queue;
- create background work;
- recursively progress delegated child Work;
- retry handed-off external cognition or capability requests;
- map Codexia state into HDE/IRR result types.

The finite loop is an explicit per-call budget over one Work. It is mechanism
control, not durable scheduling truth.

## Ephemeral results

\`\`\`text
YIELDED
BOUND_EXHAUSTED
QUIESCENT
TERMINAL_NON_YIELD
\`\`\`

None of these are new Work states.

### YIELDED

The read-only frontier is either:

- exact guarded WorkCompletion; or
- exact current-head AttentionNeed.

### BOUND_EXHAUSTED

The Work remains ACTIVE, no durable yield exists, and the explicit transition
budget ended.

This does not imply failure, blocking, human attention, or retry permission.

### QUIESCENT

The Work remains ACTIVE but this finite interpreter has no safe transition to
perform now.

Examples include:

- a durable cognition handoff awaiting external outcome;
- a durable capability handoff awaiting external outcome;
- a live delegated child preventing guarded parent completion;
- a Workflow step that proposes no new durable state.

QUIESCENT is not a durable Work state.

### TERMINAL_NON_YIELD

The Work is terminal through a state not accepted as the current HDE Worker
return frontier, such as CANCELLED.

No host result is fabricated.

## Existing Work only

The service accepts a \`work_id\` and never calls \`Work.create()\` or
\`WorkStore.create()\`.

When no WorkflowRun exists, activation is allowed only when the existing Work
has no chronology. A Work with pre-activation chronology fails closed rather
than being silently adopted.

## Exact Workflow / Pack activation

The caller supplies:

- exact versioned technical \`provider_ref\`;
- configured \`workflow_id\`;
- configured \`workflow_version\`.

Codexia resolves one exact \`ResolvedPackDistribution\` through the existing
Invariant bridge.

If no WorkflowRun exists:

\`\`\`text
exact provider distribution
→ exactly one matching WorkflowBinding
→ WorkflowRun
\`\`\`

On the next bounded transition:

\`\`\`text
same exact provider distribution
→ exact PackBinding
→ PackWorkflowBinding
\`\`\`

Once durable WorkflowRun + Pack pin exist, their semantic identity is canonical.
There is no latest-version reselection.

## Provider-independent restart frontiers

Technical provider availability is not required to re-observe already durable
facts when no new Workflow computation is needed.

In particular:

- existing WorkCompletion returns immediately;
- current-head AttentionNeed returns immediately;
- durable cognition handoff awaiting outcome becomes QUIESCENT;
- durable capability handoff awaiting outcome becomes QUIESCENT;
- current-head admitted CompletionClaim can attempt guarded WorkCompletion
  without re-running the completion criterion.

A new Workflow proposal still requires exact provider implementation and exact
pinned Pack validation through existing Invariant bridges.

## One bounded semantic transition

Each budget unit invokes at most one existing bounded Codexia boundary:

- WorkflowAdmission start;
- PackAdmission pin;
- one WorkflowProgressionService call;
- one RoleCognitionProgressionService call;
- one CapabilityProgressionService call;
- one WorkCompletionAdmissionService call.

The underlying cognition/capability bridge may durably admit its handoff and a
synchronous outcome within that one existing bounded operation. The new service
does not duplicate those semantics.

## Unresolved Role / Capability lanes

If exactly one unresolved RoleRun exists, the caller must supply:

- CognitionPort;
- RoleInstructionsMaterialPort;
- ContextProjectionMaterialPort.

If exactly one pending CapabilityNeed exists, the caller must supply a
CapabilityHostPort.

If multiple unresolved Role/Capability lanes coexist, the interpreter fails
closed rather than inventing ordering policy.

## External handoff restart safety

The existing cognition and capability bridges already define:

\`\`\`text
durable handoff present
→ never redispatch the external request
\`\`\`

The finite interpreter checks those handoffs before invoking the progression
service. A restart therefore returns QUIESCENT instead of performing a second
external call.

## Delegated child Work

Workflow delegation may create durable child Work through the existing
DelegationAdmission boundary.

This interpreter never progresses that child.

If an admitted parent CompletionClaim cannot become WorkCompletion because a
child remains live:

\`\`\`text
guarded terminal admission
→ DelegationChildrenLiveError
→ parent QUIESCENT
\`\`\`

The child keeps its independent durable lifecycle.

## Completion

A current-head \`completion.claim-admitted\` event is already the result of the
existing Pack completion criterion boundary.

The interpreter may construct the exact WorkCompletion and submit it to
\`WorkCompletionAdmissionService\`.

It does not re-evaluate the criterion and does not bypass the existing live-child
terminal guard.

## HDE / IRR boundary

Production code imports no HDE or IRR types.

It does not know:

- WorkerResult;
- WorkerNeed;
- WorkerNeedKind;
- ContinuationInput;
- parent HDE completion;
- HDE Governance / Authorization / Executor semantics.

The host may consume a durable yield only after this Codexia-owned boundary
stops.
