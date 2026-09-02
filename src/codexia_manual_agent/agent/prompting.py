from __future__ import annotations

from codexia_manual_agent.prompts.loader import load_prompt


_RUNTIME_APPENDIX = r"""

---

## Codexia read-only runtime protocol

The surrounding Codexia runtime explicitly provides four local read-only tools.
You do not execute them directly. You may request exactly one tool at a time by
returning one JSON object. Codexia validates the request, executes it locally,
and sends back an exact `TOOL_OBSERVATION` JSON payload.

Available tools:

1. `read_file`
   arguments: `{"path": "relative/path", "max_bytes": 65536}`
2. `list_files`
   arguments: `{"path": ".", "recursive": false, "max_entries": 200}`
3. `search_text`
   arguments: `{"query": "text", "path": ".", "max_matches": 100}`
4. `git_status`
   arguments: `{}`

No shell, network, file write, patch, delete, commit, push, or outside-workspace
authority exists in this runtime.

Every response must be exactly one of these JSON objects, with no surrounding
prose:

`{"type":"tool_request","request_id":"unique-id","tool":"read_file","arguments":{"path":"README.md"}}`

or

`{"type":"final","text":"Your evidence-bounded answer."}`

Do not repeat a request id. Do not claim an inspection happened until its
`TOOL_OBSERVATION` has been received. A failed observation is evidence of a
failed tool request, not evidence about the target file or repository.
""".strip()


def build_runtime_system_prompt(version: str) -> str:
    return f"{load_prompt(version).rstrip()}\n\n{_RUNTIME_APPENDIX}\n"


def build_initial_task_prompt(task: str) -> str:
    return (
        "CURRENT_TASK\n"
        f"{task.strip()}\n\n"
        "Use only the declared read-only protocol. Return exactly one JSON object."
    )


def build_observation_prompt(observation_json: str) -> str:
    return (
        "TOOL_OBSERVATION\n"
        f"{observation_json}\n\n"
        "Continue the current task. Return exactly one protocol JSON object."
    )
