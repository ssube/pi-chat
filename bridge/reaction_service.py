"""Scope-bound local action endpoint for Unicode reactions.

The Pi process receives a capability, never a Matrix or Discord credential.
Emoji forms come from Unicode's Emoji 17.0 test data in this package.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import socket
import stat
from collections.abc import Awaitable, Callable
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, TypedDict, cast


MAX_REQUEST_BYTES = 16_384
log = logging.getLogger("agent_bridge.reaction")


class ReactionRequest(TypedDict):
    transport: Literal["matrix", "discord"]
    scope: str
    message_id: str
    emoji: str
    capability: str


React = Callable[[ReactionRequest], Awaitable[None]]
_DESCRIPTION = re.compile(r"\s*\S+\s+E[0-9.]+\s+(.+)$")
_LOCAL_REJECTION_MESSAGES = {
    "capability": "reaction capability is not valid for this conversation",
    "awake": "reactions are available only for an awake conversation",
    "matrix_target": "Invalid Matrix reaction target",
    "matrix_room": "Room is not configured for this bridge",
    "matrix_scope": "Reaction target is outside this room or thread",
    "discord_target": "Invalid Discord reaction target",
    "discord_channel": "Discord channel is not enabled",
    "discord_scope": "Reaction target is outside this channel",
}


class ReactionRejected(ValueError):
    """A local reaction rejection with a fixed, Pi-safe message."""

    def __init__(self, reason: Literal["capability", "awake", "matrix_target",
                                       "matrix_room", "matrix_scope", "discord_target",
                                       "discord_channel", "discord_scope"]) -> None:
        message = _LOCAL_REJECTION_MESSAGES.get(reason)
        if message is None:
            raise ValueError("unknown local reaction rejection")
        self.message = message
        super().__init__(self.message)


@lru_cache(maxsize=1)
def emoji_forms() -> dict[str, str]:
    """Map standard minimally/unqualified forms to emoji presentation forms."""
    rows: list[tuple[str, str, str]] = []
    source = Path(__file__).with_name("emoji-test-17.0.txt")
    for line in source.read_text(encoding="utf-8").splitlines():
        if ";" not in line or "#" not in line:
            continue
        codes, rest = line.split(";", 1)
        status, description = rest.split("#", 1)
        match = _DESCRIPTION.fullmatch(description)
        if match is None:
            continue
        form = "".join(chr(int(code, 16)) for code in codes.split())
        rows.append((form, status.strip(), match.group(1)))
    canonical = {name: form for form, status, name in rows
                 if status == "fully-qualified"}
    return {form: canonical[name] for form, status, name in rows
            if status in {"fully-qualified", "minimally-qualified", "unqualified"}
            and name in canonical}


def validate_request(value: Any) -> ReactionRequest:
    if not isinstance(value, dict) or set(value) != {
            "transport", "scope", "message_id", "emoji", "capability"}:
        raise ValueError("reaction request has invalid fields")
    transport = value["transport"]
    scope = value["scope"]
    message_id = value["message_id"]
    form = value["emoji"]
    capability = value["capability"]
    if not isinstance(transport, str) or transport not in {"matrix", "discord"}:
        raise ValueError("transport must be matrix or discord")
    if not isinstance(scope, str) or not 0 < len(scope) <= 512:
        raise ValueError("scope must be a channel or thread identifier")
    if not isinstance(message_id, str) or not 0 < len(message_id) <= 512:
        raise ValueError("message_id must identify a message")
    if not isinstance(form, str) or not 0 < len(form) <= 64:
        raise ValueError("emoji must be one Unicode emoji")
    canonical = emoji_forms().get(form)
    if canonical is None:
        raise ValueError("emoji must be one standard Unicode emoji; aliases and custom emoji are unsupported")
    if not isinstance(capability, str) or not 32 <= len(capability) <= 256:
        raise ValueError("reaction capability is required")
    return {"transport": cast(Literal["matrix", "discord"], transport), "scope": scope,
            "message_id": message_id, "emoji": canonical,
            "capability": capability}


class ReactionService:
    """One reaction request and JSON result per local Unix socket connection."""

    def __init__(self, socket_path: Path, react: React) -> None:
        self.socket_path = Path(socket_path)
        self.react = react
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        parent = self.socket_path.parent
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        parent.chmod(0o700)
        if self.socket_path.exists() or self.socket_path.is_socket():
            info = self.socket_path.lstat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.geteuid():
                raise RuntimeError("refusing to replace an unexpected reaction socket path")
            probe = socket.socket(socket.AF_UNIX)
            try:
                if probe.connect_ex(str(self.socket_path)) == 0:
                    raise RuntimeError("another reaction service owns this socket")
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
        try:
            raw = await asyncio.wait_for(reader.readline(), timeout=10)
            if len(raw) > MAX_REQUEST_BYTES or not raw.endswith(b"\n"):
                raise ValueError("reaction request exceeds the size limit")
            request = validate_request(json.loads(raw))
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            response = {"ok": False, "error": str(exc)}
        except TimeoutError:
            response = {"ok": False, "error": "reaction request timed out"}
        except Exception as exc:
            log.warning("Reaction request failed: error_class=%s", type(exc).__name__)
            response = {"ok": False, "error": "reaction request failed"}
        else:
            transport = request["transport"]
            try:
                await asyncio.wait_for(self.react(request), timeout=30)
            except ReactionRejected as exc:
                response = {"ok": False, "error": exc.message}
            except TimeoutError:
                log.warning("Reaction delivery failed: transport=%s error_class=TimeoutError",
                            transport)
                response = {"ok": False, "error": "reaction request timed out"}
            except Exception as exc:
                # Only allowlisted facts cross the log boundary. Exception messages,
                # tracebacks, request IDs, and credentials may contain remote data.
                log.warning("Reaction delivery failed: transport=%s error_class=%s",
                            transport, type(exc).__name__)
                response = {"ok": False, "error": "reaction delivery failed"}
            else:
                response = {"ok": True, "emoji": request["emoji"]}
        try:
            writer.write((json.dumps(response, ensure_ascii=False) + "\n").encode())
            await writer.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (BrokenPipeError, ConnectionResetError):
                pass
