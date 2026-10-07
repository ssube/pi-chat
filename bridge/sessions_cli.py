"""Ask the local bridge for a snapshot of active Pi sessions.

This command is intended for deploy playbooks that need to check whether any
conversation is in progress before restarting the bridge process.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys

from bridge.sessions_service import MAX_RESPONSE_BYTES, validate_summary


def fetch(socket_path: str, *, detail: str) -> dict[str, object]:
    request = {"detail": detail}
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(10)
        connection.connect(socket_path)
        connection.sendall((json.dumps(request, ensure_ascii=False) + "\n").encode())
        with connection.makefile("rb") as stream:
            raw = stream.readline(MAX_RESPONSE_BYTES + 1)
    if len(raw) > MAX_RESPONSE_BYTES or not raw.endswith(b"\n"):
        raise RuntimeError("sessions response exceeds the size limit")
    response = json.loads(raw)
    if not isinstance(response, dict) or type(response.get("ok")) is not bool:
        raise RuntimeError("invalid sessions response")
    if not response["ok"]:
        error = response.get("error")
        raise RuntimeError(error if isinstance(error, str) and error else
                           "sessions request failed")
    summary = validate_summary(response.get("summary"))
    return dict(summary)


def safe_to_restart(summary: dict[str, object]) -> bool:
    """Only active turns block a scoped source restart; awake is advisory."""
    active = summary.get("active")
    non_idle = summary.get("active_non_idle")
    if type(active) is not int or type(non_idle) is not int:
        raise ValueError("sessions summary is missing active counts")
    return active == 0 and non_idle == 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Report active Pi sessions from the local bridge runtime")
    parser.add_argument("--socket-path",
                        help="path to the bridge sessions Unix socket; "
                             "defaults to AGENT_BRIDGE_SESSIONS_SOCKET")
    parser.add_argument("--detail", choices=("summary", "full"), default="summary",
                        help="level of detail in the report")
    parser.add_argument("--require-idle", action="store_true",
                        help="exit unsuccessfully while a Pi turn is active")
    args = parser.parse_args()
    socket_path = args.socket_path or os.environ.get("AGENT_BRIDGE_SESSIONS_SOCKET")
    if not socket_path:
        parser.error("missing socket path; pass --socket-path or set AGENT_BRIDGE_SESSIONS_SOCKET")
    try:
        summary = fetch(socket_path, detail=args.detail)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"bridge-sessions: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if args.require_idle and not safe_to_restart(summary):
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
