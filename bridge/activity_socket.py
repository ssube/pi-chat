"""Capability-bound local status setter for an awake Pi conversation."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import stat
import struct
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from bridge.activity import ActivityRejected


MAX_BYTES = 4096
SetStatus = Callable[[dict[str, Any]], Awaitable[None]]
log = logging.getLogger("agent_bridge.activity_socket")


class ActivitySocket:
    def __init__(self, path: Path, set_status: SetStatus) -> None:
        self.path = path
        self.set_status = set_status
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path.parent.chmod(0o700)
        if self.path.exists() or self.path.is_socket():
            info = self.path.lstat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.geteuid():
                raise RuntimeError("refusing to replace an unexpected activity socket")
            probe = socket.socket(socket.AF_UNIX)
            try:
                if probe.connect_ex(str(self.path)) == 0:
                    raise RuntimeError("another activity service owns this socket")
            finally:
                probe.close()
            self.path.unlink()
        self._server = await asyncio.start_unix_server(self._handle, path=str(self.path))
        self.path.chmod(0o600)

    async def close(self) -> None:
        if self._server is None:
            return
        self._server.close()
        await self._server.wait_closed()
        self._server = None
        if self.path.is_socket():
            self.path.unlink()

    async def _handle(self, reader: asyncio.StreamReader,
                      writer: asyncio.StreamWriter) -> None:
        try:
            raw = await asyncio.wait_for(reader.readline(), timeout=10)
            if len(raw) > MAX_BYTES or not raw.endswith(b"\n"):
                raise ValueError("activity request exceeds the size limit")
            request = json.loads(raw)
            if not isinstance(request, dict):
                raise ValueError("invalid activity request")
            owner_fields = {"text", "hours", "operator"}
            agent_fields = {"transport", "scope", "capability", "text", "hours"}
            if set(request) == owner_fields and request.get("operator") is True:
                peer = writer.get_extra_info("socket")
                if peer is None:
                    raise ValueError("activity peer is unavailable")
                _, uid, _ = struct.unpack("3i", peer.getsockopt(
                    socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))
                if uid != os.geteuid():
                    raise ValueError("activity peer is not the bridge owner")
            elif set(request) != agent_fields:
                raise ValueError("invalid activity request fields")
            if (not isinstance(request.get("text"), str) or
                    type(request.get("hours")) not in (int, float) or
                    (set(request) == agent_fields and not all(
                        isinstance(request[field], str) for field in
                        ("transport", "scope", "capability")))):
                raise ValueError("invalid activity request fields")
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            response = {"ok": False, "error": str(exc)}
        except TimeoutError:
            response = {"ok": False, "error": "activity request timed out"}
        except Exception as exc:
            log.warning("Activity request failed: error_class=%s", type(exc).__name__)
            response = {"ok": False, "error": "activity request failed"}
        else:
            try:
                await asyncio.wait_for(self.set_status(request), timeout=30)
            except ActivityRejected as exc:
                response = {"ok": False, "error": exc.message}
            except TimeoutError:
                log.warning("Activity update failed: error_class=TimeoutError")
                response = {"ok": False, "error": "activity request timed out"}
            except Exception as exc:
                log.warning("Activity update failed: error_class=%s", type(exc).__name__)
                response = {"ok": False, "error": "activity update failed"}
            else:
                response = {"ok": True}
        try:
            writer.write((json.dumps(response) + "\n").encode())
            await writer.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (BrokenPipeError, ConnectionResetError):
                pass
