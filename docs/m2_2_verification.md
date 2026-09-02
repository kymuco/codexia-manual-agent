# M2.2 verification checklist

The M2.2 merge gate requires all of the following:

- known families construct exact local argv;
- model-supplied executable/raw argv/risk/capabilities/approval fields are rejected;
- `python_version` and `git_version` require human authorization rather than auto-execution;
- `python_compileall` is rejected by the default `execute_process`-only envelope;
- `python_unittest_discover` is rejected as unbounded repository-controlled child code;
- expanding an admission policy capability set cannot silently expand the M2.2→M2.1 proposal bridge;
- approval previews contain no authority tokens;
- the existing M1.1 parser still rejects `process_request`;
- `ToolName` remains the four read-only tools;
- full Ubuntu/Windows Python 3.11–3.13 CI passes;
- both web-provider contract jobs pass;
- Codex review has no current P1/P2 findings on the merge head.
