# M2.3 — Controlled Workspace Mutation

## Purpose

M2.3 adds a local, human-authorized `write_workspace` primitive without exposing
workspace writes to the remote model and without introducing patch semantics.

Two operations are deliberately distinct:

- `workspace.create_file.v1` requires an absent preimage;
- `workspace.replace_file.v1` requires a present, digest-bound preimage.

There is no upsert operation and no delete operation. Delete remains governed by
the independent `delete_files` capability.

## Supported execution boundary

Proposal construction, capability policy, authorization binding, and observation
models remain platform-neutral. The actual M2.3 filesystem commit backend is
**Windows-only**.

Linux mutation execution is intentionally fail-closed before one-shot receipt
consumption. The previous held-dirfd implementation prevented symlink redirection
but could not prevent the already-open parent directory inode itself from being
relocated outside the workspace in the final commit window. That is not accepted
as a best-effort security boundary.

A future Linux backend must provide a constrained commit primitive whose workspace
ancestry remains enforced by the kernel through the actual mutation syscall.
Until then, Linux can prepare and authorize a proposal but cannot execute it.

Windows create keeps the existing no-clobber handle backend. Windows strict
replacement uses Transactional NTFS (TxF) only on a supported local NTFS volume.
TxF is capability-gated before one-shot authorization consumption and there is no
fallback to the weaker path-based replace implementation if TxF is unavailable.
Microsoft documents TxF as unavailable on non-NTFS and network/SMB volumes and as
a legacy API surface that may be absent on future Windows versions; those cases
therefore fail closed rather than silently weakening the mutation contract.

Strict replacement is deliberately narrower than arbitrary Windows filesystem
replacement. The exact destination must support transacted handle-based
final-path, security-descriptor, attribute, and data-stream inspection. Targets
with named data streams or unsupported filesystem attributes fail closed before
receipt consumption rather than silently losing metadata during replacement.

## Authority flow

```text
local human write intent
→ canonical workspace target
→ exact preimage snapshot
→ exact postimage bytes
→ ActionProposal(WRITE_WORKSPACE)
→ local approval policy
→ human authorization
→ [non-Windows: fail closed before receipt consumption]
→ [Windows: reject reserved namespace spellings / ADS syntax]
→ [replace: require local-NTFS TxF capability]
→ pin verified Windows workspace-to-parent identity
→ replace: CreateTransaction
→ replace: CreateFileTransactedW(exact destination, share=0)
→ replace: verify canonical final path + exact preimage
→ replace: inspect preservable security/attribute/stream metadata
→ ONLY NOW consume one-shot receipt
→ EXECUTED
→ create: held staging-file identity + fsync + no-clobber handle rename
→ replace: create held transacted stage with FILE_SHARE_DELETE only
→ replace: apply and verify destination DACL/attributes on held stage
→ replace: revalidate destination metadata + parent + staging identity
→ replace: MoveFileTransactedW(stage → target, REPLACE_EXISTING)
→ replace: close transacted target/stage handles
→ replace: CommitTransaction
→ mark filesystem commit only after CommitTransaction succeeds
→ rollback unfinished transaction on every pre-commit abort
→ collect cleanup evidence
→ exact postimage + preserved metadata verification when committed
→ OBSERVED
```

`write_workspace` remains a side-effect capability and therefore requires a
human-sourced authorization receipt in both `always` and `risky`. `never` denies
it. Admission is not authorization, and M2.3 does not change the model tool
surface.

## Exact proposal binding

Each proposal binds:

- operation;
- canonical workspace-relative target;
- expected preimage state;
- for replacement: preimage size, SHA-256, and permission mode as a precondition;
- exact postimage size;
- exact postimage SHA-256;
- exact postimage bytes encoded as base64 inside the digest-bound proposal.

Postimages are limited to 1 MiB. Replacement preimages have a 16 MiB hashing
budget enforced both before opening and cumulatively while streaming, so a file
that grows concurrently cannot drive hashing past the declared bound.

Windows replacement additionally snapshots the exact destination's current
preservable metadata through the transacted exact handle before receipt
consumption. That runtime metadata snapshot is not authority to change metadata:
it is a commit precondition requiring the replacement object to preserve the
destination's current discretionary security descriptor and supported file
attributes. If the metadata changes while the exact destination is held, the
operation aborts rather than publishing a different security state.

## Path and parent governance

Mutation targets:

