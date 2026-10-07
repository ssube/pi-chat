import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bridge.history_cli import fetch
from bridge.history_service import HistoryRejected, HistoryService, validate_request


class HistoryRequestTests(unittest.TestCase):
    def test_validation_and_page_bounds(self):
        self.assertEqual(validate_request({"transport": "matrix", "scope": "!r:example",
                                           "capability": "x" * 32}),
                         {"transport": "matrix", "scope": "!r:example",
                          "before": None, "limit": 50, "capability": "x" * 32})
        for bad in ({"transport": "matrix", "scope": "!r:example", "capability": "x" * 32, "limit": True},
                    {"transport": "matrix", "scope": "!r:example", "capability": "x" * 32, "limit": 101},
                    {"transport": "other", "scope": "!r:example"},
                    {"transport": "matrix", "scope": ""},
                    {"transport": "matrix", "scope": "!r:example", "capability": "x" * 32, "extra": 1}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_request(bad)


class HistoryServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "state" / "bridge-history.sock"
        self.calls = []

        async def page(request):
            self.calls.append(request)
            if request["scope"] != "!approved:example":
                raise ValueError("channel is not enabled")
            return {"messages": [{"id": "$1", "sender": "@a:example",
                                  "timestamp": "2026-09-30T12:00:00Z",
                                  "content": "hello"}],
                    "next_before": "cursor-1", "has_more": True}

        self.service = HistoryService(self.path, page)
        await self.service.start()

    async def asyncTearDown(self):
        await self.service.close()
        self.directory.cleanup()

    async def test_page_cursor_and_private_socket(self):
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        page = await asyncio.to_thread(fetch, str(self.path), "matrix",
                                       "!approved:example", "x" * 32,
                                       before="cursor-2", limit=42)
        self.assertEqual(page["messages"][0]["content"], "hello")
        self.assertEqual(page["next_before"], "cursor-1")
        self.assertEqual(self.calls[0]["before"], "cursor-2")
        self.assertEqual(self.calls[0]["limit"], 42)

    async def test_rejected_scope_does_not_expose_messages(self):
        with self.assertLogs("agent_bridge.history", level="WARNING") as captured:
            with self.assertRaisesRegex(RuntimeError, "history fetch failed"):
                await asyncio.to_thread(fetch, str(self.path), "matrix", "!other:example",
                                        "x" * 32)
        self.assertIn("transport=matrix error_class=ValueError", captured.output[0])
        self.assertNotIn("!other:example", captured.output[0])

    async def test_remote_error_and_invalid_page_do_not_leak_to_pi_or_log(self):
        async def remote_error(request):
            raise ValueError("remote body token=history-secret")

        async def invalid_page(request):
            return {"messages": "remote body token=history-secret"}

        for callback, error_class in ((remote_error, "ValueError"),
                                      (invalid_page, "ValueError")):
            with self.subTest(callback=callback.__name__):
                self.service.fetch_page = callback
                with self.assertLogs("agent_bridge.history", level="WARNING") as captured:
                    with self.assertRaisesRegex(RuntimeError, "history fetch failed") as failure:
                        await asyncio.to_thread(fetch, str(self.path), "matrix",
                                                "!approved:example", "x" * 32)
                self.assertNotIn("history-secret", str(failure.exception))
                self.assertIn("transport=matrix error_class=" + error_class,
                              captured.output[0])
                self.assertNotIn("history-secret", captured.output[0])

    async def test_unexpected_validation_failure_returns_generic_error(self):
        with patch("bridge.history_service.validate_request",
                   side_effect=RuntimeError("token=validation-secret")):
            with self.assertLogs("agent_bridge.history", level="WARNING") as captured:
                with self.assertRaisesRegex(RuntimeError, "history request failed") as failure:
                    await asyncio.to_thread(fetch, str(self.path), "matrix",
                                            "!approved:example", "x" * 32)
        self.assertNotIn("validation-secret", str(failure.exception))
        self.assertIn("error_class=RuntimeError", captured.output[0])
        self.assertNotIn("validation-secret", captured.output[0])

    async def test_local_rejection_keeps_fixed_actionable_message(self):
        for reason, expected in (
                ("capability", "history capability is not valid for this conversation"),
                ("awake", "history is available only for an awake conversation")):
            with self.subTest(reason=reason):
                async def reject(request):
                    raise HistoryRejected(reason)

                self.service.fetch_page = reject
                with self.assertRaisesRegex(RuntimeError, expected):
                    await asyncio.to_thread(fetch, str(self.path), "matrix",
                                            "!approved:example", "x" * 32)

    async def test_invalid_request_does_not_reach_adapter(self):
        reader, writer = await asyncio.open_unix_connection(str(self.path))
        writer.write(b'{"transport":"matrix","scope":"!approved:example","limit":1000}\n')
        await writer.drain()
        response = json.loads(await reader.readline())
        writer.close()
        await writer.wait_closed()
        self.assertFalse(response["ok"])
        self.assertEqual(self.calls, [])

    async def test_active_socket_cannot_be_replaced(self):
        another = HistoryService(self.path, self.service.fetch_page)
        with self.assertRaisesRegex(RuntimeError, "another history service"):
            await another.start()
        self.assertTrue(self.path.is_socket())
