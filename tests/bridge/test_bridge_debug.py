"""Bridge-native debug replies must not start or inspect private Pi sessions."""

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bridge.adapters.discord import DiscordAdapter
from bridge.adapters.matrix import MatrixAdapter, bridge_command, is_debug_command
from bridge.config import DiscordSettings, MatrixSettings
from bridge.debug import debug_status
from bridge.runtime.state import SessionKey


class DebugTests(unittest.IsolatedAsyncioTestCase):
    async def test_status_for_missing_idle_active_and_sleeping_scopes(self):
        key = SessionKey("agent_one", "matrix", "!room:test:main")
        runtime = SimpleNamespace(_scopes={})
        with patch("bridge.debug.bridge_commit", return_value="abc123"):
            result = await debug_status(runtime, key)
            self.assertIn("sleeping", result)
            self.assertIn("none loaded", result)
            self.assertIn("abc123", result)
            scope = SimpleNamespace(active=False, client=object(),
                                    checkpoint=SimpleNamespace(status="idle"))
            runtime._scopes[key] = scope
            self.assertIn("awake (idle)", await debug_status(runtime, key))
            scope.active = True
            scope.checkpoint.status = "running"
            self.assertIn("awake (working)", await debug_status(runtime, key))
            scope.active = False
            scope.client = None
            scope.checkpoint.status = "checkpoint_error"
            result = await debug_status(runtime, key)
            self.assertIn("sleeping", result)
            self.assertIn("checkpoint_error", result)

    async def test_matrix_debug_bypasses_admission_and_pi(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = MatrixSettings("https://example.test", "@agent_one:example.test",
                                      "TEST_MATRIX_TOKEN", ("!room:example.test",))
            runtime = SimpleNamespace(_scopes={}, is_awake=AsyncMock(return_value=False),
                                      admit=AsyncMock(), submit=AsyncMock(),
                                      last_wake=AsyncMock(return_value=(None, None)))
            with patch.dict(os.environ, {"TEST_MATRIX_TOKEN": "test"}):
                adapter = MatrixAdapter("agent_one", settings, runtime, Path(directory))
            adapter.output = AsyncMock()
            event = {"type": "m.room.message", "event_id": "$debug",
                     "sender": "@operator:example.test",
                     "content": {"msgtype": "m.text", "body": "@agent_one:example.test !debug"}}
            await adapter._handle_event("!room:example.test", event)
            self.assertIn("Uptime:", adapter.output.await_args.args[2])
            self.assertIn("sleeping", adapter.output.await_args.args[2])
            runtime.admit.assert_not_awaited()
            runtime.submit.assert_not_awaited()
            self.assertIn("$debug", adapter._seen)
            event["event_id"] = "$display-name"
            event["content"] = {"msgtype": "m.text", "body": "Agent_one: !debug",
                                "m.mentions": {"user_ids": [settings.user_id]}}
            await adapter._handle_event("!room:example.test", event)
            self.assertIn("Uptime:", adapter.output.await_args.args[2])
            self.assertEqual(adapter.output.await_count, 2)
            runtime.admit.assert_not_awaited()
            event["event_id"] = "$linked"
            event["content"] = {"msgtype": "m.text",
                                "body": ("[@agent_one:example.test]"
                                         "(https://matrix.to/#/@agent_one:example.test) !debug"),
                                "m.mentions": {"user_ids": [settings.user_id]}}
            await adapter._handle_event("!room:example.test", event)
            self.assertIn("Uptime:", adapter.output.await_args.args[2])
            self.assertEqual(adapter.output.await_count, 3)
            runtime.admit.assert_not_awaited()
            runtime.submit.assert_not_awaited()
            event["event_id"] = "$ordinary"
            event["content"] = {"msgtype": "m.text", "body": "!debug"}
            adapter._history = AsyncMock(return_value="")
            await adapter._handle_event("!room:example.test", event)
            self.assertEqual(adapter.output.await_count, 4)
            runtime.admit.assert_awaited_once()

    def test_structured_mention_does_not_make_ordinary_text_a_command(self):
        user_id = "@agent_one:example.test"
        for body in ("hello Agent_one: !debug", "Agent_one: !debug please",
                     "Agent_two: !debug"):
            with self.subTest(body=body):
                self.assertFalse(is_debug_command(
                    {"msgtype": "m.text", "body": body,
                     "m.mentions": {"user_ids": [user_id]}}, user_id))
        self.assertFalse(is_debug_command(
            {"msgtype": "m.text", "body": "Agent_one: !debug",
             "m.mentions": {"user_ids": ["@other:example.test"]}}, user_id))

    def test_bare_command_with_structured_mention_is_a_command(self):
        # A client can send the command as the whole body with the mention only
        # in m.mentions, so the address never reaches the text. The token alone
        # is unambiguous; anything longer stays ordinary text.
        user_id = "@agent_one:example.test"
        for body, expected in (("!debug", "debug"), (" !stop ", "stop"),
                               ("!STOP", "stop")):
            with self.subTest(body=body):
                self.assertEqual(expected, bridge_command(
                    {"msgtype": "m.text", "body": body,
                     "m.mentions": {"user_ids": [user_id]}}, user_id))
        # The mention is required: the token alone in plain text stays ordinary.
        self.assertIsNone(bridge_command(
            {"msgtype": "m.text", "body": "!stop"}, user_id))
        # Trailing prose keeps the message out of the command path.
        self.assertIsNone(bridge_command(
            {"msgtype": "m.text", "body": "!stop now",
             "m.mentions": {"user_ids": [user_id]}}, user_id))

    def test_matrix_to_link_requires_exact_structured_target_and_whole_command(self):
        user_id = "@agent_three-agent:example.test"
        linked = ("[@agent_three-agent:example.test]"
                  "(https://matrix.to/#/@agent_three-agent:example.test)")
        encoded_link = linked.replace("https://matrix.to/#/@agent_three-agent:example.test",
                                      "https://matrix.to/#/%40agent_three-agent%3Aexample.test")
        # Only the literal live Element shape is accepted; other URI styles
        # require their own observed event and reviewed parser boundary.
        for command in ("!debug", "!stop"):
            with self.subTest(command=command):
                content = {"msgtype": "m.text", "body": f"{linked} {command}",
                           "m.mentions": {"user_ids": [user_id]}}
                self.assertEqual(command[1:], bridge_command(content, user_id, "Agent_three"))
                for body, mentions in (
                    (content["body"], []),
                    (content["body"], ["@other:example.test"]),
                    (f"{linked} {command} please", [user_id]),
                    (f"{linked.replace('matrix.to', 'elsewhere.test')} {command}", [user_id]),
                    (f"{linked.replace('@agent_three-agent', '@other')} {command}", [user_id]),
                    (f"{linked.replace('@agent_three-agent', '@Agent_three-Agent')} {command}",
                     [user_id]),
                    (f"{encoded_link} {command}", [user_id]),
                    (f"{linked.replace('https://matrix.to/#/', 'matrix:u/')} {command}",
                     [user_id]),
                    (f"{linked.replace('[@agent_three-agent:example.test]', '[Agent_three]')} {command}",
                     [user_id]),
                ):
                    self.assertIsNone(bridge_command(
                        {"msgtype": "m.text", "body": body,
                         "m.mentions": {"user_ids": mentions}}, user_id, "Agent_three"))

    def test_display_name_ping_matches_when_it_differs_from_localpart(self):
        # The bridge's Matrix display name can differ from its user_id localpart
        # ("Agent_three" vs "agent_three-agent"). A client renders a structured mention
        # using the display name, so that body must still trigger the command.
        user_id = "@agent_three-agent:example.test"
        display_name = "Agent_three"
        content = {"msgtype": "m.text", "body": "Agent_three: !debug",
                   "m.mentions": {"user_ids": [user_id]}}
        self.assertEqual("debug", bridge_command(content, user_id, display_name))
        self.assertTrue(is_debug_command(content, user_id, display_name))
        # Without the display name configured, the same body does not match:
        # "Agent_three" is not the localpart "agent_three-agent".
        self.assertIsNone(bridge_command(content, user_id))
        # The display name is only honoured when the user is actually mentioned,
        # so plain text using the display name without a mention stays ordinary.
        self.assertIsNone(bridge_command(
            {"msgtype": "m.text", "body": "Agent_three: !debug"}, user_id, display_name))

    async def test_matrix_stop_is_scope_local_and_not_a_pi_prompt(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = MatrixSettings("https://example.test", "@agent_one:example.test",
                                      "TEST_MATRIX_TOKEN", ("!room:example.test",))
            runtime = SimpleNamespace(_scopes={}, is_awake=AsyncMock(return_value=True),
                                      admit=AsyncMock(), submit=AsyncMock(),
                                      stop_session=AsyncMock(return_value=True))
            with patch.dict(os.environ, {"TEST_MATRIX_TOKEN": "test"}):
                adapter = MatrixAdapter("agent_one", settings, runtime, Path(directory))
            adapter.output = AsyncMock()
            event = {"type": "m.room.message", "event_id": "$stop",
                     "sender": "@operator:example.test",
                     "content": {"msgtype": "m.text", "body": "Agent_one: !stop",
                                 "m.mentions": {"user_ids": [settings.user_id]}}}
            await adapter._handle_event("!room:example.test", event)
            runtime.stop_session.assert_awaited_once_with(
                SessionKey("agent_one", "matrix", "!room:example.test:main"),
                "@operator:example.test")
            self.assertIn("Unsaved context", adapter.output.await_args.args[2])
            runtime.admit.assert_not_awaited()
            runtime.submit.assert_not_awaited()
            self.assertIsNone(bridge_command(
                {"msgtype": "m.text", "body": "Agent_one: !stop now",
                 "m.mentions": {"user_ids": [settings.user_id]}}, settings.user_id))
            self.assertIsNone(bridge_command(
                {"msgtype": "m.text", "body": "Agent_one: !stop"}, settings.user_id))

    async def test_bare_stop_with_structured_mention_is_not_a_pi_prompt(self):
        # A client may send "!stop" as the whole body with the mention only in
        # m.mentions; that must still stop the session without waking Pi.
        with tempfile.TemporaryDirectory() as directory:
            settings = MatrixSettings("https://example.test", "@agent_one:example.test",
                                      "TEST_MATRIX_TOKEN", ("!room:example.test",))
            runtime = SimpleNamespace(_scopes={}, is_awake=AsyncMock(return_value=True),
                                      admit=AsyncMock(), submit=AsyncMock(),
                                      stop_session=AsyncMock(return_value=True))
            with patch.dict(os.environ, {"TEST_MATRIX_TOKEN": "test"}):
                adapter = MatrixAdapter("agent_one", settings, runtime, Path(directory))
            adapter.output = AsyncMock()
            event = {"type": "m.room.message", "event_id": "$bare-stop",
                     "sender": "@operator:example.test",
                     "content": {"msgtype": "m.text", "body": "!stop",
                                 "m.mentions": {"user_ids": [settings.user_id]}}}
            await adapter._handle_event("!room:example.test", event)
            runtime.stop_session.assert_awaited_once_with(
                SessionKey("agent_one", "matrix", "!room:example.test:main"),
                "@operator:example.test")
            self.assertIn("Unsaved context", adapter.output.await_args.args[2])
            runtime.admit.assert_not_awaited()
            runtime.submit.assert_not_awaited()

    async def test_discord_mention_debug_bypasses_admission(self):
        runtime = SimpleNamespace(_scopes={}, admit=AsyncMock(), submit=AsyncMock())
        adapter = DiscordAdapter("agent_two", DiscordSettings("TOKEN", 1, (2,)), runtime)
        bot = SimpleNamespace(id=9)
        adapter.client = SimpleNamespace(user=bot)
        reply = AsyncMock()
        message = SimpleNamespace(id=10, channel=SimpleNamespace(id=2, parent_id=None),
                                  guild=SimpleNamespace(id=1),
                                  author=SimpleNamespace(id=3, bot=False),
                                  mentions=[bot], content="<@9> !debug", reply=reply)
        await adapter._on_message(message)
        self.assertIn("sleeping", reply.await_args.args[0])
        runtime.admit.assert_not_awaited()
        runtime.submit.assert_not_awaited()
        message.content = "<@9> !stop"
        runtime.stop_session = AsyncMock(return_value=True)
        await adapter._on_message(message)
        runtime.stop_session.assert_awaited_once_with(
            SessionKey("agent_two", "discord", "2"), "3")
        self.assertIn("Unsaved context", reply.await_args.args[0])
        runtime.admit.assert_not_awaited()
