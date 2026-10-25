"""Session-control commands (cancel, recover) behave the same on both transports.

Matrix accepts the addressed spellings of a command in every client rendering
(full ID, localpart, display name, matrix.to link, mention-only bare token) and
keeps the long-standing bare-token form after admission. Discord accepts the
mention prefix forms and the bare token after admission, plus the four guild
slash commands. Both adapters must scope every command to its conversation key
and to the human who owns the active request.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bridge.adapters.discord import DiscordAdapter
from bridge.adapters.matrix import MatrixAdapter
from bridge.config import DiscordSettings, MatrixSettings

ROOM = "!room:example.test"
AGENT_ID = "@agent_one:example.test"
OPERATOR = "@operator:example.test"


def matrix_runtime(*, awake: bool = True, cancel: bool = True,
                   recover: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        _scopes={},
        is_awake=AsyncMock(return_value=awake),
        admit=AsyncMock(return_value=True),
        submit=AsyncMock(),
        cancel=AsyncMock(return_value=cancel),
        recover=AsyncMock(return_value=recover),
        stop_session=AsyncMock(return_value=False),
        last_wake=AsyncMock(return_value=("", 0)),
        record_wake=AsyncMock(),
    )


def matrix_adapter(directory, runtime):
    settings = MatrixSettings("https://example.test", AGENT_ID, "TEST_MATRIX_TOKEN",
                              (ROOM,))
    with patch.dict(os.environ, {"TEST_MATRIX_TOKEN": "test"}):
        adapter = MatrixAdapter("agent_one", settings, runtime, Path(directory))
    adapter.output = AsyncMock()
    adapter._history = AsyncMock(return_value="")
    return adapter


def matrix_event(body: str, *, event_id: str = "$cmd",
                 mentions: list[str] | None = None,
                 msgtype: str = "m.text") -> dict:
    content: dict = {"msgtype": msgtype, "body": body}
    if mentions is not None:
        content["m.mentions"] = {"user_ids": mentions}
    return {"type": "m.room.message", "event_id": event_id, "sender": OPERATOR,
            "content": content}


class MatrixCancelTests(unittest.IsolatedAsyncioTestCase):
    async def assert_cancels_with(self, body: str,
                                  mentions: list[str] | None = None) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = matrix_runtime()
            adapter = matrix_adapter(directory, runtime)
            await adapter._handle_event(ROOM, matrix_event(body, mentions=mentions))
            key = adapter.output.await_args.args[0]
            runtime.cancel.assert_awaited_once_with(key, OPERATOR)
            self.assertEqual(key.transport, "matrix")
            self.assertEqual(adapter.output.await_args.args[1], "ack")
            self.assertIn("Cancellation requested.", adapter.output.await_args.args[2])
            runtime.submit.assert_not_awaited()
            self.assertIn("$cmd", adapter._seen)

    async def test_bare_cancel_token_while_awake(self):
        await self.assert_cancels_with("cancel")
        await self.assert_cancels_with("/cancel")
        await self.assert_cancels_with("CANCEL")

    async def test_mentioned_cancel_in_every_client_rendering(self):
        await self.assert_cancels_with("!cancel", mentions=[AGENT_ID])
        await self.assert_cancels_with("cancel", mentions=[AGENT_ID])
        await self.assert_cancels_with("Agent_one: cancel", mentions=[AGENT_ID])
        await self.assert_cancels_with(f"{AGENT_ID} cancel")  # plain full ID, no pill
        linked = (f"[{AGENT_ID}](https://matrix.to/#/{AGENT_ID}) /cancel")
        await self.assert_cancels_with(linked, mentions=[AGENT_ID])

    async def test_display_name_cancel_matches_configured_name(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = matrix_runtime()
            settings = MatrixSettings("https://example.test", "@agent_three-agent:example.test",
                                      "TEST_MATRIX_TOKEN", (ROOM,), display_name="Agent_three")
            with patch.dict(os.environ, {"TEST_MATRIX_TOKEN": "test"}):
                adapter = MatrixAdapter("example_agent", settings, runtime, Path(directory))
            adapter.output = AsyncMock()
            adapter._history = AsyncMock(return_value="")
            event = matrix_event("Agent_three: cancel", mentions=["@agent_three-agent:example.test"])
            await adapter._handle_event(ROOM, event)
            runtime.cancel.assert_awaited_once()

    async def test_cancel_scoped_to_thread_and_to_requester(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = matrix_runtime(cancel=False)
            adapter = matrix_adapter(directory, runtime)
            event = matrix_event("cancel")
            event["content"]["m.relates_to"] = {"rel_type": "m.thread",
                                                "event_id": "$root"}
            await adapter._handle_event(ROOM, event)
            key = runtime.cancel.await_args.args[0]
            self.assertEqual(key.scope, f"{ROOM}:thread:$root")
            self.assertEqual(adapter.output.await_args.args[1], "error")
            self.assertIn("No active request", adapter.output.await_args.args[2])

    async def test_cancel_needs_admission_in_a_sleeping_mention_only_room(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = matrix_runtime(awake=False)
            runtime.admit = AsyncMock(return_value=False)
            adapter = matrix_adapter(directory, runtime)
            await adapter._handle_event(ROOM, matrix_event("cancel"))
            runtime.cancel.assert_not_awaited()
            adapter.output.assert_not_awaited()
            self.assertIn("$cmd", adapter._seen)

    async def test_trailing_prose_is_a_prompt_not_a_command(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = matrix_runtime()
            adapter = matrix_adapter(directory, runtime)
            await adapter._handle_event(
                ROOM, matrix_event("cancel the deployment please", mentions=[AGENT_ID]))
            runtime.cancel.assert_not_awaited()
            runtime.submit.assert_awaited_once()
            self.assertIn("cancel the deployment please",
                          runtime.submit.await_args.args[1].text)

    async def test_directed_elsewhere_stays_silent(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = matrix_runtime(awake=False)
            adapter = matrix_adapter(directory, runtime)
            event = matrix_event("cancel", mentions=["@agent_two:example.test"])
            await adapter._handle_event(ROOM, event)
            runtime.cancel.assert_not_awaited()
            runtime.submit.assert_not_awaited()
            adapter.output.assert_not_awaited()


class MatrixRecoverTests(unittest.IsolatedAsyncioTestCase):
    async def assert_recovers_with(self, body: str,
                                   mentions: list[str] | None = None) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = matrix_runtime()
            adapter = matrix_adapter(directory, runtime)
            await adapter._handle_event(ROOM, matrix_event(body, mentions=mentions))
            runtime.recover.assert_awaited_once()
            self.assertEqual(adapter.output.await_args.args[1], "final")
            self.assertIn("Recovered.", adapter.output.await_args.args[2])
            runtime.submit.assert_not_awaited()

    async def test_recover_in_every_form(self):
        await self.assert_recovers_with("recover")
        await self.assert_recovers_with("/recover")
        await self.assert_recovers_with("!recover", mentions=[AGENT_ID])
        await self.assert_recovers_with("Agent_one: recover", mentions=[AGENT_ID])
        await self.assert_recovers_with(f"{AGENT_ID} recover")

    async def test_recover_without_failed_checkpoint_explains_itself(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = matrix_runtime(recover=False)
            adapter = matrix_adapter(directory, runtime)
            await adapter._handle_event(ROOM, matrix_event("recover", mentions=[AGENT_ID]))
            self.assertEqual(adapter.output.await_args.args[1], "error")
            self.assertIn("No failed checkpoint", adapter.output.await_args.args[2])
            runtime.submit.assert_not_awaited()

    async def test_recover_needs_admission_when_asleep_and_unaddressed(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = matrix_runtime(awake=False)
            runtime.admit = AsyncMock(return_value=False)
            adapter = matrix_adapter(directory, runtime)
            await adapter._handle_event(ROOM, matrix_event("recover"))
            runtime.recover.assert_not_awaited()
            adapter.output.assert_not_awaited()


def discord_runtime(**overrides: object) -> SimpleNamespace:
    runtime = SimpleNamespace(
        _scopes={},
        admit=AsyncMock(return_value=True),
        is_awake=AsyncMock(return_value=True),
        cancel=AsyncMock(return_value=True),
        recover=AsyncMock(return_value=True),
        stop_session=AsyncMock(return_value=False),
        submit=AsyncMock(),
        last_wake=AsyncMock(return_value=("", 0)),
        record_wake=AsyncMock(),
    )
    for name, value in overrides.items():
        setattr(runtime, name, value)
    return runtime


def discord_message(content: str, *, mentioned: bool = True, bot: bool = False,
                    channel_id: int = 2, guild_id: int | None = 1) -> SimpleNamespace:
    user = SimpleNamespace(id=3, bot=bot, display_name="Operator")
    mentions = [SimpleNamespace(id=9)] if mentioned else []
    return SimpleNamespace(id=10, content=content, mentions=mentions,
                           channel=SimpleNamespace(id=channel_id, parent_id=None),
                           guild=SimpleNamespace(id=guild_id) if guild_id else None,
                           author=user, attachments=[], reply=AsyncMock())


def discord_adapter(runtime, **settings):
    adapter = DiscordAdapter("agent_two", DiscordSettings("TOKEN", 1, (2,), **settings),
                             runtime)
    adapter.client = SimpleNamespace(user=SimpleNamespace(id=9))
    return adapter


class DiscordTextCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_mention_cancel_and_recover_forms(self):
        for content in ("<@9> cancel", "<@9> /cancel", "<@9> !cancel",
                        "<@!9> CANCEL"):
            with self.subTest(content=content):
                runtime = discord_runtime()
                adapter = discord_adapter(runtime)
                message = discord_message(content)
                await adapter._on_message(message)
                runtime.cancel.assert_awaited_once()
                self.assertEqual(runtime.cancel.await_args.args[1], "3")
                message.reply.assert_awaited_once_with("Cancellation requested.",
                                                       mention_author=False)
                runtime.submit.assert_not_awaited()
        for content in ("<@9> recover", "<@9> /recover", "<@9> !recover"):
            with self.subTest(content=content):
                runtime = discord_runtime()
                adapter = discord_adapter(runtime)
                message = discord_message(content)
                await adapter._on_message(message)
                runtime.recover.assert_awaited_once()
                message.reply.assert_awaited_once_with(
                    "Recovered. The next reply starts from the last saved summary; "
                    "recent unsaved context may be missing.", mention_author=False)
                runtime.submit.assert_not_awaited()

    async def test_cancel_from_the_wrong_person_is_explained(self):
        runtime = discord_runtime(cancel=AsyncMock(return_value=False))
        adapter = discord_adapter(runtime)
        message = discord_message("<@9> cancel")
        await adapter._on_message(message)
        message.reply.assert_awaited_once_with("No active request from you here.",
                                               mention_author=False)

    async def test_recover_without_failed_checkpoint_is_explained(self):
        runtime = discord_runtime(recover=AsyncMock(return_value=False))
        adapter = discord_adapter(runtime)
        message = discord_message("<@9> recover")
        await adapter._on_message(message)
        message.reply.assert_awaited_once_with(
            "No failed checkpoint belonging to you in this channel.",
            mention_author=False)

    async def test_bare_token_cancels_only_after_admission(self):
        # Mirrors the Matrix bare-token form: admission decides, so a bare
        # "cancel" in a sleeping non-wake channel stays ordinary silence.
        runtime = discord_runtime()
        adapter = discord_adapter(runtime)
        message = discord_message("cancel", mentioned=False)
        await adapter._on_message(message)
        runtime.cancel.assert_awaited_once()
        message.reply.assert_awaited_once_with("Cancellation requested.",
                                               mention_author=False)

        runtime = discord_runtime(admit=AsyncMock(return_value=False))
        adapter = discord_adapter(runtime)
        message = discord_message("cancel", mentioned=False)
        await adapter._on_message(message)
        runtime.cancel.assert_not_awaited()
        message.reply.assert_not_awaited()

    async def test_bot_mention_can_cancel_its_own_request(self):
        runtime = discord_runtime()
        adapter = discord_adapter(runtime)
        message = discord_message("<@9> cancel", bot=True)
        await adapter._on_message(message)
        runtime.cancel.assert_awaited_once()
        self.assertEqual(runtime.cancel.await_args.args[1], "3")
        runtime.submit.assert_not_awaited()

    async def test_command_outside_an_enabled_channel_is_ignored(self):
        runtime = discord_runtime()
        adapter = discord_adapter(runtime)
        message = discord_message("<@9> cancel", channel_id=99)
        await adapter._on_message(message)
        runtime.cancel.assert_not_awaited()
        runtime.admit.assert_not_awaited()
        message.reply.assert_not_awaited()

    async def test_trailing_prose_is_a_prompt_not_a_command(self):
        runtime = discord_runtime()
        adapter = discord_adapter(runtime)
        message = discord_message("<@9> cancel the deployment please")
        await adapter._on_message(message)
        runtime.cancel.assert_not_awaited()
        runtime.submit.assert_awaited_once()


def slash_interaction(channel_id: int = 2, guild_id: int | None = 1) -> SimpleNamespace:
    return SimpleNamespace(
        channel=SimpleNamespace(id=channel_id, parent_id=None),
        guild=SimpleNamespace(id=guild_id) if guild_id else None,
        user=SimpleNamespace(id=3),
        response=SimpleNamespace(send_message=AsyncMock()))


class DiscordSlashCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_slash_cancel_scoped_to_channel_and_user(self):
        runtime = discord_runtime()
        adapter = discord_adapter(runtime)
        interaction = slash_interaction()
        await adapter.bridge_cancel_command(interaction)
        key = runtime.cancel.await_args.args[0]
        self.assertEqual((key.agent, key.transport, key.scope), ("agent_two", "discord", "2"))
        self.assertEqual(runtime.cancel.await_args.args[1], "3")
        interaction.response.send_message.assert_awaited_once_with(
            "Cancellation requested.", ephemeral=True)

        runtime = discord_runtime(cancel=AsyncMock(return_value=False))
        adapter = discord_adapter(runtime)
        interaction = slash_interaction()
        await adapter.bridge_cancel_command(interaction)
        interaction.response.send_message.assert_awaited_once_with(
            "No active request from you here.", ephemeral=True)

    async def test_slash_commands_refuse_disabled_channels_ephemerally(self):
        for command in ("bridge_cancel_command", "bridge_debug_command",
                        "bridge_stop_command", "bridge_recover_command"):
            with self.subTest(command=command):
                runtime = discord_runtime()
                adapter = discord_adapter(runtime)
                interaction = slash_interaction(channel_id=99)
                await getattr(adapter, command)(interaction)
                interaction.response.send_message.assert_awaited_once_with(
                    "This channel is not enabled.", ephemeral=True)
                runtime.cancel.assert_not_awaited()
                runtime.recover.assert_not_awaited()
                runtime.stop_session.assert_not_awaited()

    async def test_slash_commands_refuse_direct_messages(self):
        runtime = discord_runtime()
        adapter = discord_adapter(runtime)
        interaction = slash_interaction(guild_id=None)
        await adapter.bridge_stop_command(interaction)
        interaction.response.send_message.assert_awaited_once_with(
            "This channel is not enabled.", ephemeral=True)
        runtime.stop_session.assert_not_awaited()

    async def test_slash_debug_reports_scope_status(self):
        runtime = discord_runtime()
        adapter = discord_adapter(runtime)
        interaction = slash_interaction()
        await adapter.bridge_debug_command(interaction)
        text = interaction.response.send_message.await_args.args[0]
        self.assertIn("Bridge debug (agent_two, discord)", text)
        self.assertIn("sleeping", text)
        runtime.submit.assert_not_awaited()

    async def test_slash_stop_only_stops_the_callers_scope(self):
        runtime = discord_runtime(stop_session=AsyncMock(return_value=True))
        adapter = discord_adapter(runtime)
        interaction = slash_interaction()
        await adapter.bridge_stop_command(interaction)
        runtime.stop_session.assert_awaited_once_with(
            runtime.stop_session.await_args.args[0], "3")
        interaction.response.send_message.assert_awaited_once_with(
            "Stopped this conversation's Pi session. Unsaved context was discarded; "
            "the next wake uses the last saved summary and recent posts.",
            ephemeral=True)

        runtime = discord_runtime()
        adapter = discord_adapter(runtime)
        interaction = slash_interaction()
        await adapter.bridge_stop_command(interaction)
        interaction.response.send_message.assert_awaited_once_with(
            "No running session belonging to you in this channel.", ephemeral=True)

    async def test_slash_recover_reports_outcome(self):
        runtime = discord_runtime()
        adapter = discord_adapter(runtime)
        interaction = slash_interaction()
        await adapter.bridge_recover_command(interaction)
        runtime.recover.assert_awaited_once()
        interaction.response.send_message.assert_awaited_once_with(
            "Recovered. The next reply starts from the last saved summary; "
            "recent unsaved context may be missing.", ephemeral=True)

        runtime = discord_runtime(recover=AsyncMock(return_value=False))
        adapter = discord_adapter(runtime)
        interaction = slash_interaction()
        await adapter.bridge_recover_command(interaction)
        interaction.response.send_message.assert_awaited_once_with(
            "No failed checkpoint belonging to you in this channel.", ephemeral=True)


class PeerCommandParityTests(unittest.IsolatedAsyncioTestCase):
    async def test_matrix_peer_all_bang_and_slash_session_commands(self):
        for command, method in (("debug", None), ("stop", "stop_session"),
                                ("cancel", "cancel"), ("recover", "recover")):
            for prefix in ("!", "/", "/bridge_"):
                with self.subTest(command=command, prefix=prefix), tempfile.TemporaryDirectory() as directory:
                    runtime = matrix_runtime()
                    adapter = matrix_adapter(directory, runtime)
                    event = matrix_event(prefix + command, mentions=[AGENT_ID])
                    event["sender"] = "@peer-agent:example.test"
                    event["content"]["org.example.pi_bridge"] = {
                        "kind":"final", "hop":1, "invitations":[AGENT_ID]}
                    await adapter._handle_event(ROOM, event)
                    if method:
                        call = getattr(runtime, method)
                        call.assert_awaited_once()
                        self.assertEqual(call.await_args.args[1], event["sender"])
                    else:
                        self.assertIn("Bridge debug", adapter.output.await_args.args[2])
                    runtime.submit.assert_not_awaited()

    async def test_discord_bot_all_bang_and_slash_session_commands(self):
        for command, method in (("debug", None), ("stop", "stop_session"),
                                ("cancel", "cancel"), ("recover", "recover")):
            for prefix in ("!", "/", "/bridge_"):
                with self.subTest(command=command, prefix=prefix):
                    runtime = discord_runtime()
                    adapter = discord_adapter(runtime)
                    message = discord_message("<@9> " + prefix + command, bot=True)
                    await adapter._on_message(message)
                    if method:
                        call = getattr(runtime, method)
                        call.assert_awaited_once()
                        self.assertEqual(call.await_args.args[1], "3")
                    else:
                        self.assertIn("Bridge debug", message.reply.await_args.args[0])
                    runtime.submit.assert_not_awaited()

    async def test_invitation_only_peers_dispatch_every_native_control(self):
        for command, method in (("debug", None), ("stop", "stop_session"),
                                ("cancel", "cancel"), ("recover", "recover")):
            for prefix in ("!", "/", "/bridge_"):
                with self.subTest(command=command, prefix=prefix), tempfile.TemporaryDirectory() as directory:
                    runtime = matrix_runtime()
                    adapter = matrix_adapter(directory, runtime)
                    event = matrix_event(prefix + command)
                    event["sender"] = "@peer-agent:example.test"
                    event["content"]["org.example.pi_bridge"] = {
                        "kind": "final", "hop": 1, "invitations": [AGENT_ID]}
                    await adapter._handle_event(ROOM, event)
                    runtime.submit.assert_not_awaited()
                    if method:
                        getattr(runtime, method).assert_awaited_once()
                    else:
                        self.assertIn("Bridge debug", adapter.output.await_args.args[2])
                    runtime = discord_runtime()
                    adapter = discord_adapter(runtime)
                    message = discord_message(prefix + command, mentioned=False, bot=True)
                    message.embeds = [SimpleNamespace(footer=SimpleNamespace(text=
                        "agent-bridge:v1:" + json.dumps({"kind": "final", "hop": 1,
                                                       "invitations": ["9"]})))]
                    await adapter._on_message(message)
                    runtime.submit.assert_not_awaited()
                    if method:
                        getattr(runtime, method).assert_awaited_once()
                    else:
                        self.assertIn("Bridge debug", message.reply.await_args.args[0])

    async def test_blocked_history_bare_debug_stop_never_become_prompts(self):
        for token in ("!debug", "/debug", "/bridge_debug", "!stop", "/stop", "/bridge_stop"):
            with self.subTest(token=token):
                runtime = discord_runtime()
                adapter = discord_adapter(runtime)
                adapter._blocked_channels.add(2)
                await adapter._on_message(discord_message(token, mentioned=False))
                runtime.submit.assert_not_awaited()
                runtime.stop_session.assert_not_awaited()

    async def test_discord_slash_callbacks_accept_peer_identity(self):
        for command in ("debug", "stop", "cancel", "recover"):
            runtime = discord_runtime()
            adapter = discord_adapter(runtime)
            interaction = slash_interaction()
            interaction.user.bot = True
            await getattr(adapter, "bridge_" + command + "_command")(interaction)
            interaction.response.send_message.assert_awaited_once()
            if command == "debug":
                self.assertIn("Bridge debug", interaction.response.send_message.await_args.args[0])
            else:
                method = "stop_session" if command == "stop" else command
                call = getattr(runtime, method)
                call.assert_awaited_once()
                self.assertEqual(call.await_args.args[1], "3")

    async def test_peer_cancel_remains_available_during_history_recovery(self):
        for prefix in ("", "!", "/"):
            with self.subTest(prefix=prefix):
                runtime = discord_runtime()
                adapter = discord_adapter(runtime)
                adapter._blocked_channels.add(2)
                await adapter._on_message(discord_message("<@9> " + prefix + "cancel", bot=True))
                runtime.cancel.assert_awaited_once()
                self.assertEqual(runtime.cancel.await_args.args[1], "3")


if __name__ == "__main__":
    unittest.main()
