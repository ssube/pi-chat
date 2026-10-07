import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

from bridge.runtime.pi_rpc import PiProviderError, PiRpcClient, PiRpcProtocolError, PiRpcTimeout


FAKE_PI = r'''
import json, sys

def emit(value):
    print(json.dumps(value), flush=True)

for line in sys.stdin:
    request = json.loads(line)
    kind = request["type"]
    if kind == "prompt":
        emit({"type": "response", "id": request["id"], "success": True})
        emit({"type": "message_update", "assistantMessageEvent":
              {"type": "text_delta", "delta": "Ready from RPC"}})
        emit({"type": "message_end", "message": {"role": "assistant",
              "content": [{"type": "text", "text": "Ready from RPC"}]}})
        emit({"type": "agent_end"})
    elif kind == "compact":
        emit({"type": "response", "id": request["id"], "success": True,
              "data": {"summary": "Private summary"}})
    elif kind == "get_state":
        emit({"type": "response", "id": request["id"], "success": True,
              "data": {"model": "fake"}})
    else:
        emit({"type": "response", "id": request["id"], "success": True})
'''

FAKE_PI_SMALL = r'''
import json, sys

prompts = 0
for line in sys.stdin:
    request = json.loads(line)
    kind = request["type"]
    if kind == "compact":
        print(json.dumps({"type": "response", "id": request["id"],
              "success": False, "error": "Nothing to compact (session too small)"}), flush=True)
    elif kind == "prompt":
        prompts += 1
        if prompts > 1:
            raise SystemExit("unexpected summary prompt")
        answer = "Ready from RPC"
        print(json.dumps({"type": "response", "id": request["id"], "success": True}), flush=True)
        print(json.dumps({"type": "message_end", "message": {"role": "assistant",
              "content": [{"type": "text", "text": answer}]}}), flush=True)
        print(json.dumps({"type": "agent_end"}), flush=True)
'''

FAKE_PI_PROVIDER_ERROR = r'''
import json, sys
for line in sys.stdin:
    request = json.loads(line)
    print(json.dumps({"type": "response", "id": request["id"],
                      "success": True}), flush=True)
    print(json.dumps({"type": "message_end", "message": {
        "role": "assistant", "stopReason": "error", "content": [],
        "errorMessage": "500: private upstream diagnostic must stay private"}}), flush=True)
    print(json.dumps({"type": "agent_end"}), flush=True)
'''

FAKE_PI_TOOL_TURN = r'''
import json, sys
for line in sys.stdin:
    request = json.loads(line)
    print(json.dumps({"type": "response", "id": request["id"],
                      "success": True}), flush=True)
    for text in ("I will check.", "The result is ready."):
        print(json.dumps({"type": "message_update", "assistantMessageEvent":
                          {"type": "text_delta", "delta": text}}), flush=True)
        print(json.dumps({"type": "message_end", "message": {"role": "assistant",
                          "content": [{"type": "text", "text": text}]}}), flush=True)
    print(json.dumps({"type": "agent_end"}), flush=True)
'''

FAKE_PI_BAD_RESPONSE = r'''
import json, sys
for line in sys.stdin:
    request = json.loads(line)
    print(json.dumps({"type": "response", "id": request["id"],
                      "success": "yes"}), flush=True)
'''

FAKE_PI_SLOW_PROGRESS = r'''
import json, sys, time
for line in sys.stdin:
    request = json.loads(line)
    print(json.dumps({"type": "response", "id": request["id"],
                      "success": True}), flush=True)
    for number in range(5):
        time.sleep(0.04)
        print(json.dumps({"type": "message_update", "assistantMessageEvent":
                          {"type": "text_delta", "delta": str(number)}}), flush=True)
    print(json.dumps({"type": "message_end", "message": {"role": "assistant",
                      "content": [{"type": "text", "text": "done"}]}}), flush=True)
    print(json.dumps({"type": "agent_end"}), flush=True)
'''

FAKE_PI_IDLE = r'''
import json, sys, time
for line in sys.stdin:
    request = json.loads(line)
    print(json.dumps({"type": "response", "id": request["id"],
                      "success": True}), flush=True)
    for _ in range(5):
        time.sleep(0.04)
        print(json.dumps({"type": "heartbeat"}), flush=True)
'''


class PiRpcProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_productive_turn_can_exceed_idle_interval(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "fake_pi_progress.py"
            script.write_text(FAKE_PI_SLOW_PROGRESS)
            client = PiRpcClient([sys.executable, str(script)], Path(directory),
                                 {"PATH": "/usr/bin:/bin"}, timeout_seconds=0.09,
                                 settle_on_agent_end=True)

            async def on_event(event):
                pass

            try:
                self.assertEqual((await client.prompt("work", on_event)).text, "done")
            finally:
                await client.stop()

    async def test_idle_turn_has_distinct_timeout_and_stops_process(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "fake_pi_idle.py"
            script.write_text(FAKE_PI_IDLE)
            client = PiRpcClient([sys.executable, str(script)], Path(directory),
                                 {"PATH": "/usr/bin:/bin"}, timeout_seconds=0.09,
                                 settle_on_agent_end=True)

            async def on_event(event):
                pass

            with self.assertRaises(PiRpcTimeout) as caught:
                await client.prompt("work", on_event)
            self.assertEqual(caught.exception.idle_seconds, 0.09)
            self.assertFalse(client.running)

    async def test_malformed_command_response_fails_without_hanging(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "fake_pi_bad_response.py"
            script.write_text(FAKE_PI_BAD_RESPONSE)
            client = PiRpcClient([sys.executable, str(script)], Path(directory),
                                 {"PATH": "/usr/bin:/bin"}, timeout_seconds=5)

            async def on_event(event):
                pass

            with self.assertRaises(PiRpcProtocolError):
                await client.prompt("hello", on_event)
            self.assertFalse(client.running)

    async def test_final_message_excludes_earlier_tool_turn_text(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "fake_pi_tool_turn.py"
            script.write_text(FAKE_PI_TOOL_TURN)
            client = PiRpcClient([sys.executable, str(script)], Path(directory),
                                 {"PATH": "/usr/bin:/bin"}, timeout_seconds=5,
                                 settle_on_agent_end=True)

            async def on_event(event):
                pass

            try:
                result = await client.prompt("check", on_event)
                self.assertEqual(result.text, "The result is ready.")
            finally:
                await client.stop()

    async def test_provider_error_is_not_an_empty_successful_reply(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "fake_pi_error.py"
            script.write_text(FAKE_PI_PROVIDER_ERROR)
            client = PiRpcClient([sys.executable, str(script)], Path(directory),
                                 {"PATH": "/usr/bin:/bin"}, timeout_seconds=5,
                                 settle_on_agent_end=True)

            async def on_event(event):
                pass

            with self.assertRaises(PiProviderError) as caught:
                await client.prompt("image", on_event)
            self.assertEqual(caught.exception.status, 500)
            self.assertNotIn("private upstream diagnostic", str(caught.exception))
            self.assertFalse(client.running)

    async def test_short_session_compaction_skips_without_extra_prompt(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "fake_pi_small.py"
            script.write_text(FAKE_PI_SMALL)
            client = PiRpcClient([sys.executable, str(script)], Path(directory),
                                 {"PATH": "/usr/bin:/bin"}, timeout_seconds=5,
                                 settle_on_agent_end=True)
            events = []

            async def on_event(event):
                events.append(event["type"])

            try:
                self.assertEqual((await client.prompt("hello", on_event)).text,
                                 "Ready from RPC")
                self.assertIsNone(await client.compact())
                self.assertEqual(events, ["message_end", "agent_end"])
            finally:
                await client.stop()

    async def test_prompt_completion_and_compaction_over_stdio(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "fake_pi.py"
            script.write_text(FAKE_PI)
            client = PiRpcClient([sys.executable, str(script)], Path(directory),
                                 {"PATH": "/usr/bin:/bin"}, timeout_seconds=5,
                                 settle_on_agent_end=True)
            events = []
            accepted = asyncio.Event()

            async def on_event(event):
                events.append(event["type"])

            async def on_accepted():
                accepted.set()

            try:
                result = await client.prompt("hello", on_event, on_accepted=on_accepted)
                self.assertTrue(accepted.is_set())
                self.assertEqual(result.text, "Ready from RPC")
                self.assertIn("message_end", events)
                self.assertEqual(await client.compact(), "Private summary")
                self.assertEqual((await client.get_state())["model"], "fake")
            finally:
                await client.stop()
            self.assertFalse(client.running)


if __name__ == "__main__":
    unittest.main()
