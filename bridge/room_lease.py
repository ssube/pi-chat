"""Owner-local, short-lived Matrix room handoff to an interactive Pi process."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import stat
import struct
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from .runtime.pi_rpc import JsonObject

log = logging.getLogger("agent_bridge.room_lease")
LEASE_SECONDS = 180
MAX_LINE = 16_384


@dataclass
class Lease:
    inbox: str
    secret: str
    expires: float


class RoomLeases:
    def __init__(self, path: Path, rooms: tuple[str, ...], user_id: str,
                 prepare: Callable[[str], Awaitable[bool]]) -> None:
        self.path = path
        self.user_id = user_id
        self.rooms = frozenset(rooms)
        self.prepare = prepare
        self.leases: dict[str, Lease] = {}
        self.server: asyncio.AbstractServer | None = None
        self.lock = asyncio.Lock()

    async def start(self) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path.parent.chmod(0o700)
        if self.path.exists() or self.path.is_symlink():
            info = self.path.lstat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.geteuid():
                raise RuntimeError("refusing to replace unexpected room lease path")
            try:
                reader, writer = await asyncio.open_unix_connection(str(self.path))
            except (ConnectionError, OSError):
                self.path.unlink()
            else:
                writer.close()
                await writer.wait_closed()
                raise RuntimeError("another room lease server owns this socket")
        self.server = await asyncio.start_unix_server(self._handle, str(self.path))
        self.path.chmod(0o600)

    async def close(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
            self.server = None
            self.path.unlink(missing_ok=True)
        self.leases.clear()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            peer = writer.get_extra_info("socket")
            if peer is None or struct.unpack("3i", peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[1] != os.geteuid():
                raise ValueError("socket peer is not the bridge owner")
            line = await asyncio.wait_for(reader.readline(), 5)
            if len(line) > MAX_LINE or not line.endswith(b"\n"):
                raise ValueError("invalid request length")
            request = json.loads(line)
            if not isinstance(request, dict):
                raise ValueError("invalid request")
            reply: dict[str, Any] = await self._command(request)
        except (ValueError, OSError, TimeoutError, json.JSONDecodeError) as error:
            reply = {"ok": False, "error": str(error)}
        except Exception:
            log.exception("Room lease request failed")
            reply = {"ok": False, "error": "room lease request failed"}
        try:
            writer.write(json.dumps(reply).encode() + b"\n")
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    async def _command(self, request: dict[str, Any]) -> dict[str, Any]:
        room = request.get("room")
        secret = request.get("secret")
        operation = request.get("op")
        if not isinstance(secret, str) or len(secret) < 32:
            raise ValueError("invalid lease secret")
        async with self.lock:
            if operation == "status":
                now = time.monotonic()
                return {"ok": True, "leases": [
                    {"room": name, "seconds": max(0, int(lease.expires - now))}
                    for name, lease in self.leases.items()
                    if lease.secret == secret and lease.expires > now]}
            if operation == "release_all":
                released = [name for name, lease in self.leases.items() if lease.secret == secret]
                for name in released:
                    del self.leases[name]
                return {"ok": True, "released": len(released)}
            if request.get("sender") != self.user_id or not isinstance(room, str) or room not in self.rooms:
                raise ValueError("invalid room")
            current = self.leases.get(room)
            if current is not None and current.expires <= time.monotonic():
                del self.leases[room]
                current = None
            if operation == "acquire":
                inbox = request.get("inbox")
                if not isinstance(inbox, str) or not inbox or len(inbox) > 200:
                    raise ValueError("invalid inbox")
                if current is not None:
                    if current.secret != secret or current.inbox != inbox:
                        return {"ok": False}
                else:
                    # Never run two Pi sessions on the same scope. This also
                    # checkpoints resident context before the local send starts.
                    if not await self.prepare(room):
                        return {"ok": False}
                    self.leases[room] = Lease(inbox, secret, 0)
                self.leases[room].expires = time.monotonic() + LEASE_SECONDS
                return {"ok": True, "created": current is None}
            if operation == "release":
                if current is not None and current.secret == secret:
                    del self.leases[room]
                    return {"ok": True}
                return {"ok": False}
            raise ValueError("unknown lease operation")

    def active(self, room: str) -> bool:
        lease = self.leases.get(room)
        return lease is not None and lease.expires > time.monotonic()

    async def forward(self, room: str, event: dict[str, Any],
                      image: JsonObject | None = None,
                      reply: dict[str, str] | None = None,
                      ping: bool = False) -> bool:
        """True only when the interactive Pi inbox acknowledged this event."""
        async with self.lock:
            return await self.forward_locked(room, event, image, reply, ping)

    async def forward_locked(self, room: str, event: dict[str, Any],
                             image: JsonObject | None = None,
                             reply: dict[str, str] | None = None,
                             ping: bool = False) -> bool:
        """Call only while holding lock, to serialize forwarding with admission."""
        lease = self.leases.get(room)
        if lease is None:
            return False
        if lease.expires <= time.monotonic():
            del self.leases[room]
            return False
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_unix_connection(lease.inbox), 2)
            try:
                payload = {"secret": lease.secret, "room": room,
                           "event": event, "image": image, "reply": reply,
                           "ping": ping, "recipient": self.user_id}
                writer.write(json.dumps(payload).encode() + b"\n")
                await asyncio.wait_for(writer.drain(), 15 if image else 2)
                line = await asyncio.wait_for(reader.readline(), 15 if image else 5)
                response = json.loads(line)
                accepted = isinstance(response, dict) and response.get("ok") is True
            finally:
                writer.close()
                await writer.wait_closed()
        except (OSError, ValueError, TimeoutError, json.JSONDecodeError):
            accepted = False
        if accepted:
            lease.expires = time.monotonic() + LEASE_SECONDS
        else:
            # No ack: do not drop the incoming message. Normal bridge
            # routing resumes, with recent room history on its next wake.
            del self.leases[room]
        return accepted
