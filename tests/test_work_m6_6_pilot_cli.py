from __future__ import annotations

from codexia_manual_agent.work.pilot_cli import _build_parser


def test_pilot_cli_exposes_start_drive_answer_and_status() -> None:
    parser = _build_parser()

    start = parser.parse_args(
        ["start", "Produce the delegated result.", "--conversation-id", "conversation-1"]
    )
    assert start.command == "start"
    assert start.conversation_id == "conversation-1"

    drive = parser.parse_args(["drive", "00000000-0000-0000-0000-000000000001"])
    assert drive.command == "drive"
    assert drive.max_steps == 32

    answer = parser.parse_args(
        [
            "answer",
            "00000000-0000-0000-0000-000000000001",
            "Use direction B.",
        ]
    )
    assert answer.command == "answer"
    assert answer.answer == "Use direction B."

    status = parser.parse_args(["status", "00000000-0000-0000-0000-000000000001"])
    assert status.command == "status"
