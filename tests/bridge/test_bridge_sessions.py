import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bridge.runtime.session import BridgeRuntime, Incoming
from bridge.runtime.state import SessionKey
from bridge.sessions_cli import fetch
from bridge.sessions_service import SessionsService, validate_request


class SessionsRequestValidationTests(unittest.TestCase):
    def test_defaults_and_rejections(self):
        self.assertEqual(validate_request({}), {"detail": "summary"})
        self.assertEqual(validate_request({"detail": "full"}), {"detail": "full"})
        for bad in ({"detail": "verbose"},
                    {"detail": 1},
                    {"detail": "summary", "extra": 1}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_request(bad)


class FakeRpc:
    def __init__(self, session_file):
        self.session_file = session_file
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.active = False
        self.stopped = False

    async def prompt(self, message, on_event, images=None, *, on_accepted=None):
        self.active = True
        if on_accepted:
            await on_accepted()
        self.started.set()
        await self.release.wait()
        self.active = False
        from bridge.runtime.pi_rpc import PiRunResult
        return PiRunResult("Done")

    async def steer(self, message, images=None):
        pass

    async def abort(self):
        self.release.set()

    async def compact(self):
        return "summary"

    async def stop(self):
        self.stopped = True


class SessionsServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "bridge-sessions.sock"
        self.calls = []

        async def fetch_sessions(request):
            self.calls.append(request)
            return {
                "ok": True,
                "total_scopes": 2,
                "awake": 2,
                "active": 1,
                "active_non_idle": 1,
                "state_breakdown": {"idle": 1, "running": 1},
                "per_agent": {"agent_two": 2},
                "per_transport": {"matrix": 2},
                "awake_per_agent": {"agent_two": 2},
                "awake_per_transport": {"matrix": 2},
                "critical_sessions": [{"provider": "test", "model": "test-model", "agent": "agent_two", "transport": "matrix",
                                       "status": "running", "epoch": 1,
                                       "last_activity_ms": 1000, "awake": True}],
            }

        self.service = SessionsService(self.path, fetch_sessions)
        await self.service.start()

    async def asyncTearDown(self):
        await self.service.close()
        self.directory.cleanup()

    async def test_private_socket_and_cli_round_trip(self):
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        summary = await asyncio.to_thread(fetch, str(self.path), detail="summary")
        self.assertEqual(summary["total_scopes"], 2)
        self.assertEqual(self.calls, [{"detail": "summary"}])

    async def test_invalid_request_does_not_reach_adapter(self):
        reader, writer = await asyncio.open_unix_connection(str(self.path))
        writer.write(b'{"detail":"verbose"}\n')
        await writer.drain()
        response = json.loads(await reader.readline())
        writer.close()
        await writer.wait_closed()
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"], "detail must be summary or full")
        self.assertEqual(self.calls, [])

    async def test_callback_errors_do_not_leak_into_cli_or_logs(self):
        for error_class in (ValueError, TypeError):
            with self.subTest(error_class=error_class.__name__):
                async def fail(request):
                    raise error_class("remote body token=sessions-secret")

                self.service.fetch_sessions = fail
                with self.assertLogs("agent_bridge.sessions", level="WARNING") as captured:
                    with self.assertRaisesRegex(RuntimeError, "sessions summary failed") as failure:
                        await asyncio.to_thread(fetch, str(self.path), detail="summary")
                self.assertNotIn("sessions-secret", str(failure.exception))
                self.assertEqual(len(captured.output), 1)
                self.assertIn("error_class=" + error_class.__name__, captured.output[0])
                self.assertNotIn("sessions-secret", captured.output[0])

    async def test_fixed_summary_shape_diagnostic_remains_actionable(self):
        async def invalid_summary(request):
            return {"ok": True, "total_scopes": "wrong"}

        self.service.fetch_sessions = invalid_summary
        with self.assertRaisesRegex(RuntimeError,
                                    "sessions summary total_scopes must be an integer"):
            await asyncio.to_thread(fetch, str(self.path), detail="summary")

    async def test_unexpected_request_failure_returns_generic_error(self):
        with patch("bridge.sessions_service.validate_request",
                   side_effect=RuntimeError("token=request-secret")):
            with self.assertLogs("agent_bridge.sessions", level="WARNING") as captured:
                with self.assertRaisesRegex(RuntimeError, "sessions request failed") as failure:
                    await asyncio.to_thread(fetch, str(self.path), detail="summary")
        self.assertNotIn("request-secret", str(failure.exception))
        self.assertEqual(len(captured.output), 1)
        self.assertIn("error_class=RuntimeError", captured.output[0])
        self.assertNotIn("request-secret", captured.output[0])

    async def test_active_socket_cannot_be_replaced(self):
        another = SessionsService(self.path, self.service.fetch_sessions)
        with self.assertRaisesRegex(RuntimeError, "another sessions service"):
            await another.start()
        self.assertTrue(self.path.is_socket())


class RuntimeSessionSummaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_runtime_reports_zeroes(self):
        with tempfile.TemporaryDirectory() as directory:
            async def send(key, kind, text, request_id):
                pass

            runtime = BridgeRuntime(Path(directory), lambda key, path: FakeRpc(path), send)
            summary = await runtime.session_summary()
            self.assertEqual(summary["total_scopes"], 0)
            self.assertEqual(summary["awake"], 0)
            self.assertEqual(summary["active"], 0)
            self.assertEqual(summary["active_non_idle"], 0)
            self.assertEqual(summary["critical_sessions"], [])

    async def test_running_session_appears_as_critical(self):
        with tempfile.TemporaryDirectory() as directory:
            clients = []

            def factory(key, session_file):
                client = FakeRpc(session_file)
                clients.append(client)
                return client

            async def send(key, kind, text, request_id):
                pass

            runtime = BridgeRuntime(Path(directory), factory, send)
            key = SessionKey("agent_two", "matrix", "!room:example")
            await runtime.submit(key, Incoming("Operator", "$one", "Please check"))
            await clients[0].started.wait()
            summary = await runtime.session_summary()
            self.assertEqual(summary["total_scopes"], 1)
            self.assertEqual(summary["awake"], 1)
            self.assertEqual(summary["active"], 1)
            self.assertEqual(summary["active_non_idle"], 1)
            self.assertEqual(summary["awake_per_agent"], {"agent_two": 1})
            self.assertEqual(summary["awake_per_transport"], {"matrix": 1})
            self.assertEqual(summary["state_breakdown"], {"running": 1})
            self.assertEqual(summary["per_agent"], {"agent_two": 1})
            self.assertEqual(summary["per_transport"], {"matrix": 1})
            self.assertEqual(len(summary["critical_sessions"]), 1)
            self.assertEqual(summary["critical_sessions"][0]["status"], "running")
            self.assertTrue(summary["critical_sessions"][0]["awake"])
            clients[0].release.set()
            await runtime.wait_idle(key)

    async def test_awake_count_after_final_reply_before_sweep(self):
        with tempfile.TemporaryDirectory() as directory:
            clients = []
            outputs = []

            def factory(key, session_file):
                client = FakeRpc(session_file)
                clients.append(client)
                return client

            async def send(key, kind, text, request_id):
                outputs.append((kind, text, request_id))

            runtime = BridgeRuntime(Path(directory), factory, send)
            key = SessionKey("agent_two", "matrix", "!room:example")
            await runtime.submit(key, Incoming("Operator", "$one", "Please check"))
            await clients[0].started.wait()
            clients[0].release.set()
            await runtime.wait_idle(key)
            summary = await runtime.session_summary()
            # After final reply, the task is done but the client is still
            # resident until the idle sweep closes it.
            self.assertEqual(summary["active"], 0)
            self.assertEqual(summary["active_non_idle"], 0)
            self.assertEqual(summary["awake"], 1)
            self.assertEqual(summary["awake_per_agent"], {"agent_two": 1})
            self.assertEqual(summary["awake_per_transport"], {"matrix": 1})
            self.assertEqual(summary["state_breakdown"], {"idle": 1})
            self.assertEqual(summary["critical_sessions"], [])
            self.assertFalse(clients[0].stopped)

    async def test_full_detail_includes_requester_and_final_sent(self):
        with tempfile.TemporaryDirectory() as directory:
            clients = []

            def factory(key, session_file):
                client = FakeRpc(session_file)
                clients.append(client)
                return client

            async def send(key, kind, text, request_id):
                pass

            runtime = BridgeRuntime(Path(directory), factory, send)
            key = SessionKey("agent_two", "discord", "123")
            await runtime.submit(key, Incoming("Operator", "$two", "Go"))
            await clients[0].started.wait()
            summary = await runtime.session_summary(detail="full")
            session = summary["critical_sessions"][0]
            self.assertEqual(session["requester"], "Operator")
            self.assertEqual(session["request_id"], "$two")
            self.assertEqual(session["final_sent"], False)
            self.assertEqual(len(session["scope_hash"]), 24)
            clients[0].release.set()
            await runtime.wait_idle(key)

    async def test_idle_checkpoint_is_not_critical(self):
        with tempfile.TemporaryDirectory() as directory:
            now = [1_000_000]
            clients = []

            def factory(key, session_file):
                client = FakeRpc(session_file)
                clients.append(client)
                return client

            async def send(key, kind, text, request_id):
                pass

            runtime = BridgeRuntime(Path(directory), factory, send,
                                    now_ms=lambda: now[0])
            key = SessionKey("agent_three", "matrix", "!room:example")
            await runtime.submit(key, Incoming("Operator", "$one", "Go"))
            await clients[0].started.wait()
            clients[0].release.set()
            await runtime.wait_idle(key)
            now[0] += 181_000
            self.assertEqual(await runtime.sweep_idle(), 1)
            summary = await runtime.session_summary()
            self.assertEqual(summary["active_non_idle"], 0)
            self.assertEqual(summary["critical_sessions"], [])
            self.assertEqual(summary["state_breakdown"], {"idle": 1})

    async def test_summary_does_not_expose_raw_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            clients = []

            def factory(key, session_file):
                client = FakeRpc(session_file)
                clients.append(client)
                return client

            async def send(key, kind, text, request_id):
                pass

            runtime = BridgeRuntime(Path(directory), factory, send)
            key = SessionKey("agent_two", "matrix", "!secret-room:example")
            await runtime.submit(key, Incoming("Operator", "$one", "Go"))
            await clients[0].started.wait()
            summary = await runtime.session_summary(detail="full")
            session = summary["critical_sessions"][0]
            self.assertNotIn("secret-room", str(session))
            self.assertNotIn("!room:example", str(session))
            self.assertEqual(len(session["scope_hash"]), 24)
            clients[0].release.set()
            await runtime.wait_idle(key)


if __name__ == "__main__":
    unittest.main()
