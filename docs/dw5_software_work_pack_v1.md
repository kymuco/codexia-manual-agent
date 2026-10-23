# DW5.1 — Software proposal and human review (one-file pilot)

Status: **proposal-only**. No repository reads by the model, no filesystem
writes, tests, Git effects, CapabilityNeed, host authorization, PR publication
or WorkCompletion. This is the first half of [DW5 #82](https://github.com/kymuco/codexia-manual-agent/issues/82),
not the full Software Work Pack exit proof.

## Product slice

The product host supplies a **bounded immutable snapshot** in the Work
objective (one JSON object with exactly these keys):

```json
{
  "schema_version": 1,
  "repo": "owner/repository",
  "base_head": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "path": "src/example.py",
  "original_text": "answer = 1\n",
  "preimage_sha256": "<SHA-256 hex of exact UTF-8 original_text>",
  "task": "Change one behavior without unrelated refactoring."
}
```

`SoftwareSnapshot.parse()` verifies exact fields and SHA-256. The snapshot
is host-*supplied*, not independent proof that current repository HEAD, file
bytes, or cleanliness have been inspected. Reading those local facts and
attesting their origin are deferred to the bounded host adapter milestone.
An external host MUST check canonical repository identity, HEAD, file state,
no symlink escape and trusted source permissions before allowing effects.

One pinned Workflow, one pinned Role and an empty Capability set:

```text
new Gen2 Work, exact software Pack pin
  -> one Software RoleRun (propose complete replacement for ONE existing file)
  -> SoftwarePatchProposal.parse (exact JSON schema)
  -> compare candidate repo, HEAD, path and preimage SHA with pinned snapshot
  -> compute deterministic unified diff from original/replacement text
  -> reject no-op, path escape, > 200 changed lines or > 12,000 diff characters
  -> AttentionNeed with full diff, risks, test plan, proposal digest
  -> human response: APPROVE <digest> | REJECT <digest>
  -> quiescent active Work, no technical effect, no completion
```

### Bounded candidate

`SoftwarePatchProposal` requires **exactly**: `schema_version=1`,
`stage="proposal"`, `repo`, `base_head`, `path`,
`preimage_sha256`, `replacement_text`, `summary`,
`verification_plan`, `risks`.

The `replacement_text` is the complete new UTF-8 text of that existing
file, LF line endings with a final newline, max 8,000 characters. Both
original and replacement must be nonempty. A deterministic SHA-256 over
the canonical proposal JSON is displayed at review time. The output is
a proposal, **not** a patch script or an executable instruction. The host
must independently check repository state and verify any future application.

`verification_plan` is untrusted explanatory text. It **must not** be
used as a process command. An eventual verifier must use host-selected
fixed argv/cwd, resource and time bounds, isolation from secrets/network
and independent verification of exact post-patch bytes.

### Review versus authority

AttentionResponse recording is durable user input, not proof of human identity
or host-granted permission to mutate files. `APPROVE <digest>` is only a
review intent tied to the exact proposal. The Software Workflow returns no
CapabilityNeed or CompletionClaim after this response. A trusted host must
later issue a fresh one-shot scoped authorization after validating the
review principal, exact digest, HEAD, file hashes, patch bytes and operation.

A changed file after review invalidates the original preimage. Neither a
rejected proposal nor a stale response can be applied. A model has no
access to the WorkStore, host, repo filesystem or execution privileges
through the Workflow interface.

## Intentional limitations (next DW5.2)

- One **existing** text file with an allowlisted suffix `.py`, `.md`,
  `.txt` or `.json`. No creates/deletes/renames/multi-file edits.
- No user-facing repository inspector/CLI yet; the Work must be started
  with a host-supplied snapshot and selected Pack.
- No proof of semantic patch validity beyond a bounded, reproducible diff.
- No Capability adapter to apply the proposal, no test invocation, no
  external push and no automatic PR.
- No recovery from an ambiguous external model dispatch; standard Gen2
  no-replay and Issue #88 apply.

After DW5.1 passes, implement the smallest read-only snapshot host and
operator path. Then add one explicit approval-to-host operation with
one-shot capability authority, fixed preconditions and verified test evidence.
Do not generalize to a daemon, action DSL, scheduler or new permission root.

## Acceptance tests

```bash
python -m pytest -q tests/test_dw5_software_work_pack_v1.py
python -m ruff check src/codexia_manual_agent/workflow_runtime/software_v1.py tests/test_dw5_software_work_pack_v1.py
python -m ruff format --check src/codexia_manual_agent/workflow_runtime/software_v1.py tests/test_dw5_software_work_pack_v1.py
```

The full CI covers existing Gen2 Research and Capability behavior. One
scratch SQLite integration test exercises RoleRun -> CognitionOutcome ->
AttentionNeed -> restart -> exact human response -> quiescent active Work
with zero effects. This is not a substitute for the later real repository
application acceptance proof.
