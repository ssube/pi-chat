import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

from bridge.adapters.discord import DiscordAdapter
from bridge.config import DiscordSettings


class FakeRuntime:
    def __init__(self):
        self.awake = False
        self.incoming = []
        self.markers = []

    async def is_awake(self, key):
        return self.awake

    async def admit(self, key, can_wake, *, on_wait=None):
        return self.awake or can_wake

    async def last_wake(self, key):
        return "20", 1_000_000

    async def submit(self, key, incoming):
        self.incoming.append(incoming)
        self.awake = True

    async def record_wake(self, key, event_id):
        self.markers.append(event_id)


class DiscordAwarenessTests(unittest.IsolatedAsyncioTestCase):
    def message(self, number, bot, channel, *, mentions=(), content="hello"):
        return SimpleNamespace(
            id=number, channel=channel, guild=channel.guild,
            author=SimpleNamespace(id=3, display_name="Operator", bot=False),
            mentions=list(mentions), content=content, attachments=[],
            created_at=datetime.fromtimestamp(number, timezone.utc))

    async def test_mention_wake_then_unmentioned_followup_and_other_mention(self):
        runtime = FakeRuntime()
        adapter = DiscordAdapter("agent_two", DiscordSettings("TOKEN", 1, (2,)), runtime)
        bot = SimpleNamespace(id=9)
        adapter.client = SimpleNamespace(user=bot)
        guild = SimpleNamespace(id=1)
        channel = SimpleNamespace(id=2, parent_id=None, guild=guild)
        adapter._context = lambda message: async_empty()
        await adapter._on_message(self.message(21, bot, channel))
        self.assertEqual(runtime.incoming, [])
        await adapter._on_message(self.message(22, bot, channel, mentions=(bot,)))
        await adapter._on_message(self.message(23, bot, channel))
        other = SimpleNamespace(id=10)
        await adapter._on_message(self.message(24, bot, channel, mentions=(other,)))
        self.assertEqual([item.message_id for item in runtime.incoming], ["22", "23", "24"])
        self.assertFalse(runtime.incoming[0].optional)
        self.assertTrue(runtime.incoming[1].optional)
        self.assertTrue(runtime.incoming[2].optional)
        self.assertEqual(runtime.markers, ["22", "23", "24"])

    async def test_opt_in_wake_any_and_context_count(self):
        runtime = FakeRuntime()
        adapter = DiscordAdapter("agent_two", DiscordSettings("TOKEN", 1, (2,), (2,)), runtime)
        bot = SimpleNamespace(id=9)
        adapter.client = SimpleNamespace(user=bot)
        guild = SimpleNamespace(id=1)
        channel = SimpleNamespace(id=2, parent_id=None, guild=guild)
        earlier = [self.message(i, bot, channel) for i in range(20, 61)]

        class History:
            def __aiter__(self):
                return self

            async def __anext__(self):
                if not earlier:
                    raise StopAsyncIteration
                return earlier.pop()

        channel.history = lambda **kwargs: History()
        await adapter._on_message(self.message(61, bot, channel))
        self.assertEqual(len(runtime.incoming), 1)
        self.assertTrue(runtime.incoming[0].optional)
        self.assertIn("[skipping 10 messages]", runtime.incoming[0].text)
        self.assertIn("1970-01-01T00:00:31+00:00", runtime.incoming[0].text)
        self.assertIn("[previous", runtime.incoming[0].text.lower())

    async def test_context_read_uses_state_after_admission(self):
        class RacingRuntime(FakeRuntime):
            def __init__(self):
                super().__init__()
                self.checks = 0

            async def is_awake(self, key):
                self.checks += 1
                return False

        runtime = RacingRuntime()
        adapter = DiscordAdapter("agent_two", DiscordSettings("TOKEN", 1, (2,), (2,)), runtime)
        bot = SimpleNamespace(id=9)
        adapter.client = SimpleNamespace(user=bot)
        guild = SimpleNamespace(id=1)
        channel = SimpleNamespace(id=2, parent_id=None, guild=guild)

        async def context(message):
            return "Older context"

        adapter._context = context
        await adapter._on_message(self.message(61, bot, channel))
        self.assertEqual(runtime.checks, 1)
        self.assertIn("Older context", runtime.incoming[0].text)


async def async_empty():
    return ""
