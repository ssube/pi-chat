"""Local HTTP health checks for an owner-side supervisor probe."""

from __future__ import annotations

import asyncio
from pathlib import Path

from bridge.socket_guard import remove_stale_socket


class HealthSocket:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.ready = False
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path.parent.chmod(0o700)
        remove_stale_socket(self.path, "health")
        self._server = await asyncio.start_unix_server(self._handle, path=str(self.path))
        self.path.chmod(0o600)

    async def close(self) -> None:
        self.ready = False
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
            line = await asyncio.wait_for(reader.readline(), timeout=2)
            if len(line) > 256 or not line.endswith(b"\r\n"):
                status = b"400 Bad Request"
            elif line == b"GET /health/live HTTP/1.1\r\n" or line == b"GET /health/live HTTP/1.0\r\n":
                status = b"200 OK"
            elif line == b"GET /health/ready HTTP/1.1\r\n" or line == b"GET /health/ready HTTP/1.0\r\n":
                status = b"200 OK" if self.ready else b"503 Service Unavailable"
            else:
                status = b"404 Not Found"
            writer.write(b"HTTP/1.1 " + status + b"\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            await writer.drain()
        except (TimeoutError, ConnectionError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass
