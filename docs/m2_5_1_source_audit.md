# M2.5.1 Source Audit — Governed Network Git Transport

This note records the static security boundary, repair history, and exact-candidate
validation history for M2.5.1. It is deliberately separate from executable PASS
evidence.

Exact merged M2.5 base:

```text
2073cc4ebc5bab71f552d14688ad75911d39b201
```

A final candidate is required to be one commit whose sole parent is that exact
base. This file intentionally does not contain the final candidate's own commit
SHA; the exact candidate SHA belongs in the PR body and external test evidence
because a commit cannot truthfully self-name before its object ID exists.

Fresh exact-head Windows focused/full PASS is **not claimed** until the final
frozen candidate is executed.

## Invalidated candidate evidence

### Candidate 1

The first frozen candidate was:

```text
bcff27b351d595fb7dbba611dfb4b426047ad733
```

with tree:

```text
9f7d118b2e134a8d7fcea3aab1055fd381aadf1d
```

It was tested on the target Windows environment and is **INVALIDATED**. Focused
M2.5.1 evidence was:

```text
19 failed, 46 passed, 2 skipped, 84 subtests passed in 32.34s
```

The failures exposed three repair classes described in findings 17–19 below. No
PASS is claimed for that candidate.

### Candidate 2

The repaired second frozen candidate was:

```text
8dc3bfef415f8e1406fdd0fcf6e11c8684cbb4c1
```

with tree:

```text
0aa41eaff8c822a4a8c97a0e20d14aecb0ff526c
```

It was also tested on the target Windows environment and is **INVALIDATED**.
Focused evidence was:

```text
2 failed, 63 passed, 2 skipped, 128 subtests passed in 38.62s
```

Those two failures are recorded in findings 20–21. The substantial pass count is
useful diagnostic evidence, but it is not promoted to candidate PASS.

## Preserved authority boundary

```text
workspace patch authority != git commit authority != git push authority
```

`file`, `ssh` and `https` remain backends of the same
`Capability.GIT_PUSH / git.push.v1`; no generic network or Git argv authority was
introduced.

## Common exact push contract

The network builder binds one local ref/OID, one remote name/URL, one explicit
`refs/heads/...` destination, one remote-tracking ref and one expected-old OID.
Expected remote state is not discovered by contacting the network during proposal
construction.

Execution sends:

```text
<approved-local-oid>:<destination-ref>
--force-with-lease=<destination-ref>:<expected-old-oid>
```

Local ancestry is checked separately, so the lease is an exact remote CAS guard,
not generic non-fast-forward authority.

Post-attempt exact remote observation controls terminal classification; exact
intended target observation has priority over command return code.

## SSH static boundary

Bound inputs:

- exact Git executable;
- exact Git command shell;
- exact OpenSSH executable;
- explicit SSH user;
- canonical host/port/repository path;
- exact resolved route IP;
- exact identity source;
- exact one-host known-host key/fingerprint.

Generated OpenSSH execution disables ambient config, agent/default identities,
proxy/jump routing, host-key learning, DNS trust, forwarding, local commands and
multiplexing. `ssh -G` verifies the effective installed-client configuration.

Private identity bytes are not copied for preview. Under authorized lifecycle,
but before the one-shot receipt is consumed, source/executable/namespace inputs
are admitted, exact private copies are exclusively created and locked, and the
materialized plan is revalidated. Failure in local materialization leaves the
receipt unconsumed and opens no network.

The private execution namespace contains pinned directory sentinels at both
`identity.pub` and `identity-cert.pub`. They prevent OpenSSH's implicit public-key
and certificate sibling discovery from acquiring a new unbound file after the
final local revalidation.

## HTTPS static boundary

Bound inputs:

- exact Git executable;
- exact Git command shell;
- exact `git-remote-https` plus its resolved helper target;
- exact CA bundle;
- exact host/port/repository path;
- exact resolved route IP via `http.curloptResolve`;
- exact credential source path/size/username plus keyed secret commitment.

The runtime forces `GIT_EXEC_PATH` to the parent directory of the exact bound
`git-remote-https`.

