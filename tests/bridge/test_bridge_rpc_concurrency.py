"""Production settlement and compaction exercised through actual subprocess pipes."""
import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bridge.runtime.pi_rpc import PiRpcClient, PiRpcError

SCRIPT = r'''
import json, sys
def emit(value):
    print(json.dumps(value), flush=True)
for line in sys.stdin:
    request = json.loads(line)
    kind = request['type']
    if kind == 'prompt':
        emit({'type': 'response', 'id': request['id'], 'success': True})
        emit({'type': 'tool_execution_start', 'toolName': 'read'})
        emit({'type': 'tool_execution_end', 'toolName': 'read', 'result': {
            'content': [{'type': 'image', 'mimeType': 'image/png', 'data': 'eA=='}]}})
        emit({'type': 'agent_end'})
        emit({'type': 'compaction_start', 'reason': 'threshold'})
    elif kind == 'get_state':
        emit({'type': 'compaction_end', 'reason': 'threshold', 'aborted': False, 'willRetry': True})
        emit({'type': 'message_end', 'message': {'role': 'assistant',
            'content': [{'type': 'text', 'text': 'Image result after compaction'}]}})
        emit({'type': 'agent_end'})
        emit({'type': 'agent_settled'})
        emit({'type': 'response', 'id': request['id'], 'success': True, 'data': {}})
    elif kind == 'compact':
        emit({'type': 'response', 'id': request['id'], 'success': True, 'data': {'summary': 'Checkpoint'}})
    else:
        emit({'type': 'response', 'id': request['id'], 'success': True})
'''


class RpcConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_shutdown_cancels_a_stalled_event_consumer(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pi.py"
            path.write_text(SCRIPT)
            client = PiRpcClient([sys.executable, str(path)], Path(directory), {}, timeout_seconds=2)
            entered = asyncio.Event()
            never = asyncio.Event()

            async def consumer(event):
                entered.set()
                await never.wait()

            prompt = asyncio.create_task(client.prompt("image", consumer))
            await asyncio.wait_for(entered.wait(), 1)
            await asyncio.wait_for(client.stop(), 1)
            with self.assertRaises(PiRpcError):
                await asyncio.wait_for(prompt, 1)
            self.assertFalse(client.running)
    async def test_already_compacted_requires_a_real_existing_summary(self):
        for summary in ("Existing checkpoint", "", None):
            with self.subTest(summary=summary), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "pi.py"
                import json
                path.write_text("import json,sys\n" +
                    "for line in sys.stdin:\n" +
                    " r=json.loads(line)\n" +
                    " if r['type']=='compact': print(json.dumps({'type':'response','id':r['id'],'success':False,'error':'Already compacted'}),flush=True)\n" +
                    " else: print(json.dumps({'type':'response','id':r['id'],'success':True,'data':{'messages':" +
                    json.dumps([{"role": "compactionSummary", "summary": summary}]).replace("null", "None") + "}}),flush=True)\n")
                client = PiRpcClient([sys.executable, str(path)], Path(directory), {}, timeout_seconds=1)
                try:
                    if summary:
                        self.assertEqual(await client.compact(), summary)
                    else:
                        with self.assertRaisesRegex(PiRpcError, "no saved summary"):
                            await client.compact()
                        self.assertFalse(client.running)
                    self.assertEqual(client._pending_responses, {})
                finally:
                    await client.stop()

    async def test_nonprompt_command_timeout_is_bounded_and_cleans_pending(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pi.py"
            path.write_text("import sys\nfor line in sys.stdin: pass\n")
            client = PiRpcClient([sys.executable, str(path)], Path(directory), {}, timeout_seconds=0.05)
            self.addAsyncCleanup(client.stop)
            with self.assertRaises(PiRpcError):
                await asyncio.wait_for(client.get_state(), 1)
            self.assertEqual(client._pending_responses, {})

    async def test_image_and_auto_compaction_wait_for_production_settlement(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pi.py"
            path.write_text(SCRIPT)
            client = PiRpcClient([sys.executable, str(path)], Path(directory), {}, timeout_seconds=2)
            self.addAsyncCleanup(client.stop)
            compact_started = asyncio.Event()
            events = []

            async def on_event(event):
                events.append(event)
                if event["type"] == "compaction_start":
                    compact_started.set()

            prompt = asyncio.create_task(client.prompt("view image", on_event))
            await asyncio.wait_for(compact_started.wait(), 1)
            self.assertFalse(prompt.done())
            compact = asyncio.create_task(client.compact())
            await client.get_state()  # Release the controlled post-agent_end continuation.
            result = await asyncio.wait_for(prompt, 1)
            self.assertEqual(result.text, "Image result after compaction")
            self.assertEqual(await asyncio.wait_for(compact, 1), "Checkpoint")
            self.assertIn("image", str(events[1]))
            self.assertEqual(client._pending_responses, {})

    async def test_cancellation_fails_pending_commands_and_stops_process(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pi.py"
            path.write_text(SCRIPT)
            client = PiRpcClient([sys.executable, str(path)], Path(directory), {}, timeout_seconds=2)
            accepted = asyncio.Event()

            async def event(value):
                if value["type"] == "compaction_start":
                    accepted.set()

            prompt = asyncio.create_task(client.prompt("image", event))
            await asyncio.wait_for(accepted.wait(), 1)
            prompt.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await prompt
            self.assertFalse(client.running)
            self.assertFalse(client.active)
            self.assertEqual(client._pending_responses, {})

    async def test_stderr_is_redacted_when_process_dies(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pi.py"
            path.write_text("import sys\nprint('secret-image-and-token', file=sys.stderr, flush=True)\n")
            client = PiRpcClient([sys.executable, str(path)], Path(directory), {}, timeout_seconds=1)
            self.addAsyncCleanup(client.stop)
            with self.assertLogs("agent_bridge.pi_rpc", level="DEBUG") as logs:
                with self.assertRaises(PiRpcError):
                    await client.prompt("private prompt", lambda event: asyncio.sleep(0))
            self.assertNotIn("secret-image-and-token", str(logs.output))
            self.assertNotIn("private prompt", str(logs.output))
            # The exit still reports bounded, value-free facts a reader can act on.
            self.assertIn("stderr_lines=1", str(logs.output))
            self.assertIn("stderr_shape=<w>", str(logs.output))
            self.assertIn("returncode=", str(logs.output))

    async def test_eof_waits_for_delayed_child_exit_status_before_logging(self):
        with tempfile.TemporaryDirectory() as directory:
            client = PiRpcClient([sys.executable], Path(directory), {})
            process = SimpleNamespace(returncode=None)

            async def exited():
                await asyncio.sleep(0)
                process.returncode = 3
                return 3

            process.wait = AsyncMock(side_effect=exited)
            client._process = process
            with self.assertLogs("agent_bridge.pi_rpc", level="ERROR") as logs:
                await client._log_stderr_exit()
            process.wait.assert_awaited_once()
            self.assertIn("returncode=3", str(logs.output))

    async def test_eof_exit_status_wait_remains_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            client = PiRpcClient([sys.executable], Path(directory), {})

            async def not_exited():
                await asyncio.Event().wait()

            client._process = SimpleNamespace(returncode=None, wait=AsyncMock(side_effect=not_exited))
            with patch("bridge.runtime.pi_rpc.STDERR_DRAIN_GRACE_SECONDS", 0.01):
                with self.assertLogs("agent_bridge.pi_rpc", level="ERROR") as logs:
                    await asyncio.wait_for(client._log_stderr_exit(), 1)
            self.assertIn("returncode=None", str(logs.output))

    async def test_exit_reports_failure_shape_and_code(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pi.py"
            path.write_text(
                "import sys\n"
                "print('Error: Cannot find module \\\'/home/dev/private-package/index.js\\\'', file=sys.stderr, flush=True)\n"
                "sys.exit(3)\n")
            client = PiRpcClient([sys.executable, str(path)], Path(directory), {}, timeout_seconds=1)
            self.addAsyncCleanup(client.stop)
            with self.assertLogs("agent_bridge.pi_rpc", level="ERROR") as logs:
                with self.assertRaises(PiRpcError):
                    await client.prompt("private prompt", lambda event: asyncio.sleep(0))
            report = str(logs.output)
            self.assertIn("returncode=3", report)
            self.assertIn("stderr_shape=error cannot find module <w>", report)
            self.assertIn("stderr_digest=", report)
            self.assertNotIn("private-package", report)
            self.assertNotIn("/home/dev", report)
