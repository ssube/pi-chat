"""Discord output and mention routing for Agent_two's shared bridge profile."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from bridge.config import DiscordSettings
from bridge.activity import Activity, Presence
from bridge.history_service import HistoryPage
from bridge.mentions import Mention, plan_mentions, replace_mentions, roster_line
from bridge.reaction_service import ReactionRejected
from bridge.debug import debug_status
from bridge.runtime.pi_rpc import RpcImage
from bridge.runtime.session import BridgeRuntime, Incoming
from bridge.runtime.state import SessionKey


log = logging.getLogger("agent_bridge.discord")
DISCORD_LIMIT = 1900
# Session-control tokens shared with the Matrix adapter: the plain words a
# requester types (`cancel`, `recover`), the chat-client slash spellings, and
# the bang spelling of the diagnostic commands. These run after admission,
# like their Matrix forms; `!debug` and `!stop` stay pre-admission.
DISCORD_SESSION_COMMANDS = {
    "cancel": "cancel",
    "/cancel": "cancel",
    "!cancel": "cancel",
    "recover": "recover",
    "/recover": "recover",
    "!recover": "recover",
}


def chunks(text: str, limit: int = DISCORD_LIMIT) -> list[str]:
    if limit < 1:
        raise ValueError("Discord chunk limit must be positive")
    if not text:
        text = "The agent finished without a text reply."
    pieces = []
    while text:
        if len(text) <= limit:
            pieces.append(text)
            break
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = text.rfind(" ", 0, limit)
        if cut < limit // 2:
            cut = limit
        pieces.append(text[:cut])
        text = text[cut:]
    return pieces


class DiscordAdapter:
    def __init__(self, agent: str, settings: DiscordSettings,
                 runtime: BridgeRuntime) -> None:
        self.agent = agent
        self.settings = settings
        self.runtime = runtime
        self.client: Any = None
        self._requests: dict[tuple[SessionKey, str], Any] = {}
        self._progress: dict[tuple[SessionKey, str], Any] = {}
        self._input_locks: dict[str, asyncio.Lock] = {}

    async def publish_activity(self, activity: Activity) -> bool:
        if self.client is None or not self.client.is_ready():
            return False
        import discord
        status = {
            Presence.AWAKE: discord.Status.online,
            Presence.CONNECTED: discord.Status.idle,
            Presence.DISCONNECTED: discord.Status.offline,
        }[activity.presence]
        await self.client.change_presence(
            status=status,
            activity=discord.CustomActivity(name=activity.text))
        return True

    def _approved(self, message: Any) -> bool:
        return self._approved_channel(message.channel, message.guild)

    def _approved_channel(self, channel: Any, guild: Any) -> bool:
        scope_id = int(channel.id)
        parent = getattr(channel, "parent_id", None)
        return (guild is not None and int(guild.id) == self.settings.guild_id and
                (scope_id in self.settings.channel_ids or parent in self.settings.channel_ids))

    def _wake_on_any(self, channel: Any) -> bool:
        return (int(channel.id) in self.settings.wake_on_any_channel_ids or
                getattr(channel, "parent_id", None) in self.settings.wake_on_any_channel_ids)

    async def _context(self, message: Any) -> str:
        key = SessionKey(self.agent, "discord", str(message.channel.id))
        previous_id, summary_ms = await self.runtime.last_wake(key)
        try:
            history = [item async for item in message.channel.history(
                limit=101, before=message)]
        except Exception:
            log.exception("Could not read Discord context")
            return ""
        history.reverse()
        previous_index = next((index for index, item in enumerate(history)
                               if str(item.id) == previous_id), None)
        unseen = history[previous_index + 1:] if previous_index is not None else history
        lines = []
        for item in unseen:
            if not item.content or item.author.id == self.client.user.id:
                continue
            stamp = item.created_at.astimezone(timezone.utc).isoformat(timespec="seconds")
            lines.append(f"{stamp} {item.author.display_name} ({item.author.id}) "
                         f"[{item.id}]: {item.content[:1800]}")
        selected = lines[-30:]
        while selected and len("\n".join(selected)) > 24_000:
            selected.pop(0)
        omitted = len(lines) - len(selected)
        header = []
        if summary_ms:
            header.append("[Previous private conversation summary saved " +
                          datetime.fromtimestamp(summary_ms / 1000, timezone.utc).isoformat(
                              timespec="seconds") + "]")
        if omitted:
            header.append(f"[skipping {omitted} messages]" if previous_index is not None else
                          "[older messages omitted; count unknown]")
        elif previous_id and previous_index is None:
            header.append("[earlier messages may be omitted; count unknown]")
        return "\n".join([*header, *selected])

    async def _images(self, message: Any) -> tuple[RpcImage, ...]:
        images = []
        for attachment in message.attachments[:4]:
            mime = attachment.content_type or ""
            if not mime.startswith("image/") or attachment.size > 20_000_000:
                continue
            try:
                images.append(RpcImage(mime, await attachment.read()))
            except Exception:
                log.exception("Could not read Discord image")
        return tuple(images)

    async def _on_message(self, message: Any) -> None:
        if not self._approved(message) or message.author.id == self.client.user.id:
            return
        scope = str(message.channel.id)
        async with self._input_locks.setdefault(scope, asyncio.Lock()):
            await self._deliver_message(message)

    async def _deliver_message(self, message: Any) -> None:
        key = SessionKey(self.agent, "discord", str(message.channel.id))
        mentioned = self.client.user in message.mentions
        if message.author.bot and not mentioned:
            return
        command = (message.content.replace(f"<@{self.client.user.id}>", "")
                   .replace(f"<@!{self.client.user.id}>", "").strip().lower())
        if mentioned and not message.author.bot and command in {"!debug", "!stop"}:
            if command == "!debug":
                reply = await debug_status(self.runtime, key)
            else:
                stopped = await self.runtime.stop_session(key, str(message.author.id))
                reply = ("Stopped this conversation's Pi session. Unsaved context was "
                         "discarded; the next wake uses the last saved summary and recent posts."
                         if stopped else "No running session belonging to you here.")
            await message.reply(reply, mention_author=False)
            return
        if not await self.runtime.admit(key, mentioned or self._wake_on_any(message.channel)):
            return
        if not message.author.bot:
            session_command = DISCORD_SESSION_COMMANDS.get(
                command if mentioned else message.content.strip().lower())
            if session_command == "cancel":
                cancelled = await self.runtime.cancel(key, str(message.author.id))
                await message.reply(
                    "Cancellation requested." if cancelled else
                    "No active request from you here.", mention_author=False)
                return
            if session_command == "recover":
                recovered = await self.runtime.recover(key, str(message.author.id))
                await message.reply(
                    "Recovered. The next reply starts from the last saved summary; "
                    "recent unsaved context may be missing." if recovered else
                    "No failed checkpoint belonging to you in this channel.",
                    mention_author=False)
                return
        awake = await self.runtime.is_awake(key)
        text = message.content
        if not awake:
            context = await self._context(message)
            if context:
                text = f"Recent channel posts (read before replying):\n{context}\n\nCurrent request:\n{text}"
        request_id = str(message.id)
        self._requests[(key, request_id)] = message
        incoming = Incoming(str(message.author.id), request_id,
                            f"Discord display name: {message.author.display_name}\n{text}",
                            await self._images(message), optional=not mentioned,
                            # Another bridge's bot expects no reaction.
                            ack_required=not message.author.bot)
        try:
            await self.runtime.submit(key, incoming)
            await self.runtime.record_wake(key, request_id)
        except Exception:
            log.exception("Discord request could not be submitted id=%s", request_id)
            try:
                await self.output(key, "error",
                                  "I could not start this request. If the checkpoint failed, use /bridge_recover to restart from the last saved summary.",
                                  request_id)
            except Exception:
                log.exception("Discord request error could not be delivered id=%s", request_id)
                self._requests.pop((key, request_id), None)

    async def _render_mentions(self, request: Any, text: str) -> tuple[str, list[int], list[int], bool]:
        """Resolve named users and mentionable roles; never authorize broadcasts."""
        if "@" not in text:
            return text, [], [], False
        guild = getattr(request, "guild", None)
        channel = getattr(request, "channel", None)
        members: dict[int, Any] = {}
        for source in (guild, channel):
            for member in getattr(source, "members", None) or ():
                identifier = getattr(member, "id", None)
                if isinstance(identifier, int):
                    members[identifier] = member
        roles: dict[int, Any] = {}
        for role in getattr(guild, "roles", None) or ():
            identifier = getattr(role, "id", None)
            name = getattr(role, "name", None)
            is_default = getattr(role, "is_default", None)
            if (isinstance(identifier, int) and isinstance(name, str) and name.strip()
                    and getattr(role, "mentionable", False) is True
                    and identifier != getattr(guild, "id", None)
                    and not (callable(is_default) and is_default())
                    and name.casefold() not in {"everyone", "here", "channel"}):
                roles[identifier] = role
        if not members and not roles:
            # No roster is available for name resolution. The send path still
            # supplies an empty allowlist, so raw @everyone and role tags stay silent.
            return text, [], [], False
        by_name: dict[str, int] = {}
        for user_id, member in members.items():
            for name in (getattr(member, "name", None),
                         getattr(member, "global_name", None),
                         getattr(member, "display_name", None)):
                if isinstance(name, str) and name.strip():
                    by_name.setdefault(name.casefold(), user_id)
        by_role: dict[str, int] = {}
        for role_id, role in roles.items():
            name = role.name.casefold()
            by_role.setdefault(name, role_id)
            by_role.setdefault(name.replace(" ", "-"), role_id)

        def lookup(mention: Mention) -> str | None:
            if mention.server:
                return None
            name = mention.localpart.casefold()
            if name in {"everyone", "here", "channel"}:
                return None
            if mention.localpart.isdecimal():
                return f"user:{mention.localpart}" if int(mention.localpart) in members else None
            user_id = by_name.get(name)
            role_id = by_role.get(name)
            if user_id is not None and role_id is not None:
                return None  # Ambiguous name: do not silently ping the wrong target.
            if user_id is not None:
                return f"user:{user_id}"
            return f"role:{role_id}" if role_id is not None else None

        plan = plan_mentions(text, lookup)
        if plan.unknown:
            unknown = ", ".join(f"`{mention.raw}`" for mention in plan.unknown)
            labels = sorted((getattr(member, "display_name", None) or str(user_id)
                             for user_id, member in members.items()), key=str.casefold)
            role_labels = sorted((role.name for role in roles.values()), key=str.casefold)
            return (f"I did not post that reply: it mentioned {unknown}, which is not a "
                    f"resolved person or pingable role here. People: {roster_line(labels)}. "
                    f"Roles: {roster_line(role_labels)}. Rewrite the reply using one of "
                    "those names.", [], [], True)
        if not plan.known:
            return text, [], [], False
        rendered = replace_mentions(
            text, plan, lambda _mention, target: (
                f"<@{target.removeprefix('user:')}>" if target.startswith("user:")
                else f"<@&{target.removeprefix('role:')}>"))
        users = [int(target.removeprefix("user:")) for target in plan.user_ids
                 if target.startswith("user:")]
        role_ids = [int(target.removeprefix("role:")) for target in plan.user_ids
                    if target.startswith("role:")]
        return rendered, users, role_ids, False

    async def output(self, key: SessionKey, kind: str, text: str,
                     request_id: str) -> None:
        if key.transport != "discord":
            return
        if kind == "cleanup":
            self._requests.pop((key, request_id), None)
            progress = self._progress.pop((key, request_id), None)
            if progress is not None:
                try:
                    await progress.delete()
                except Exception:
                    log.warning("Could not remove Discord progress status")
            return
        request = self._requests.get((key, request_id))
        if request is None:
            raise RuntimeError("Discord request message is unavailable")
        if kind == "steered":
            await request.reply(text, mention_author=False)
            self._requests.pop((key, request_id), None)
        elif kind == "ack":
            await request.channel.typing()
            self._progress[(key, request_id)] = await request.reply(
                text or "Working…", mention_author=False)
        elif kind == "progress":
            progress = self._progress.get((key, request_id))
            if progress is None:
                self._progress[(key, request_id)] = await request.reply(
                    text, mention_author=False)
            else:
                await progress.edit(content=text)
        else:
            mentioned: list[int] = []
            mentioned_roles: list[int] = []
            if kind == "final":
                text, mentioned, mentioned_roles, rejected = await self._render_mentions(
                    request, text)
                if rejected:
                    kind = "error"
                    mentioned = []
                    mentioned_roles = []
            import discord
            allowed_users = [discord.Object(id=user_id) for user_id in mentioned]
            allowed_roles = [discord.Object(id=role_id) for role_id in mentioned_roles]
            for index, chunk in enumerate(chunks(text)):
                allowed = discord.AllowedMentions(
                    users=allowed_users, roles=allowed_roles, everyone=False,
                    replied_user=index == 0)
                if index == 0:
                    await request.reply(chunk, mention_author=True,
                                        allowed_mentions=allowed)
                else:
                    await request.channel.send(
                        chunk, allowed_mentions=allowed)
            progress = self._progress.get((key, request_id))
            if progress is not None:
                try:
                    await progress.delete()
                except Exception:
                    log.warning("Could not remove Discord progress status")
            self._requests.pop((key, request_id), None)
            self._progress.pop((key, request_id), None)
        await self.runtime.record_activity(key)

    async def history_page(self, key: SessionKey, before: str | None,
                           limit: int) -> HistoryPage:
        if key.transport != "discord" or not key.scope.isdecimal():
            raise ValueError("Invalid Discord history scope")
        channel = self.client.get_channel(int(key.scope))
        if channel is None:
            channel = await self.client.fetch_channel(int(key.scope))
        if channel is None or not self._approved_channel(channel, channel.guild):
            raise ValueError("Discord channel is not enabled")
        import discord
        before_object = discord.Object(id=int(before)) if before else None
        items = [item async for item in channel.history(limit=limit + 1,
                                                         before=before_object)]
        has_more = len(items) > limit
        items = items[:limit]
        return {"messages": [{"id": str(item.id), "sender": str(item.author),
                              "timestamp": item.created_at.astimezone(timezone.utc).isoformat(
                                  timespec="seconds"), "content": item.content}
                             for item in items],
                "next_before": str(items[-1].id) if items and has_more else None,
                "has_more": has_more}

    async def _slash_key(self, interaction: Any) -> SessionKey | None:
        """Scope of a slash command, or an ephemeral refusal in a disabled channel."""
        channel = interaction.channel
        if channel is None or not self._approved_channel(channel, interaction.guild):
            await interaction.response.send_message("This channel is not enabled.",
                                                    ephemeral=True)
            return None
        return SessionKey(self.agent, "discord", str(channel.id))

    async def bridge_cancel_command(self, interaction: Any) -> None:
        """Slash form of the requester's `cancel`: abort only their active turn."""
        key = await self._slash_key(interaction)
        if key is None:
            return
        cancelled = await self.runtime.cancel(key, str(interaction.user.id))
        await interaction.response.send_message(
            "Cancellation requested." if cancelled else "No active request from you here.",
            ephemeral=True,
        )

    async def bridge_debug_command(self, interaction: Any) -> None:
        """Slash form of `!debug`: scope-local diagnostics without waking Pi."""
        key = await self._slash_key(interaction)
        if key is None:
            return
        await interaction.response.send_message(await debug_status(self.runtime, key),
                                                ephemeral=True)

    async def bridge_stop_command(self, interaction: Any) -> None:
        """Slash form of `!stop`: stop only this conversation's resident Pi process."""
        key = await self._slash_key(interaction)
        if key is None:
            return
        stopped = await self.runtime.stop_session(key, str(interaction.user.id))
        await interaction.response.send_message(
            "Stopped this conversation's Pi session. Unsaved context was discarded; "
            "the next wake uses the last saved summary and recent posts."
            if stopped else "No running session belonging to you in this channel.",
            ephemeral=True)

    async def bridge_recover_command(self, interaction: Any) -> None:
        """Slash form of `recover`: restart from the last saved summary."""
        key = await self._slash_key(interaction)
        if key is None:
            return
        recovered = await self.runtime.recover(key, str(interaction.user.id))
        await interaction.response.send_message(
            "Recovered. The next reply starts from the last saved summary; recent unsaved context may be missing."
            if recovered else "No failed checkpoint belonging to you in this channel.",
            ephemeral=True,
        )

    async def react(self, key: SessionKey, message_id: str, emoji: str) -> None:
        """React only to a message fetched from the active approved channel."""
        if (key.transport != "discord" or key.agent != self.agent or
                not key.scope.isdecimal() or not message_id.isdecimal()):
            raise ReactionRejected("discord_target")
        channel = self.client.get_channel(int(key.scope))
        if channel is None:
            channel = await self.client.fetch_channel(int(key.scope))
        if channel is None or not self._approved_channel(channel, channel.guild):
            raise ReactionRejected("discord_channel")
        message = await channel.fetch_message(int(message_id))
        if int(message.channel.id) != int(key.scope):
            raise ReactionRejected("discord_scope")
        await message.add_reaction(emoji)
        await self.runtime.record_activity(key)

    async def run(self) -> None:
        import discord  # Optional for Matrix-only installations.

        intents = discord.Intents.default()
        intents.message_content = True
        client = discord.Client(intents=intents)
        tree = discord.app_commands.CommandTree(client)
        self.client = client

        @client.event
        async def on_message(message: discord.Message) -> None:
            try:
                await self._on_message(message)
            except Exception:
                log.exception("Discord message delivery failed")

        @client.event
        async def on_ready() -> None:
            log.info("Discord connected for %s", self.agent)
            try:
                await tree.sync(guild=discord.Object(id=self.settings.guild_id))
            except Exception:
                log.exception("Discord command sync failed")

        guild = discord.Object(id=self.settings.guild_id)
        for name, description, handler in (
                ("bridge_cancel", "Cancel your active agent request",
                 self.bridge_cancel_command),
                ("bridge_debug", "Show bridge uptime and this conversation's status",
                 self.bridge_debug_command),
                ("bridge_stop", "Stop your conversation's Pi session immediately",
                 self.bridge_stop_command),
                ("bridge_recover", "Restart your conversation after a failed checkpoint",
                 self.bridge_recover_command)):
            tree.command(name=name, description=description, guild=guild)(handler)

        import os
        await client.start(os.environ[self.settings.token_env])

    async def stop(self) -> None:
        if self.client is not None:
            await self.client.close()