- must be relative;
- cannot traverse `..`;
- must have an existing directory parent;
- must remain inside the canonical workspace;
- cannot traverse a symlink/junction parent alias;
- cannot themselves be symlinks;
- cannot target `.git` or `.codexia` trees;
- cannot target credential/private-key paths excluded by the shared sensitive
  path policy.

Windows additionally rejects path-component spellings that Win32 can interpret as
another object or stream, including reserved namespace characters such as `:`,
trailing spaces/periods, control characters, and reserved DOS device names such as
`NUL`, `COM1`, and their reserved extensions. For existing replacement targets,
the exact pinned handle's final normalized path must equal the approved canonical
path; a short-name or other filesystem alias therefore fails closed.

On Windows, the canonical workspace-to-parent directory chain is opened and
verified without `FILE_SHARE_DELETE`. Those held handles prevent the workspace
root or target-parent path components from being renamed/replaced while the
commit backend is active.

The Linux held-dirfd helper remains useful test/research code for staged-inode
semantics, but it is no longer an active public execution backend in M2.3.

## Staged-file identity governance

Windows create staging uses an exclusive Win32 file handle with zero sharing and
with `DELETE` access. Strict replace creates its staging object inside the same
TxF transaction as the exact target. The stage requests only
`FILE_SHARE_DELETE`: this permits its own transacted namespace move while still
denying external write sharing. A transacted-created stage is not externally
visible before commit and its name is transactionally reserved.

CRT descriptors remain in binary mode (`O_BINARY`) so proposal bytes remain exact
and `\n` is never translated to `\r\n`. Staging begins with normal file
attributes. Strict replace applies the exact supported destination attributes and
DACL to the held stage and re-reads its metadata before the transacted move.

The staged handle is retained through content/metadata verification and
`MoveFileTransactedW`. Once that move succeeds, TxF owns the pending namespace
transition. The transacted file handles are then closed before transaction end,
and the transaction lock continues to prevent external rename/write substitution
until commit or rollback.

## Create semantics

Create binds `preimage.state=absent`. The postimage is staged and fsynced before
commit. Windows commit is no-clobber: if a target appears after authorization,
the held staged object is not allowed to replace it and the mutation terminates
as `target_appeared`.

Creation becomes committed immediately when the no-clobber target rename
succeeds. Later handle or housekeeping failures cannot retroactively report
`applied=false`; they are retained as error evidence while the committed target
is verified normally.

Create does not require TxF replace capability.

## Strict replace semantics

Replace is not an upsert.

Before any replace receipt is consumed, M2.3 requires the Windows TxF API surface
and a local NTFS target volume. Unsupported/non-NTFS/network cases fail closed.
The backend then creates a transaction and opens the current destination with
`CreateFileTransactedW`, `GENERIC_READ`, zero sharing, and
`FILE_FLAG_OPEN_REPARSE_POINT` **before authorization consumption**. That exact
held destination is hashed and converted into a `PreimageSnapshot`.

If the target disappeared, changed, cannot be transactionally pinned, or resolves
through a filesystem alias, execution stops while the receipt remains unconsumed.
The same held object is inspected for a normalized owner/group/DACL security
descriptor, supported file attributes, and data streams. Named data streams are
currently unsupported because publishing a new file object would otherwise drop
those streams.

Zero sharing remains the exact-object protection during authorization and stage
preparation. Unlike the previous `FileRenameInfoEx` design, CMA does not need to
relax the destination's share mode to perform its own replacement. The replacement
stage is created within the same transaction and is revalidated while held.

After the destination metadata is applied to the stage,
`MoveFileTransactedW(..., MOVEFILE_REPLACE_EXISTING)` records the pending namespace
transition inside the transaction. Only after that transition succeeds are the
transacted file handles closed. TxF's transaction lock then owns the target name:
external mutation attempts cannot use the target name while the transaction is
pending. `CommitTransaction` is the filesystem commit point.

If any pre-commit step fails, the transaction is rolled back and the original
target remains the committed external state. Closing the last transaction handle
without commit also rolls back the transaction; focused Windows regression tests
cover process death between the transacted move and commit and require restoration
of the old target with no transacted staging debris.

This protocol fixes the original self-conflict where an exact zero-sharing target
pin caused ordinary `FileRenameInfoEx` replacement to fail with
`ERROR_SHARING_VIOLATION`, without replacing it with a weaker
`FILE_SHARE_DELETE` destination pin that permits target-name substitution.

The final committed target is re-read for both postimage identity and preserved
Windows metadata before `APPLIED` is reported.

