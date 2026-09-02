# Security Policy

Codexia contains security-sensitive code for local process execution, filesystem mutation, Git operations, persistent authority state, and model/provider integration. Please treat security reports as potentially sensitive even when a bug appears to affect only a development configuration.

## Reporting a vulnerability

Please **do not open a public issue** for a vulnerability that could expose credentials, escape an authority boundary, bypass authorization, weaken containment, corrupt durable evidence, or enable unintended mutation/network access.

Use GitHub's private vulnerability reporting for this repository when that option is available. If it is not available, contact the maintainer through a private channel associated with the GitHub account/repository rather than publishing exploit details.

A useful report includes:

- the affected commit/version and platform;
- the capability or trust boundary involved;
- a minimal reproduction or failing test;
- the expected and observed behavior;
- the security impact;
- whether live credentials or private data were involved.

Do not include active secrets in a report. If a real secret has been exposed, revoke or rotate it immediately.

## Secrets and authentication material

Never commit ChatGPT/web-session authentication state, cookies, access tokens, API keys, private keys, Codex account files, or other live credentials.

The optional `chatgpt-web-adapter` and other model/provider transports are **not** security boundaries. Local execution and mutation authority must remain inside Codexia's explicit admission, authorization, execution, and observation layers.

## Public-release audit boundary

Changing an existing private GitHub repository to Public exposes more than ordinary reachable Git objects. A release decision therefore requires two independent gates:

```text
python scripts/public_release_audit.py
python scripts/github_public_surface_audit.py --repo <owner/name>
```

`public_release_audit.py` audits reachable Git object/ref data only. It must run from the canonical repository root in a complete, non-shallow, non-partial clone and pins one Git executable outside the audited workspace while removing inherited `GIT_*` repository redirection.

`github_public_surface_audit.py` audits GitHub-side metadata such as issues, pull requests, issue comments, review comments, commit comments, releases, workflow-run metadata, and artifact metadata. Enabled Wiki, Pages, and Discussions surfaces are explicitly treated as requiring separate content review. Existing Actions run logs, artifact payloads, and release-asset payloads are independent publication surfaces and require explicit content audit or an explicitly recorded review decision; their existence is never inferred to be safe from Git history.

A `GIT_PASS` result from the local audit is therefore **not** a repository-publication PASS by itself. Repository visibility must not change until both surfaces have been reviewed and all HIGH findings are resolved.

## Supported versions

Codexia is alpha software. Security fixes target the current development line; old commits, historical prompt versions, and archived experimental artifacts are retained primarily for provenance and should not be assumed to receive security backports.

## Disclosure

Please allow time for a fix and coordinated disclosure before publishing exploit details. After a vulnerability is resolved, a public issue or advisory can document the impact and repair without exposing live credentials or unnecessary private data.
