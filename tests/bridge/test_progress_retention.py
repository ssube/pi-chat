"""Moving the live target must not edit/delete its previous visible card."""
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bridge.runtime.state import SessionKey
from test_progress_summaries import (FakeDiscordChannel, discord_adapter, matrix_adapter,
                                     matrix_key, matrix_runtime)


class ProgressRetentionTests(unittest.IsolatedAsyncioTestCase):
    async def test_matrix_compaction_keeps_prior_status_on_active_card_and_seals_it(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter = matrix_adapter(directory, matrix_runtime())
            adapter._request = AsyncMock(return_value={"event_id": "$card"})
            key = matrix_key()
            await adapter.output(key, "ack", "Using `bash`…", "$request")
            await adapter.output(key, "progress_summary",
                                 "Compacting, one moment...\n[12:00 UTC] Using `bash`…", "$request")
            edit = adapter._request.await_args.kwargs["data"]
            self.assertEqual(edit["m.relates_to"]["event_id"], "$card")
            self.assertEqual(edit["m.new_content"]["body"],
                             "Compacting, one moment...\n[12:00 UTC] Using `bash`…")
            await adapter.output(key, "final", "Done", "$request")
            sealed = adapter._request.await_args.kwargs["data"]["m.new_content"]["body"]
            self.assertIn("[12:00 UTC] Using `bash`…", sealed)
            self.assertNotIn("Compacting, one moment...", sealed)

    async def test_discord_compaction_keeps_prior_status_on_active_card_and_seals_it(self):
        adapter = discord_adapter(matrix_runtime())
        key = SessionKey("agent_two", "discord", "2")
        card = SimpleNamespace(id=1, edit=AsyncMock(), delete=AsyncMock())
        request = SimpleNamespace(channel=FakeDiscordChannel(), reply=AsyncMock(return_value=card))
        adapter._requests[(key, "1")] = request
        await adapter.output(key, "ack", "Using `bash`…", "1")
        await adapter.output(key, "progress_summary",
                             "Compacting, one moment...\n[12:00 UTC] Using `bash`…", "1")
        self.assertEqual(card.edit.await_args.kwargs["content"],
                         "Compacting, one moment...\n[12:00 UTC] Using `bash`…")
        self.assertEqual(request.reply.await_count, 1)
        await adapter.output(key, "final", "Done", "1")
        self.assertIn("[12:00 UTC] Using `bash`…", card.edit.await_args.kwargs["content"])
        self.assertNotIn("Compacting, one moment...", card.edit.await_args.kwargs["content"])
        card.delete.assert_not_awaited()

    async def test_matrix_handoff_preserves_old_card_with_or_without_history(self):
        for history in (False, True):
            with self.subTest(history=history), tempfile.TemporaryDirectory() as directory:
                adapter = matrix_adapter(directory, matrix_runtime())
                adapter._request = AsyncMock(return_value={"event_id": "$card"})
                key = matrix_key()
                await adapter.output(key, "ack", "Working", "$old")
                if history:
                    await adapter.output(key, "progress_summary", "Working\n[12:00 UTC] Kept.", "$old")
                adapter._request.reset_mock()
                await adapter.output(key, "steered", "Added to the current thread.", "$new")
                adapter._request.assert_awaited_once()
                posted = adapter._request.await_args.kwargs["data"]
                self.assertEqual(posted["body"], "Added to the current thread.")
                self.assertNotIn("m.new_content", posted)
                self.assertNotIn((key, "$old"), adapter._acks)
                await adapter.output(key, "progress", "Late old update", "$old")
                self.assertEqual(adapter._request.await_count, 1)

    async def test_discord_handoff_preserves_old_card_with_or_without_history(self):
        for history in (False, True):
            with self.subTest(history=history):
                adapter = discord_adapter(matrix_runtime())
                key = SessionKey("agent_two", "discord", "2")
                old = SimpleNamespace(id=1, edit=AsyncMock(), delete=AsyncMock())
                new = SimpleNamespace(id=2, edit=AsyncMock(), delete=AsyncMock())
                channel = FakeDiscordChannel()
                first = SimpleNamespace(channel=channel, reply=AsyncMock(return_value=old))
                second = SimpleNamespace(channel=channel, reply=AsyncMock(return_value=new))
                adapter._requests[(key, "1")] = first
                adapter._requests[(key, "2")] = second
                await adapter.output(key, "ack", "Working", "1")
                if history:
                    await adapter.output(key, "progress_summary", "Working\n[12:00 UTC] Kept.", "1")
                old.edit.reset_mock()
                await adapter.output(key, "steered", "Added to the current thread.", "2")
                old.edit.assert_not_awaited()
                old.delete.assert_not_awaited()
                self.assertNotIn((key, "1"), adapter._progress)
                await adapter.output(key, "progress", "Current update", "2")
                new.edit.assert_awaited_once()
                old.edit.assert_not_awaited()
                old.delete.assert_not_awaited()
