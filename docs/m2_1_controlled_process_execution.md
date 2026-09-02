# M2.1 — Controlled Process Execution

## Goal

Introduce the first real side-effect executor behind the repaired M2.0 authority
spine without exposing arbitrary process execution to the remote model.

M2.1 is a **local human-authorized execution surface**, not a general OS sandbox.
An explicitly approved executable may itself read, write, use the network, or
spawn other programs while it is alive. Because `execute_process` cannot enforce
those downstream capabilities by itself, the M1.1 remote-model protocol remains
read-only.

## Canonical path

```text
structured argv
→ local ActionProposal(EXECUTE_PROCESS)
→ executable/cwd/environment identity bound into proposal digest
→ local human authorization
→ AUTHORIZED
→ revalidate executable bytes and canonical cwd
→ establish platform process-tree containment
→ consume one-shot receipt
→ subprocess creation with shell=False
→ prove approved-target startup
→ EXECUTED / OBSERVED
```

Containment prerequisites are checked before the receipt is consumed whenever
possible. Linux requires Bubblewrap at `/usr/bin/bwrap`; Windows uses native Job
Objects.

## CLI

Use `--` to separate Codexia options from the exact child argv:

```powershell
codexia exec `
  --workspace W:\dev\some-repository `
  --approve `
  --timeout 60 `
  -- `
  python -m unittest discover -s tests -v
```

Without `--approve`, `always` and `risky` modes stop at the explicit-human
approval boundary. `never` denies execution even if `--approve` is supplied.

## Structured argv

M2.1 never evaluates a shell string. The executable and every argument remain
separate argv values and child creation uses `shell=False`.

Common shell interpreters (`cmd`, PowerShell, `sh`, `bash`, and related shells)
are rejected from this surface. This is defense in depth rather than a complete
sandbox: another explicitly approved executable can still implement arbitrary
behavior.

## Executable identity

At proposal creation Codexia records:

- original structured argv;
- canonical resolved executable path;
- executable byte size;
- SHA-256 of the executable;
- canonical workspace-relative cwd;
- the exact minimal environment profile;
- timeout/stdout/stderr budgets.

Immediately before receipt consumption the executor resolves and hashes the
executable again. A path-resolution or byte change rejects execution while the
receipt is still unused.

This narrows approval/execution drift. M2.1 does not claim to eliminate every
platform-level TOCTOU race between the final identity check and OS process
creation.

## Workspace-bound cwd

The process cwd must be relative to the canonical workspace root. Absolute cwd,
`..` escapes, missing directories, and symlink resolutions outside the workspace
are rejected before approval consumption.

This is a cwd boundary, not a filesystem sandbox for the child process.

## Environment isolation

The approved process does not inherit the parent environment. M2.1 supplies only
the `minimal-v1` profile:

- `CODEXIA_EXECUTION=1`;
- UTF-8 Python controls;
- platform root/locale variables when needed.

Credentials, arbitrary tokens, `HOME`, user PATH additions, cloud secrets, and
other host variables are not copied automatically. The resolved executable path
is absolute, so target creation does not require an inherited user PATH.

On Linux Bubblewrap and the fixed internal exec trampoline receive this same
minimal environment. Target argv remains ordinary separate argv entries rather
than being serialized into one environment variable, so the accepted aggregate
argv budget is not collapsed into Linux's per-string environment limit.

## Process-tree containment

M2.1 requires the complete launched tree to remain bounded even if descendants
create new sessions, double-fork, or outlive the direct root process.

### Windows

The root process is created with `CREATE_SUSPENDED`, assigned to a Job Object,
and only then resumed. This removes the spawn-before-assignment race. Timeout or
output-budget termination targets the Job Object, and cleanup also covers failed
authorization consumption and failed Job Object assignment.

### Linux

Linux execution requires Bubblewrap at `/usr/bin/bwrap`. Codexia creates a
private PID namespace using:

```text
bwrap
  --unshare-pid
  --die-with-parent
  --bind / /
  --proc /proc
  --chdir <approved cwd>
  --json-status-fd <status fd>
  -- <fixed exec trampoline> <approved executable> <approved argv...>
