"""Compaction concurrency and control-path regressions; no provider or transport calls."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bridge.runtime.pi_rpc import PiRunResult, RpcImage
from bridge.runtime.session import BridgeRuntime, Incoming
from bridge.runtime.state import SessionKey


class ControlledRpc:
    def __init__(self, session_file):
        self.session_file = session_file
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.active = False
        self.aborted = False
        self.stopped = False
        self.steers = []
        self.on_event = None
        self.in_compaction = False

    async def prompt(self, message, on_event, images=None, *, on_accepted=None):
        self.active = True
        self.on_event = on_event
        self.session_file.write_text("synthetic transcript\n")
        if on_accepted:
            await on_accepted()
        self.started.set()
        await self.release.wait()
        self.active = False
        return PiRunResult("Synthetic completion")

    async def compact_event(self, started):
        self.in_compaction = started
        await self.on_event({"type": "compaction_start" if started else "compaction_end",
                             "reason": "threshold"})

    async def steer(self, message, images=None):
        if self.in_compaction:
            raise AssertionError("steer interrupted compaction")
        self.steers.append(message)

    async def abort(self):
        self.aborted = True
        if self.in_compaction:
            await self.compact_event(False)
        self.release.set()

    async def stop(self):
        self.stopped = True
        self.release.set()

    async def compact(self):
        return "Synthetic checkpoint"


class CompactionFaultTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.clients = []
        self.noticed = asyncio.Event()
        self.fail_notice = False

        def factory(key, session_file):
            client = ControlledRpc(session_file)
            self.clients.append(client)
            return client

        async def send(key, kind, text, request_id):
            if kind == "compacting":
                self.noticed.set()
                if self.fail_notice:
                    raise OSError("synthetic notice failure")

        self.runtime = BridgeRuntime(Path(self.directory.name), factory, send)
        self.key = SessionKey("agent_one", "matrix", "synthetic-compaction-scope")
        await self.runtime.submit(self.key, Incoming("Owner", "initial", "Start"))
        await self.clients[0].started.wait()
        self.client = self.clients[0]

    async def asyncTearDown(self):
        await self.runtime.close()
        self.directory.cleanup()

    # Exercise scheduling without hundreds of durable writes. Checkpoint
    # persistence is covered separately by the boundary and runtime tests.
    @patch("bridge.runtime.session.write_checkpoint", new=lambda *_args: None)
    async def test_bursts_repeated_compaction_and_cancelled_inputs_preserve_order(self):
        for cycle in range(10):
            await self.client.compact_event(True)
            self.noticed.clear()
            tasks = [asyncio.create_task(self.runtime.submit(
                self.key, Incoming("Owner", f"{cycle}-{i}", f"item-{cycle}-{i:03d}")))
                for i in range(64)]
            await asyncio.wait_for(self.noticed.wait(), 1)
            cancelled = set(range(8, 64, 8))
            for i in cancelled:
                tasks[i].cancel()
            # Two adjacent event transitions must not let a woken waiter pass
            # through the next compaction, nor spin on a stale set event.
            await self.client.compact_event(False)
            await self.client.compact_event(True)
            await asyncio.sleep(0)
            self.assertFalse(tasks[0].done())
            self.assertEqual(len(self.client.steers), cycle * (64 - len(cancelled)))
            await self.client.compact_event(False)
            results = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 1)
            expected = [f"item-{cycle}-{i:03d}" for i in range(64) if i not in cancelled]
            delivered = self.client.steers[-len(expected):]
            self.assertTrue(all(marker in message for marker, message in zip(expected, delivered)))
            for i, result in enumerate(results):
                if i in cancelled:
                    self.assertIsInstance(result, asyncio.CancelledError)
                else:
                    self.assertEqual(result, "steered")
            self.assertFalse(self.client.aborted)
            self.assertFalse(self.client.stopped)
            self.assertEqual(len(self.clients), 1)

    async def test_idle_compaction_cannot_run_while_image_caption_is_active(self):
        self.client.release.set()
        await self.runtime.wait_idle(self.key)
        caption_started = asyncio.Event()
        finish_caption = asyncio.Event()

        async def caption(key, images):
            caption_started.set()
            await finish_caption.wait()
            return "Synthetic caption"

        self.runtime.image_captioner = caption
        pending = asyncio.create_task(self.runtime.submit(self.key, Incoming(
            "Owner", "image", "Image input", (RpcImage("image/png", b"synthetic"),))))
        await asyncio.wait_for(caption_started.wait(), 1)
        sweep = asyncio.create_task(self.runtime.sweep_idle(force=True))
        try:
            await asyncio.sleep(0)
            self.assertFalse(sweep.done())
            self.assertFalse((await self.runtime._scope(self.key)).compacting)
            self.assertFalse(self.client.stopped)
            finish_caption.set()
            self.assertEqual(await asyncio.wait_for(pending, 1), "started")
            await asyncio.wait_for(sweep, 1)
        finally:
            finish_caption.set()
            for task in (pending, sweep):
                if not task.done():
                    task.cancel()
            await asyncio.gather(pending, sweep, return_exceptions=True)

    async def test_failed_submit_notice_does_not_lose_input_or_abort_compaction(self):
        self.fail_notice = True
        await self.client.compact_event(True)
        pending = asyncio.create_task(self.runtime.submit(
            self.key, Incoming("Owner", "follow-up", "Keep this input")))
        await asyncio.wait_for(self.noticed.wait(), 1)
        self.assertFalse(pending.done())
        self.assertFalse(self.client.aborted)
        await self.client.compact_event(False)
        self.assertEqual(await asyncio.wait_for(pending, 1), "steered")
        self.assertIn("Keep this input", self.client.steers[0])

    async def test_explicit_stop_remains_available_with_waiting_submit(self):
        await self.client.compact_event(True)
        pending = asyncio.create_task(self.runtime.submit(
            self.key, Incoming("Owner", "follow-up", "Pending input")))
        await asyncio.wait_for(self.noticed.wait(), 1)
        try:
            self.assertTrue(await asyncio.wait_for(self.runtime.stop_session(self.key, "Owner"), 0.5))
            self.assertTrue(self.client.stopped)
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)

    async def test_stop_after_synthetic_compaction_event_in_caption_is_not_lock_blocked(self):
        async def caption(key, images):
            await self.client.compact_event(True)
            return "Synthetic caption"

        self.runtime.image_captioner = caption
        pending = asyncio.create_task(self.runtime.submit(self.key, Incoming(
            "Owner", "image", "Image input", (RpcImage("image/png", b"synthetic"),))))
        await asyncio.wait_for(self.noticed.wait(), 1)
        try:
            self.assertTrue(await asyncio.wait_for(self.runtime.stop_session(self.key, "Owner"), 0.5))
            self.assertTrue(self.client.stopped)
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
