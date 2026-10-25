"""Bounded Read-Host behavior; real WinPS execution is tested on Windows."""
import os
import base64

import pytest

from codexia_manual_agent.execution.powershell_read_host import (
    read_host_prompt,
    stdin_for_answer,
    wrap_read_host,
)

SAMPLE = """$env:PI_R1D_UID = Read-Host "Genshin UID"
Write-Output $env:PI_R1D_UID"""


def test_detect_single_literal_read_host_and_preserve_original_code():
    assert read_host_prompt(SAMPLE) == "Genshin UID"
    assert read_host_prompt("Write-Output 1") is None
    assert read_host_prompt("$env:X = read-host 'UID'") == "UID"
    wrapped = wrap_read_host(SAMPLE, "Genshin UID")
    assert SAMPLE in wrapped
    assert "[Console]::In.ReadLine()" in wrapped
    assert "-NonInteractive" not in wrapped
    assert "123456789" not in wrapped


def test_reject_unsupported_prompts_and_bound_input():
    for code in ["Read-Host 'UID'", "Read-Host -AsSecureString", "$env:X = Read-Host 'One'\n$env:Y = Read-Host 'Two'"]:
        with pytest.raises(ValueError, match="Only one literal"):
            read_host_prompt(code)
    for answer in ["", "a\nb", "x" * 2100]:
        with pytest.raises(ValueError):
            stdin_for_answer(answer)
    assert base64.b64decode(stdin_for_answer("абв").strip()) == "абв".encode()


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell host only")
def test_real_powershell_read_host_via_bounded_stdin(tmp_path):
    from codexia_manual_agent.application.execute_process import ExecuteProcessService
    from codexia_manual_agent.execution.process_contained import POWERSHELL_PROFILE
    source = "$env:PI_INPUT = Read-Host 'Input Label'\nWrite-Output ('LEN=' + $env:PI_INPUT.Length)"
    cmd = wrap_read_host(source, "Input Label")
    result = ExecuteProcessService().run(
        workspace=tmp_path,
        argv=["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", cmd],
        shell_profile=POWERSHELL_PROFILE,
        stdin_bytes=stdin_for_answer("123456789"),
        approved=True,
    )
    observation = result.observation
    assert observation.exit_code == 0, observation.stderr.text_utf8
    assert "LEN=9" in observation.stdout.text_utf8
    assert "123456789" not in observation.stdout.text_utf8
