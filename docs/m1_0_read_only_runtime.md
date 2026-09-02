# M1.0 — Read-Only Runtime Contracts

## Goal

Establish a locally testable Codexia runtime without granting a model network,
shell, or write authority.

## Shipped surface

```text
codexia --version
codexia run
codexia resume
codexia sessions
codexia inspect read
codexia inspect list
codexia inspect search
codexia inspect git-status
```

`run` creates a local manifest only. It does not contact ChatGPT or another
model provider.

## Runtime contracts

- `ModelProvider` is a port with no live implementation in M1.0.
- `WorkspaceReader` exposes four bounded read-only operations.
- `SessionStore` persists local manifests atomically.
- `InspectWorkspaceService` converts structured requests into exact
  observations.
- Session capability is exactly `read_workspace`.

## Workspace governance

The filesystem adapter:

- rejects absolute paths;
- rejects `..` and symlink escapes after canonical resolution;
- applies file, entry, match, and traversal limits;
- skips common internal and generated directories;
- rejects binary and oversized file reads;
- never exposes an arbitrary command surface.

`git_status` uses one fixed argv:

```text
git -C <workspace> status --short --branch --untracked-files=normal
```

The model cannot change the executable, arguments, working directory, timeout,
or environment through a tool request. This is a dedicated read-only adapter,
not the future `execute_process` capability.

## Deferred to M1.1

- `chatgpt-web-adapter`;
- conversation transport;
- model/tool iteration;
- structured model response parsing;
- live session resume.

## Forbidden before M2

- arbitrary shell commands;
- file mutation;
- patch application;
- Git mutation;
- network actions;
- approval automation.
