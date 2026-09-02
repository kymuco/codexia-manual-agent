# M2.5 — Explicit Git Mutation Governance

## Purpose

M2.5 adds two explicit Git mutation authorities without allowing workspace patch,
process, model, or other authority to imply Git authority:

```text
workspace patch authority != git commit authority != git push authority
```

The supported actions are:

- `git.commit.v1` under `git_commit`;
- `git.push.v1` under `git_push`.

Each action has its own digest-bound `ActionProposal`, canonical human review
surface, one-shot HUMAN authorization receipt, execution id, and terminal
observation. ALWAYS and RISKY require a local human; NEVER denies.

M2.5 is not a general Git command capability.

## Platform and repository boundary

Proposal construction and read-only revalidation are ordinary local Git
inspection. Mutation execution is deliberately narrower:

- Windows only;
- local writable NTFS with accepted TxF support;
- governed workspace is exactly the repository top level;
- `.git` is a real in-workspace directory, not a worktree file or redirect;
- detached HEAD, initial/root commit, linked worktrees, external `GIT_DIR`, and
  redirected critical metadata are rejected;
- SHA-1 and SHA-256 repositories are admitted;
- the resolved Git executable is bound by absolute path, size, and SHA-256 and
  revalidated before authorization consumption.

On unsupported platforms or filesystems, execution fails closed before the
one-shot receipt is consumed. Linux Git mutation execution is therefore not
claimed by M2.5.

Caller-controlled Git redirection environment is removed. In particular,
`GIT_DIR`, `GIT_WORK_TREE`, `GIT_INDEX_FILE`, object-directory variables,
`GIT_CONFIG_*`, `GIT_SSH*`, replacement-object, external-diff, trace, and similar
control variables cannot redirect the governed action. Global and system Git
config are disabled for M2.5 Git subprocesses; repository identity comes from
local governed state. Governed subprocesses also force `GIT_NO_LAZY_FETCH=1` and
disable Git hook discovery so nominally local mutation cannot inherit network or
hidden process authority.

## Windows namespace admission

A path string or ordinary directory handle is not treated as proof that a Git
repository remains at the same namespace location. M2.5 reuses the accepted TxF
boundary learned in M2.4.5.

Immediately before authorization consumption, execution creates one short-lived
uncommitted TxF transaction and unique transacted marker files inside every
critical mutation directory. Marker handles remain alive while ordinary Git I/O
runs. Their final paths are verified by handle. The marker transaction contains
no Git data and is always rolled back/closed; it is never committed.

For critical configuration files and the proposal-bound Git executable, M2.5
also holds read-only handles without write/delete sharing so approved bytes cannot
be rewritten or replaced while the mutation is executing. Commit execution also
locks the live `.git/index` across final revalidation and mutation.

The order is:

```text
read-only proposal/precondition validation
    -> TxF namespace admission
    -> exact revalidation under the pin
    -> consume one-shot authorization
    -> Git object/ref mutation
    -> exact terminal observation
    -> rollback/close namespace-pin transaction
```

Namespace or platform admission failure never spends the authorization receipt.

## Commit action

### Proposal identity

A commit proposal binds:

- canonical repository/workspace;
- physical `.git` identity;
- exact symbolic `refs/heads/...` HEAD ref;
- exact parent commit OID;
- object format;
- raw index byte size and SHA-256;
- complete stage-0 index manifest digest over path/mode/blob OID;
- bounded exact staged diff SHA-256 and human-readable UTF-8 diff;
- exact UTF-8 commit message;
- author/committer name and email from local repository config;
- exact author/committer timestamp;
- expected tree OID;
- expected commit OID;
- exact held commit-pack size and SHA-256;
- exact Git executable identity.

No staged changes, unresolved merge stages, non-UTF-8 review diff, invalid ref
shape, and budget violations fail closed.

### Exact prebuilt commit object

The commit object is constructed before human authorization in a temporary object
store. A frozen copy of the exact live index is used with the governed repository
object store only as a read-only alternate:

```text
exact frozen index
    -> write-tree in temporary object store
    -> commit-tree with exact parent/identity/time/message
    -> verify raw commit bytes semantically
    -> pack exact new commit graph
    -> bind tree OID + commit OID + pack SHA-256 into proposal
```

The live repository ref has not moved at this stage.

Immediately before mutation, M2.5 revalidates the live HEAD/ref, repository
physical identity, index bytes/manifest, staged diff, author identity, Git
executable, held pack digest, expected tree, expected commit, and complete
human-facing preview.

### Commit execution

Under the Windows namespace pin, authorization is consumed exactly once. The
executor then installs only the already approved held pack and verifies the exact
approved commit object before changing any ref:

```text
index-pack < approved pack
    -> parse and validate exact installed-pack OID
    -> verify expected commit/tree/parent/identity/time/message
    -> update-ref <approved-ref> <approved-new-oid> <approved-old-oid>
```

