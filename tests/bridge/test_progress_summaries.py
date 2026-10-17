"""Periodic work summaries are isolated from Pi and survive later progress edits."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bridge.adapters.discord import DiscordAdapter
from bridge.adapters.matrix import MatrixAdapter
from bridge.config import DiscordSettings, MatrixSettings, Profile
from bridge.filtering import metadata_rejection
from bridge.runtime.pi_rpc import PiRunResult
from bridge.runtime.session import (BridgeRuntime, Incoming, PROGRESS_EDIT_INTERVAL,
                                    PROGRESS_SUMMARY_INTERVAL)
from bridge.runtime.state import SessionKey


def matrix_runtime():
    return SimpleNamespace(record_activity=AsyncMock())


def matrix_key():
    return SessionKey("agent_one", "matrix", "!room:example.test:main")


def matrix_adapter(directory, runtime):
    settings = MatrixSettings("https://example.test", "@agent_one:example.test", "TEST_MATRIX_TOKEN", ())
    with patch.dict("os.environ", {"TEST_MATRIX_TOKEN": "fixture"}):
        return MatrixAdapter("agent_one", settings, runtime, Path(directory))


def discord_adapter(runtime):
    adapter = DiscordAdapter("agent_two", DiscordSettings("TOKEN", 1, (2,)), runtime)
    adapter.client = SimpleNamespace(user=SimpleNamespace(id=9))
    return adapter


def FakeDiscordChannel():
    return SimpleNamespace(id=2, parent_id=None, guild=SimpleNamespace(id=1),
                           send=AsyncMock(), typing=AsyncMock())


class WorkingRpc:
    active = True

    def __init__(self):
        self.release = asyncio.Event()
        self.started = asyncio.Event()

    async def prompt(self, text, on_event, images=None, *, on_accepted=None):
        self.on_event = on_event
        await on_accepted()
        await on_event({"type": "message_end", "message": {
            "role": "assistant", "content": [
                {"type": "thinking", "thinking": "PRIVATE_REASONING"},
                {"type": "text", "text": "Checking progress delivery tests."}]}})
        await on_event({"type": "tool_execution_start", "toolName": "bash",
                        "args": {"secret": "PRIVATE_ARGUMENT"}})
        await on_event({"type": "tool_execution_end", "toolName": "bash"})
        self.started.set()
        await self.release.wait()
        return PiRunResult("Done")

    async def steer(self, text, images=None):
        pass

    async def stop(self):
        self.release.set()


class ProgressSummaryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        for name, value in (("PROGRESS_SUMMARY_INTERVAL", 0.02),
                            ("PROGRESS_SUMMARY_TIMEOUT", 0.1),
                            ("PROGRESS_EDIT_INTERVAL", 0)):
            setting = patch("bridge.runtime.session." + name, value)
            setting.start()
            self.addCleanup(setting.stop)
        self.rpc = WorkingRpc()
        self.key = SessionKey("agent_one", "matrix", "!room:example.test:main")
        self.outputs = []
        self.updated = asyncio.Event()

    async def send(self, key, kind, text, request_id):
        self.outputs.append((kind, text, request_id))
        self.updated.set()

    def runtime(self, summarizer):
        runtime = BridgeRuntime(Path(self.directory.name), lambda *_: self.rpc,
                                self.send, progress_summarizer=summarizer,
                                now_ms=lambda: 0)
        self.addAsyncCleanup(runtime.close)
        return runtime

    async def wait_output(self, predicate):
        async def wait():
            while not any(predicate(item) for item in self.outputs):
                self.updated.clear()
                await self.updated.wait()
        await asyncio.wait_for(wait(), 1)

    async def test_summaries_accumulate_and_follow_latest_message(self):
        summarize = AsyncMock(side_effect=["Checking delivery tests.", "Testing the notice handoff."])
        runtime = self.runtime(summarize)
        await runtime.submit(self.key, Incoming("Operator", "1", "Improve progress delivery"))
        await self.wait_output(lambda item: item[0] == "progress_summary")
        context = summarize.call_args.args[1]
        self.assertIn("Improve progress delivery", json.dumps(context))
        self.assertIn("Checking progress delivery tests.", json.dumps(context))
        self.assertIn("bash", json.dumps(context))
        self.assertNotIn("PRIVATE_", json.dumps(context))
        await runtime.submit(self.key, Incoming("Operator", "2", "Include the newest notice"))
        await self.wait_output(lambda item: "Testing the notice handoff." in item[1])
        self.assertIn("Include the newest notice", json.dumps(summarize.call_args.args[1]))
        latest = self.outputs[-1]
        self.assertEqual(latest[2], "2")
        self.assertNotIn("Checking delivery tests.", latest[1])
        self.assertIn("[00:00 UTC] Testing the notice handoff.", latest[1])
        await self.rpc.on_event({"type": "tool_execution_start", "toolName": "Read"})
        await self.wait_output(lambda item: item[1].startswith("Using `Read`"))
        self.assertNotIn("Checking delivery tests.", self.outputs[-1][1])
        self.assertIn("Testing the notice handoff.", self.outputs[-1][1])
        self.rpc.release.set()
        await runtime.wait_idle(self.key)
        self.assertEqual(self.outputs[-1], ("final", "Done", "1"))

    async def test_compaction_updates_active_card_without_losing_prior_status(self):
        runtime = self.runtime(AsyncMock(return_value="Checked delivery."))
        await runtime.submit(self.key, Incoming("Operator", "1", "Go"))
        await self.wait_output(lambda item: item[0] == "progress_summary")
        await self.rpc.on_event({"type": "tool_execution_start", "toolName": "Read"})
        await self.wait_output(lambda item: item[1].startswith("Using `Read`"))
        await self.rpc.on_event({"type": "compaction_start"})
        await self.wait_output(lambda item: item[1].startswith("Compacting, one moment..."))
        kind, text, request_id = self.outputs[-1]
        self.assertEqual((kind, request_id), ("progress_summary", "1"))
        self.assertIn("[00:00 UTC] Using `Read`…", text)
        self.assertIn("[00:00 UTC] Checked delivery.", text)
        self.assertEqual(sum(item[0] == "ack" for item in self.outputs), 1)
        await self.rpc.on_event({"type": "compaction_end"})
        self.rpc.release.set()
        await runtime.wait_idle(self.key)

    async def test_compaction_keeps_prior_status_without_generated_summary(self):
        runtime = self.runtime(None)
        await runtime.submit(self.key, Incoming("Operator", "1", "Go"))
        await asyncio.wait_for(self.rpc.started.wait(), 1)
        await self.rpc.on_event({"type": "compaction_start"})
        await self.wait_output(lambda item: item[1].startswith("Compacting, one moment..."))
        self.assertEqual(self.outputs[-1],
                         ("progress_summary", "Compacting, one moment...\n[00:00 UTC] Using `bash`…", "1"))
        self.assertEqual(sum(item[0] == "ack" for item in self.outputs), 1)
        await self.rpc.on_event({"type": "compaction_end"})
        self.rpc.release.set()
        await runtime.wait_idle(self.key)

    async def test_status_growth_does_not_rotate_or_copy_summary_history(self):
        runtime = self.runtime(AsyncMock(return_value="Checked delivery."))
        with patch("bridge.runtime.session.PROGRESS_SUMMARY_CARD_LIMIT", 80):
            await runtime.submit(self.key, Incoming("Operator", "1", "Go"))
            await self.wait_output(lambda item: item[0] == "progress_summary")
            await self.rpc.on_event({"type": "tool_execution_start",
                                     "toolName": "VeryLongToolName_" * 3})
            await self.wait_output(lambda item: item[0] == "progress_summary" and
                                   item[1].startswith("Using `VeryLongToolName_"))
            self.assertEqual(sum(kind == "ack" for kind, _, _ in self.outputs), 1)
            self.assertIn("Checked delivery.", self.outputs[-1][1])
            self.rpc.release.set()
            await runtime.wait_idle(self.key)

    async def test_summary_finishing_in_terminal_grace_is_flushed_once(self):
        started = asyncio.Event()
        release = asyncio.Event()

        async def summarize(key, context):
            started.set()
            await release.wait()
            return "Checked the last delivery case."

        runtime = self.runtime(summarize)
        await runtime.submit(self.key, Incoming("Operator", "1", "Go"))
        await asyncio.wait_for(started.wait(), 1)
        self.rpc.release.set()
        await asyncio.sleep(0)
        release.set()
        await asyncio.wait_for(runtime.wait_idle(self.key), 1)
        summaries = [item for item in self.outputs if item[0] == "progress_summary"]
        self.assertEqual(len(summaries), 1)
        self.assertIn("Checked the last delivery case.", summaries[0][1])
        self.assertEqual(self.outputs[-1], ("final", "Done", "1"))

    async def test_context_is_bounded_unchanged_inputs_skip_and_calls_are_capped(self):
        summarize = AsyncMock(return_value="Checking delivery.")
        with patch("bridge.runtime.session.PROGRESS_SUMMARY_MAX_CALLS", 2):
            runtime = self.runtime(summarize)
            await runtime.submit(self.key, Incoming("Operator", "1", "Review progress delivery"))
            await self.wait_output(lambda item: item[0] == "progress_summary")
            await asyncio.sleep(0.06)
            self.assertEqual(summarize.await_count, 1)
            for _ in range(5):
                await self.rpc.on_event({"type": "message_end", "message": {
                    "role": "assistant", "content": [{"type": "text", "text": "x" * 3000}]}})
            async def wait_second():
                while summarize.await_count < 2:
                    await asyncio.sleep(0.005)
            await asyncio.wait_for(wait_second(), 1)
            context = summarize.call_args.args[1]
            self.assertIn("Review progress delivery", context[0])
            self.assertLessEqual(len(context), 5)  # four message entries plus tool names
            self.assertTrue(all(len(entry) <= 1030 for entry in context))
            await self.rpc.on_event({"type": "message_end", "message": {
                "role": "assistant", "content": [{"type": "text", "text": "Next case"}]}})
            await asyncio.sleep(0.06)
            self.assertEqual(summarize.await_count, 2)
            self.rpc.release.set()
            await runtime.wait_idle(self.key)

    async def test_failed_summary_does_not_abort_turn(self):
        called = asyncio.Event()

        async def fail(key, context):
            called.set()
            raise RuntimeError("provider unavailable")

        runtime = self.runtime(fail)
        await runtime.submit(self.key, Incoming("Operator", "1", "Go"))
        await asyncio.wait_for(called.wait(), 1)
        self.rpc.release.set()
        await runtime.wait_idle(self.key)
        self.assertEqual(self.outputs[-1], ("final", "Done", "1"))
        self.assertFalse(any(item[0] == "progress_summary" for item in self.outputs))

    async def test_slow_summary_is_cancelled_before_final(self):
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def slow(key, context):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        runtime = self.runtime(slow)
        await runtime.submit(self.key, Incoming("Operator", "1", "Go"))
        await asyncio.wait_for(started.wait(), 1)
        self.rpc.release.set()
        await asyncio.wait_for(runtime.wait_idle(self.key), 1)
        self.assertTrue(cancelled.is_set())
        self.assertEqual(self.outputs[-1], ("final", "Done", "1"))

    async def test_summaries_pause_until_all_running_tools_finish(self):
        summarize = AsyncMock(return_value="Checking the wait results.")
        runtime = self.runtime(summarize)
        await runtime.submit(self.key, Incoming("Operator", "1", "Wait for results"))
        await self.rpc.started.wait()
        await self.rpc.on_event({"type": "tool_execution_start", "toolName": "sleep_until"})
        await self.rpc.on_event({"type": "tool_execution_start", "toolName": "wait"})
        await asyncio.sleep(0.06)
        summarize.assert_not_awaited()
        await self.rpc.on_event({"type": "tool_execution_end", "toolName": "sleep_until"})
        await asyncio.sleep(0.04)
        summarize.assert_not_awaited()
        await self.rpc.on_event({"type": "tool_execution_end", "toolName": "wait"})
        await self.wait_output(lambda item: item[0] == "progress_summary")
        self.rpc.release.set()
        await runtime.wait_idle(self.key)

    async def test_inflight_summary_is_not_posted_after_a_tool_starts(self):
        started = asyncio.Event()
        release = asyncio.Event()

        async def summarize(key, context):
            started.set()
            await release.wait()
            return "Outdated activity."

        runtime = self.runtime(summarize)
        await runtime.submit(self.key, Incoming("Operator", "1", "Go"))
        await asyncio.wait_for(started.wait(), 1)
        await self.rpc.on_event({"type": "tool_execution_start", "toolName": "sleep_until"})
        release.set()
        await asyncio.sleep(0.04)
        self.assertFalse(any(item[0] == "progress_summary" for item in self.outputs))
        self.rpc.release.set()
        await runtime.wait_idle(self.key)

    async def test_optional_turn_does_not_request_summaries(self):
        summarize = AsyncMock(return_value="Testing.")
        runtime = self.runtime(summarize)
        await runtime.submit(self.key, Incoming("Operator", "1", "Ambient", optional=True))
        await self.rpc.started.wait()
        await asyncio.sleep(0.06)
        summarize.assert_not_awaited()
        self.rpc.release.set()
        await runtime.wait_idle(self.key)

    async def test_summary_for_old_target_is_discarded_after_steer(self):
        started = asyncio.Event()
        release = asyncio.Event()
        next_call = asyncio.Event()

        async def summarize(key, context):
            if started.is_set():
                next_call.set()
                await asyncio.Event().wait()
            started.set()
            await release.wait()
            return "Outdated task summary."

        runtime = self.runtime(summarize)
        await runtime.submit(self.key, Incoming("Operator", "1", "First"))
        await asyncio.wait_for(started.wait(), 1)
        await runtime.submit(self.key, Incoming("Operator", "2", "Second"))
        release.set()
        await asyncio.wait_for(next_call.wait(), 1)
        self.rpc.release.set()
        await runtime.wait_idle(self.key)
        self.assertFalse(any(item[0] == "progress_summary" for item in self.outputs))

    async def test_summary_timeout_leaves_turn_running(self):
        timed_out = asyncio.Event()

        async def slow(key, context):
            try:
                await asyncio.Event().wait()
            finally:
                timed_out.set()

        with patch("bridge.runtime.session.PROGRESS_SUMMARY_TIMEOUT", 0.01):
            runtime = self.runtime(slow)
            await runtime.submit(self.key, Incoming("Operator", "1", "Go"))
            await asyncio.wait_for(timed_out.wait(), 1)
            self.assertFalse(any(item[0] == "final" for item in self.outputs))
            self.rpc.release.set()
            await runtime.wait_idle(self.key)
            self.assertEqual(self.outputs[-1], ("final", "Done", "1"))

    async def test_rejects_empty_or_mention_summaries(self):
        called = asyncio.Event()

        async def invalid(key, context):
            called.set()
            return "@everyone checking tests"

        runtime = self.runtime(invalid)
        await runtime.submit(self.key, Incoming("Operator", "1", "Go"))
        await asyncio.wait_for(called.wait(), 1)
        self.rpc.release.set()
        await runtime.wait_idle(self.key)
        self.assertFalse(any(item[0] == "progress_summary" for item in self.outputs))

    async def test_summary_cards_rotate_without_truncating_previous_lines(self):
        with patch("bridge.runtime.session.PROGRESS_SUMMARY_CARD_LIMIT", 65):
            runtime = self.runtime(AsyncMock(side_effect=["Checking delivery tests.",
                                                        "Checking notice handoff."]))
            await runtime.submit(self.key, Incoming("Operator", "1", "Go"))
            await self.wait_output(lambda item: "Checking delivery tests." in item[1])
            await self.rpc.on_event({"type": "message_end", "message": {
                "role": "assistant", "content": [{"type": "text", "text": "Testing handoff next."}]}})
            await self.wait_output(lambda item: "Checking notice handoff." in item[1])
            cards = [item[1] for item in self.outputs if item[0] == "progress_summary"]
            self.assertIn("Checking delivery tests.", cards[0])
            self.assertNotIn("Checking delivery tests.", cards[-1])
            self.assertIn("Checking notice handoff.", cards[-1])
            self.assertEqual(sum(item[0] == "ack" for item in self.outputs), 2)
            self.rpc.release.set()
            await runtime.wait_idle(self.key)

    def test_default_interval_is_two_minutes_and_peer_filter_is_silent(self):
        self.assertEqual(PROGRESS_SUMMARY_INTERVAL, 120)
        self.assertIsNotNone(metadata_rejection({"kind": "progress_summary"},
                                                awake=True, addressed=False, wake_any=True))

    def test_progress_cadence_stays_inside_the_stock_message_budget(self):
        """One live turn must not outrun Synapse's stock `rc_message` allowance.

        Stock `rc_message` sustains 0.2 writes/s with a burst of 10, and the
        acknowledgement and the final reply share that same per-identity
        bucket. A shorter cadence spends the burst, then starves every later
        write in the turn — including the acknowledgement that starts it.
        """
        self.assertGreaterEqual(PROGRESS_EDIT_INTERVAL, 5.0)
        self.assertLessEqual(1.0 / PROGRESS_EDIT_INTERVAL, 0.2)


class SummaryCardPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_matrix_summary_card_survives_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter = matrix_adapter(directory, matrix_runtime())
            adapter._request = AsyncMock(return_value={"event_id": "$card"})
            key = matrix_key()
            await adapter.output(key, "ack", "Working", "$request")
            await adapter.output(key, "progress_summary", "Working\n[12:00 UTC] Testing.", "$request")
            content = adapter._request.await_args.kwargs["data"]
            self.assertEqual(content["m.relates_to"]["event_id"], "$card")
            await adapter.output(key, "cleanup", "", "$request")
            self.assertEqual(adapter._request.await_count, 3)
            frozen = adapter._request.await_args.kwargs["data"]["m.new_content"]["body"]
            self.assertIn("Work log — finished", frozen)
            self.assertIn("[12:00 UTC] Testing.", frozen)
            self.assertNotIn("Working", frozen)
            self.assertFalse(adapter._summary_cards)
            self.assertFalse(adapter._acks)

    async def test_failed_seal_never_prevents_the_final_reply(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter = matrix_adapter(directory, matrix_runtime())
            adapter._request = AsyncMock(side_effect=[{"event_id": "$card"}, {},
                                                      {"event_id": "$final"},
                                                      RuntimeError("seal unavailable")])
            key = matrix_key()
            await adapter.output(key, "ack", "Working", "$request")
            await adapter.output(key, "progress_summary", "Working\n[12:00 UTC] Testing.", "$request")
            self.assertIs(await adapter.output(key, "final", "Done", "$request"), True)
            self.assertEqual(adapter._request.await_args_list[-2].kwargs["data"]["body"], "Done")
            self.assertTrue(adapter._request.await_args.kwargs["data"]["m.new_content"][
                "org.example.pi_bridge"]["sealed"])
        adapter = discord_adapter(matrix_runtime())
        key = SessionKey("agent_two", "discord", "2")
        card = SimpleNamespace(id=1, edit=AsyncMock(side_effect=[None, RuntimeError("seal unavailable")]),
                               delete=AsyncMock())
        reply = AsyncMock(return_value=card)
        adapter._requests[(key, "1")] = SimpleNamespace(channel=FakeDiscordChannel(), reply=reply)
        await adapter.output(key, "ack", "Working", "1")
        await adapter.output(key, "progress_summary", "Working\n[12:00 UTC] Testing.", "1")
        await adapter.output(key, "final", "Done", "1")
        self.assertEqual(reply.await_args.args, ("Done",))
        card.delete.assert_not_awaited()

    async def test_matrix_rotation_seals_each_physical_card_once(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter = matrix_adapter(directory, matrix_runtime())
            counter = 0

            async def transport(*args, **kwargs):
                nonlocal counter
                counter += 1
                return {"event_id": f"$event{counter}"}

            adapter._request = AsyncMock(side_effect=transport)
            key = matrix_key()
            await adapter.output(key, "steered", "Added to the current thread.", "$request")
            first = adapter._acks[(key, "$request")]
            await adapter.output(key, "progress_summary", "Using `Read`…\n[12:00 UTC] First.", "$request")
            live = adapter._request.await_args.kwargs["data"]["m.new_content"]["body"]
            self.assertTrue(live.startswith("Added to the current thread.\nUsing"))
            count = adapter._request.await_count
            await adapter.output(key, "ack", "Still working…", "$request")
            second = adapter._acks[(key, "$request")]
            self.assertEqual(adapter._request.await_count, count + 1)
            self.assertNotIn(first, adapter._summary_cards)
            self.assertNotIn(first, adapter._acks.values())
            await adapter.output(key, "progress_summary", "Using `bash`…\n[12:02 UTC] Second.", "$request")
            await adapter.output(key, "final", "Done", "$request")
            calls = adapter._request.await_args_list
            seals = [call.kwargs["data"] for call in calls
                     if call.kwargs["data"].get("org.example.pi_bridge", {}).get("sealed")]
            self.assertEqual([body["m.relates_to"]["event_id"] for body in seals], [second])
            self.assertIn("Second.", seals[0]["body"])
            self.assertNotIn("First.", seals[0]["body"])
            first_edits = [call.kwargs["data"] for call in calls
                           if call.kwargs["data"].get("m.relates_to", {}).get("event_id") == first]
            self.assertEqual(len(first_edits), 1)  # Original summary, no retirement edit.
            self.assertIn("First.", first_edits[0]["body"])
            self.assertEqual(calls[-2].kwargs["data"]["body"], "Done")
            self.assertEqual(calls[-1].kwargs["data"]["m.relates_to"]["event_id"], second)
            count = adapter._request.await_count
            await adapter.output(key, "cleanup", "", "$request")
            self.assertEqual(adapter._request.await_count, count)

    async def test_discord_rotation_and_completion_never_touch_sealed_cards(self):
        adapter = discord_adapter(matrix_runtime())
        key = SessionKey("agent_two", "discord", "2")
        first = SimpleNamespace(id=1, edit=AsyncMock(), delete=AsyncMock())
        second = SimpleNamespace(id=2, edit=AsyncMock(), delete=AsyncMock())
        reply = AsyncMock(side_effect=[first, second, SimpleNamespace(id=3)])
        adapter._requests[(key, "1")] = SimpleNamespace(channel=FakeDiscordChannel(), reply=reply)
        await adapter.output(key, "steered", "Added to the current thread.", "1")
        await adapter.output(key, "progress_summary", "Using `Read`…\n[12:00 UTC] First.", "1")
        await adapter.output(key, "ack", "Still working…", "1")
        sealed = first.edit.await_args.kwargs["content"]
        self.assertIn("First.", sealed)
        self.assertIn("Using", sealed)  # Original snapshot is not rewritten at rotation.
        self.assertNotIn("Work log — continued", sealed)
        await adapter.output(key, "progress_summary", "Using `bash`…\n[12:02 UTC] Second.", "1")
        await adapter.output(key, "final", "Done", "1")
        await adapter.output(key, "cleanup", "", "1")
        self.assertEqual(first.edit.await_count, 1)
        first.delete.assert_not_awaited()
        self.assertEqual(second.edit.await_count, 2)
        self.assertIn("Second.", second.edit.await_args.kwargs["content"])
        self.assertNotIn("First.", second.edit.await_args.kwargs["content"])
        self.assertNotIn("Using", second.edit.await_args.kwargs["content"])
        first.delete.assert_not_awaited()
        second.delete.assert_not_awaited()
        self.assertFalse(adapter._summary_cards)

    async def test_ambient_cleanup_does_not_remove_active_cards(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter = matrix_adapter(directory, matrix_runtime())
            adapter._request = AsyncMock(return_value={"event_id": "$card"})
            key = matrix_key()
            await adapter.output(key, "ack", "Working", "$direct")
            await adapter.output(key, "cleanup_request", "", "$ambient")
            self.assertEqual(adapter._acks[(key, "$direct")], "$card")
            self.assertEqual(adapter._request.await_count, 1)
        adapter = discord_adapter(matrix_runtime())
        key = SessionKey("agent_two", "discord", "2")
        card = SimpleNamespace(delete=AsyncMock())
        adapter._requests[(key, "1")] = SimpleNamespace()
        adapter._requests[(key, "2")] = SimpleNamespace()
        adapter._progress[(key, "1")] = card
        await adapter.output(key, "cleanup_request", "", "2")
        self.assertIs(adapter._progress[(key, "1")], card)
        self.assertIn((key, "1"), adapter._requests)
        card.delete.assert_not_awaited()

    async def test_discord_summary_card_survives_final_and_cleanup(self):
        for terminal in ("final", "cleanup", "cancelled", "error"):
            with self.subTest(terminal=terminal):
                adapter = discord_adapter(matrix_runtime())
                key = SessionKey("agent_two", "discord", "2")
                card = SimpleNamespace(id=100, edit=AsyncMock(), delete=AsyncMock())
                request = SimpleNamespace(channel=FakeDiscordChannel(), reply=AsyncMock(return_value=card))
                adapter._requests[(key, "1")] = request
                await adapter.output(key, "ack", "Working", "1")
                await adapter.output(key, "progress_summary", "Working\n[12:00 UTC] Testing.", "1")
                await adapter.output(key, terminal, "Done", "1")
                card.delete.assert_not_awaited()
                self.assertEqual(card.edit.await_count, 2)
                frozen = card.edit.await_args.kwargs["content"]
                outcome = "finished" if terminal in {"final", "cleanup"} else terminal
                self.assertIn("Work log — " + outcome, frozen)
                self.assertIn("[12:00 UTC] Testing.", frozen)
                self.assertNotIn("Working", frozen)
                self.assertFalse(adapter._summary_cards)
                self.assertFalse(adapter._progress)
                self.assertFalse(adapter._requests)


class ProgressGeneratorTests(unittest.IsolatedAsyncioTestCase):
    async def test_generator_uses_tool_free_isolated_session_and_stops(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "profile.json"
            path.write_text(json.dumps({"agent": "agent_one", "work_dir": directory,
                                        "state_dir": directory, "pi_agent_dir": directory,
                                        "provider": "test", "model": "test-model", "pi_executable": "/usr/bin/pi",
                                        "matrix": {"homeserver": "https://example.test",
                                                   "user_id": "@agent_one:example.test",
                                                   "token_env": "TEST_MATRIX_TOKEN", "rooms": []}}))
            with patch.dict("os.environ", {"TEST_MATRIX_TOKEN": "fixture",
                                           "COUNCIL_MATRIX_ROOM_ID": "!council:example.test"}, clear=True):
                profile = Profile.load(path)
            client = SimpleNamespace(prompt=AsyncMock(return_value=PiRunResult("Testing delivery.")),
                                     stop=AsyncMock())
            with patch.object(Profile, "pi_client", return_value=client) as factory:
                result = await profile.summarize_progress(matrix_key(), ["Room message: Test delivery"])
            self.assertEqual(result, "Testing delivery.")
            self.assertTrue(factory.call_args.kwargs["safe_mode"])
            self.assertTrue(factory.call_args.kwargs["progress_mode"])
            self.assertIn("Room message: Test delivery", client.prompt.call_args.args[0])
            client.stop.assert_awaited_once()
            self.assertFalse(list(root.glob("progress-*")))
