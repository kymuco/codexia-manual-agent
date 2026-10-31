"""The opt-in project goal informs choices; it never grants execution."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from tools import dw6_worker_continuation_v1 as wc


def _binding(tmp_path):
    return SimpleNamespace(
        workspace=str(tmp_path),
        project="player-intelligence",
        repo="kymuco/player-intelligence",
    )


def _record():
    return {"task": "Choose the next bounded engineering step for the player."}


def test_missing_goal_is_backward_compatible(tmp_path):
    binding = _binding(tmp_path)
    assert wc._project_brief(binding) is None
    msg = wc._prompt(binding=binding, record=_record())
    assert "Project goal" not in msg
    assert "not permission to execute commands" in msg
    assert len(msg) < 1100


def test_worker_sees_product_goal_and_reuses_existing_approval(tmp_path):
    binding = _binding(tmp_path)
    folder = tmp_path / ".codexia"
    folder.mkdir()
    brief = (
        "# Player Intelligence\n"
        "Deliver a usable real-account progression advisor in the existing "
        "Web UI. Focus on verified improvements, costs, and re-observation. "
        "Avoid another standalone coverage audit when implementation is justified."
    )
    (folder / "PROJECT_BRIEF.md").write_text(brief, encoding="utf-8")
    message = wc._prompt(binding=binding, record=_record())
    assert brief in message
    assert "Compare feasible approaches by value" in message
    assert "human discussion" in message
    assert "not execution authority" in message
    assert "separate human approval" in message
    assert "W:" not in message
    assert "SHA-256" not in message


def test_goal_size_and_encoding_are_bounded(tmp_path):
    folder = tmp_path / ".codexia"
    folder.mkdir()
    path = folder / "PROJECT_BRIEF.md"
    binding = _binding(tmp_path)
    path.write_bytes(b"x" * 8193)
    with pytest.raises(ValueError, match="too large"):
        wc._project_brief(binding)
    path.write_bytes(b"invalid " + bytes([255]) + b" data" * 20)
    with pytest.raises(ValueError, match="UTF-8"):
        wc._project_brief(binding)
    path.write_text("too short", encoding="utf-8")
    with pytest.raises(ValueError, match="80..8192"):
        wc._project_brief(binding)


def test_goal_rejects_symlink_file(tmp_path):
    folder = tmp_path / ".codexia"
    folder.mkdir()
    target = tmp_path / "outside.md"
    target.write_text("External goal " * 20, encoding="utf-8")
    link = folder / "PROJECT_BRIEF.md"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable for this test environment")
    with pytest.raises(ValueError, match="regular local file"):
        wc._project_brief(_binding(tmp_path))
