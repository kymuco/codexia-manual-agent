# M2.4.6 — Bounded Model Patch Requests, Provenance-Bound Proposals and Governed Human Approval

## Purpose

M2.4.6 closes the M2.4 series by connecting model-authored code-change intent to the
already accepted patch proposal and mutation pipeline without giving the remote model
local write authority.

The boundary is:

```text
model patch intent
    ↓
strict bounded patch_request parser
    ↓
local workspace selection + canonical target/preimage capture
    ↓
existing M2.4.1 ActionProposal + exact human preview
    ↓
existing M2.0 authority policy
    ↓
M2.4.2 plan → M2.4.3 apply → M2.4.4 observe → M2.4.5 recover
```

Model intent is not permission.

## Model-owned schema

The standalone model protocol accepts one JSON object:

```json
{
  "type": "patch_request",
  "request_id": "patch-1",
  "changes": [
    {
      "operation": "create",
      "target": "src/example.py",
      "content": "print('example')\n"
    }
  ]
}
```

The model owns only:

- a bounded `request_id`;
- `create` or `replace`;
- a workspace-relative target string;
- UTF-8 postimage text.

The model cannot supply:

- `workspace_root`;
- capability or action names;
- preimage state, bytes, metadata, SHA-256 values or change digests;
- proposal ids or proposal digests;
- authorization decisions, receipts or authority tokens;
- delete, rename or metadata-only operations.

Exact-key parsing rejects attempts to add these fields.

## Protocol budgets

The M2.4.6 intake is bounded before local proposal construction:

- at most 32 file changes;
- at most 1 MiB UTF-8 postimage bytes per change;
- at most 4 MiB total model-owned postimage bytes;
- bounded request text and target lengths;
- duplicate JSON keys and non-standard JSON constants are rejected.

The deeper M2.4.1 proposal builder independently re-applies its own file, total-content,
preview, target and preimage limits.

## Local proposal construction

`prepare_model_patch_proposal()` accepts a typed `ModelPatchRequest` plus a locally
selected workspace. It maps the model operation to the existing `MutationOperation`,
then delegates to the accepted hardened M2.4.1 proposal builder.

That local builder remains authoritative for:

- canonical workspace resolution;
- canonical target normalization;
- `.git`, `.codexia`, traversal, symlink, junction and sensitive-path guards;
- duplicate-target rejection;
- exact current preimage capture;
- create-absent and replace-present requirements;
- replace no-op rejection;
- exact postimage bytes;
- per-file `change_digest`;
- whole-set `change_set_digest`;
- deterministic unified-diff review rendering.

M2.4.6 does not trust model-supplied claims about any of those facts.

## Provenance binding

Every accepted model request has a stable `request_digest` over:

- protocol schema version;
- request id;
- ordered model change intent.

The bridge places the request id and request digest in the locally generated proposal
summary. `ActionProposal.proposal_digest` already covers the summary, so the existing
authority identity cryptographically commits to that model-request provenance without
changing the frozen M2.4 patch parameter schema.

The human-facing `ModelPatchApprovalPreview` also binds:

- `request_digest`;
- `proposal_digest`;
- `change_set_digest`;

into a separate `preparation_digest`.

This preparation digest is review evidence only. It is not an authorization receipt and
cannot grant local authority.

## Authority boundary

The produced proposal is the existing:

- capability: `write_workspace`;
- action: `workspace.apply_patch.v1`.

Therefore the frozen local policy remains unchanged:

- `always` → explicit human decision required;
- `risky` → explicit human decision required;
- `never` → denied.

M2.4.6 does not create, apply, consume or synthesize an authorization receipt.

## No hidden write-agent path

`patch_request` is deliberately a separate parser, following the precedent of the M2.2
`process_request` protocol.

The M1.1 `ReadOnlyAgentLoop` is unchanged and continues to accept only `tool_request`
and `final`. Feeding `patch_request` to that loop remains a protocol error.

M2.4.6 therefore does not expose a remote model write tool and does not automatically
turn a model response into filesystem execution.

## Execution boundary

M2.4.6 stops at an unapproved, digest-bound proposal and review preview. It does not:

- consume authority;
- create an `ActionLifecycle` execution transition;
- build or execute a transaction;
- stage or publish files;
- commit or roll back TxF;
- emit mutation observations or recovery receipts;
- perform Git mutation.

After an explicit local human authorization, the accepted M2.4.2–M2.4.5 machinery
continues unchanged.

## Security properties

M2.4.6 preserves these distinctions:

- model intent ≠ local action proposal facts;
- local proposal ≠ authorization;
- authorization ≠ execution evidence;
- current filesystem state ≠ model assertion;
- review provenance ≠ permission.

A model can request a bounded patch. Only CMA can construct the exact local proposal,
and only the local authority boundary can permit its later execution.

## Next milestone

M2.4 is complete after this PR.

M2.5 adds explicit Git mutation governance. Commit and push remain separate actions and
do not inherit authority from patch application or process execution.
