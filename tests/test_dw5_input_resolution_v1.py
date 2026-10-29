"""Input authority and provenance, with no CWA/model inference."""
import json
import os

import pytest

from codexia_manual_agent.execution.powershell_input_resolution_v1 import (
    ProjectInputStore, sensitive_input,
)
from codexia_manual_agent.execution.powershell_read_host import read_host_request


def test_read_host_request_extracts_exact_target_and_prompt():
    req = read_host_request('$env:PI_R1D_UID = Read-Host "Genshin UID"')
    assert (req.target, req.prompt) == ("PI_R1D_UID", "Genshin UID")
    assert read_host_request("Write-Output 1") is None
    with pytest.raises(ValueError):
        read_host_request("Read-Host 'UID'")


def test_unknown_input_requires_human(tmp_path, monkeypatch):
    monkeypatch.delenv("CODEXIA_INPUT_PI_R1D_UID", raising=False)
    store = ProjectInputStore(tmp_path / "data.json")
    assert store.lookup("player-intelligence", "PI_R1D_UID", "Genshin UID") is None
    assert not store.path.exists()


def test_explicit_operator_environment_is_known_without_persistence(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEXIA_INPUT_PI_R1D_UID", "UID_SAMPLE")
    store = ProjectInputStore(tmp_path / "data.json")
    assert store.lookup("player-intelligence", "PI_R1D_UID", "Genshin UID") == ("UID_SAMPLE", "operator-env")
    assert not store.path.exists()


def test_consent_scopes_cached_value_to_prompt_and_project(tmp_path, monkeypatch):
    monkeypatch.delenv("CODEXIA_INPUT_PI_R1D_UID", raising=False)
    store = ProjectInputStore(tmp_path / "data.json")
    store.remember("player-intelligence", "PI_R1D_UID", "Genshin UID", "UID_SAMPLE")
    assert store.lookup("player-intelligence", "PI_R1D_UID", "Genshin UID") == ("UID_SAMPLE", "human-saved")
    assert store.lookup("other", "PI_R1D_UID", "Genshin UID") is None
    assert store.lookup("player-intelligence", "PI_R1D_UID", "Other UID") is None
    assert json.loads(store.path.read_text())["schema_version"] == 1


def test_secrets_never_reuse_or_persist_even_when_present(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEXIA_INPUT_LOGIN_PASSWORD", "not_a_secret")
    store = ProjectInputStore(tmp_path / "data.json")
    assert sensitive_input("LOGIN_PASSWORD", "Password")
    assert store.lookup("player-intelligence", "LOGIN_PASSWORD", "Password") is None
    with pytest.raises(ValueError, match="cannot be persisted"):
        store.remember("player-intelligence", "LOGIN_PASSWORD", "Password", "not_a_secret")


def test_invalid_values_and_corrupt_store_fail_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEXIA_INPUT_PI_R1D_UID", "two\nlines")
    store = ProjectInputStore(tmp_path / "data.json")
    with pytest.raises(ValueError):
        store.lookup("player-intelligence", "PI_R1D_UID", "Genshin UID")
    monkeypatch.delenv("CODEXIA_INPUT_PI_R1D_UID", raising=False)
    store.path.write_text('{"schema_version": 2, "values": {}}')
    with pytest.raises(ValueError, match="schema"):
        store.lookup("player-intelligence", "PI_R1D_UID", "Genshin UID")
