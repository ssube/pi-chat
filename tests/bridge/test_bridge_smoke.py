import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bridge.runtime.pi_rpc import PiRunResult
from bridge.smoke import smoke


class FakeClient:
    def __init__(self, answer):
        self.answer = answer
        self.compacted = False
        self.stopped = False

    async def prompt(self, message, on_event):
        return PiRunResult(self.answer)

    async def compact(self):
        self.compacted = True
        return "summary"

    async def stop(self):
        self.stopped = True


class SmokeTests(unittest.IsolatedAsyncioTestCase):
    async def test_success_compacts_and_removes_private_session(self):
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient('"BRIDGE_RPC_OK."')

            def pi_client(key, session_file, **options):
                self.assertEqual(options, {"safe_mode": True, "timeout_seconds": 350})
                self.assertEqual(session_file.parent.parent, Path(directory))
                self.assertEqual(session_file.parent.stat().st_mode & 0o777, 0o700)
                return client

            profile = SimpleNamespace(state_dir=Path(directory), agent="agent_three",
                                      matrix=object(), pi_client=pi_client)
            with patch("builtins.print"):
                await smoke(profile, timeout_seconds=350)
            self.assertTrue(client.compacted)
            self.assertTrue(client.stopped)
            self.assertEqual(list(Path(directory).iterdir()), [])

    async def test_marker_failure_stops_without_compacting(self):
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient("Unexpected response")
            profile = SimpleNamespace(state_dir=Path(directory), agent="agent_two",
                                      matrix=None, pi_client=lambda *a, **kw: client)
            with self.assertRaisesRegex(RuntimeError, "smoke marker"):
                await smoke(profile)
            self.assertFalse(client.compacted)
            self.assertTrue(client.stopped)
            self.assertEqual(list(Path(directory).iterdir()), [])

    async def test_compaction_failure_still_stops(self):
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient("BRIDGE_RPC_OK")

            async def failed_compact():
                raise RuntimeError("provider failed")

            client.compact = failed_compact
            profile = SimpleNamespace(state_dir=Path(directory), agent="agent_three",
                                      matrix=object(), pi_client=lambda *a, **kw: client)
            with self.assertRaisesRegex(RuntimeError, "provider failed"):
                await smoke(profile)
            self.assertTrue(client.stopped)
            self.assertEqual(list(Path(directory).iterdir()), [])

    async def test_short_session_compaction_skip_is_a_clean_smoke_result(self):
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient("BRIDGE_RPC_OK")

            async def too_small():
                return None

            client.compact = too_small
            profile = SimpleNamespace(state_dir=Path(directory), agent="agent_two",
                                      matrix=object(), pi_client=lambda *a, **kw: client)
            with patch("builtins.print") as printed:
                await smoke(profile)
            self.assertIn("compaction skipped", printed.call_args.args[0])
            self.assertTrue(client.stopped)
            self.assertEqual(list(Path(directory).iterdir()), [])
