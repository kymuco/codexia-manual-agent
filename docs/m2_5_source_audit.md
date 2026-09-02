# M2.5 source-audit invariants

This note records the current static boundary for M2.5, including formal-review, real-Windows, and post-Ready review repairs.

- `git_commit` and `git_push` remain separate capabilities and proposal identities.
- No M2.5 API accepts arbitrary Git argv, shell text, tag/delete/reset/rebase/merge/fetch operations, or implicit upstream selection.
- Commit execution uses only a proposal-bound held pack plus explicit `update-ref <new> <expected-old>` CAS after one-shot authorization.
- Push v1 is limited to an existing branch in an exact local bare `file://` repository and uses a held incremental pack plus explicit destination `update-ref` CAS.
- Proposal/revalidation removes caller Git redirection environment and disables replacement objects, external diff/textconv, fsmonitor, system config, and global config.
- Every governed Git subprocess disables traditional hook discovery with `core.hooksPath=<null-device>` and explicitly disables the `reference-transaction` hook event so ref mutation cannot inherit hidden process authority.
- Every governed Git subprocess environment forces `GIT_NO_LAZY_FETCH=1`; partial-clone/promisor state therefore cannot turn a nominally local M2.5 operation into an implicit network fetch.
- The Git executable resolved during proposal construction is reused by exact absolute identity for revalidation/execution; later host `PATH` drift cannot select a different Git binary.
- Mutation execution performs a complete read-only proposal/precondition revalidation before Windows TxF admission, then repeats exact revalidation under the namespace pin before authorization consumption. This keeps precondition tests meaningful cross-platform while valid mutation remains Windows/TxF-only.
- Windows mutation execution holds the proposal-bound Git executable read-only without write/delete sharing while the authorized mutation runs, closing executable replacement after final revalidation.
- Windows mutation execution requires TxF namespace admission before authorization consumption.
- Commit namespace admission automatically locks the live `.git/index` read-only so raw index bytes, manifest, staged diff, and exact prebuilt commit identity cannot be raced during final revalidation.
- Locked Git config rejects `include` / `includeIf` sections before authorization consumption.
- Git roots with `info/grafts`, object alternates, HTTP alternates, or `commondir` fail closed before authorization consumption.
- Critical Git config files are held without write/delete sharing while the authorized mutation runs.
- Commit execution accepts Git's canonical `index-pack --stdin` stdout form `pack<TAB><oid>` (plus equivalent whitespace/single-OID forms) and validates the extracted OID exactly; ambiguous output fails closed.
- An existing push destination branch may be stored as a loose ref or only in `packed-refs`; exact branch existence and old OID are resolved and proposal-bound with Git, while missing destinations remain rejected as branch creation.
- Exact intended terminal OID observation has precedence over the local CAS return code: if another actor independently reaches the approved target between revalidation and CAS, the observation is `APPLIED` rather than a false `REJECTED`.
- `REJECTED` is reserved for an explicit nonzero ref-CAS result with a different observable terminal ref state. Exceptions before a definitive CAS result remain `INCOMPLETE` unless the intended new OID is actually observed as applied.
- Rejected CAS may leave proposal-bound unreachable immutable pack objects; no hidden cleanup authority is implied.
- `OBSERVED` means terminal evidence was captured, not that the mutation succeeded.

Evidence history:

- Candidate `a9eed2801e808d64457e2b7358bf2fab3e207104`: first real-Windows focused run produced `33 passed, 3 failed, 6 subtests passed`; all three failures were one `index-pack` stdout parser defect. This candidate is invalidated as PASS evidence.
- Candidate `c7312846587399bb021351c7aa10a5eb53878dc7`: real Windows/NTFS/TxF focused gate produced `38 passed, 9 subtests passed in 22.28s`; full suite produced `385 passed, 28 skipped, 90 subtests passed in 28.17s`. After that PASS, Ready-for-Review analysis found three additional defects: pre-TxF validation ordering on Linux, packed-only destination refs, and exact-target concurrent CAS observation semantics. The code changed to repair those findings, so the `c731284...` PASS remains historical evidence only and does not validate the next exact head.

Executable PASS is therefore not claimed for the current post-Ready repair head. Fresh real-Windows focused and full-suite evidence is required after the repairs are frozen into the next exact single-commit candidate.
