"""Bounded Discord READY reconstruction without replaying accepted work."""

import asyncio
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bridge.adapters.discord import DiscordAdapter, _snowflake_at
from bridge.config import DiscordSettings
from bridge.runtime.session import BridgeRuntime
from bridge.runtime.state import SessionKey, read_checkpoint


class Channel:
    def __init__(self, channel_id, messages=(), *, parent_id=None, error=None):
        self.id = channel_id
        self.parent_id = parent_id
        self.guild = SimpleNamespace(id=1)
        self.messages = list(messages)
        self.last_message_id = max((message.id for message in messages), default=None)
        self.error = error

    def history(self, *, after, limit, oldest_first):
        if self.error is not None:
            raise self.error

        async def iterate():
            selected = [message for message in self.messages if message.id > after.id]
            selected.sort(key=lambda message: message.id, reverse=not oldest_first)
            for message in selected[:limit]:
                yield message
        return iterate()


def message(message_id, channel):
    return SimpleNamespace(id=message_id, channel=channel, guild=channel.guild,
                           author=SimpleNamespace(id=3, bot=False), content="",
                           created_at=datetime.now(timezone.utc))


class DiscordRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.runtime = BridgeRuntime(root, lambda key, path: None, AsyncMock())
        self.adapter = DiscordAdapter("agent_two", DiscordSettings("TOKEN", 1, (2, 3)), self.runtime)
        self.adapter.client = SimpleNamespace(user=SimpleNamespace(id=9))
        self.adapter._recovery_floor = _snowflake_at(datetime.now(timezone.utc))
        self.handled = []

        async def deliver(event):
            self.handled.append(event.id)
        self.adapter._deliver_message = deliver

    async def asyncTearDown(self):
        if self.adapter._retry_task is not None:
            self.adapter._retry_task.cancel()
            await asyncio.gather(self.adapter._retry_task, return_exceptions=True)
        await self.runtime.close()

    async def test_cursor_is_atomic_per_scope_and_does_not_extend_idle(self):
        key = SessionKey("agent_two", "discord", "2")
        await self.runtime.record_discord_observed(key, "100")
        await self.runtime.record_discord_observed(key, "99")
        state = read_checkpoint(key.directory(self.runtime.state_root))
        self.assertEqual(state.last_observed_message_id, "100")
        self.assertEqual(state.last_activity_ms, 0)
        self.assertEqual(await self.runtime.discord_recovery_state(key), ("100", "", "idle"))
        with self.assertRaises(ValueError):
            await self.runtime.record_discord_observed(key, "not-a-snowflake")

    async def test_failed_submit_does_not_advance_observation_watermark(self):
        channel = Channel(2)
        event = message(self.adapter._recovery_floor + 1, channel)
        event.content = "<@9> please help"
        event.mentions = [self.adapter.client.user]
        event.author.display_name = "Requester"
        self.adapter._deliver_message = DiscordAdapter._deliver_message.__get__(self.adapter)
        self.adapter._roster = lambda *args: SimpleNamespace(
            plan=lambda text: SimpleNamespace(user_ids=set(), known=[]))
        self.adapter._context = AsyncMock(return_value="")
        self.adapter._images = AsyncMock(return_value=())
        self.adapter.output = AsyncMock()
        self.runtime.submit = AsyncMock(side_effect=RuntimeError("checkpoint unavailable"))
        await self.adapter._on_message(event)
        state = read_checkpoint(SessionKey("agent_two", "discord", "2").directory(
            self.runtime.state_root))
        self.assertEqual(state.last_observed_message_id, "")
        self.assertEqual(state.last_message_id, "")
        self.adapter.output.assert_awaited_once()

    async def test_corrupt_observation_id_is_rejected_on_read(self):
        key = SessionKey("agent_two", "discord", "2")
        directory = key.directory(self.runtime.state_root)
        directory.mkdir(parents=True)
        (directory / "state.json").write_text('{"last_observed_message_id": "bad-id"}')
        with self.assertRaisesRegex(ValueError, "Discord observation"):
            read_checkpoint(directory)

    async def test_gap_notice_is_durable_and_not_reannounced_each_restart(self):
        key = SessionKey("agent_two", "discord", "2")
        self.assertTrue(await self.runtime.record_discord_gap(key, "age", 100, "unknown"))
        self.assertFalse(await self.runtime.record_discord_gap(key, "age", 200, "unknown"))
        state = read_checkpoint(key.directory(self.runtime.state_root))
        self.assertIn("boundary=200", state.discord_recovery_gap)
        await self.runtime.clear_discord_gap(key)
        self.assertTrue(await self.runtime.record_discord_gap(key, "age", 300, "unknown"))
        self.assertTrue(await self.runtime.record_discord_gap(key, "transient", 0, "held"))
        self.assertFalse(await self.runtime.record_discord_gap(key, "error", 0, "held"))
        self.assertFalse(await self.runtime.record_discord_gap(key, "transient", 0, "held"))
        await self.runtime.clear_discord_gap(key)
        self.assertTrue(await self.runtime.record_discord_gap(key, "error", 0, "held"))

    async def test_ready_orders_missed_messages_before_live_gateway_input(self):
        floor = _snowflake_at(datetime.now(timezone.utc))
        channel = Channel(2)
        channel.messages = [message(floor + 2, channel), message(floor + 1, channel)]
        await self.runtime.record_discord_observed(SessionKey("agent_two", "discord", "2"),
                                                   str(floor))
        self.adapter._recovery_scopes = AsyncMock(return_value=([channel], set(), set()))
        self.adapter._recovery_ready.clear()
        live = asyncio.create_task(self.adapter._on_message(message(floor + 3, channel)))
        await self.adapter._reconcile_ready()
        await live
        self.assertEqual(self.handled, [floor + 1, floor + 2, floor + 3])
        self.assertEqual((await self.runtime.discord_recovery_state(
            SessionKey("agent_two", "discord", "2")))[0], str(floor + 3))

    async def test_out_of_order_live_gateway_events_are_not_dropped(self):
        floor = self.adapter._recovery_floor
        channel = Channel(2)
        await self.adapter._on_message(message(floor + 2, channel))
        await self.adapter._on_message(message(floor + 1, channel))
        self.assertEqual(self.handled, [floor + 2, floor + 1])
        self.assertEqual((await self.runtime.discord_recovery_state(
            SessionKey("agent_two", "discord", "2")))[0], str(floor + 2))

    async def test_first_enable_does_not_replay_old_channel_history(self):
        floor = self.adapter._recovery_floor
        channel = Channel(2)
        channel.messages = [message(floor - 1, channel), message(floor + 1, channel)]
        await self.adapter._recover_scope(channel)
        self.assertEqual(self.handled, [floor + 1])
        state = read_checkpoint(SessionKey("agent_two", "discord", "2").directory(
            self.runtime.state_root))
        self.assertIn("baseline_reset reason=fresh_install", state.discord_recovery_gap)
        self.assertIn(f"boundary={floor}", state.discord_recovery_gap)

    async def test_paused_scope_keeps_addressed_recover_without_advancing_cursor(self):
        channel = Channel(2)
        self.adapter._blocked_channels = {2}
        self.adapter._deliver_message = AsyncMock()
        ordinary = message(self.adapter._recovery_floor + 1, channel)
        ordinary.content = "ordinary request"
        await self.adapter._on_message(ordinary)
        self.adapter._deliver_message.assert_not_awaited()
        recovery = message(self.adapter._recovery_floor + 2, channel)
        recovery.content = "<@9> recover"
        await self.adapter._on_message(recovery)
        self.adapter._deliver_message.assert_awaited_once_with(recovery)
        self.assertEqual((await self.runtime.discord_recovery_state(
            SessionKey("agent_two", "discord", "2")))[0], "")

    async def test_failed_fetch_holds_only_affected_scope(self):
        floor = self.adapter._recovery_floor
        broken = Channel(2, error=RuntimeError("read forbidden"))
        healthy = Channel(3)
        healthy.messages = [message(floor + 1, healthy)]
        self.adapter._recovery_scopes = AsyncMock(return_value=([broken, healthy], set(), set()))
        await self.adapter._reconcile_ready()
        self.assertEqual(self.adapter._blocked_channels, {2})
        self.assertEqual(self.handled, [floor + 1])
        await self.adapter._on_message(message(floor + 2, broken))
        await self.adapter._on_message(message(floor + 2, healthy))
        self.assertEqual(self.handled, [floor + 1, floor + 2])

    async def test_capped_scopes_are_not_expired_without_an_inspection_attempt(self):
        floor = self.adapter._recovery_floor
        scopes = [Channel(index, parent_id=2) for index in range(100, 135)]
        for channel in scopes:
            channel.messages = [message(floor + channel.id, channel)]
        self.adapter._recovery_scopes = AsyncMock(return_value=(scopes, set(), set()))
        await self.adapter._reconcile_ready()
        await self.adapter._reconcile_ready(retry=True)
        await self.adapter._reconcile_ready(retry=True)
        self.assertEqual(self.adapter._blocked_channels, set(range(130, 135)))
        self.assertEqual(len(self.handled), 30)
        for channel_id in range(130, 135):
            state = read_checkpoint(SessionKey("agent_two", "discord", str(channel_id)).directory(
                self.runtime.state_root))
            self.assertIn("recovery_paused reason=limit", state.discord_recovery_gap)
            self.assertEqual(state.last_observed_message_id, "")
        await self.adapter._reconcile_ready(retry=True)
        self.assertEqual(self.adapter._blocked_channels, set())
        self.assertEqual(len(self.handled), 35)

    async def test_eleventh_scope_is_recovered_on_retry_not_held_forever(self):
        parents = {2: Channel(2), 3: Channel(3)}
        threads = [Channel(index, parent_id=2) for index in range(20, 29)]
        guild = SimpleNamespace(active_threads=AsyncMock(return_value=threads))
        self.adapter.client = SimpleNamespace(user=SimpleNamespace(id=9),
                                              get_guild=lambda id: guild,
                                              get_channel=lambda id: parents[id])
        await self.adapter._reconcile_ready()
        self.assertEqual(self.adapter._blocked_channels, {2})
        await self.adapter._reconcile_ready(retry=True)
        self.assertEqual(self.adapter._blocked_channels, set())

    async def test_held_scope_retries_without_gateway_reconnect(self):
        floor = self.adapter._recovery_floor
        channel = Channel(2, error=RuntimeError("temporary REST failure"))
        channel.messages = [message(floor + 1, channel)]
        self.adapter._recovery_scopes = AsyncMock(return_value=([channel], set(), set()))
        self.adapter._retry_delay = 0.01
        await self.adapter._reconcile_ready()
        self.assertEqual(self.adapter._blocked_channels, {2})
        channel.error = None
        await asyncio.wait_for(self.adapter._retry_task, timeout=1)
        self.assertEqual(self.adapter._blocked_channels, set())
        self.assertEqual(self.handled, [floor + 1])

    async def test_permanent_rest_failure_expires_hold_with_named_baseline(self):
        floor = self.adapter._recovery_floor
        channel = Channel(2, error=PermissionError("history revoked"))
        self.adapter._recovery_scopes = AsyncMock(return_value=([channel], set(), set()))
        await self.adapter._reconcile_ready()
        for _ in range(2):
            await self.adapter._reconcile_ready(retry=True)
        self.assertEqual(self.adapter._blocked_channels, set())
        state = read_checkpoint(SessionKey("agent_two", "discord", "2").directory(
            self.runtime.state_root))
        self.assertIn("baseline_reset reason=hold_expired", state.discord_recovery_gap)
        self.assertGreater(int(state.last_observed_message_id), floor)
        await self.adapter._on_message(message(_snowflake_at(datetime.now(timezone.utc)) + 1,
                                               channel))
        self.assertEqual(len(self.handled), 1)

    async def test_disappeared_held_thread_does_not_retry_forever(self):
        thread = Channel(20, parent_id=2, error=RuntimeError("history failed"))
        self.adapter._recovery_scopes = AsyncMock(side_effect=[
            ([thread], set(), set()), ([], set(), set()), ([], set(), set())])
        await self.adapter._reconcile_ready()
        self.assertEqual(self.adapter._blocked_channels, {20})
        await self.adapter._reconcile_ready(retry=True)
        await self.adapter._reconcile_ready(retry=True)
        self.assertEqual(self.adapter._blocked_channels, set())
        state = read_checkpoint(SessionKey("agent_two", "discord", "20").directory(
            self.runtime.state_root))
        self.assertIn("baseline_reset reason=hold_expired", state.discord_recovery_gap)

    async def test_permanent_thread_listing_failure_expires_thread_hold(self):
        parent = Channel(2)
        other = Channel(3)
        guild = SimpleNamespace(active_threads=AsyncMock(side_effect=RuntimeError("down")))
        self.adapter.client = SimpleNamespace(user=SimpleNamespace(id=9),
                                              get_guild=lambda id: guild,
                                              get_channel=lambda id: {2: parent, 3: other}[id])
        await self.adapter._reconcile_ready()
        for _ in range(2):
            await self.adapter._reconcile_ready(retry=True)
        self.assertEqual(self.adapter._blocked_thread_parents, set())
        state = read_checkpoint(SessionKey("agent_two", "discord", "2").directory(
            self.runtime.state_root))
        self.assertIn("baseline_reset reason=thread_hold_expired", state.discord_recovery_gap)
        await self.adapter._on_message(message(_snowflake_at(datetime.now(timezone.utc)) + 1,
                                               Channel(20, parent_id=2)))
        self.assertEqual(len(self.handled), 1)

    async def test_count_overflow_names_skip_and_reconstructs_only_latest_300(self):
        floor = self.adapter._recovery_floor
        channel = Channel(2)
        channel.messages = [message(floor + index, channel) for index in range(1, 302)]
        await self.runtime.record_discord_observed(SessionKey("agent_two", "discord", "2"),
                                                   str(floor))
        await self.adapter._recover_scope(channel)
        self.assertEqual(self.handled, list(range(floor + 2, floor + 302)))
        state = read_checkpoint(SessionKey("agent_two", "discord", "2").directory(
            self.runtime.state_root))
        self.assertIn("baseline_reset reason=count", state.discord_recovery_gap)
        self.assertIn("skipped=>=1", state.discord_recovery_gap)
        self.assertEqual(state.last_observed_message_id, str(floor + 301))

    async def test_discovery_includes_approved_active_and_public_archived_threads(self):
        parent = Channel(2)
        other = Channel(3)
        active = Channel(20, parent_id=2)
        archived = Channel(21, parent_id=2)

        def archived_threads(*, limit):
            self.assertEqual(limit, 101)
            async def iterate():
                yield archived
            return iterate()
        parent.archived_threads = archived_threads
        guild = SimpleNamespace(active_threads=AsyncMock(return_value=[active]))
        self.adapter.client = SimpleNamespace(get_guild=lambda id: guild,
                                              get_channel=lambda id: {2: parent, 3: other}[id])
        scopes, blocked, thread_blind = await self.adapter._recovery_scopes()
        self.assertEqual({channel.id for channel in scopes}, {2, 3, 20, 21})
        self.assertEqual((blocked, thread_blind), (set(), set()))

    async def test_unreadable_parent_does_not_mute_healthy_parent(self):
        floor = self.adapter._recovery_floor
        healthy = Channel(3)
        healthy.messages = [message(floor + 1, healthy)]
        guild = SimpleNamespace(active_threads=AsyncMock(return_value=[]))
        def get_channel(channel_id):
            if channel_id == 2:
                raise PermissionError("parent unreadable")
            return healthy
        self.adapter.client = SimpleNamespace(user=SimpleNamespace(id=9),
                                              get_guild=lambda id: guild,
                                              get_channel=get_channel)
        await self.adapter._reconcile_ready()
        self.assertEqual(self.adapter._blocked_channels, {2})
        self.assertEqual(self.handled, [floor + 1])

    async def test_active_thread_discovery_failure_holds_threads_not_parent_posts(self):
        floor = self.adapter._recovery_floor
        parent = Channel(2)
        other = Channel(3)
        guild = SimpleNamespace(active_threads=AsyncMock(side_effect=RuntimeError("REST down")))
        self.adapter.client = SimpleNamespace(user=SimpleNamespace(id=9),
                                              get_guild=lambda id: guild,
                                              get_channel=lambda id: {2: parent, 3: other}[id])
        await self.adapter._reconcile_ready()
        self.assertEqual(self.adapter._blocked_channels, set())
        self.assertEqual(self.adapter._blocked_thread_parents, {2, 3})
        await self.adapter._on_message(message(floor + 1, parent))
        await self.adapter._on_message(message(floor + 2, Channel(20, parent_id=2)))
        self.assertEqual(self.handled, [floor + 1])

    async def test_incomplete_archived_thread_list_refuses_to_claim_recovery(self):
        parent = Channel(2)
        other = Channel(3)
        def archived_threads(*, limit):
            async def iterate():
                for index in range(limit):
                    yield Channel(100 + index, parent_id=2)
            return iterate()
        parent.archived_threads = archived_threads
        guild = SimpleNamespace(active_threads=AsyncMock(return_value=[]))
        self.adapter.client = SimpleNamespace(user=SimpleNamespace(id=9),
                                              get_guild=lambda id: guild,
                                              get_channel=lambda id: {2: parent, 3: other}[id])
        scopes, blocked, thread_blind = await self.adapter._recovery_scopes()
        self.assertEqual({channel.id for channel in scopes}, {2, 3})
        self.assertEqual((blocked, thread_blind), (set(), {2}))
        floor = self.adapter._recovery_floor
        other.messages = [message(floor + 1, other)]
        parent.send = AsyncMock()
        self.adapter.settings = DiscordSettings("TOKEN", 1, (2, 3), startup_channel_id=2)
        await self.adapter._reconcile_ready()
        await self.adapter._reconcile_ready()
        self.assertEqual(parent.send.await_count, 1)
        self.assertEqual(self.adapter._blocked_channels, set())
        self.assertEqual(self.adapter._blocked_thread_parents, {2})
        await self.adapter._on_message(message(floor + 2, parent))
        await self.adapter._on_message(message(floor + 3, Channel(20, parent_id=2)))
        self.assertEqual(self.handled, [floor + 1, floor + 2])

    async def test_new_thread_uses_its_approved_parent_watermark(self):
        floor = self.adapter._recovery_floor
        parent = SessionKey("agent_two", "discord", "2")
        await self.runtime.record_discord_observed(parent, str(floor))
        thread = Channel(floor + 1, parent_id=2)
        thread.messages = [message(floor + 2, thread)]
        await self.adapter._recover_scope(thread)
        self.assertEqual(self.handled, [floor + 2])

    async def test_crash_after_acceptance_uses_last_accepted_as_floor(self):
        floor = self.adapter._recovery_floor
        channel = Channel(2)
        channel.messages = [message(floor + 1, channel), message(floor + 2, channel)]
        key = SessionKey("agent_two", "discord", "2")
        await self.runtime.record_discord_observed(key, str(floor))
        scope = await self.runtime._scope(key)
        scope.checkpoint.last_message_id = str(floor + 1)
        from bridge.runtime.state import write_checkpoint
        write_checkpoint(scope.directory, scope.checkpoint)
        await self.adapter._recover_scope(channel)
        self.assertEqual(self.handled, [floor + 2])

    async def test_old_accepted_input_without_observation_is_not_a_first_install_cursor(self):
        floor = self.adapter._recovery_floor
        channel = Channel(2)
        channel.messages = [message(floor - 1, channel), message(floor + 1, channel)]
        key = SessionKey("agent_two", "discord", "2")
        scope = await self.runtime._scope(key)
        scope.checkpoint.last_message_id = str(floor - 2)
        from bridge.runtime.state import write_checkpoint
        write_checkpoint(scope.directory, scope.checkpoint)
        await self.adapter._recover_scope(channel)
        self.assertEqual(self.handled, [floor + 1])
        state = read_checkpoint(key.directory(self.runtime.state_root))
        self.assertIn("baseline_reset reason=first_enable", state.discord_recovery_gap)
        self.assertIn(f"boundary={floor}", state.discord_recovery_gap)
        self.assertIn(f"since={floor - 2}", state.discord_recovery_gap)


if __name__ == "__main__":
    unittest.main()
