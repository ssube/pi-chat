"""Event-controlled fakes shared by transport and lifecycle contract tests."""
import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

from bridge.adapters.discord import DiscordAdapter
from bridge.adapters.matrix import BRIDGE_META, MatrixAdapter
from bridge.config import DiscordSettings, MatrixSettings
from bridge.filtering import DISCORD_META_PREFIX
from bridge.runtime.pi_rpc import PiRunResult
from bridge.runtime.session import BridgeRuntime
from bridge.runtime.state import SessionKey


class ControlledRpc:
    def __init__(self, session_file, *, hold=False):
        self.session_file = session_file
        self.active = False
        self.prompts = []
        self.steers = []
        self.accepted = asyncio.Event()
        self.release = asyncio.Event()
        if not hold:
            self.release.set()
        self.compact_started = asyncio.Event()
        self.compact_release = asyncio.Event()
        self.compact_release.set()
        self.answer = "Result ready."
        self.stopped = False
        self.compactions = 0

    async def prompt(self, message, on_event, images=None, *, on_accepted=None):
        self.active = True
        self.prompts.append((message, images or []))
        if on_accepted:
            await on_accepted()
        self.accepted.set()
        try:
            await self.release.wait()
            await on_event({"type": "message_end", "message": {
                "role": "assistant", "content": [{"type": "text", "text": self.answer}]}})
            return PiRunResult(self.answer)
        finally:
            self.active = False

    async def steer(self, message, images=None):
        assert self.active
        self.steers.append((message, images or []))

    async def compact(self):
        assert not self.active
        self.compact_started.set()
        await self.compact_release.wait()
        self.compactions += 1
        return "Sound summary"

    async def abort(self):
        self.release.set()

    async def stop(self):
        self.stopped = True
        self.release.set()


class Harness:
    def __init__(self, root: Path, transport: str, *, wake_any=False, hold=False):
        self.transport = transport
        self.clients: list[ControlledRpc] = []
        self.sent: list[dict[str, Any]] = []
        self.adapter: Any

        def factory(key, session_file):
            client = ControlledRpc(session_file, hold=hold)
            self.clients.append(client)
            return client

        async def output(key, kind, text, request_id):
            await self.adapter.output(key, kind, text, request_id)

        self.runtime = BridgeRuntime(root / "sessions", factory, output)
        self.room = "!room:example.test"
        self.guild = SimpleNamespace(id=1, chunked=True, roles=[], members=[
            SimpleNamespace(id=3, name="alice", display_name="Alice Smith", global_name="Alice"),
            SimpleNamespace(id=4, name="bob", display_name="Bob", global_name=None)])
        self.channel = SimpleNamespace(id=2, parent_id=None, guild=self.guild,
                                       members=self.guild.members, typing=AsyncMock())
        self.channel.send = AsyncMock(side_effect=self._discord_send)
        if transport == "matrix":
            settings = MatrixSettings("https://example.test", "@bridge:example.test", "TEST_TOKEN",
                                      (self.room,), wake_on_any_room_ids=(self.room,) if wake_any else ())
            with patch.dict("os.environ", {"TEST_TOKEN": "synthetic"}):
                self.adapter = MatrixAdapter("agent_two", settings, self.runtime, root / "matrix")

            async def request(method, path, **kwargs):
                if method == "GET":
                    return {"joined": {"@alice:example.test": {"display_name": "Alice Smith"},
                                       "@bob:example.test": {"display_name": "Bob"}}}
                if "/redact/" in path:
                    return {}  # A redaction is not a newly posted message.
                self.sent.append(kwargs.get("data", {}))
                return {"event_id": f"$sent-{len(self.sent)}"}

            self.adapter._request = request
            self.adapter._context = AsyncMock(return_value="")
            self.key = SessionKey("agent_two", "matrix", self.room + ":main")
        else:
            self.adapter = DiscordAdapter("agent_two", DiscordSettings("TEST_TOKEN", 1, (2,),
                                          wake_on_any_channel_ids=(2,) if wake_any else ()), self.runtime)
            self.adapter.client = SimpleNamespace(user=SimpleNamespace(id=9),
                get_channel=lambda _id: self.channel, close=AsyncMock())
            self.adapter._context = AsyncMock(return_value="")
            self.key = SessionKey("agent_two", "discord", "2")

    async def _discord_send(self, text, **kwargs):
        self.sent.append({"body": text, **kwargs})
        return SimpleNamespace(delete=AsyncMock(), edit=AsyncMock())

    def event(self, identifier, text="hello", *, ping=True, meta=None, peer=False):
        if self.transport == "matrix":
            content = {"msgtype": "m.text", "body": text,
                       "m.mentions": {"user_ids": ["@bridge:example.test"] if ping else []}}
            if meta is not None:
                content[BRIDGE_META] = meta
            return {"type": "m.room.message", "event_id": f"${identifier}",
                    "sender": "@alice:example.test", "content": content}
        import discord
        embeds = []
        if meta is not None:
            import json
            embeds = [discord.Embed().set_footer(text=DISCORD_META_PREFIX + json.dumps(meta))]
        return SimpleNamespace(id=identifier, content=text, channel=self.channel, guild=self.guild,
            author=SimpleNamespace(id=3, name="alice", display_name="Alice Smith", bot=peer),
            mentions=[self.adapter.client.user] if ping else [], attachments=[], embeds=embeds,
            reply=AsyncMock(side_effect=self._discord_send))

    async def deliver(self, event):
        if self.transport == "matrix":
            await self.adapter._handle_event(self.room, event)
        else:
            await self.adapter._on_message(event)

    async def idle(self):
        await asyncio.wait_for(self.runtime.wait_idle(self.key), 2)

    async def close(self):
        await self.runtime.close()
