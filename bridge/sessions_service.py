"""Read-only, local sessions endpoint for the bridge runtime.

Reports how many Pi conversations are loaded, active, or in critical states.
No transport credentials are exposed; the socket is local to the bridge user.
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
log = logging.getLogger("agent_bridge.sessions")


class SessionsRequest(TypedDict):
    detail: Literal["summary", "full"]


class SessionsSummary(TypedDict):
    ok: Literal[True]
    total_scopes: int
    awake: int
    active: int
    active_non_idle: int
    state_breakdown: dict[str, int]
    per_agent: dict[str, int]
    per_transport: dict[str, int]
    awake_per_agent: dict[str, int]
    awake_per_transport: dict[str, int]
    critical_sessions: list[dict[str, Any]]


FetchSessions = Callable[[SessionsRequest], Awaitable[dict[str, object]]]


def validate_request(value: Any) -> SessionsRequest:
    if not isinstance(value, dict):
        raise ValueError("sessions request must be an object")
    if set(value) - {"detail"}:
        raise ValueError("sessions request contains unknown fields")
    detail = value.get("detail", "summary")
    if not isinstance(detail, str) or detail not in {"summary", "full"}:
        raise ValueError("detail must be summary or full")
    return {"detail": cast(Literal["summary", "full"], detail)}


def validate_summary(value: Any) -> SessionsSummary:
    if not isinstance(value, dict):
        raise ValueError("sessions summary must be an object")
    for field in ("total_scopes", "awake", "active", "active_non_idle"):
        if type(value.get(field)) is not int:
            raise ValueError(f"sessions summary {field} must be an integer")
    for field in ("state_breakdown", "per_agent", "per_transport",
                  "awake_per_agent", "awake_per_transport"):
        if not isinstance(value.get(field), dict):
            raise ValueError(f"sessions summary {field} must be an object")
    critical = value.get("critical_sessions")
    if not isinstance(critical, list):
        raise ValueError("sessions summary critical_sessions must be a list")
    return cast(SessionsSummary, value)


class SessionsService:
    """One request and one JSON response per local Unix socket connection."""

    def __init__(self, socket_path: Path, fetch_sessions: FetchSessions) -> None:
        self.socket_path = Path(socket_path)
        self.fetch_sessions = fetch_sessions
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        parent = self.socket_path.parent
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        parent.chmod(0o700)
        if self.socket_path.exists() or self.socket_path.is_socket():
            info = self.socket_path.lstat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.geteuid():
                raise RuntimeError("refusing to replace an unexpected sessions socket path")
            probe = socket.socket(socket.AF_UNIX)
            try:
                if probe.connect_ex(str(self.socket_path)) == 0:
                    raise RuntimeError("another sessions service owns this socket")
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
                raise ValueError("sessions request exceeds the size limit")
            request = validate_request(json.loads(raw))
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            error = str(exc)
        except TimeoutError:
            error = "sessions request timed out"
        except Exception as exc:
            log.warning("Sessions request failed: error_class=%s", type(exc).__name__)
            error = "sessions request failed"
        else:
            try:
                raw_summary = await asyncio.wait_for(self.fetch_sessions(request), timeout=5)
            except TimeoutError:
                log.warning("Sessions summary failed: error_class=TimeoutError")
                error = "sessions request timed out"
            except Exception as exc:
                log.warning("Sessions summary failed: error_class=%s", type(exc).__name__)
                error = "sessions summary failed"
            else:
                try:
                    summary = validate_summary(raw_summary)
                except ValueError as exc:
                    # This validator emits only fixed shape and field messages.
                    error = str(exc)
                except Exception as exc:
                    log.warning("Sessions validation failed: error_class=%s",
                                type(exc).__name__)
                    error = "sessions summary failed"
                else:
                    try:
                        encoded = (json.dumps({"ok": True, "summary": summary},
                                             ensure_ascii=False) + "\n").encode()
                    except Exception as exc:
                        log.warning("Sessions encoding failed: error_class=%s",
                                    type(exc).__name__)
                        error = "sessions summary failed"
        try:
            if error is not None:
                encoded = (json.dumps({"ok": False, "error": error},
                                     ensure_ascii=False) + "\n").encode()
            if len(encoded) > MAX_RESPONSE_BYTES:
                encoded = b'{"ok": false, "error": "sessions response exceeds the size limit"}\n'
            writer.write(encoded)
            await writer.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (BrokenPipeError, ConnectionResetError):
                pass
