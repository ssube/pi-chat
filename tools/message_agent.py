#!/usr/bin/env python3
"""Send one verified, attributed message in an established Matrix-to-Pi room."""

from __future__ import annotations

import argparse
import json
import os
import socket
from pathlib import Path


try:
    from .matrix_client import MatrixClient, MatrixError
    from .room_history import private_json
except ImportError:
    from matrix_client import MatrixClient, MatrixError
    from room_history import private_json


def contact_route(path: Path, contact: str) -> tuple[str, str]:
    info = path.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise RuntimeError("private connector mapping must be owner-only")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError("private connector mapping must be a JSON object")
    route = data.get(contact)
    if not isinstance(route, dict):
        raise RuntimeError(f"no private connector mapping for {contact}")
    room_id = route.get("room_id")
    user_id = route.get("user_id")
    if not isinstance(room_id, str) or not room_id.startswith("!") or ":" not in room_id:
        raise RuntimeError(f"invalid private connector room for {contact}")
    if not isinstance(user_id, str) or not user_id.startswith("@") or ":" not in user_id:
        raise RuntimeError(f"stale or unexpected Matrix identity for {contact}")
    return room_id, user_id


def room_lease(operation: str, room_id: str, sender: str) -> bool:
    """Claim before sending, so a fast Matrix reply cannot wake bridge Pi."""
    path = os.environ["PI_ROOM_LEASE_BRIDGE_SOCKET"]
    request = {"op": operation, "room": room_id, "sender": sender,
               "inbox": os.environ["PI_ROOM_LEASE_INBOX"],
               "secret": os.environ["PI_ROOM_LEASE_SECRET"]}
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(120)
        connection.connect(path)
        connection.sendall(json.dumps(request).encode() + b"\n")
        with connection.makefile("rb") as stream:
            reply = json.loads(stream.readline())
    if reply.get("ok") is not True:
        raise RuntimeError("interactive room lease refused; Matrix message not sent")
    return reply.get("created") is True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    route = parser.add_mutually_exclusive_group(required=True)
    route.add_argument("--room-id")
    route.add_argument("--contact", help="name from the private contact mapping")
    parser.add_argument("--to", help="recipient's full Matrix user ID with --room-id")
    parser.add_argument("--contacts-file", type=Path,
                        default=Path.home() / ".config/pi-chat/contacts.json")
    parser.add_argument("--message-file", type=Path)
    parser.add_argument("--profile-file", type=Path, required=True)
    parser.add_argument("--check", action="store_true", help="verify access without sending")
    args = parser.parse_args()
    if args.contact:
        if args.to:
            parser.error("--to cannot be combined with --contact")
        room_id, recipient = contact_route(args.contacts_file, args.contact)
    else:
        if not args.to:
            parser.error("--to is required with --room-id")
        room_id, recipient = args.room_id, args.to
    if not room_id.startswith("!") or ":" not in room_id:
        parser.error("--room-id must be a full Matrix room ID")
    if not recipient.startswith("@") or ":" not in recipient:
        parser.error("--to must be a full Matrix user ID")
    if not args.check and args.message_file is None:
        parser.error("--message-file is required unless --check is set")

    settings = private_json(args.profile_file).get("matrix")
    if not isinstance(settings, dict):
        raise RuntimeError("profile has no Matrix settings")
    url, user = settings.get("homeserver"), settings.get("user_id")
    token = os.environ.get(settings.get("token_env", ""))
    from urllib.parse import urlsplit
    endpoint = urlsplit(url) if isinstance(url, str) else None
    if (endpoint is None or not endpoint.hostname or endpoint.username or endpoint.password
            or endpoint.query or endpoint.fragment
            or (endpoint.scheme != "https" and not
                (endpoint.scheme == "http" and endpoint.hostname in {"127.0.0.1", "::1", "localhost"}))
            or not isinstance(user, str) or not user.startswith("@") or ":" not in user or not token):
        raise RuntimeError("invalid Matrix endpoint, identity, or token environment variable")
    if room_id not in settings.get("rooms", []):
        raise RuntimeError("room is not enabled in this profile")
    own = MatrixClient(url, user, token, room_id)
    if own.whoami() != own.user_id:
        raise RuntimeError("Matrix token does not match the configured sender")
    room = own
    members = room.joined_members()
    if own.user_id not in members or recipient not in members:
        raise RuntimeError("sender or recipient is not joined to this connector room")
    if args.check:
        print(f"ready sender={own.user_id} recipient={recipient}")
        return 0

    message = args.message_file.read_text(encoding="utf-8").strip()
    if not message or len(message) > 8000:
        parser.error("message must be nonempty and at most 8000 characters")
    if not message.startswith(recipient):
        message = f"{recipient} {message}"
    expected_socket = os.environ.get("AGENT_BRIDGE_ROOM_LEASE_SOCKET")
    lease_fields = ("PI_ROOM_LEASE_INBOX", "PI_ROOM_LEASE_BRIDGE_SOCKET", "PI_ROOM_LEASE_SECRET")
    interactive = any(name in os.environ for name in lease_fields)
    if (interactive and not all(os.environ.get(name) for name in lease_fields) or
            expected_socket and (not interactive or
                                 os.environ.get("PI_ROOM_LEASE_BRIDGE_SOCKET") != expected_socket)):
        raise RuntimeError("configured interactive Pi room lease extension unavailable; Matrix message not sent")
    created = room_lease("acquire", room_id, own.user_id) if interactive else False
    try:
        event_id = room.send(message, recipient)
    except MatrixError as error:
        # A transport failure may follow a successful Matrix PUT. Keep the
        # lease until timeout rather than route a real reply to bridge Pi.
        if created and error.args and str(error).startswith((
                "Matrix HTTP 400", "Matrix HTTP 401", "Matrix HTTP 403",
                "Matrix HTTP 404")):
            try:
                room_lease("release", room_id, own.user_id)
            except (OSError, ValueError, RuntimeError):
                pass  # The bridge expires a stale lease automatically.
        raise
    print(f"sent event={event_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
