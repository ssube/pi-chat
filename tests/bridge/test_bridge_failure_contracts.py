"""Fault injection checks privacy, no replay and checkpoint durability on both transports."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from bridge.runtime.pi_rpc import PiProviderError
from bridge.runtime.session import Incoming
from bridge.telemetry import Telemetry
from reliability_helpers import Harness


class FailureContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_uncertain_final_delivery_never_posts_another_terminal(self):
        for transport in ("matrix", "discord"):
            with self.subTest(transport=transport), tempfile.TemporaryDirectory() as directory:
                h = Harness(Path(directory), transport)
                self.addAsyncCleanup(h.close)
                output = h.runtime.output
                attempts = []

                async def uncertain(key, kind, text, request_id):
                    attempts.append(kind)
                    await output(key, kind, text, request_id)
                    if kind == "final":
                        raise TimeoutError("acknowledgement lost after send")

                h.runtime.output = uncertain
                await h.deliver(h.event(1))
                await h.idle()
                self.assertEqual(attempts, ["ack", "final", "cleanup"])
                self.assertEqual(h.runtime._scopes[h.key].checkpoint.status, "delivery_error")

    async def test_provider_failure_after_image_tool_is_never_replayed(self):
        for transport in ("matrix", "discord"):
            with self.subTest(transport=transport), tempfile.TemporaryDirectory() as directory:
                h = Harness(Path(directory), transport)
                self.addAsyncCleanup(h.close)
                original = h.runtime.client_factory

                def factory(key, path):
                    client = original(key, path)

                    async def prompt(message, on_event, images=None, *, on_accepted=None):
                        if on_accepted:
                            await on_accepted()
                        await on_event({"type": "tool_execution_start", "toolName": "view_image"})
                        await on_event({"type": "tool_execution_end", "toolName": "view_image"})
                        raise PiProviderError(500)

                    client.prompt = AsyncMock(side_effect=prompt)
                    return client

                h.runtime.client_factory = factory
                await h.deliver(h.event(1))
                await h.idle()
                h.clients[0].prompt.assert_awaited_once()
                self.assertTrue(any("A tool may have run" in item["body"] for item in h.sent))
                self.assertEqual(h.runtime.telemetry.counts[transport, "provider", "retry"], 0)

    async def test_checkpoint_write_failure_keeps_sound_summary_and_stops_client(self):
        for transport in ("matrix", "discord"):
            with self.subTest(transport=transport), tempfile.TemporaryDirectory() as directory:
                h = Harness(Path(directory), transport)
                self.addAsyncCleanup(h.close)
                h.runtime.output = AsyncMock()
                await h.runtime.submit(h.key, Incoming("owner", "1", "first"))
                await h.idle()
                scope = h.runtime._scopes[h.key]
                scope.checkpoint.summary = "Last durable summary"
                with patch("bridge.runtime.session.write_checkpoint", side_effect=OSError("disk full")):
                    await h.runtime.sweep_idle(force=True)
                self.assertEqual(scope.checkpoint.summary, "Last durable summary")
                self.assertEqual(scope.checkpoint.status, "checkpoint_error")
                self.assertTrue(h.clients[0].stopped)
                self.assertIsNone(scope.client)

    async def test_shutdown_during_compaction_marks_recovery_and_unblocks_waiters(self):
        with tempfile.TemporaryDirectory() as directory:
            h = Harness(Path(directory), "matrix")
            h.runtime.output = AsyncMock()
            await h.runtime.submit(h.key, Incoming("owner", "1", "first"))
            await h.idle()
            client = h.clients[0]
            client.compact_release.clear()
            sweep = asyncio.create_task(h.runtime.sweep_idle(force=True))
            await asyncio.wait_for(client.compact_started.wait(), 1)
            await asyncio.wait_for(h.close(), 1)
            self.assertTrue(sweep.cancelled())
            self.assertTrue(client.stopped)
            self.assertEqual(h.runtime._scopes[h.key].checkpoint.status, "checkpoint_error")
            self.assertEqual(h.runtime._operations, 0)


class TelemetryTests(unittest.TestCase):
    def test_histograms_counts_gauges_and_no_identifier_labels(self):
        telemetry = Telemetry()
        for transport in ("matrix", "discord"):
            telemetry.record(transport, "compact", "saved", seconds=2.0, scope_hash="private-scope", epoch=12)
            telemetry.record(transport, "compact", "error", seconds=0.2)
            telemetry.inflight[transport, "input"] = 2
        body = telemetry.render("agent_two")
        self.assertIn('operation="compact",outcome="error"} 1', body)
        self.assertIn('operation="compact",le="1.0"} 1.0', body)
        self.assertIn('operation="compact",le="+Inf"} 2.0', body)
        self.assertIn('operation="compact"} 2.2', body)
        self.assertIn('operation="input"} 2', body)
        self.assertNotIn("private-scope", body)
        self.assertNotIn("epoch", body)

    def test_metric_label_values_escape_quotes_backslashes_and_newlines(self):
        telemetry = Telemetry()
        telemetry.record('mat"rix', 'submit\\retry', 'bad\noutcome')
        body = telemetry.render('agent"\\name\n')
        self.assertIn('agent="agent\\"\\\\name\\n"', body)
        self.assertIn('transport="mat\\"rix"', body)
        self.assertIn('operation="submit\\\\retry"', body)
        self.assertIn('outcome="bad\\noutcome"', body)

    def test_broken_log_sink_does_not_break_operations(self):
        telemetry = Telemetry()
        with patch("bridge.telemetry.log.debug", side_effect=OSError("sink unavailable")):
            telemetry.record("matrix", "submit", "started", seconds=0.1)
        self.assertEqual(telemetry.counts["matrix", "submit", "started"], 1)
