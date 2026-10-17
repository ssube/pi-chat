import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bridge.runtime.pi_rpc import (CONTROL_COMMAND_TIMEOUT_SECONDS, STDERR_TAIL_CHARS,
                                   STEER_RECEIPT_GRACE_SECONDS, PiProviderError, PiRpcClient,
                                   PiRpcError, PiRpcProtocolError, PiRpcTimeout, stderr_digest,
                                   stderr_fingerprint)


FAKE_PI_HELD_STEER = r'''
import json, sys

def emit(value):
    print(json.dumps(value), flush=True)

steer_id = None
for line in sys.stdin:
    request = json.loads(line)
    kind = request["type"]
    if kind == "prompt" and request.get("streamingBehavior") == "steer":
        steer_id = request["id"]  # Held: Pi may admit it minutes from now.
    elif kind == "prompt":
        emit({"type": "response", "id": request["id"], "success": True})
    elif kind == "get_state":
        if steer_id is not None:
            emit({"type": "agent_end"})
            emit({"type": "response", "id": steer_id, "success": True,
                  "data": {"disposition": "started"}})
            steer_id = None
        else:
            emit({"type": "agent_start"})
            emit({"type": "message_end", "message": {"role": "assistant",
                  "content": [{"type": "text", "text": "Continued from the steer"}]}})
            emit({"type": "agent_settled"})
        emit({"type": "response", "id": request["id"], "success": True, "data": {}})
    else:
        emit({"type": "response", "id": request["id"], "success": True})
'''

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

    async def test_input_at_settlement_atomically_starts_and_collects_continuation(self):
        for response_first, disposition in ((True, "started"), (False, "started"),
                                            (True, "queued"), (False, "queued"),
                                            (True, "handled")):
            with self.subTest(response_first=response_first, disposition=disposition), tempfile.TemporaryDirectory() as directory:
                script = Path(directory) / "fake_pi_boundary.py"
                script.write_text("response_first = " + repr(response_first) + "\ndisposition = "
                                  + repr(disposition) + "\n" + r'''
import json, sys, time
def emit(value):
    print(json.dumps(value), flush=True)
first = json.loads(sys.stdin.readline())
emit({"type": "response", "id": first["id"], "success": True,
      "data": {"disposition": "started"}})
emit({"type": "agent_start"})
emit({"type": "message_end", "message": {"role": "assistant", "stopReason": "stop" if disposition == "handled" else "error",
      "content": [{"type": "text", "text": "Old reply"}] if disposition == "handled" else [], "errorMessage": "400: context_length_exceeded"}})
second = json.loads(sys.stdin.readline())
assert second["type"] == "prompt" and second["streamingBehavior"] == "steer"
emit({"type": "agent_settled"})
reply = {"type": "response", "id": second["id"], "success": True,
         "data": {"disposition": disposition}}
if response_first:
    emit(reply)
    time.sleep(0.02)
if disposition != "handled":
    emit({"type": "agent_start"})
    emit({"type": "message_end", "message": {"role": "assistant", "stopReason": "stop",
          "content": [{"type": "text", "text": "Recovered continuation"}]}})
    emit({"type": "agent_settled"})
if not response_first:
    emit(reply)
for line in sys.stdin:
    pass
''')
                client = PiRpcClient([sys.executable, str(script)], Path(directory),
                                     {"PATH": "/usr/bin:/bin"}, timeout_seconds=1)
                accepted = asyncio.Event()

                async def on_event(event):
                    pass

                async def on_accepted():
                    accepted.set()

                task = asyncio.create_task(client.prompt("first", on_event, on_accepted=on_accepted))
                try:
                    await asyncio.wait_for(accepted.wait(), 1)
                    await client.steer("second")
                    self.assertEqual((await asyncio.wait_for(task, 1)).text,
                                     "Old reply" if disposition == "handled" else "Recovered continuation")
                    self.assertFalse(client.active)
                    self.assertTrue(client.running)
                finally:
                    await client.stop()
                    await asyncio.gather(task, return_exceptions=True)

    async def test_automatic_compaction_events_reach_runtime_and_reset_idle_deadline(self):
        script_text = r'''
import json, sys, time
for line in sys.stdin:
    request = json.loads(line)
    print(json.dumps({"type": "response", "id": request["id"],
                      "success": True}), flush=True)
    print(json.dumps({"type": "compaction_start", "reason": "overflow"}), flush=True)
    for _ in range(5):
        time.sleep(0.04)
        print(json.dumps({"type": "summarization_retry_attempt_start",
                          "source": "compaction", "reason": "overflow"}), flush=True)
    print(json.dumps({"type": "compaction_end", "reason": "overflow",
                      "aborted": False, "willRetry": True,
                      "result": {"summary": "private auto-summary"}}), flush=True)
    print(json.dumps({"type": "message_end", "message": {"role": "assistant",
                      "content": [{"type": "text", "text": "done"}]}}), flush=True)
    print(json.dumps({"type": "agent_end"}), flush=True)
    print(json.dumps({"type": "agent_settled"}), flush=True)
'''
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "fake_pi_auto_compact.py"
            script.write_text(script_text)
            client = PiRpcClient([sys.executable, str(script)], Path(directory),
                                 {"PATH": "/usr/bin:/bin"}, timeout_seconds=0.09)
            events = []

            async def on_event(event):
                events.append(event["type"])

            try:
                self.assertEqual((await client.prompt("work", on_event)).text, "done")
                self.assertIn("compaction_start", events)
                self.assertIn("compaction_end", events)
                self.assertLess(events.index("compaction_end"), events.index("agent_settled"))
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

    async def test_already_compacted_reuses_saved_summary_without_another_prompt(self):
        script_text = FAKE_PI_SMALL.replace(
            "Nothing to compact (session too small)", "Already compacted").replace(
            '    elif kind == "prompt":',
            '''    elif kind == "get_messages":
        print(json.dumps({"type": "response", "id": request["id"], "success": True,
              "data": {"messages": [{"role": "compactionSummary",
                                    "summary": "Latest private auto-summary"}]}}), flush=True)
    elif kind == "prompt":''')
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "fake_pi_compacted.py"
            script.write_text(script_text)
            client = PiRpcClient([sys.executable, str(script)], Path(directory),
                                 {"PATH": "/usr/bin:/bin"}, timeout_seconds=5,
                                 settle_on_agent_end=True)

            async def on_event(event):
                pass

            try:
                await client.prompt("hello", on_event)
                self.assertEqual(await client.compact(), "Latest private auto-summary")
            finally:
                await client.stop()

    async def test_already_compacted_without_a_summary_still_fails_closed(self):
        script_text = FAKE_PI_SMALL.replace(
            "Nothing to compact (session too small)", "Already compacted").replace(
            '    elif kind == "prompt":',
            '''    elif kind == "get_messages":
        print(json.dumps({"type": "response", "id": request["id"], "success": True,
              "data": {"messages": []}}), flush=True)
    elif kind == "prompt":''')
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "fake_pi_empty_summary.py"
            script.write_text(script_text)
            client = PiRpcClient([sys.executable, str(script)], Path(directory),
                                 {"PATH": "/usr/bin:/bin"}, timeout_seconds=5)
            try:
                with self.assertRaisesRegex(PiRpcProtocolError, "no saved summary"):
                    await client.compact()
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

    async def test_compaction_is_not_capped_by_the_control_deadline(self):
        """Compaction may run for minutes; only the client's own interval bounds it."""
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "fake_pi.py"
            script.write_text(FAKE_PI)
            client = PiRpcClient([sys.executable, str(script)], Path(directory), {},
                                 timeout_seconds=600.0)
            self.addAsyncCleanup(client.stop)
            original = asyncio.wait_for
            with patch("bridge.runtime.pi_rpc.asyncio.wait_for", side_effect=original) as wait:
                self.assertEqual(await client.compact(), "Private summary")
            bounds = [call.args[1] if len(call.args) > 1 else call.kwargs.get("timeout")
                      for call in wait.await_args_list]
            self.assertEqual(bounds, [600.0])
            self.assertNotIn(CONTROL_COMMAND_TIMEOUT_SECONDS, bounds)

    async def test_control_commands_keep_the_short_deadline(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "fake_pi.py"
            script.write_text(FAKE_PI)
            client = PiRpcClient([sys.executable, str(script)], Path(directory), {},
                                 timeout_seconds=600.0)
            self.addAsyncCleanup(client.stop)
            original = asyncio.wait_for
            with patch("bridge.runtime.pi_rpc.asyncio.wait_for", side_effect=original) as wait:
                await client.abort()
            bounds = [call.args[1] if len(call.args) > 1 else call.kwargs.get("timeout")
                      for call in wait.await_args_list]
            self.assertEqual(bounds, [CONTROL_COMMAND_TIMEOUT_SECONDS])

    async def test_steer_returns_after_delivery_without_a_receipt(self):
        """Steering is fire and forget: Pi may hold the admission for minutes."""
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "fake_pi_held_steer.py"
            script.write_text(FAKE_PI_HELD_STEER)
            client = PiRpcClient([sys.executable, str(script)], Path(directory), {},
                                 timeout_seconds=30)
            self.addAsyncCleanup(client.stop)
            accepted = asyncio.Event()

            async def on_event(event):
                pass

            async def on_accepted():
                accepted.set()

            prompt = asyncio.create_task(
                client.prompt("first", on_event, on_accepted=on_accepted))
            await asyncio.wait_for(accepted.wait(), 1)
            original = asyncio.wait
            steer = asyncio.create_task(client.steer("second"))
            with patch("bridge.runtime.pi_rpc.asyncio.wait", side_effect=original) as wait:
                await asyncio.wait_for(steer, 10)
            bounds = [call.args[1] if len(call.args) > 1 else call.kwargs.get("timeout")
                      for call in wait.await_args_list]
            self.assertIn(STEER_RECEIPT_GRACE_SECONDS, bounds)
            self.assertNotIn(CONTROL_COMMAND_TIMEOUT_SECONDS, bounds)
            # The frame was delivered and Pi's admission reply is still owed.
            self.assertNotEqual(client._pending_responses, {})
            self.assertTrue(client.active)
            prompt.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await prompt

    async def test_late_steer_admission_keeps_the_continuation_owned(self):
        """A steer admitted after the grace still owns the run's settlement."""
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "fake_pi_held_steer.py"
            script.write_text(FAKE_PI_HELD_STEER)
            client = PiRpcClient([sys.executable, str(script)], Path(directory), {},
                                 timeout_seconds=5, settle_on_agent_end=True)
            self.addAsyncCleanup(client.stop)
            accepted = asyncio.Event()

            async def on_event(event):
                pass

            async def on_accepted():
                accepted.set()

            prompt = asyncio.create_task(
                client.prompt("first", on_event, on_accepted=on_accepted))
            await asyncio.wait_for(accepted.wait(), 1)
            with patch("bridge.runtime.pi_rpc.STEER_RECEIPT_GRACE_SECONDS", 0.05):
                await asyncio.wait_for(client.steer("second"), 5)
            self.assertNotEqual(client._pending_responses, {})
            # Pi ends the turn before admitting the steer: the superseded answer
            # must not settle the run while the continuation is still unowned.
            await client.get_state()
            self.assertFalse(prompt.done())
            # The continuation's own turn decides the run's answer.
            await client.get_state()
            self.assertEqual((await asyncio.wait_for(prompt, 1)).text,
                             "Continued from the steer")
            self.assertEqual(client._pending_responses, {})

    async def test_steer_rejection_inside_the_delivery_window_is_returned(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "fake_pi_rejecting_steer.py"
            script.write_text(FAKE_PI_HELD_STEER.replace(
                '        steer_id = request["id"]  # Held: Pi may admit it minutes from now.',
                '        emit({"type": "response", "id": request["id"], "success": False,\n'
                '              "error": "Cannot steer while compacting"})'))
            client = PiRpcClient([sys.executable, str(script)], Path(directory), {},
                                 timeout_seconds=5)
            self.addAsyncCleanup(client.stop)
            accepted = asyncio.Event()

            async def on_event(event):
                pass

            async def on_accepted():
                accepted.set()

            prompt = asyncio.create_task(
                client.prompt("first", on_event, on_accepted=on_accepted))
            await asyncio.wait_for(accepted.wait(), 1)
            with self.assertRaisesRegex(PiRpcError, "Cannot steer while compacting"):
                await asyncio.wait_for(client.steer("second"), 5)
            self.assertEqual(client._pending_responses, {})
            self.assertTrue(client.active)
            prompt.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await prompt

    async def test_cancelled_steer_still_collects_its_admission(self):
        """A caller that gives up inside the window does not drop the admission."""
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "fake_pi_held_steer.py"
            script.write_text(FAKE_PI_HELD_STEER)
            client = PiRpcClient([sys.executable, str(script)], Path(directory), {},
                                 timeout_seconds=30)
            self.addAsyncCleanup(client.stop)
            accepted = asyncio.Event()

            async def on_event(event):
                pass

            async def on_accepted():
                accepted.set()

            prompt = asyncio.create_task(
                client.prompt("first", on_event, on_accepted=on_accepted))
            await asyncio.wait_for(accepted.wait(), 1)
            active = client._active_run
            assert active is not None
            with patch("bridge.runtime.pi_rpc.STEER_RECEIPT_GRACE_SECONDS", 30.0):
                steer = asyncio.create_task(client.steer("second"))
                await asyncio.sleep(0.2)
                self.assertEqual(len(client._pending_responses), 1)
                steer.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await steer
            # The admission Pi may still send stays owned, so the run's
            # settlement is still gated on it.
            self.assertEqual(len(client._pending_responses), 1)
            self.assertEqual(active.pending_inputs, 1)
            # A dying process fails that admission; the collector retrieves it
            # instead of leaving the future unowned.
            process = client._process
            assert process is not None
            reader = client._reader_task
            with self.assertLogs("agent_bridge.pi_rpc", level="INFO") as logs:
                process.kill()
                with self.assertRaises(PiRpcError):
                    await asyncio.wait_for(reader, 5)
                await asyncio.sleep(0.1)
            self.assertIn("error_class=PiRpcError", str(logs.output))
            self.assertEqual(client._pending_responses, {})
            self.assertEqual(active.pending_inputs, 0)
            with self.assertRaises(PiRpcError):
                await asyncio.wait_for(prompt, 1)

    async def test_cancelled_steer_admission_still_owns_the_continuation(self):
        """A cancelled caller's steer keeps the continuation that follows it."""
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "fake_pi_held_steer.py"
            script.write_text(FAKE_PI_HELD_STEER)
            client = PiRpcClient([sys.executable, str(script)], Path(directory), {},
                                 timeout_seconds=5, settle_on_agent_end=True)
            self.addAsyncCleanup(client.stop)
            accepted = asyncio.Event()

            async def on_event(event):
                pass

            async def on_accepted():
                accepted.set()

            prompt = asyncio.create_task(
                client.prompt("first", on_event, on_accepted=on_accepted))
            await asyncio.wait_for(accepted.wait(), 1)
            with patch("bridge.runtime.pi_rpc.STEER_RECEIPT_GRACE_SECONDS", 30.0):
                steer = asyncio.create_task(client.steer("second"))
                await asyncio.sleep(0.2)
                steer.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await steer
            # Pi ends the turn and then admits the delivered steer. The
            # superseded answer must not settle the run, because the
            # continuation Pi started has no other collector.
            await client.get_state()
            await asyncio.sleep(0.1)
            self.assertFalse(prompt.done())
            await client.get_state()
            self.assertEqual((await asyncio.wait_for(prompt, 1)).text,
                             "Continued from the steer")
            self.assertEqual(client._pending_responses, {})

    def test_stderr_fingerprint_keeps_diagnostics_without_values(self):
        shape = stderr_fingerprint(
            b"Error: Cannot find module '/home/dev/private-package/index.js'\n")
        self.assertTrue(shape.startswith("error cannot find module"))
        self.assertNotIn("private", shape)
        self.assertNotIn("home", shape)
        self.assertNotIn("/", shape)
        self.assertEqual(stderr_fingerprint(b"node:internal/modules/cjs/loader:1\n"),
                         "node internal modules cjs loader <n>")
        self.assertEqual(stderr_fingerprint(b"\x1b[31mEACCES: permission denied\x1b[0m\n"),
                         "eacces permission denied")
        self.assertEqual(stderr_fingerprint(b"exit code 137\n"), "exit code <n>")
        self.assertEqual(stderr_fingerprint(b"Authorization: Bearer sk-abcdefghijklmnopqrst\n"),
                         "authorization bearer <w>")
        self.assertEqual(stderr_fingerprint(b"   \n"), "")

    def test_stderr_fingerprint_marks_binary_and_caps_length(self):
        self.assertEqual(stderr_fingerprint(b"\x89PNG\r\n\x1a\n\x00\x01\x02\x03"),
                         "[binary 12 bytes]")
        shape = stderr_fingerprint(b"error file not found " * 20)
        self.assertEqual(len(shape), STDERR_TAIL_CHARS)
        self.assertEqual(stderr_digest(["error <w>", "error <w>"]),
                         stderr_digest(["error <w>", "error <w>"]))
        self.assertNotEqual(stderr_digest(["error <w>"]), stderr_digest([]))
        self.assertEqual(stderr_digest([]), "none")


if __name__ == "__main__":
    unittest.main()
