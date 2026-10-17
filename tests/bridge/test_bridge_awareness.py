import asyncio
import tempfile
import unittest
from pathlib import Path

from bridge.runtime.pi_rpc import PiRunResult
from bridge.runtime.session import BridgeRuntime, Incoming
from bridge.runtime.state import SessionKey, read_checkpoint


class FakePi:
    def __init__(self, result="reply"):
        self.result = result
        self.active = False
        self.prompts = []
        self.steers = []
        self.on_event = None
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def prompt(self, prompt, on_event, images=None, *, on_accepted=None):
        self.active = True
        self.prompts.append(prompt)
        self.on_event = on_event
        if on_accepted:
            await on_accepted()
        self.started.set()
        await self.release.wait()
        self.active = False
        return PiRunResult(self.result)

    async def steer(self, prompt, images=None):
        self.steers.append(prompt)

    async def compact(self):
        return "saved conversation"

    async def stop(self):
        pass

    async def abort(self):
        self.release.set()


class AwarenessTests(unittest.IsolatedAsyncioTestCase):
    async def test_agent_four_transport_scopes_compact_before_three_minute_sleep(self):
        with tempfile.TemporaryDirectory() as directory:
            now = [1_000_000]
            clients = {}
            lifecycle = []

            def factory(key, path):
                pi = FakePi()

                async def compact():
                    lifecycle.append((key.transport, "compact"))
                    return f"saved {key.transport} conversation"

                async def stop():
                    lifecycle.append((key.transport, "stop"))

                pi.compact = compact
                pi.stop = stop
                clients[key] = pi
                return pi

            async def output(*args):
                pass

            runtime = BridgeRuntime(Path(directory), factory, output,
                                    now_ms=lambda: now[0])
            matrix = SessionKey("agent_four", "matrix", "!room:test:main")
            discord = SessionKey("agent_four", "discord", "123")
            for key in (matrix, discord):
                self.assertFalse(await runtime.admit(key, False))
                self.assertTrue(await runtime.admit(key, True))
                await runtime.submit(key, Incoming("Operator", "ping", "Please reply"))
                await clients[key].started.wait()
                clients[key].release.set()
                await runtime.wait_idle(key)
            now[0] += 179_999
            self.assertEqual(await runtime.sweep_idle(), 0)
            self.assertTrue(await runtime.is_awake(matrix))
            self.assertTrue(await runtime.is_awake(discord))
            now[0] += 1
            self.assertEqual(await runtime.sweep_idle(), 2)
            for key in (matrix, discord):
                self.assertFalse(await runtime.admit(key, False))
                self.assertEqual([event for transport, event in lifecycle
                                  if transport == key.transport], ["compact", "stop"])
                checkpoint = read_checkpoint(key.directory(Path(directory)))
                self.assertEqual(checkpoint.summary, f"saved {key.transport} conversation")
                self.assertEqual(checkpoint.summary_saved_ms, now[0])
            await runtime.close()

    async def test_ambient_steer_does_not_erase_completed_direct_reply(self):
        with tempfile.TemporaryDirectory() as directory:
            pi = FakePi("[[bridge:pass]]")
            outputs = []

            async def output(key, kind, text, event_id):
                outputs.append((kind, text, event_id))

            runtime = BridgeRuntime(Path(directory), lambda key, path: pi, output)
            key = SessionKey("agent_one", "matrix", "!room:test:main")
            await runtime.submit(key, Incoming("Agent_three", "$direct", "Please reply"))
            await pi.started.wait()
            await pi.on_event({
                "type": "message_end",
                "message": {"role": "assistant", "content": [
                    {"type": "text", "text": "pong agent_three-agent_one"},
                ]},
            })
            self.assertEqual(await runtime.submit(
                key, Incoming("Agent_three", "$ambient", "Status update", optional=True)),
                "steered")
            pi.release.set()
            with self.assertLogs("agent_bridge.runtime", level="INFO") as logs:
                await runtime.wait_idle(key)

            self.assertIn(("final", "pong agent_three-agent_one", "$direct"), outputs)
            self.assertNotIn(("cleanup", "", "$direct"), outputs)
            self.assertTrue(any("Preserved direct reply across ambient steer" in line
                                for line in logs.output))
            self.assertFalse(any("Passed over a direct ping without a reaction" in line
                                 for line in logs.output))

    async def test_later_direct_steer_does_not_revive_earlier_reply(self):
        with tempfile.TemporaryDirectory() as directory:
            pi = FakePi("[[bridge:pass]]")
            outputs = []

            async def output(key, kind, text, event_id):
                outputs.append((kind, text, event_id))

            runtime = BridgeRuntime(Path(directory), lambda key, path: pi, output)
            key = SessionKey("agent_one", "matrix", "!room:test:main")
            await runtime.submit(key, Incoming("Agent_three", "$first", "First request"))
            await pi.started.wait()
            await pi.on_event({
                "type": "message_end",
                "message": {"role": "assistant", "content": [
                    {"type": "text", "text": "First answer"},
                ]},
            })
            self.assertEqual(await runtime.submit(
                key, Incoming("Agent_two", "$second", "Second request")), "steered")
            self.assertEqual(await runtime.submit(
                key, Incoming("Agent_three", "$ambient", "Status update", optional=True)),
                "steered")
            pi.release.set()
            await runtime.wait_idle(key)

            self.assertFalse(any(kind == "final" and text == "First answer"
                                 for kind, text, _ in outputs))

    async def test_post_reply_awake_pass_and_idle_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            now = [1_000_000]
            clients = []
            outputs = []

            def factory(key, path):
                client = FakePi("reply" if not clients else "[[bridge:pass]]")
                clients.append(client)
                return client

            async def output(key, kind, text, event_id):
                outputs.append((kind, text, event_id))

            runtime = BridgeRuntime(Path(directory), factory, output,
                                    now_ms=lambda: now[0])
            key = SessionKey("agent_one", "matrix", "!room:test:main")
            self.assertFalse(await runtime.admit(key, False))
            self.assertTrue(await runtime.admit(key, True))
            await runtime.submit(key, Incoming("Operator", "one", "ping"))
            await clients[0].started.wait()
            clients[0].release.set()
            await runtime.wait_idle(key)
            self.assertTrue(await runtime.is_awake(key))
            now[0] += 60_000
            self.assertTrue(await runtime.admit(key, False))
            clients[0].result = "[[bridge:pass]]"
            clients[0].started.clear()
            clients[0].release.clear()
            await runtime.submit(key, Incoming("Agent_three", "two", "ordinary", optional=True))
            await runtime.record_wake(key, "two")
            await clients[0].started.wait()
            clients[0].release.set()
            await runtime.wait_idle(key)
            self.assertEqual(len(clients), 1)
            self.assertEqual([kind for kind, _, _ in outputs if kind == "final"], ["final"])
            self.assertFalse(any(event_id == "two" and kind in {"ack", "progress", "final"}
                                 for kind, _, event_id in outputs))
            now[0] += 181_000
            self.assertEqual(await runtime.sweep_idle(), 1)
            self.assertFalse(await runtime.is_awake(key))
            self.assertFalse(await runtime.admit(key, False))
            checkpoint = read_checkpoint(key.directory(Path(directory)))
            self.assertEqual(checkpoint.summary, "saved conversation")
            self.assertEqual(checkpoint.summary_saved_ms, now[0])
            self.assertEqual(checkpoint.last_wake_id, "two")

    async def test_mandatory_steer_does_not_require_a_reaction(self):
        with tempfile.TemporaryDirectory() as directory:
            clients = []
            outputs = []

            def factory(key, path):
                client = FakePi("[[bridge:pass]]")
                clients.append(client)
                return client

            async def output(key, kind, text, event_id):
                outputs.append((kind, text, event_id))

            runtime = BridgeRuntime(Path(directory), factory, output)
            key = SessionKey("agent_two", "discord", "123")
            await runtime.submit(key, Incoming("Operator", "one", "general", optional=True))
            await clients[0].started.wait()
            self.assertEqual(await runtime.submit(key, Incoming("Operator", "two", "@Agent_two direct")),
                             "steered")
            clients[0].release.set()
            await runtime.wait_idle(key)
            self.assertFalse(any(kind == "error" for kind, _, _ in outputs))
            self.assertTrue(any(kind == "cleanup" for kind, _, _ in outputs))
            self.assertFalse(any(text == "[[bridge:pass]]" for _, text, _ in outputs))
