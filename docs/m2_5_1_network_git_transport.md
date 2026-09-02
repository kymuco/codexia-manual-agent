# M2.5.1 — Governed Network Git Transport

## Purpose

M2.5.1 extends the existing `git.push.v1` authority from the M2.5 local bare
`file://` backend to governed SSH and HTTPS transports without turning a reviewed
Git URL into authority for ambient SSH configuration, proxy routing, arbitrary
credential helpers, helper-owned OAuth flows, implicit refspecs, or generic
network/process execution.

The authority separation remains unchanged:

```text
workspace patch authority != git commit authority != git push authority
```

All three backends (`file`, `ssh`, `https`) remain implementations of the same
`Capability.GIT_PUSH / git.push.v1` action.

## Common network intent

Network proposal construction is deliberately side-effect free with respect to
the remote. Before HUMAN authorization it does not run `ls-remote`, fetch, push,
SSH, TLS, credential-helper network flows, or any other remote-state discovery.

A network proposal binds:

- canonical workspace and exact Git executable identity;
- exact local symbolic ref and exact local commit OID;
- exact local Git config identity;
- one configured remote name and exactly one admitted push URL;
- canonical transport, host, port and repository path;
- one explicit `refs/heads/...` destination;
- one local remote-tracking ref and its exact expected-old OID;
- a local fast-forward proof from expected-old to the approved local OID;
- transport-specific trust/executable identities;
- the exact human review surface and proposal digest.

Multiple push URLs, URL rewrite rules, receive-pack overrides, mirror behavior and
configured push options fail closed.

## Exact remote compare-and-swap

Execution sends the approved commit OID itself, not a live `HEAD` name:

```text
<approved-local-oid>:<destination-ref>
```

and uses only the stable explicit lease form:

```text
--force-with-lease=<destination-ref>:<expected-old-oid>
```

The lease is an exact remote CAS guard, not generic force authority. Branch
creation/deletion, tags, mirror, `--all`, wildcard refspecs, implicit upstreams
and arbitrary force behavior remain outside this milestone.

## Terminal observation

A network command return code is not terminal truth. After each push attempt the
same bound transport queries the exact destination ref.

Classification retains M2.5 semantics:

- `APPLIED`: exact approved target OID is observed, even if our push returned
  nonzero because another actor independently reached the same target;
- `REJECTED`: the push/lease failed and a different terminal OID is observable;
- `MISMATCH`: the push reported success but the terminal ref identity differs;
- `INCOMPLETE`: exact terminal state cannot be established.

## SSH backend — `network-ssh-direct.v1`

### Proposal-bound identity

The SSH backend binds:

- exact OpenSSH executable path/size/SHA-256;
- exact Git command-shell executable identity;
- explicit SSH user;
- canonical reviewed host and port;
- exact repository path;
- one exact resolved IPv4/IPv6 route address;
- one exact private-key source identity;
- exactly one admitted known-host key and SHA256 host-key fingerprint.

SSH user/system configuration is not authority. The generated command uses
`-F none` and an isolated configuration surface. Ambient `SSH_*` and shell-startup
environment variables are removed.

The execution options explicitly disable or neutralize agent/default-identity
inheritance, password/keyboard-interactive fallback, proxy/jump routing,
forwarding, DNS host-key trust, host-key learning, local commands and connection
multiplexing. `HostName` is the proposal-bound IP route while `HostKeyAlias`
remains the human-reviewed hostname, preserving exact host-key pin semantics.

`ssh -G` is run as a network-free admission probe to verify that the installed
OpenSSH actually interprets the generated option set with the intended effective
route, user, identity set and trust databases.

### Secret materialization and authority order

Preview construction does **not** copy the private key. It creates only an empty
private bundle namespace plus two directory sentinels:

```text
identity.pub
identity-cert.pub
```

These occupy the exact implicit sibling names OpenSSH may probe for a public key
or certificate. The sentinels are namespace-pinned before execution so a new
unbound sibling file cannot appear after final local revalidation.

After a HUMAN receipt exists, but before it is consumed:

```text
read-only proposal revalidation
    -> source/executable/namespace pin admission
    -> second exact revalidation
    -> exclusive-create exact private-key + known-hosts copies
    -> deny-write/delete lock of the materialized copies
    -> materialized-plan revalidation
    -> consume one-shot HUMAN receipt
    -> exact SSH Git push
    -> exact SSH ls-remote observation
```

If local materialization, namespace admission or locking fails, the receipt is not
consumed and no network operation is opened.

## HTTPS backend — `network-https-bound-git-default.v1`

### TLS and route boundary

The HTTPS contract is HTTPS-only. Its TLS policy is `bound-git-default`: CMA binds
the exact Git executable and `git-remote-https` helper, then allows that bound Git
installation to select its compiled/default TLS backend. CMA does not override
`http.sslBackend`.

The contract binds:

- exact Git executable identity;
- exact `git-remote-https` identity and resolved helper target;
- exact Git command-shell identity;
- exact CA-bundle path/size/SHA-256;
- `http.sslVerify=true`, the exact `http.sslCAInfo`, and an empty `http.sslCAPath`;
- `http.schannelUseSSLCAInfo=true`, so Git-for-Windows Schannel honors the bound CA;
- one exact resolved route address through `http.curloptResolve`;
- the original reviewed hostname for TLS SNI/certificate verification.

`GIT_EXEC_PATH` is generated from the parent directory of the exact bound
`git-remote-https`, so the runtime remote-helper lookup is not left to ambient
installation routing.

System/global Git config and `GIT_CONFIG*` injection are removed from the governed
environment. Local `.git/config` is digest-bound and revalidated; enabled
`extensions.worktreeConfig`, `include.path`, and `includeIf.*.path` semantics fail
closed before HTTPS transport binding. Local HTTP/credential settings that could
alter TLS, credentials, proxying, redirects, or remote behavior are also rejected.
This keeps client certificate/key and pinned-public-key policy out of unbound
configuration without relying on synthetic empty `http.sslCert`, `http.sslKey`,
`http.pinnedPubkey`, or TLS-backend command-line overrides.

Ambient proxy, curl/OpenSSL configuration, Git HTTP/TLS override variables,
cookies, redirects, extra headers, GCM settings, interactive prompting and TLS key
logging/tracing are removed or explicitly neutralized.

### Frozen shell-only credential response

M2.5.1 intentionally does **not** accept an arbitrary credential-helper
executable. Git credential helpers receive `get`, `store` and `erase`; helpers
such as credential managers may also perform their own OAuth/network activity.
That graph is broader than one approved Git destination.

HTTPS v1 therefore accepts one explicit credential **source file** containing
exactly one credential URL for the reviewed HTTPS host, port and repository path.
The source is keyed-commitment-bound in the proposal. Before receipt consumption,
CMA derives one private Git credential-protocol response file:

```text
username=<exact-reviewed-username>
password=<exact-secret>
```

The generated helper is a shell snippet executed by the exact proposal-bound Git
command shell:

```text
get         -> shell-builtin read/printf of the frozen private response file
store/erase -> no-op success
other       -> no-op success
```

There is no `git credential-store` subprocess, no `git-credential-store`
executable, no arbitrary helper executable, and no helper-owned OAuth/network
path. A network-free bind-time probe requires `read` and `printf` to resolve as
**shell builtins** in the exact bound Git shell; otherwise HTTPS admission fails
closed instead of silently invoking an ambient utility such as `cat.exe`.

The raw secret, a normal SHA256 digest of the credential source, and the private
commitment key are never serialized into proposal/preview/observation data.
Instead, proposal state contains a keyed commitment:

```text
commitment_key_sha256 = SHA256(random-256-bit-key)
secret_hmac_sha256    = HMAC-SHA256(random-256-bit-key, exact-credential-source)
```

The random key remains only in the in-memory preparation. This binds the exact
credential source while preventing the serialized commitment from becoming a
practical offline password/token oracle.

### HTTPS authority order

The credential source, CA, Git executable, Git shell and `git-remote-https` are
held against write/delete replacement before secret materialization. The derived
credential response is then created exclusively in an isolated bundle and itself
deny-write/delete locked before the receipt is consumed:

```text
read-only proposal revalidation
    -> transport/source file admission
    -> private credential-bundle namespace admission
    -> second exact revalidation
    -> derive exact username/password response from bound source
    -> exclusive-create private response file
    -> deny-write/delete lock of private response
    -> keyed-commitment + response + transport revalidation
    -> consume one-shot HUMAN receipt
    -> exact HTTPS Git push
    -> exact HTTPS ls-remote observation
```

`credential.useHttpPath=true` keeps credential lookup scoped to the exact
repository path. Redirects are disabled, so the command-scoped helper cannot be
silently redirected to a different credential context.

## Real transport integration evidence

The test suite contains transport-level harnesses in addition to mocked lifecycle
regressions:

- HTTPS: a loopback TLS smart-HTTP server fronts a real bare repository through
  `git http-backend`, requires Basic authentication, and exercises a real
  receive-pack plus post-push `ls-remote`. The certificate is generated at test
  runtime with SAN `example.test`; the governed route maps that reviewed hostname
  to `127.0.0.1` through the exact `curloptResolve` binding. OpenSSL CLI is used
  only to generate the loopback certificate/key; the Git client executes through
  the bound installation's compiled/default TLS backend. The integration skips if
  the certificate-generation CLI is unavailable, not because Git lacks an OpenSSL
  backend.
- SSH: an optional POSIX loopback integration starts a real local `sshd` with
  ephemeral host/user keys and exercises real public-key authentication,
  receive-pack and post-push observation. It is intentionally not presented as a
  Windows server-path proof.

Windows TxF/file-handle admission remains a separate exact-head Windows gate.
Loopback integration does not substitute for that evidence.

## Explicit non-claims

- No generic `git push <argv>` authority.
- No branch creation/deletion, tags, mirror, `--all`, wildcard refspecs or multiple
  destinations.
- No fetch/reset/rebase/merge network authority.
- No SSH proxy/jump/agent/hardware-token provider inheritance.
- No plain HTTP.
- No ambient HTTP proxy inheritance.
- No arbitrary HTTPS credential-helper executables or credential-manager OAuth
  flows in v1.
- No secret values or normal password-derived digests in serialized proposal,
  preview or observation payloads.
- No claim that hosted CI executed when GitHub exposes no job steps/logs.

## Validation boundary before Ready

M2.5.1 is not Ready for Review until the implementation is squashed onto the exact
M2.5 base, all source-review and executable-gate findings are closed, and fresh
Windows evidence is obtained for the exact frozen candidate. Expected test counts
are not PASS evidence; only actual exact-head execution is.
