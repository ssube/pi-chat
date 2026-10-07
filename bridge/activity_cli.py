"""Set a short public status on this agent's Matrix and Discord accounts."""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys

from bridge.activity_socket import MAX_BYTES


def main() -> int:
    parser = argparse.ArgumentParser(description="Set this agent's public bridge status")
    parser.add_argument("text", help="short public text; never include private details")
    parser.add_argument("--hours", type=float, default=3,
                        help="duration of this override, 0 to 24 hours (default: 3)")
    parser.add_argument("--socket-path", help="owner-only local bridge activity socket")
    args = parser.parse_args()
    names = ("AGENT_BRIDGE_ACTIVITY_SOCKET", "AGENT_BRIDGE_ACTIVITY_TRANSPORT",
             "AGENT_BRIDGE_ACTIVITY_SCOPE", "AGENT_BRIDGE_ACTIVITY_CAPABILITY")
    if args.socket_path:
        socket_path = args.socket_path
        request = {"operator": True, "text": args.text, "hours": args.hours}
    else:
        missing = [name for name in names if not os.environ.get(name)]
        if missing:
            parser.error("missing bridge context: " + ", ".join(missing))
        socket_path = os.environ[names[0]]
        request = {"transport": os.environ[names[1]], "scope": os.environ[names[2]],
                   "capability": os.environ[names[3]], "text": args.text,
                   "hours": args.hours}
    try:
        with socket.socket(socket.AF_UNIX) as connection:
            connection.settimeout(35)
            connection.connect(socket_path)
            connection.sendall((json.dumps(request, ensure_ascii=False) + "\n").encode())
            with connection.makefile("rb") as stream:
                raw = stream.readline(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES or not raw.endswith(b"\n"):
            raise RuntimeError("invalid activity response")
        response = json.loads(raw)
        if not isinstance(response, dict) or response.get("ok") is not True:
            raise RuntimeError(response.get("error", "activity update failed"))
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"bridge-activity: {exc}", file=sys.stderr)
        return 1
    print("Activity updated.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