```

The trampoline has one narrow purpose: prove whether the approved executable
itself reached `execve`. It writes `READY`, marks a dedicated status FD
`FD_CLOEXEC`, then calls `execve` with the approved executable and original argv.
Successful exec closes that FD automatically; failed exec writes a bounded error
record instead. Codexia requires both Bubblewrap's reported sandbox-child PID and
the trampoline `READY → CLOEXEC EOF` transition before recording `started=true`
or a target PID. A regular file that exists but is not executable is therefore a
`spawn_error` with `started=false`, rather than a false successful execution.

Bubblewrap supplies the PID-namespace PID 1 reaper. A target cannot escape the
PID namespace with `setsid()`, `start_new_session=True`, or double-forking. If
the approved root exits while descendants remain, Bubblewrap teardown removes
those descendants rather than allowing them to outlive the authorized root. On
timeout/output overflow Codexia terminates the host-side Bubblewrap process;
`--die-with-parent` plus PID-namespace teardown removes the remaining sandbox
processes.

The root filesystem is intentionally bind-mounted read/write and the host network
namespace is retained. Bubblewrap is used here as a PID-tree containment
primitive, **not** as a filesystem or network permission boundary.

#### Ubuntu 24.04 AppArmor setup

Ubuntu 24.04 restricts unprivileged user namespaces through AppArmor. Installing
`bubblewrap` alone may therefore leave `/usr/bin/bwrap` unable to create the PID
namespace. Ubuntu provides a purpose-built profile in the `apparmor-profiles`
package, but it is not enabled automatically on Noble.

A supported setup is:

```bash
sudo apt-get update
sudo apt-get install -y bubblewrap apparmor-profiles apparmor-utils
sudo install -m 0644 \
  /usr/share/apparmor/extra-profiles/bwrap-userns-restrict \
  /etc/apparmor.d/bwrap-userns-restrict
sudo apparmor_parser -r /etc/apparmor.d/bwrap-userns-restrict
```

CI performs the same setup and then executes a standalone Bubblewrap PID-
namespace preflight before installing/running the Python test suite. This makes
containment support an explicit platform gate instead of letting every process
execution fail later with an opaque wrapper exit code.

Other Linux distributions may require distribution-specific user-namespace or
LSM configuration. Other POSIX platforms fail closed in M2.1 rather than
claiming process-tree containment that the runtime cannot enforce.

## Timeout and output budgets

Default limits:

- timeout: 30 seconds;
- stdout: 64 KiB;
- stderr: 64 KiB.

Timeout is bounded to 0.05–3600 seconds. Each output budget is bounded to
1 byte–16 MiB.

If a timeout or output budget is exceeded, Codexia terminates the contained
process tree. Stream collectors retain bounded payloads while hashing and
counting all observed bytes. The Linux target-start handshake shares the same
overall execution deadline and has a maximum startup window of five seconds.

## Exact observation

Each output stream records:

- number of bytes actually observed;
- SHA-256 over all observed bytes;
- bounded stored bytes as base64;
- UTF-8 text view when the stored bytes are valid UTF-8;
- whether stored bytes are truncated.

The process observation also records:

- proposal and authorization receipt identity;
- execution id;
- whether the approved executable itself was proven started;
- target PID when the platform proves its identity without confusing a helper
  process with the approved target;
- approved absolute executable argv;
- cwd;
- exit code;
- termination reason (`exited`, `timeout`, `output_limit`, `spawn_error`);
- duration;
- a SHA-256 observation digest.

On Linux Bubblewrap's JSON status supplies the sandbox child host PID, while the
CLOEXEC handshake proves that the same child successfully replaced the internal
trampoline with the approved executable. Bubblewrap propagates the initial
command's exit status on normal completion.

A spawn or approved-target exec failure after authorization consumption is itself
recorded as an execution attempt and observation. The consumed receipt is not
reusable.

## Deliberate non-goals

M2.1 does not add:

- a process tool to the remote model;
- shell-string execution;
- a general filesystem/network sandbox;
- write/apply-patch APIs;
- Git commit or push;
- durable receipt/event persistence across process restarts.

Those boundaries are handled by later M2.x/M3 work rather than being silently
inferred from generic process permission.