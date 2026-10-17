"""Local health endpoint contract."""

import asyncio
import socket
import tempfile
import unittest
from pathlib import Path

from bridge.health_socket import HealthSocket


class HealthSocketTests(unittest.IsolatedAsyncioTestCase):
    async def test_liveness_readiness_and_shutdown(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "health.sock"
            health = HealthSocket(path)
            await health.start()
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

            async def status(request: bytes) -> bytes:
                reader, writer = await asyncio.open_unix_connection(str(path))
                writer.write(request)
                await writer.drain()
                response = await reader.read()
                writer.close()
                await writer.wait_closed()
                return response.split(b"\r\n", 1)[0]

            try:
                self.assertEqual(await status(b"GET /health/live HTTP/1.1\r\n\r\n"),
                                 b"HTTP/1.1 200 OK")
                self.assertEqual(await status(b"GET /health/ready HTTP/1.1\r\n\r\n"),
                                 b"HTTP/1.1 503 Service Unavailable")
                health.ready = True
                self.assertEqual(await status(b"GET /health/ready HTTP/1.1\r\n\r\n"),
                                 b"HTTP/1.1 200 OK")
                self.assertEqual(await status(b"GET /health/live HTTP/1.0\r\n\r\n"),
                                 b"HTTP/1.1 200 OK")
                self.assertEqual(await status(b"GET /health/ready HTTP/1.0\r\n\r\n"),
                                 b"HTTP/1.1 200 OK")
                self.assertEqual(await status(b"GET /missing HTTP/1.1\r\n\r\n"),
                                 b"HTTP/1.1 404 Not Found")
                self.assertEqual(await status(b"GET /health/live HTTP/1.1\n"),
                                 b"HTTP/1.1 400 Bad Request")
                health.ready = False  # Shutdown begins before the socket closes.
                self.assertEqual(await status(b"GET /health/ready HTTP/1.1\r\n\r\n"),
                                 b"HTTP/1.1 503 Service Unavailable")
            finally:
                await health.close()
            self.assertFalse(path.exists())
            self.assertFalse(health.ready)

    async def test_existing_live_socket_is_not_replaced(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "health.sock"
            first = HealthSocket(path)
            await first.start()
            try:
                with self.assertRaisesRegex(RuntimeError, "another health service"):
                    await HealthSocket(path).start()
            finally:
                await first.close()

    async def test_abandoned_socket_is_replaced(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "health.sock"
            abandoned = socket.socket(socket.AF_UNIX)
            abandoned.bind(str(path))
            abandoned.close()
            health = HealthSocket(path)
            await health.start()
            try:
                self.assertTrue(path.is_socket())
                reader, writer = await asyncio.open_unix_connection(str(path))
                writer.write(b"GET /health/live HTTP/1.1\r\n\r\n")
                await writer.drain()
                self.assertTrue((await reader.read()).startswith(b"HTTP/1.1 200 OK"))
                writer.close()
                await writer.wait_closed()
            finally:
                await health.close()