## Observation and cleanup ordering

`WorkspaceMutationObservation` is immutable and digest-bound to:

- proposal and receipt identities/digests;
- mutation id;
- operation and target;
- expected and last-observed preimage snapshots;
- whether mutation bytes were applied;
- verified postimage size/SHA-256 when available;
- termination reason;
- error detail when applicable.

Termination reasons are:

- `applied`;
- `preimage_changed`;
- `target_appeared`;
- `target_disappeared`;
- `boundary_changed`;
- `write_error`;
- `postimage_mismatch`.

A preimage, exact-pin, canonical-path, platform/volume capability, stream-policy,
or preservable-metadata failure detected before receipt consumption raises without
consuming the one-shot authorization. After consumption, inspection/boundary or
transaction failures become terminal digest-bound observations after the
unfinished transaction is rolled back and held-resource cleanup is collected.

The governing invariant is:

```text
filesystem side-effect truth != housekeeping success

CommitTransaction success establishes replace commit truth
cleanup evidence is collected second
immutable observation is emitted last
```

## CLI

```powershell
codexia write `
  --workspace W:\dev\repo `
  --create src\new_module.py `
  --content-file W:\tmp\new_module.py `
  --approve
```

```powershell
codexia write `
  --workspace W:\dev\repo `
  --replace README.md `
  --content-file W:\tmp\README.new.md `
  --approve
```

The content source is local-human input. Its exact bytes become part of the
proposal before approval. The CLI checks source size before allocation and then
performs a bounded read of at most `MAX_POSTIMAGE_BYTES + 1`, so an oversized
content source cannot be loaded without bound before the 1 MiB postimage limit
is enforced.

On non-Windows hosts, the CLI can construct/govern the request but filesystem
execution fails closed before the authorization receipt is consumed. On Windows,
create remains available through the no-clobber backend; replace additionally
requires usable local-NTFS TxF semantics and otherwise fails closed.

## Review hardening

The implementation incorporates the prior Codex/security review rounds plus a
live Windows strict-replace isolation campaign:

1. parent rename/symlink swap, post-consumption observation completion, committed
   create cleanup truth, and bounded CLI input;
2. staged-object identity, streaming hash budget enforcement, and exact mode-zero
   preimage handling;
3. Windows binary staging, successful replace commit truth across cleanup, and
   cleanup-complete early-abort observations;
4. normal Windows publish attributes and guaranteed release of partially acquired
   parent pins when context entry fails;
5. Linux parent-inode relocation escape and replace-to-create degradation. Linux
   execution is fail-closed and direct legacy replace remains sealed;
6. replace platform capability is preflighted before authorization consumption;
7. Win32 namespace alias/ADS handling, pre-consumption exact destination pinning,
   default-stream-only policy, and DACL/supported-attribute preservation;
8. real Windows isolation reproduced the `FileRenameInfoEx` self-conflict, proved
   that adding `FILE_SHARE_DELETE` permits destination substitution, and rejected
   `ReplaceFileW`, delete-pending fencing, parent-pin-only fencing, and naive
   oplock release as equivalent-safe repairs;
9. TxF feasibility proved exact `share=0` target pin + transacted replacement,
   held transacted staging with metadata preservation, explicit rollback,
   transaction-lock protection after file-handle close, and process-death rollback
   with no staging leftovers.

Focused regressions include the earlier review suites plus
`tests/test_workspace_mutation_txf_review.py`, which covers the exact-pin
self-block repair, post-move transaction lock, commit-failure rollback, and
process-death rollback.

## Deliberate non-goals

M2.3 does not:

- add a write tool to `ToolName`;
- change the remote model protocol or runtime prompt;
- apply model patches;
- execute workspace mutation on Linux;
- replace files that contain named Windows data streams;
- claim support for Windows filesystem metadata classes it cannot inspect and
  preserve through the held-handle backend;
- provide a weaker fallback when TxF is unavailable;
- claim arbitrary power-loss durability beyond the transaction guarantees exposed
  by the supported Windows/NTFS environment;
- delete files as an operation;
- create parent directories;
- write `.git`, `.codexia`, or sensitive credential paths;
- grant Git commit/push authority;
- provide durable mutation chronology.

A constrained Linux mutation backend should be introduced as a dedicated
follow-up (M2.3a or equivalent) rather than silently restoring the weaker dirfd
commit path. Patch preview/application and richer mutation receipts are M2.4.
Git mutation is M2.5. Durable chronology is M3.