TLS policy is `bound-git-default`: the exact bound Git installation selects its
compiled/default TLS backend and CMA does not set `http.sslBackend`. Execution
forces `http.sslVerify=true`, the exact CA bundle through `http.sslCAInfo`, an
empty `http.sslCAPath`, and `http.schannelUseSSLCAInfo=true` so Git-for-Windows
Schannel consumes the same bound CA material.

System/global Git config and `GIT_CONFIG*` injection are removed. Local
`.git/config` is digest-bound and revalidated; enabled `extensions.worktreeConfig`,
`include.path`, and `includeIf.*.path` semantics fail closed before HTTPS binding,
and local HTTP/credential configuration that could change transport behavior is
rejected. Ambient proxy/curl/OpenSSL/Git HTTP/GCM/tracing behavior is removed or
neutralized. Client certificate/key and pinned-public-key behavior cannot arrive
through an unbound config source, and CMA does not depend on synthetic empty TLS
path/backend overrides to suppress them.

The current HTTPS credential contract rejects arbitrary helpers and helper-owned
network flows. It accepts one exact credential source file and derives, before
receipt consumption, one private Git credential-protocol response:

```text
username=<exact-reviewed-username>
password=<exact-secret>
```

The exact proposal-bound Git command shell then runs a generated helper snippet:

```text
get         -> shell-builtin read/printf of the frozen response file
store/erase -> no-op
other       -> no-op
```

There is no credential-manager executable, no `git-credential-store` executable,
and no nested `git credential-store` subprocess. A network-free bind-time probe
requires `read` and `printf` to resolve as builtins in the exact bound shell;
otherwise HTTPS admission fails closed.

The ordinary source-file digest and raw commitment key remain private. Serialized
proposal state receives only:

```text
SHA256(random-256-bit commitment key)
HMAC-SHA256(random-256-bit commitment key, exact credential source bytes)
```

This cryptographically binds the preparation to the approved credential source
without exposing an offline-testable ordinary password/token digest.

The source credential, CA and transport executables are held against replacement;
then the derived response is exclusively created in a private namespace and the
response file is locked before authority consumption.

## Real integration boundary

Two transport-level harnesses exist in addition to lifecycle mocks:

1. HTTPS loopback: runtime-generated TLS identity, reviewed hostname mapped to
   loopback by exact `curloptResolve`, Basic auth from frozen read-only credential
   source, real `git http-backend`, real receive-pack, real post-push ls-remote.
   OpenSSL CLI is used only to generate the loopback certificate/key; the Git
   client exercises the bound installation's compiled/default TLS backend. The
   harness skips when certificate generation is unavailable, not because Git lacks
   an OpenSSL backend.
2. SSH loopback: optional POSIX real `sshd`, ephemeral host/user keys, exact host
   key pin, real public-key auth, real receive-pack, real post-push ls-remote.

The SSH integration is optional when `sshd` is unavailable and intentionally does
not claim Windows server-path coverage.

Neither integration substitutes for Windows TxF/file-handle evidence.

## Source-review and executable-gate findings and repairs

The source review and exact-head Windows gates found and repaired the following
concrete defects or hidden authority edges before any PASS claim:

1. Git config canonical variable names are matched case-sensitively by
   `--get-regexp`; rewrite checks were corrected to canonical lower-case
   `insteadof/pushinsteadof` spellings.
2. SSH trust inputs originally accepted `expanduser()`, making `~` depend on
   ambient HOME; trust paths now require literal absolute identities.
3. OpenSSH can implicitly discover `<IdentityFile>-cert.pub`; execution uses an
   explicit blocked certificate path in a pinned private namespace.
4. `GIT_SSH_COMMAND` is interpreted through Git's command shell; the exact Git
   shell is now proposal-bound.
5. Git credential helpers are also shell-mediated; shell identity therefore
   belongs to HTTPS transport state as well.
6. OpenSSH file-path options support token/environment expansion; execution bundle
   paths are constrained and all `SSH_*` inheritance is removed.
7. DNS hostname alone was not an exact route; SSH and HTTPS now bind an explicit
   resolved IP while preserving reviewed host identity for host-key/TLS checks.
8. SSH preparation initially copied the private key during preview; copies are now
   materialized only under authorized lifecycle and are locked before receipt
   consumption.
