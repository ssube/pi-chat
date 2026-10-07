"""React to a message in this Pi conversation's room or thread."""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
from typing import Literal, TypedDict, cast

from bridge.reaction_service import MAX_REQUEST_BYTES


class ReactionResult(TypedDict):
    ok: Literal[True]
    emoji: str


def send(socket_path: str, transport: str, scope: str, capability: str,
         message_id: str, emoji: str) -> ReactionResult:
    request = {"transport": transport, "scope": scope,
               "message_id": message_id, "emoji": emoji,
               "capability": capability}
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(35)
        connection.connect(socket_path)
        connection.sendall((json.dumps(request, ensure_ascii=False) + "\n").encode())
        with connection.makefile("rb") as stream:
            raw = stream.readline(MAX_REQUEST_BYTES + 1)
    if len(raw) > MAX_REQUEST_BYTES or not raw.endswith(b"\n"):
        raise RuntimeError("reaction response exceeds the size limit")
    response = json.loads(raw)
    if not isinstance(response, dict) or type(response.get("ok")) is not bool:
        raise RuntimeError("invalid reaction response")
    if not response["ok"]:
        error = response.get("error")
        raise RuntimeError(error if isinstance(error, str) and error else
                           "reaction failed")
    if not isinstance(response.get("emoji"), str) or not response["emoji"]:
        raise RuntimeError("invalid reaction response emoji")
    return cast(ReactionResult, response)


def main() -> int:
    parser = argparse.ArgumentParser(description="React to a message in this bridge conversation")
    parser.add_argument("--message-id", required=True,
                        help="event or message ID from this conversation or its history tool")
    parser.add_argument("--emoji", required=True,
                        help="one standard Unicode emoji, such as 👍, ☕, or ❤️")
    args = parser.parse_args()
    names = ("AGENT_BRIDGE_REACTION_SOCKET", "AGENT_BRIDGE_REACTION_TRANSPORT",
             "AGENT_BRIDGE_REACTION_SCOPE", "AGENT_BRIDGE_REACTION_CAPABILITY")
    missing = [name for name in names if not os.environ.get(name)]
    if missing:
        parser.error("missing bridge context: " + ", ".join(missing))
    try:
        result = send(os.environ[names[0]], os.environ[names[1]],
                      os.environ[names[2]], os.environ[names[3]],
                      args.message_id, args.emoji)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"bridge-react: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
