import json
import asyncio
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bridge.activity import Activity, ActivityRejected, ActivityService, Presence, clean_status
from bridge.activity_cli import main as activity_cli_main
from bridge.activity_socket import ActivitySocket
from bridge.adapters.discord import DiscordAdapter
from bridge.adapters.matrix import MatrixAdapter
from bridge.runtime.session import BridgeRuntime
from bridge.runtime.state import Checkpoint, SessionKey, write_checkpoint


class ActivityTests(unittest.IsolatedAsyncioTestCase):
    def test_status_validation_rejects_private_identifier_shapes(self):
        for value in ("https://example.test", "@operator:example.test", "/private/file",
                      "Ticket #12345", "Call 5551234567", "line one\nline two"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                clean_status(value)

    async def test_saved_summaries_yield_only_fixed_public_topics(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key = SessionKey("agent_one", "matrix", "secret-room")
            write_checkpoint(key.directory(root), Checkpoint(
                summary="Operator's private medical note and Python deployment on /private/data",
                last_activity_ms=100))
            runtime = BridgeRuntime(root, lambda *_: None, AsyncMock())
            await runtime._scope(key)
            topics = runtime.activity_topics()
            self.assertIn("code", topics)
            self.assertIn("infrastructure", topics)
            self.assertNotIn("Operator", json.dumps(topics))
            self.assertNotIn("private", json.dumps(topics))

    async def test_status_generator_skips_while_external_review_owns_pi(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = BridgeRuntime(Path(directory) / "sessions", lambda *_: None,
                                    AsyncMock())
            generated = AsyncMock(return_value="Following a thread")
            service = ActivityService("agent_one", runtime, (),
                                      Path(directory) / "activity.json", generated,
                                      now=lambda: 0.0, chance=lambda: 0.0)
            async with runtime.external_job() as reserved:
                self.assertTrue(reserved)
                await service.refresh()
            generated.assert_not_called()

    async def test_coin_flip_history_manual_override_and_parity(self):
        with tempfile.TemporaryDirectory() as directory:
            now = [0.0]
            runtime = BridgeRuntime(Path(directory) / "sessions", lambda *_: None,
                                    AsyncMock())
            runtime.activity_topics = lambda: ["research"]
            matrix = SimpleNamespace(publish_activity=AsyncMock(return_value=True))
            discord = SimpleNamespace(publish_activity=AsyncMock(return_value=True))
            generated = AsyncMock(side_effect=["Following a new thread", "Pursuing another lead"])
            service = ActivityService("agent_one", runtime, (matrix, discord),
                                      Path(directory) / "activity.json", generated,
                                      now=lambda: now[0], chance=lambda: 0.8)
            await service.refresh()  # First status always generated.
            self.assertEqual(generated.await_count, 1)
            self.assertEqual(generated.await_args.args, ([], ["research"]))
            self.assertEqual(matrix.publish_activity.await_args.args,
                             discord.publish_activity.await_args.args)
            now[0] = 10800
            await service.refresh()  # Coin flip says keep it.
            self.assertEqual(generated.await_count, 1)
            await service.set_status("Reading quietly", hours=1)
            self.assertEqual(matrix.publish_activity.await_args.args[0].text,
                             "Reading quietly")
            now[0] = 21600
            await service.refresh()  # Manual override expired; coin flip still keeps it.
            self.assertEqual(generated.await_count, 1)
            self.assertEqual(service.history[-2:], ["Following a new thread", "Reading quietly"])

    async def test_matrix_presence_uses_encoded_own_id(self):
        adapter = MatrixAdapter.__new__(MatrixAdapter)
        adapter.settings = SimpleNamespace(user_id="@agent_one:example.test")
        adapter.session = object()
        adapter._request = AsyncMock(return_value={})
        self.assertTrue(await adapter.publish_activity(Activity("Following threads", Presence.CONNECTED)))
        self.assertEqual(adapter._request.await_args.args[:2],
                         ("PUT", "/_matrix/client/v3/presence/%40agent_one%3Aexample.test/status"))
        self.assertEqual(adapter._request.await_args.kwargs["data"],
                         {"presence": "unavailable", "status_msg": "Following threads"})

    async def test_matrix_presence_disabled_does_not_retry_forever(self):
        adapter = MatrixAdapter.__new__(MatrixAdapter)
        adapter.settings = SimpleNamespace(user_id="@agent_one:example.test")
        adapter.agent = "agent_one"
        adapter.session = object()
        adapter._request = AsyncMock(side_effect=RuntimeError("Matrix PUT failed with HTTP 403"))
        self.assertFalse(await adapter.publish_activity(Activity("Reading", Presence.CONNECTED)))
        self.assertFalse(await adapter.publish_activity(Activity("Reading", Presence.CONNECTED)))
        self.assertEqual(adapter._request.await_count, 1)

    async def test_discord_presence_uses_custom_activity(self):
        adapter = DiscordAdapter.__new__(DiscordAdapter)
        adapter.client = SimpleNamespace(is_ready=lambda: True,
                                         change_presence=AsyncMock())
        with patch.dict("sys.modules", {"discord": SimpleNamespace(
                Status=SimpleNamespace(online="online", idle="idle", offline="offline"),
                CustomActivity=lambda name: ("custom", name))}):
            self.assertTrue(await adapter.publish_activity(Activity("Working in the code", Presence.AWAKE)))
        self.assertEqual(adapter.client.change_presence.await_args.kwargs,
                         {"status": "online", "activity": ("custom", "Working in the code")})

    async def test_agent_cli_delivers_status_over_private_socket(self):
        with tempfile.TemporaryDirectory() as directory:
            received = []

            async def set_status(request):
                if not request.get("operator") and request["capability"] != "test-capability":
                    raise ValueError("invalid capability")
                received.append(request["text"])

            service = ActivitySocket(Path(directory) / "activity.sock", set_status)
            await service.start()
            try:
                env = {"AGENT_BRIDGE_ACTIVITY_SOCKET": str(service.path),
                       "AGENT_BRIDGE_ACTIVITY_TRANSPORT": "matrix",
                       "AGENT_BRIDGE_ACTIVITY_SCOPE": "!room:example.test:main",
                       "AGENT_BRIDGE_ACTIVITY_CAPABILITY": "test-capability"}
                with patch.dict(os.environ, env), patch.object(
                        sys, "argv", ["bridge.activity_cli", "Reading quietly"]):
                    result = await asyncio.to_thread(activity_cli_main)
                self.assertEqual(result, 0)
                self.assertEqual(received, ["Reading quietly"])
                with patch.object(sys, "argv", ["bridge.activity_cli", "Tending notes",
                                                     "--socket-path", str(service.path)]):
                    result = await asyncio.to_thread(activity_cli_main)
                self.assertEqual(result, 0)
                self.assertEqual(received, ["Reading quietly", "Tending notes"])
            finally:
                await service.close()

    async def test_socket_callback_errors_are_generic_and_logs_are_safe(self):
        with tempfile.TemporaryDirectory() as directory:
            async def set_status(request):
                raise ValueError("remote body token=activity-secret")

            service = ActivitySocket(Path(directory) / "activity.sock", set_status)
            await service.start()
            try:
                request = {"transport": "matrix", "scope": "!private:example.test",
                           "capability": "capability-secret", "text": "Reading quietly",
                           "hours": 3}
                for error_class in (ValueError, TypeError):
                    with self.subTest(error_class=error_class.__name__):
                        async def fail(request):
                            raise error_class("remote body token=activity-secret")

                        service.set_status = fail
                        with self.assertLogs("agent_bridge.activity_socket",
                                             level="WARNING") as captured:
                            reader, writer = await asyncio.open_unix_connection(
                                str(service.path))
                            writer.write((json.dumps(request) + "\n").encode())
                            await writer.drain()
                            response = json.loads(await reader.readline())
                            writer.close()
                            await writer.wait_closed()
                        self.assertEqual(response, {"ok": False,
                                                    "error": "activity update failed"})
                        self.assertEqual(len(captured.output), 1)
                        self.assertIn("error_class=" + error_class.__name__,
                                      captured.output[0])
                        self.assertNotIn("activity-secret", captured.output[0])
                        self.assertNotIn("capability-secret", captured.output[0])
                        self.assertNotIn("!private:example.test", captured.output[0])
            finally:
                await service.close()

    async def test_unexpected_request_failure_returns_generic_error(self):
        with tempfile.TemporaryDirectory() as directory:
            service = ActivitySocket(Path(directory) / "activity.sock", AsyncMock())
            await service.start()
            try:
                with patch("bridge.activity_socket.struct.unpack",
                           side_effect=RuntimeError("token=request-secret")):
                    with self.assertLogs("agent_bridge.activity_socket",
                                         level="WARNING") as captured:
                        reader, writer = await asyncio.open_unix_connection(str(service.path))
                        writer.write((json.dumps({"operator": True, "text": "Reading quietly",
                                                  "hours": 3}) + "\n").encode())
                        await writer.drain()
                        response = json.loads(await reader.readline())
                        writer.close()
                        await writer.wait_closed()
                self.assertEqual(response, {"ok": False, "error": "activity request failed"})
                self.assertEqual(len(captured.output), 1)
                self.assertIn("error_class=RuntimeError", captured.output[0])
                self.assertNotIn("request-secret", captured.output[0])
            finally:
                await service.close()

    async def test_socket_keeps_fixed_local_status_rejection(self):
        with tempfile.TemporaryDirectory() as directory:
            async def reject(request):
                raise ActivityRejected("status")

            service = ActivitySocket(Path(directory) / "activity.sock", reject)
            await service.start()
            try:
                reader, writer = await asyncio.open_unix_connection(str(service.path))
                writer.write((json.dumps({"operator": True, "text": "Reading quietly",
                                          "hours": 3}) + "\n").encode())
                await writer.drain()
                response = json.loads(await reader.readline())
                writer.close()
                await writer.wait_closed()
                self.assertEqual(response, {"ok": False,
                                            "error": "status must be short public text without identifiers"})
            finally:
                await service.close()

    async def test_refresh_maps_awake_to_online_and_sleep_to_idle(self):
        with tempfile.TemporaryDirectory() as directory:
            state = {"active": True, "awake": True}

            def snapshot() -> tuple[bool, bool, str]:
                return (state["active"], state["awake"], "bash")

            runtime = SimpleNamespace(activity_snapshot=snapshot,
                                      activity_topics=lambda: ["code"])
            matrix = SimpleNamespace(publish_activity=AsyncMock(return_value=True))
            generated = AsyncMock(side_effect=["Working in the code"])
            service = ActivityService("agent_one", runtime, (matrix,),
                                      Path(directory) / "activity.json", generated,
                                      now=lambda: 0.0, chance=lambda: 0.0)
            await service.refresh()
            self.assertEqual(matrix.publish_activity.await_args.args[0].presence,
                             Presence.AWAKE)
            state["active"] = False
            state["awake"] = False
            await service.refresh()
            self.assertEqual(matrix.publish_activity.await_args.args[0].presence,
                             Presence.CONNECTED)

    async def test_shutdown_publishes_disconnected_state(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = BridgeRuntime(Path(directory) / "sessions", lambda *_: None,
                                    AsyncMock())
            runtime.activity_topics = lambda: ["research"]
            matrix = SimpleNamespace(publish_activity=AsyncMock(return_value=True))
            generated = AsyncMock(side_effect=["Following threads"])
            service = ActivityService("agent_one", runtime, (matrix,),
                                      Path(directory) / "activity.json", generated,
                                      now=lambda: 0.0, chance=lambda: 0.0)
            await service.refresh()
            matrix.publish_activity.reset_mock()
            await service.shutdown()
            self.assertEqual(matrix.publish_activity.await_args.args[0].presence,
                             Presence.DISCONNECTED)
            self.assertEqual(matrix.publish_activity.await_args.args[0].text,
                             "Following threads")

    async def test_shutdown_cancels_poll_before_publishing_disconnected(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = SimpleNamespace(activity_snapshot=lambda: (False, False, ""),
                                      activity_topics=lambda: ["research"])
            release = asyncio.Event()
            published: list[Presence] = []

            class SlowAdapter:
                async def publish_activity(self, activity):
                    published.append(activity.presence)
                    if activity.presence is Presence.CONNECTED:
                        await release.wait()  # simulate a slow/retrying publish
                    return True

            generated = AsyncMock(side_effect=["Following threads"])
            service = ActivityService("agent_one", runtime, (SlowAdapter(),),
                                      Path(directory) / "activity.json", generated,
                                      now=lambda: 0.0, chance=lambda: 0.0)
            poll = asyncio.create_task(service.run())
            for _ in range(1000):
                if published:
                    break
                await asyncio.sleep(0)
            self.assertEqual(published, [Presence.CONNECTED])
            await service.shutdown()
            self.assertTrue(poll.done())
            self.assertEqual(published[-1], Presence.DISCONNECTED)

    async def test_shutdown_publish_is_bounded_by_timeout(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = SimpleNamespace(activity_snapshot=lambda: (False, False, ""),
                                      activity_topics=lambda: ["research"])

            class WedgedAdapter:
                async def publish_activity(self, activity):
                    await asyncio.sleep(3600)  # never completes
                    return True

            generated = AsyncMock(side_effect=["Following threads"])
            service = ActivityService("agent_one", runtime, (WedgedAdapter(),),
                                      Path(directory) / "activity.json", generated,
                                      now=lambda: 0.0, chance=lambda: 0.0)
            started = time.monotonic()
            await service.shutdown(publish_timeout=0.05)
            self.assertLess(time.monotonic() - started, 1.0)


if __name__ == "__main__":
    unittest.main()
