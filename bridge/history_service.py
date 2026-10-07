"""Read-only, local history endpoint for an agent's active chat bridge.

The adapter callback owns transport authentication and channel authorization.
This endpoint deliberately has no network listener or transport credentials.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import stat
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Literal, TypedDict, cast


MAX_REQUEST_BYTES = 16_384
MAX_RESPONSE_BYTES = 512_000
MAX_PAGE_SIZE = 100
log = logging.getLogger("agent_bridge.history")
_LOCAL_REJECTION_MESSAGES = {
    "capability": "history capability is not valid for this conversation",
    "awake": "history is available only for an awake conversation",
}


class HistoryRejected(Exception):
    """A local authorization rejection with a fixed, Pi-safe message."""

    def __init__(self, reason: Literal["capability", "awake"]) -> None:
        self.reason = reason
        super().__init__(_LOCAL_REJECTION_MESSAGES[reason])


class HistoryRequest(TypedDict):
    transport: Literal["matrix", "discord"]
    scope: str
    before: str | None
    limit: int
    capability: str


class HistoryMessage(TypedDict):
    id: str
    sender: str
    timestamp: str
    content: str


class HistoryPage(TypedDict):
    messages: list[HistoryMessage]
    next_before: str | None
    has_more: bool


FetchPage = Callable[[HistoryRequest], Awaitable[HistoryPage]]


def validate_request(request: Any) -> HistoryRequest:
    if not isinstance(request, dict):
        raise ValueError("history request must be an object")
    if set(request) - {"transport", "scope", "before", "limit", "capability"}:
        raise ValueError("history request contains unknown fields")
    transport = request.get("transport")
    scope = request.get("scope")
    before = request.get("before")
    limit = request.get("limit", 50)
    capability = request.get("capability")
    if not isinstance(transport, str) or transport not in {"matrix", "discord"}:
        raise ValueError("transport must be matrix or discord")
    if not isinstance(scope, str) or not scope or len(scope) > 512:
        raise ValueError("scope must be a nonempty channel or thread identifier")
    if before is not None and (not isinstance(before, str) or
                               not before or len(before) > 4096):
        raise ValueError("before must be a nonempty cursor string")
    if type(limit) is not int or not 1 <= limit <= MAX_PAGE_SIZE:
        raise ValueError("limit must be an integer between 1 and 100")
    if not isinstance(capability, str) or not 32 <= len(capability) <= 256:
        raise ValueError("history capability is required")
    return {"transport": cast(Literal["matrix", "discord"], transport), "scope": scope,
            "before": before, "limit": limit, "capability": capability}


def validate_page(value: Any) -> HistoryPage:
    if not isinstance(value, dict):
        raise ValueError("history page must be an object")
    messages = value.get("messages")
    if not isinstance(messages, list):
        raise ValueError("history page messages must be a list")
    for message in messages:
        if not isinstance(message, dict) or any(
                not isinstance(message.get(field), str)
                for field in ("id", "sender", "timestamp", "content")):
            raise ValueError("history page contains an invalid message")
    before = value.get("next_before")
    if before is not None and not isinstance(before, str):
        raise ValueError("history page next_before must be a string or null")
    if type(value.get("has_more")) is not bool:
        raise ValueError("history page has_more must be a boolean")
    return cast(HistoryPage, value)


class HistoryService:
    """One request and one JSON response per local Unix socket connection."""

    def __init__(self, socket_path: Path, fetch_page: FetchPage) -> None:
        self.socket_path = Path(socket_path)
        self.fetch_page = fetch_page
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        parent = self.socket_path.parent
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        parent.chmod(0o700)
        if self.socket_path.exists() or self.socket_path.is_socket():
            info = self.socket_path.lstat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.geteuid():
                raise RuntimeError("refusing to replace an unexpected history socket path")
            probe = socket.socket(socket.AF_UNIX)
            try:
                if probe.connect_ex(str(self.socket_path)) == 0:
                    raise RuntimeError("another history service owns this socket")
            finally:
                probe.close()
            self.socket_path.unlink()
        self._server = await asyncio.start_unix_server(self._handle,
                                                        path=str(self.socket_path))
        self.socket_path.chmod(0o600)

    async def close(self) -> None:
        if self._server is None:
            return
        self._server.close()
        await self._server.wait_closed()
        self._server = None
        if self.socket_path.is_socket():
            self.socket_path.unlink()

    async def _handle(self, reader: asyncio.StreamReader,
                      writer: asyncio.StreamWriter) -> None:
        error: str | None = None
        try:
            raw = await asyncio.wait_for(reader.readline(), timeout=10)
            if len(raw) > MAX_REQUEST_BYTES or not raw.endswith(b"\n"):
                raise ValueError("history request exceeds the size limit")
            request = validate_request(json.loads(raw))
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            error = str(exc)
        except TimeoutError:
            error = "history request timed out"
        except Exception as exc:
            log.warning("History request failed: error_class=%s", type(exc).__name__)
            error = "history request failed"
        else:
            transport = request["transport"]
            try:
                page = validate_page(await asyncio.wait_for(self.fetch_page(request), timeout=30))
                encoded = (json.dumps({"ok": True, "page": page}, ensure_ascii=False) + "\n").encode()
            except HistoryRejected as exc:
                error = _LOCAL_REJECTION_MESSAGES[exc.reason]
            except TimeoutError:
                log.warning("History fetch failed: transport=%s error_class=TimeoutError",
                            transport)
                error = "history request timed out"
            except Exception as exc:
                # Only allowlisted facts cross this log boundary. Remote messages,
                # tracebacks, scope IDs, and credentials may contain secrets.
                log.warning("History fetch failed: transport=%s error_class=%s",
                            transport, type(exc).__name__)
                error = "history fetch failed"
        try:
            if error is not None:
                encoded = (json.dumps({"ok": False, "error": error}, ensure_ascii=False) + "\n").encode()
            if len(encoded) > MAX_RESPONSE_BYTES:
                encoded = b'{"ok": false, "error": "history response exceeds the size limit"}\n'
            writer.write(encoded)
            await writer.drain()
        except (BrokenPipeError, ConnectionResetError):
            # A socket ownership probe or cancelled client can disconnect early.
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (BrokenPipeError, ConnectionResetError):
                pass
