"""Ask the local bridge for an older page from the current channel or thread.

Pi can invoke this stdlib-only command from its shell tool. The bridge process
retains Matrix and Discord credentials; this command only sees a local socket.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys

from bridge.history_service import MAX_PAGE_SIZE, MAX_RESPONSE_BYTES, HistoryPage, validate_page


def fetch(socket_path: str, transport: str, scope: str, capability: str, *,
          before: str | None = None, limit: int = 50) -> HistoryPage:
    request = {"transport": transport, "scope": scope,
               "before": before, "limit": limit, "capability": capability}
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(35)
        connection.connect(socket_path)
        connection.sendall((json.dumps(request) + "\n").encode())
        with connection.makefile("rb") as stream:
            raw = stream.readline(MAX_RESPONSE_BYTES + 1)
    if len(raw) > MAX_RESPONSE_BYTES or not raw.endswith(b"\n"):
        raise RuntimeError("history response exceeds the size limit")
    response = json.loads(raw)
    if not isinstance(response, dict) or type(response.get("ok")) is not bool:
        raise RuntimeError("invalid history response")
    if not response["ok"]:
        error = response.get("error")
        raise RuntimeError(error if isinstance(error, str) and error else
                           "history request failed")
    try:
        return validate_page(response.get("page"))
    except ValueError as exc:
        raise RuntimeError("invalid history response page") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description="Read older messages in this bridge channel")
    parser.add_argument("--before", help="next_before cursor from a prior page")
    parser.add_argument("--limit", type=int, default=50,
                        help=f"messages per page, 1–{MAX_PAGE_SIZE} (default: 50)")
    args = parser.parse_args()
    if not 1 <= args.limit <= MAX_PAGE_SIZE:
        parser.error(f"--limit must be between 1 and {MAX_PAGE_SIZE}")
    names = ("AGENT_BRIDGE_HISTORY_SOCKET", "AGENT_BRIDGE_HISTORY_TRANSPORT",
             "AGENT_BRIDGE_HISTORY_SCOPE", "AGENT_BRIDGE_HISTORY_CAPABILITY")
    missing = [name for name in names if not os.environ.get(name)]
    if missing:
        parser.error("missing bridge context: " + ", ".join(missing))
    try:
        page = fetch(os.environ[names[0]], os.environ[names[1]],
                     os.environ[names[2]], os.environ[names[3]],
                     before=args.before, limit=args.limit)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"bridge-history: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(page, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
