"""Enumerate canonical mention targets through the authenticated history socket."""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys

from .history_service import MAX_RESPONSE_BYTES, MembersPage, validate_members_page

SAFE_ERRORS = frozenset({
    "history capability is not valid for this conversation",
    "history is available only for an awake conversation",
    "unknown or ambiguous target; enumerate canonical members",
})


def fetch(socket_path: str, transport: str, scope: str, capability: str, *,
          before: str | None = None, limit: int = 50, targets: list[str] | None = None) -> MembersPage:
    request: dict[str, object] = {"transport": transport, "scope": scope, "capability": capability,
               "operation": "members", "before": before, "limit": limit}
    if targets is not None:
        request["targets"] = targets
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(35)
        connection.connect(socket_path)
        connection.sendall((json.dumps(request) + "\n").encode())
        with connection.makefile("rb") as stream:
            raw = stream.readline(MAX_RESPONSE_BYTES + 1)
    if len(raw) > MAX_RESPONSE_BYTES or not raw.endswith(b"\n"):
        raise ValueError("members response exceeds size limit")
    response = json.loads(raw)
    if not isinstance(response, dict) or response.get("ok") is not True:
        error = response.get("error") if isinstance(response, dict) else None
        if not isinstance(error, str) or error not in SAFE_ERRORS:
            error = "members lookup failed; check conversation capability and availability"
        raise RuntimeError(error)
    return validate_members_page(response.get("page"))


def main() -> int:
    parser = argparse.ArgumentParser(description="List canonical mention targets and accepted aliases")
    parser.add_argument("--before", help="next_before cursor from the previous page")
    parser.add_argument("--limit", type=int, default=50, choices=range(1, 101), metavar="1..100")
    parser.add_argument("--resolve", action="append", help="resolve an explicit alias or ID; repeat for multiple targets")
    args = parser.parse_args()
    names = ("SOCKET", "TRANSPORT", "SCOPE", "CAPABILITY")
    context = [os.environ.get("AGENT_BRIDGE_HISTORY_" + name, "") for name in names]
    if not all(context):
        parser.error("missing bridge history context")
    try:
        result = fetch(*context, before=args.before, limit=args.limit, targets=args.resolve)
    except (OSError, ValueError, RuntimeError) as exc:
        message = str(exc) if isinstance(exc, RuntimeError) and str(exc) in SAFE_ERRORS else (
            "lookup failed; check local bridge diagnostics")
        print(f"bridge-members: {message}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
