# M2.4 — Patch application and mutation receipts

M2.4 builds on the M2.3 controlled workspace mutation boundary. The goal is to
let Codexia represent, preview, authorize, apply, and observe a bounded multi-file
workspace change without giving the remote model direct write authority.

## M2.4.1 — Exact patch proposal contract, preimage-bound change set, and preview

M2.4.1 defines the proposal and human-review surface only. It does **not** add a
patch executor, a model write tool, delete semantics, Git authority, or any new
filesystem commit primitive.

The first contract deliberately represents a patch as a self-contained set of
exact before/after file images rather than treating unified-diff text as the
authority-bearing executable object.

For each file the proposal binds:

- explicit `create` or `replace` operation;
- canonical workspace-relative target;
- exact expected preimage state, size, SHA-256, and mode;
- exact preimage bytes for a replace;
- exact postimage bytes, size, and SHA-256;
- a per-file `change_digest`.

The whole ordered change set carries a separate `change_set_digest`. The
`ActionProposal` then binds that complete change-set payload again through the
existing M2.0 proposal digest.

This gives the authority chain two useful identities:

```text
exact before/after file change
→ change_digest

ordered bounded multi-file change set
→ change_set_digest

ActionProposal metadata + change-set payload
→ proposal_digest
```

The proposal remains `Capability.WRITE_WORKSPACE`. Existing authority policy
requires a local human decision in approval modes that permit workspace mutation;
`NEVER` denies the action. No receipt or authority token is placed in the human
preview.

## Preimage capture boundary

Proposal preparation is a live filesystem operation and must prove that exact
preimage bytes come from the admitted workspace namespace.

On Windows, proposal preparation now pins the workspace directory object
**before** canonical workspace/target validation. A workspace alias spelling may
still resolve to its canonical directory; the canonical path is accepted only if
the object opened by that first operation reports the same final path and
identity. For each target, the workspace-to-parent handle chain is then retained
before `_normalize_target()` validates the live target parent, and the chain's
root handle must identify the same directory as the original workspace anchor. A
Windows host may still permit a directory rename while those handles are held,
so correctness does **not** depend on the rename being physically blocked.

The exact target leaf is opened with `NtOpenFile` using the already-held target
parent handle as `OBJECT_ATTRIBUTES.RootDirectory`. Target object selection is
therefore relative to the pinned parent object rather than to the mutable path
that originally named that parent. A rename/replacement/restore sequence after
validation cannot redirect the target open to a replacement directory. The
relative open retains reparse-point-open semantics; reparse targets and directory
targets are rejected, and the held target handle is read and re-hashed within the
existing byte bounds. Live parent/root verification still runs before capture,
before the first payload byte, and after capture, but those checks now confirm
that the authorized path spelling remains current rather than determining which
target object was opened.

Windows per-directory case sensitivity remains observable through the established
`_filesystem_case_sensitive` / `_query_windows_directory_case_sensitive` helper
surface. During live proposal preparation those helper calls are context-routed
to the already-held parent handle, so production does not reopen a mutable parent
path merely to obtain `FileCaseSensitiveInfo`. The same held handle is queried
twice; inconsistent evidence is treated as unknown and therefore fails closed.
The handle-relative target open also obtains leaf-lookup case behavior from that
held parent directory rather than from an ancestor path.

On POSIX, proposal preparation pins the workspace directory **before** canonical
workspace/target validation can be separated from capture by a namespace swap.
The canonical workspace path is accepted only after proving that it still names
the inode held by that first root fd. Every target parent is then traversed with
`openat` + `O_NOFOLLOW` relative to the held root fd, and the target is opened and
read relative to the held parent fd. Target-namespace case evidence is also taken
from that held parent fd rather than from a later path reopen, and the probe
consumes at most 64 directory entries lazily. Duplicate-target identity is built
from the pinned parent `(st_dev, st_ino)` plus an NFC-normalized leaf name, with
casefold applied when case sensitivity is false or cannot be proven. Ancestor
spellings therefore cannot distinguish two paths that resolve to the same parent
inode and same leaf. Parent identity is re-opened relative to the same root anchor
after capture, and the canonical workspace path is checked again against the
original root inode before the proposal is returned. Therefore a workspace or
parent rename/replacement cannot redirect authority-bearing reads to a replacement
namespace: bytes remain bound to the original pinned inode and a changed live path
fails closed.

