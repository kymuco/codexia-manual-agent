"""One-shot human-supplied Read-Host input for explicit Windows PowerShell.

The original controller command remains unchanged in Gen2. The host's derived
PowerShell script is separately authorized as the actual process argv. The
human answer is carried only on a bounded stdin pipe, not in argv or Work.
"""
from __future__ import annotations

import base64
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class ReadHostRequest:
    target: str
    prompt: str


# Deliberately narrow: one literal prompt assigned to an environment variable.
# Other Read-Host forms (SecureString, dynamic prompts, repeated prompts) fail closed.
_ASSIGNMENT = re.compile(
    r"^[ \t]*\$env:(?P<target>[A-Za-z_][A-Za-z0-9_]*)[ \t]*=[ \t]*"
    r"Read-Host[ \t]+(?:\"(?P<dquoted>[^\"\r\n]{1,128})\"|'(?P<squoted>[^'\r\n]{1,128})')[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)
_OCCURRENCE = re.compile(r"\bRead-Host\b", re.IGNORECASE)


def read_host_prompt(command: str) -> str | None:
    request = read_host_request(command)
    return request.prompt if request is not None else None


def read_host_request(command: str) -> ReadHostRequest | None:
    if type(command) is not str:
        raise ValueError("PowerShell command must be text")
    occurrences = list(_OCCURRENCE.finditer(command))
    if not occurrences:
        return None
    matches = list(_ASSIGNMENT.finditer(command))
    if len(occurrences) != 1 or len(matches) != 1:
        raise ValueError(
            "Only one literal '$env:NAME = Read-Host \"prompt\"' is supported"
        )
    match = matches[0]
    if not (match.start() <= occurrences[0].start() < match.end()):
        raise ValueError("Read-Host source is ambiguous")
    return ReadHostRequest(
        target=match.group("target"),
        prompt=match.group("dquoted") or match.group("squoted"),
    )


def stdin_for_answer(answer: str) -> bytes:
    if type(answer) is not str or not answer or "\r" in answer or "\n" in answer:
        raise ValueError("Read-Host value must be one nonempty line")
    encoded = answer.encode("utf-8")
    if len(encoded) > 2048 or "\x00" in answer:
        raise ValueError("Read-Host value exceeds the bounded input policy")
    return base64.b64encode(encoded) + b"\n"


def wrap_read_host(command: str, prompt: str) -> str:
    if read_host_prompt(command) != prompt:
        raise ValueError("Read-Host prompt differs from the approved command")
    escaped = prompt.replace("'", "''")
    prelude = (
        "function global:Read-Host {\n"
        "  param([string]$Prompt)\n"
        f"  if ($Prompt -cne '{escaped}') {{ throw 'Unexpected Read-Host prompt' }}\n"
        "  if ($script:__codexia_input_consumed) { throw 'Read-Host input already used' }\n"
        "  $script:__codexia_input_consumed = $true\n"
        "  $encoded = [Console]::In.ReadLine()\n"
        "  if ($null -eq $encoded) { throw 'Read-Host input missing' }\n"
        "  return [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($encoded))\n"
        "}\n"
    )
    script = prelude + "& {\n" + command + "\n}\n"
    if len(script) > 12_000:
        raise ValueError("derived PowerShell command exceeds explicit profile limit")
    return script
