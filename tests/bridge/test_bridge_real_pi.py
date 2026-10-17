"""Optional offline contract with the installed production Pi package."""
import os
import tempfile
import unittest
from pathlib import Path

from bridge.runtime.pi_rpc import PiRpcClient


@unittest.skipUnless(os.environ.get("BRIDGE_TEST_PI_CLI"), "set BRIDGE_TEST_PI_CLI for offline real-Pi contracts")
class RealPiTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_image_tool_compaction_and_settlement(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agent = root / "agent"
            agent.mkdir()
            (agent / "settings.json").write_text(
                '{"compaction":{"enabled":true,"reserveTokens":1024,"keepRecentTokens":1}}')
            fixture = Path(__file__).parent / "fixtures" / "offline-pi.ts"
            command = [os.environ.get("BRIDGE_TEST_NODE", "node"), os.environ["BRIDGE_TEST_PI_CLI"],
                       "--mode", "rpc", "--provider", "bridge-offline", "--model", "test",
                       "--thinking", "off", "--session", str(root / "session.jsonl"),
                       "--extension", str(fixture.resolve()), "--tools", "view_test_image"]
            # Use the package entrypoint directly, never an owner launcher that loads credentials.
            env = {"PATH": os.environ.get("PATH", ""), "HOME": str(root),
                   "PI_CODING_AGENT_DIR": str(agent), "JITI_CACHE_DIR": str(root / "jiti")}
            client = PiRpcClient(command, root, env, timeout_seconds=30)
            self.addAsyncCleanup(client.stop)
            events = []

            async def event(value):
                events.append(value)

            result = await client.prompt("Inspect the synthetic image", event)
            self.assertEqual(result.text, "Image inspected successfully.")
            kinds = [value["type"] for value in events]
            self.assertIn("tool_execution_end", kinds)
            self.assertIn("compaction_start", kinds)
            self.assertIn("compaction_end", kinds)
            self.assertEqual(kinds[-1], "agent_settled")
            self.assertLess(kinds.index("tool_execution_end"), kinds.index("compaction_start"))
            summary = await client.compact()
            self.assertIn("Offline checkpoint", summary)
