"""Pin the project-local Python bytes in an approved PowerShell proposal."""
import os

import pytest


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell proposal only")
def test_project_venv_selected_instead_of_codexia_runtime(tmp_path):
    from codexia_manual_agent.execution.process_contained import _shell_python

    interpreter = tmp_path / ".venv" / "Scripts" / "python.exe"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_bytes(b"test executable identity")
    assert _shell_python(tmp_path) == interpreter.resolve(strict=True)


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell proposal only")
def test_project_venv_identity_must_still_match_after_approval(tmp_path):
    from codexia_manual_agent.execution.process_contained import (
        POWERSHELL_PROFILE, _validate_process_proposal, prepare_process_proposal,
    )
    from codexia_manual_agent.domain.errors import ProcessExecutableChangedError

    interpreter = tmp_path / ".venv" / "Scripts" / "python.exe"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_bytes(b"project-runtime-before")
    proposal = prepare_process_proposal(
        workspace=tmp_path,
        argv=["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", "Write-Output 1"],
        shell_profile=POWERSHELL_PROFILE,
    )
    parameters = proposal.to_dict()["parameters"]
    assert parameters["shell_python"] == str(interpreter.resolve(strict=True))
    assert parameters["shell_python_size"] == len(b"project-runtime-before")
    _validate_process_proposal(proposal)

    interpreter.write_bytes(b"project-runtime-after")
    with pytest.raises(ProcessExecutableChangedError, match="Python bytes changed"):
        _validate_process_proposal(proposal)