This preimage pinning protects proposal construction only. It does not grant
mutation authority and does not replace the future execution-time preimage and
workspace-boundary revalidation.

## Human-readable preview

The preview is deterministically derived from the exact bytes already bound in
the proposal. For UTF-8 text files it renders a display-safe escaped unified diff
together with:

- operation and target;
- preimage size/SHA-256;
- postimage size/SHA-256;
- per-file `change_digest`;
- whole-set `change_set_digest`;
- file and byte totals.

Terminal/control and Unicode-format characters are rendered visibly rather than
emitted raw. Missing final LF is marked explicitly, and empty-file CREATE has an
explicit non-empty Codexia review marker.

The preview is not used as an executable patch program. Future application must
use the exact bound postimages and must revalidate each exact preimage before any
authority is consumed.

Because the proposal carries its own exact preimage bytes, proposal parsing and
preview rendering are self-contained after construction. They validate the
stored workspace root and targets lexically, validate digests and content bounds,
and do **not** require target parent directories to still exist. Filesystem-aware
namespace checks remain part of live proposal preparation/direct authoring and
must run again during future execution. Thus renaming or removing a target parent
after proposal creation cannot silently switch preview bytes or make an otherwise
valid bound proposal unreviewable.

## Initial bounds

M2.4.1 is intentionally narrow:

- maximum 32 files per change set;
- maximum 1 MiB preimage and 1 MiB postimage per file;
- maximum 4 MiB combined exact before/after content per proposal;
- maximum 512 KiB human-readable preview rendering;
- UTF-8 text only;
- `create` and `replace` only;
- duplicate filesystem targets are rejected according to parent directory
  identity plus target leaf-name semantics during live preparation and direct
  change-set authoring;
- POSIX namespace probing examines at most 64 entries from the pinned parent and
  never materializes the whole directory merely to infer case behavior;
- direct change-set constructor/factory inputs are consumed only through
  `MAX_PATCH_FILES + 1` before retention;
- no-op replacements are rejected.

For both live POSIX proposal preparation and direct change-set validation,
ancestor path spelling is not part of target identity once two paths identify the
same parent directory. The target key uses the parent directory identity plus an
NFC-normalized leaf; case is preserved only when the target namespace is proven
case-sensitive, otherwise NFC + casefold is used fail-closed.

On Windows, live proposal preparation reads per-directory case sensitivity from
the already-held target parent handle while preserving the established helper
surface for direct authoring, diagnostics, and tests. Outside the pinned live
context the helper retains its path-based behavior. If sensitivity cannot be
established, target identity fails closed to NFC + casefold alias protection.
Non-Windows live detection remains read-only, uses evidence only from the pinned
target namespace, and likewise fails closed when sensitivity cannot be proven.
Canonically equivalent Unicode spellings are conservatively collapsed
independently of case semantics.

Binary changes remain possible through the M2.3 exact-write surface but are not
accepted by the M2.4.1 patch-review contract.

## Non-goals of M2.4.1

M2.4.1 does not:

- apply the change set;
- consume an authorization receipt;
- call the M2.3 filesystem executor;
- add a remote-model write tool;
- support delete, rename, chmod, or metadata-only changes;
- support Git commit or push;
- claim Linux workspace-write support.

An M2.4.1 proposal handed to the M2.3 single-file executor is rejected before
receipt consumption.

## Planned M2.4 follow-ups

The intended sequence after M2.4.1 is:

1. revalidate the complete multi-file preimage set and live target namespace
   before authority consumption;
2. define an execution plan that maps each exact file change onto the accepted
   M2.3 Windows create/replace primitives;
3. define all-or-fail or explicit partial-failure semantics for multi-file
   application;
4. emit digest-bound changed-file mutation receipts and failure observations;
5. add rollback/recovery behavior appropriate to the accepted commit model;
6. only then consider wiring a model-produced patch request into the local human
   approval surface.

M2.5 remains separate: patch authority does not imply Git commit or push
authority.