Git's canonical `index-pack --stdin` stdout is `pack<TAB><oid>` on the exercised
Windows path. M2.5 accepts that exact whitespace-token form (plus compatible
single-OID output) and validates the extracted full OID; ambiguous output fails
closed before ref CAS.

`update-ref` is the commit point and compare-and-swap guard. A concurrent ref move
cannot silently redirect the approval. Worktree and live index bytes are not used
to construct a commit after authorization consumption, so later unstaged changes
cannot enter the approved commit.

If the ref CAS rejects after the immutable pack was installed, unreachable Git
objects may remain as a bounded storage artifact. No branch ref is moved by that
rejection; object cleanup is not an additional hidden authority in M2.5.

## Push action

### M2.5 v1 push boundary

M2.5 v1 executes only an update of an **existing branch in a local bare
`file://` repository**. This gives the destination a locally observable physical
identity and allows the same Windows namespace boundary as local commit.

The following are not admitted in M2.5 v1:

- SSH/SCP-like or HTTPS network push;
- destination branch creation;
- tag mutation;
- branch deletion;
- implicit upstream selection;
- arbitrary refspecs;
- no-op push;
- locally provable non-fast-forward push.

Network push is intentionally deferred to M2.5.1 because binding a URL is not
sufficient proof of an exact SSH/HTTPS endpoint, helper chain, credential helper,
proxy, host alias, or host identity.

### Push proposal identity

A push proposal binds:

- exact local HEAD commit OID and symbolic ref;
- exact local object format;
- exact local `.git/config` size and SHA-256;
- exact configured remote name and effective `file://` URL;
- canonical bare-repository path and physical repository identity;
- exact remote object format;
- exact remote config size and SHA-256;
- exact existing destination `refs/heads/...` ref;
- exact expected old remote OID;
- exact held push-pack size and SHA-256;
- exact Git executable identity.

The expected old remote commit must be present locally and be an ancestor of the
approved local commit. The exact incremental object pack is built before approval
from:

```text
<approved-local-oid>
^<approved-old-remote-oid>
```

### Push execution

Before consumption, M2.5 revalidates local HEAD/ref/config, effective remote URL,
remote physical identity/config/ref, fast-forward relation, Git executable, held
pack digest, and the complete displayed preview.

Then the local bare destination's root, objects directory, pack directory, and
exact destination-ref parent are pinned with TxF markers; remote config is held
read-only. After a second exact revalidation under that pin, the HUMAN receipt is
consumed once:

```text
index-pack < approved incremental pack into exact bare repo
    -> verify approved local commit exists in destination object database
    -> update-ref <destination> <approved-local-oid> <approved-old-remote-oid>
```

The final `update-ref` is an exact compare-and-swap. A competing ref update after
approval is preserved and this action returns REJECTED rather than overwriting it.
As with commit, a rejected ref CAS may leave unreachable immutable pack objects;
no competing ref is overwritten.

## Human review boundary

Both actions rebuild the canonical approval preview from current local evidence
and require full equality before receipt consumption. A real proposal digest
cannot be paired with a different displayed diff, message, object OID, URL,
destination, pack digest, or other review value.

The CLI is preview-first:

```text
prepare exact proposal
    -> display proposal + canonical approval preview
    -> local human types literal YES
    -> issue HUMAN receipt
    -> execute governed action
```

Without `--approve`, `codexia git commit` and `codexia git push` are preview-only
and do not issue or consume authority. There is no `codexia git -- <argv>` escape
surface.

## Observation semantics

After receipt consumption the lifecycle records EXECUTED. Terminal evidence then
advances it to OBSERVED. OBSERVED means evidence was captured; it does not imply
success.

Outcomes are:

- `APPLIED` — exact intended ref/OID is observed;
- `REJECTED` — an explicit ref-CAS command returned nonzero and old/competing ref
  state is observed;
- `MISMATCH` — mutation command reported success but terminal identity differs;
- `INCOMPLETE` — execution failed before a definitive CAS result, or terminal ref
  identity could not be established. If an exception occurs but the exact intended
  new OID is nevertheless observed, the terminal outcome is `APPLIED`.

Commit observations bind expected/new OID, tree, message digest, backend, and pack
identity. Push observations bind destination, expected/observed OIDs, backend, and
pack identity.

## Explicit non-claims

- M2.5 does not perform `git add`; it commits only the exact index staged by some
  separately authorized/user-controlled workflow.
- It does not expose reset, rebase, merge, tag creation/deletion, branch deletion,
  fetch, arbitrary refspecs, or generic Git execution.
- It does not execute network Git push; that is M2.5.1.
- It does not authorize GitHub API mutations such as PR review/comment/merge.
- It does not add durable crash-safe lifecycle persistence; that remains M3.
- Git immutable object installation can precede a ref CAS, so rejected CAS may
  leave unreachable bounded objects without changing the authorized ref state.