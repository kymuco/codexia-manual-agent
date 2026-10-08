from __future__ import annotations

"""Read one completed CWA synthesis turn without sending a model request."""

import argparse
import json


def extract_turn(messages, *, request_message_id: str, response_message_id: str):
    users = [
        (index, item)
        for index, item in enumerate(messages)
        if item.role == "user" and item.message_id == request_message_id
    ]
    answers = [
        (index, item)
        for index, item in enumerate(messages)
        if item.role == "assistant" and item.message_id == response_message_id
    ]
    if len(users) != 1 or len(answers) != 1:
        raise ValueError("CWA target request/response identity is not unique")
    user_index, user = users[0]
    answer_index, answer = answers[0]
    if user_index >= answer_index:
        raise ValueError("CWA response does not follow the target request")
    if any(item.role == "user" for item in messages[user_index + 1 : answer_index]):
        raise ValueError("CWA has an intervening user turn")
    if answer.finish_reason != "stop":
        raise ValueError("CWA target response has no canonical stop completion")
    if not isinstance(answer.text, str) or not answer.text.strip():
        raise ValueError("CWA target response is empty")

    previous = [
        item
        for item in messages[:user_index]
        if item.role == "assistant" and item.finish_reason == "stop"
    ]
    if len(previous) != 1:
        raise ValueError("CWA synthesis chat requires one unique prior completed revision")

    return {
        "request": {"message_id": user.message_id, "text": user.text},
        "response": {
            "message_id": answer.message_id,
            "finish_reason": answer.finish_reason,
            "text": answer.text,
        },
        "prior_revision": {
            "message_id": previous[0].message_id,
            "finish_reason": previous[0].finish_reason,
            "text": previous[0].text,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--conversation", required=True)
    parser.add_argument("--auth-file", required=True)
    parser.add_argument("--request-message-id", required=True)
    parser.add_argument("--response-message-id", required=True)
    args = parser.parse_args()

    from chatgpt_web_adapter import ChatGPTWebClient

    client = ChatGPTWebClient(auth_file=args.auth_file, timeout=120)
    messages = client.get_messages(args.conversation, limit=40)
    extracted = extract_turn(
        messages,
        request_message_id=args.request_message_id,
        response_message_id=args.response_message_id,
    )
    print(json.dumps(extracted, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