9. HTTPS initially accepted an arbitrary exact credential helper. Git invokes
   helpers with `get/store/erase`, and credential managers may perform OAuth/network
   flows; HTTPS v1 was narrowed to a frozen read-only credential path.
10. An early HTTPS pin plan would have required marker creation in executable
    installation directories such as Program Files. The accepted M2.5 model is
    retained: writable `.git` namespace pin plus deny-write/delete handles for
    external exact files.
11. SSH local materialization initially happened after receipt consumption;
    materialization/lock/revalidation now complete before consumption.
12. HTTPS ambient `CURL_*`, `OPENSSL_*`, TLS key logging and Git curl tracing were
    not fully cleared; those channels are now removed.
13. `git-remote-https` was hashed but helper lookup was not explicitly routed to
    the bound executable directory; execution now sets exact `GIT_EXEC_PATH`.
14. Serializing a normal credential-file digest would create an offline token
    oracle, while omitting identity would permit preparation substitution. A
    random-key HMAC commitment now provides exact binding without exposing the key.
15. The first HTTPS loopback fixture embedded a manually supplied certificate/key
    pair; it now generates a fresh cryptographically matched SAN certificate at
    runtime through OpenSSL instead of trusting fixture crypto.
16. OpenSSH can also probe `<IdentityFile>.pub` before extracting the public key
    from the private identity. A second pinned directory sentinel now occupies
    that exact sibling path, so neither implicit public-key nor certificate files
    can appear after authorization-state revalidation.
17. The first Windows focused gate exposed a non-portable assumption that
    `git-credential-store` must exist as a separate file in `git --exec-path`.
    Seventeen focused failures cascaded from that one assumption. The second
    candidate instead reused the exact Git executable and invoked
    `git credential-store` for `get`.
18. A changed remote-tracking ref could make the rebuilt intent become a no-op and
    therefore raise only an initial-admission `InvalidGitMutationError`, even
    though during execution this is pre-consumption drift. Mutable intent-state
    failures now use the narrow `NetworkGitPushIntentStateError`, which is both an
    invalid initial intent and a `GitMutationPreconditionChangedError` when the
    same side-effect-free builder is reused for revalidation.
19. One SSH governance regression still expected the older ambiguous
    `ssh://git@example.com:22~/...` review spelling. Production had already moved
    to the explicit home-relative form `ssh://git@example.com:22/~/...`; the stale
    test was corrected without weakening production canonicalization.
20. The second Windows focused gate reached the real Git credential protocol and
    showed that the generated nested `git credential-store ... get` path returned
    no password on the target Git-for-Windows environment (`fatal: unable to get
    password from user`). The audit does not guess an unproven internal cause.
    Instead, the nested Git subprocess was removed entirely. CMA now derives the
    credential response itself and the exact bound Git shell emits it using only
    admitted `read`/`printf` builtins; `store/erase` remain no-ops.
21. The same second gate exposed a pure test-fixture defect in the CA/credential
    drift regression: it reused one already-ahead repository and attempted to
    commit identical `ahead\n` bytes a second time. The test now resets to the
    original base before constructing its second independent preparation. No
    production behavior was changed for this fixture repair.
22. The portable TLS repair moved HTTPS execution to the bound Git installation's
    compiled/default backend, but two normative M2.5.1 documents still described
    the superseded OpenSSL-forced contract. The documentation now names
    `network-https-bound-git-default.v1` / `bound-git-default`, describes the
    config-source fail-closed boundary, and a regression test prevents those
    normative claims from silently drifting back to the obsolete contract.

## Hosted CI classification

Historical Draft workflow runs have shown jobs with `steps=null` and
`logs_url=null`. Those runs are classified only as:

```text
HOSTED CI NOT EXECUTED / PASS NOT CLAIMED
```

A top-level workflow conclusion without executable step/log evidence is not used
as code-regression evidence and is not treated as PASS.

## Required work before Ready

Before M2.5.1 can become Ready for Review:

1. create a new single-parent frozen candidate from the repaired reviewed tree;
2. obtain fresh focused and full Windows results for that exact candidate;
3. update the PR body with exact candidate identity and executable evidence;
4. perform final exact-head and review-thread checks after Ready;
5. merge only the exact tested head.

Expected test accounting, once calculated, is only an expectation and must not be
reported as PASS evidence before actual execution.
