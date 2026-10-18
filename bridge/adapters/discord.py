"""Discord output and mention routing for Agent_two's shared bridge profile."""

from __future__ import annotations

import asyncio
import logging
import json
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from bridge import __version__
from bridge.config import DiscordSettings
from bridge.activity import Activity, Presence
from bridge.filtering import DISCORD_META_PREFIX, discord_metadata, metadata_rejection
from bridge.telemetry import Telemetry
from bridge.turn_status import Indicator, TURN_EMOJI, TurnStatus
from bridge.progress import sealed_progress
from bridge.history_service import HistoryPage
from bridge.mentions import (BROADCAST_REJECTION, Member, Roster, find_mentions, has_broadcast_ping,
                             replace_mentions)
from bridge.reaction_service import ReactionRejected
from bridge.debug import debug_status
from bridge.modes import ModeSwitchError, mode_command
from bridge.runtime.pi_rpc import RpcImage
from bridge.runtime.session import BridgeRuntime, Incoming, ReplyRejected, StopRequested
from bridge.runtime.state import SessionKey


log = logging.getLogger("agent_bridge.discord")
DISCORD_LIMIT = 1900
DISCORD_EPOCH_MS = 1_420_070_400_000


def _snowflake_at(moment: datetime) -> int:
    return max(0, int(moment.timestamp() * 1000) - DISCORD_EPOCH_MS) << 22

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
        telemetry = getattr(runtime, "telemetry", None)
        self.telemetry = telemetry if isinstance(telemetry, Telemetry) else Telemetry()
        self._scope_hops: dict[SessionKey, int] = {}
        self._seen: dict[int, None] = {}
        self.client: Any = None
        self._requests: dict[tuple[SessionKey, str], Any] = {}
        self._status_requests: dict[tuple[SessionKey, str, int], Any] = {}
        self._progress: dict[tuple[SessionKey, str], Any] = {}
        self._summary_cards: dict[int, str] = {}
        self._progress_headers: dict[tuple[SessionKey, str], str] = {}
        self._input_locks: dict[str, asyncio.Lock] = {}
        self._startup_sent = False
        self._recovery_ready = asyncio.Event()
        self._recovery_ready.set()  # Direct adapter calls outside run() remain usable.
        self._recovery_lock = asyncio.Lock()
        self._blocked_channels: set[int] = set()
        self._recovery_floor = 0
        self._gap_alert_pending = False
        self._blocked_thread_parents: set[int] = set()
        self._retry_task: asyncio.Task[None] | None = None
        self._retry_delay = 30
        self._hold_failures: dict[tuple[str, int], int] = {}

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
            meta = discord_metadata(item)
            if (not item.content or item.author.id == self.client.user.id or
                    metadata_rejection(meta, awake=True, addressed=False, wake_any=False)):
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

    async def _on_message(self, message: Any, *, recovering: bool = False) -> None:
        if not self._approved(message):
            return
        blocked = False
        if not recovering:
            await self._recovery_ready.wait()
            blocked = (int(message.channel.id) in self._blocked_channels or
                       getattr(message.channel, "parent_id", None) in
                       (self._blocked_channels | self._blocked_thread_parents))
            if blocked:
                # Keep the requester's escape hatch available even when history
                # cannot be inspected; a control never advances the gap cursor.
                control = (message.content.replace(f"<@{self.client.user.id}>", "")
                           .replace(f"<@!{self.client.user.id}>", "").strip().lower())
                if message.author.bot or control not in {"recover", "/recover", "!recover",
                                                             "!debug", "!stop"}:
                    return
        scope = str(message.channel.id)
        key = SessionKey(self.agent, "discord", scope)
        async with self.telemetry.queued(self._input_locks.setdefault(scope, asyncio.Lock()), "discord",
                                        key.scope_hash):
            if message.id in self._seen:
                self.telemetry.record("discord", "filter", "duplicate")
                return
            recovery_state = getattr(self.runtime, "discord_recovery_state", None)
            if recovery_state is not None:
                observed, accepted, _ = await recovery_state(key)
                watermark = max((int(value) for value in (observed, accepted) if value),
                                default=0)
                if (recovering and int(message.id) <= watermark or
                        not recovering and int(message.id) < self._recovery_floor):
                    self.telemetry.record("discord", "filter", "duplicate")
                    return
            if message.author.id != self.client.user.id:
                if await self._deliver_message(message) is False:
                    return  # A failed submit is not an inspected/admitted message.
            record_observed = getattr(self.runtime, "record_discord_observed", None)
            if not blocked and record_observed is not None:
                await record_observed(key, str(message.id))
            self._seen[message.id] = None
            if len(self._seen) > 10_000:
                self._seen.pop(next(iter(self._seen)))

    async def _deliver_message(self, message: Any) -> bool:
        key = SessionKey(self.agent, "discord", str(message.channel.id))
        meta = discord_metadata(message)
        awake = await self.runtime.is_awake(key) if message.author.bot else False
        invitations = meta.get("invitations", []) if meta else []
        intentional = isinstance(invitations, list) and str(self.client.user.id) in invitations
        reason = metadata_rejection(meta, awake=awake, addressed=intentional,
                                    wake_any=self._wake_on_any(message.channel))
        if reason:
            self.telemetry.record("discord", "filter", reason, scope_hash=key.scope_hash)
            return True
        roster = self._roster(message.channel, message.guild, self.client.user)
        self_id = f"<@{self.client.user.id}>"
        plan = roster.plan(message.content)
        mentioned = self.client.user in message.mentions or self_id in plan.user_ids
        command_body = (message.content.replace(f"<@{self.client.user.id}>", "")
                        .replace(f"<@!{self.client.user.id}>", "").strip())
        leading = next((mention for mention, target in plan.known
                        if target == self_id and not message.content[:mention.start].strip()), None)
        if leading is not None:
            command_body = message.content[leading.end:].lstrip(" :,\n\t")
        command = command_body.lower()
        if mentioned and not message.author.bot and command in {"!debug", "!stop"}:
            if command == "!debug":
                reply = await debug_status(self.runtime, key)
            else:
                stopped = await self.runtime.stop_session(key, str(message.author.id))
                reply = ("Stopped this conversation's Pi session. Unsaved context was "
                         "discarded; the next wake uses the last saved summary and recent posts."
                         if stopped else "No running session belonging to you here.")
            await message.reply(reply, mention_author=False)
            return True
        async def compaction_notice() -> None:
            await message.reply("Compacting, one moment...", mention_author=False)

        if not await self.runtime.admit(key, mentioned or self._wake_on_any(message.channel),
                                        on_wait=compaction_notice):
            return True
        if not message.author.bot:
            session_command = DISCORD_SESSION_COMMANDS.get(
                command if mentioned else message.content.strip().lower())
            if session_command == "cancel":
                cancelled = await self.runtime.cancel(key, str(message.author.id))
                await message.reply(
                    "Cancellation requested." if cancelled else
                    "No active request from you here.", mention_author=False)
                return True
            if session_command == "recover":
                recovered = await self.runtime.recover(key, str(message.author.id))
                await message.reply(
                    "Recovered. The next reply starts from the last saved summary; "
                    "recent unsaved context may be missing." if recovered else
                    "No failed checkpoint belonging to you in this channel.",
                    mention_author=False)
                return True
        awake = await self.runtime.is_awake(key)
        if meta:
            hop = meta.get("hop", 0)
            self._scope_hops[key] = max(self._scope_hops.get(key, 0), hop) if awake else hop
        elif not awake:
            self._scope_hops[key] = 0
        text = message.content
        selected_mode = None
        if not message.author.bot:
            mode_body = (message.content[leading.end:].lstrip(" :,\n\t")
                         if leading is not None else message.content)
            selection = mode_command(mode_body,
                                     getattr(self.runtime, "modes", {}))
            if selection is not None:
                selected_mode, text = selection
                if not text:
                    try:
                        notice = await self.runtime.change_mode(key, selected_mode, str(message.author.id))
                    except StopRequested:
                        return True  # The queued stop/cancel owns its acknowledgment.
                    except ModeSwitchError as error:
                        notice = str(error)
                    await message.reply(notice, mention_author=False,
                                        embed=self._metadata_embed("mode"))
                    return True
        if not awake:
            context = await self._context(message)
            if context:
                text = f"Recent channel posts (read before replying):\n{context}\n\nCurrent request:\n{text}"
        request_id = str(message.id)
        self._requests[(key, request_id)] = message
        incoming = Incoming(str(message.author.id), request_id,
                            f"Discord display name: {message.author.display_name}\n{text}",
                            await self._images(message), optional=not mentioned and selected_mode is None,
                            # Another bridge's bot expects no reaction.
                            ack_required=not message.author.bot, mode=selected_mode)
        try:
            await self.runtime.submit(key, incoming)
            await self.runtime.record_wake(key, request_id)
        except StopRequested:
            self._requests.pop((key, request_id), None)
            return True
        except ModeSwitchError as error:
            await message.reply(str(error), mention_author=False)
            self._requests.pop((key, request_id), None)
            return True
        except Exception:
            log.exception("Discord request could not be submitted id=%s", request_id)
            try:
                await self.output(key, "error",
                                  "I could not start this request. If the checkpoint failed, use /bridge_recover to restart from the last saved summary.",
                                  request_id)
            except Exception:
                log.exception("Discord request error could not be delivered id=%s", request_id)
                self._requests.pop((key, request_id), None)
            return False
        return True

    def _broadcast_allowed(self, channel: Any) -> bool:
        return (getattr(channel, "id", None) in self.settings.broadcast_allowed_channel_ids or
                getattr(channel, "parent_id", None) in self.settings.broadcast_allowed_channel_ids)

    def _roster(self, channel: Any, guild: Any, author: Any = None) -> Roster:
        members: dict[int, Any] = {}
        for source in (guild, channel):
            for member in getattr(source, "members", None) or ():
                if type(getattr(member, "id", None)) is int:
                    members[member.id] = member
        if author is not None and type(getattr(author, "id", None)) is int:
            members[author.id] = author
        targets = []
        for identifier, member in members.items():
            names = tuple(dict.fromkeys(name for name in (
                str(identifier), "!" + str(identifier), getattr(member, "name", None),
                getattr(member, "global_name", None), getattr(member, "display_name", None))
                if isinstance(name, str) and name))
            targets.append(Member(f"<@{identifier}>", getattr(member, "name", None) or str(identifier), names))
        for role in getattr(guild, "roles", None) or ():
            if (type(getattr(role, "id", None)) is not int or not getattr(role, "mentionable", False)
                    or role.id == getattr(guild, "id", None)
                    or (callable(getattr(role, "is_default", None)) and role.is_default())
                    or not isinstance(getattr(role, "name", None), str)
                    or role.name.casefold() in {"everyone", "here", "channel", "room"}):
                continue
            targets.append(Member(f"<@&{role.id}>", role.name,
                                  ("&" + str(role.id), role.name, role.name.replace(" ", "-")), "role"))
        complete = bool(getattr(guild, "chunked", False))
        return Roster(targets, complete=complete, available=bool(targets))

    async def member_roster(self, key: SessionKey) -> Roster:
        if key.transport != "discord" or not key.scope.isdecimal():
            raise ValueError("Invalid Discord member scope")
        channel = self.client.get_channel(int(key.scope)) or await self.client.fetch_channel(int(key.scope))
        if channel is None or not self._approved_channel(channel, channel.guild):
            raise ValueError("Discord channel is not enabled")
        return self._roster(channel, channel.guild)

    async def _render_mentions(self, request: Any, text: str) -> tuple[str, list[int], list[int], bool]:
        if "@" not in text:
            return text, [], [], False
        channel = getattr(request, "channel", None)
        roster = self._roster(channel, getattr(request, "guild", None), getattr(request, "author", None))
        plan = roster.plan(text, allow_broadcast=self._broadcast_allowed(channel))
        feedback = roster.feedback(plan)
        if feedback:
            self.telemetry.record("discord", "mention", "rejected")
            return feedback, [], [], True
        users = [int(target[2:-1]) for target in plan.user_ids if not target.startswith("<@&")]
        roles = [int(target[3:-1]) for target in plan.user_ids if target.startswith("<@&")]
        return replace_mentions(text, plan, lambda _mention, target: target), users, roles, False

    def _metadata_embed(self, kind: str, hop: int = 0, invitations: list[str] | None = None) -> Any:
        import discord
        embed = discord.Embed()
        embed.set_footer(text=DISCORD_META_PREFIX + json.dumps(
            {"kind": kind, "hop": hop, "invitations": invitations or []}, separators=(",", ":")))
        return embed

    async def _seal_progress(self, key: SessionKey, request_id: str, outcome: str) -> None:
        progress = self._progress.pop((key, request_id), None)
        header = self._progress_headers.pop((key, request_id), "")
        if progress is not None:
            history = self._summary_cards.pop(progress.id, "") if self._summary_cards else ""
            if outcome == "continued":
                return  # Handoff/rotation leaves the last visible card untouched.
            try:
                if history:
                    import discord
                    await asyncio.wait_for(progress.edit(
                        content=sealed_progress(header, history, outcome),
                        embed=self._metadata_embed("progress_summary"),
                        allowed_mentions=discord.AllowedMentions.none()), 2)
                else:
                    await asyncio.wait_for(progress.delete(), 2)
            except Exception:
                log.warning("Could not seal Discord progress status")

    async def turn_status(self, record: Indicator, status: TurnStatus) -> str | None:
        """Own reactions only; Discord has no atomic replace-reaction endpoint."""
        key, request_id = record.key, record.request_id
        if (key.transport != "discord" or key.agent != self.agent or
                not key.scope.isdecimal() or not request_id.isdecimal()):
            raise ReactionRejected("discord_target")
        request = (self._status_requests.get(record.target)
                   or self._requests.get((key, request_id)))
        if request is None:
            request = await self._reaction_message(key, request_id)
        if str(request.channel.id) != key.scope:
            raise ReactionRejected("discord_scope")
        self._status_requests[record.target] = request
        if status == "running":
            await request.add_reaction(TURN_EMOJI[status])
            return request_id
        if status != "clear":
            await request.add_reaction(TURN_EMOJI[status])
        if record.pending_attempted:
            import discord
            try:
                await request.remove_reaction(TURN_EMOJI["running"], self.client.user)
            except discord.NotFound:
                # Recovery may repeat an already successful removal, or the
                # pending add may never have reached Discord. Both are clear.
                pass
        self._status_requests.pop(record.target, None)
        return None

    async def output(self, key: SessionKey, kind: str, text: str,
                     request_id: str) -> bool | None:
        if key.transport != "discord":
            return None
        import discord
        request_error = kind == "request_error"
        if request_error:
            kind = "error"
        if kind == "cleanup_request":
            self._requests.pop((key, request_id), None)
            return None  # An ambient steer does not finish the active turn.
        if kind == "cleanup":
            for target in [target for target in self._requests if target[0] == key]:
                self._requests.pop(target, None)
            for target in [target for target in self._progress if target[0] == key]:
                await self._seal_progress(*target, "finished")
            return None
        request = self._requests.get((key, request_id))
        if request is None:
            raise RuntimeError("Discord request message is unavailable")
        broadcast_allowed = self._broadcast_allowed(getattr(request, "channel", None))
        if not broadcast_allowed and has_broadcast_ping(text):
            raise ReplyRejected(BROADCAST_REJECTION)
        if kind in {"compacting", "mode"}:
            await request.reply(text, mention_author=False, embed=self._metadata_embed(kind),
                                allowed_mentions=discord.AllowedMentions.none())
            return None  # Non-terminal notice; keep the request for its later reply.
        if kind == "steered":
            card = await request.reply(text, mention_author=False, embed=self._metadata_embed(kind),
                                       allowed_mentions=discord.AllowedMentions.none())
            for target in [target for target in self._progress if target[0] == key]:
                await self._seal_progress(*target, "continued")
            self._progress[(key, request_id)] = card
            self._progress_headers[(key, request_id)] = text
        elif kind == "ack":
            header = self._progress_headers.get((key, request_id), "")
            text = f"{header}\n{text}" if header else text
            await request.channel.typing()
            card = await request.reply(
                text or "Working…", mention_author=False, embed=self._metadata_embed(kind),
                allowed_mentions=discord.AllowedMentions.none())
            await self._seal_progress(key, request_id, "continued")
            self._progress[(key, request_id)] = card
            if header:
                self._progress_headers[(key, request_id)] = header
        elif kind in {"progress", "progress_summary"}:
            history = text.partition("\n")[2]
            header = self._progress_headers.get((key, request_id), "")
            text = f"{header}\n{text}" if header else text
            progress = self._progress.get((key, request_id))
            if progress is None:
                self._progress[(key, request_id)] = await request.reply(
                    text, mention_author=False, embed=self._metadata_embed(kind),
                    allowed_mentions=discord.AllowedMentions.none())
            else:
                await progress.edit(content=text, embed=self._metadata_embed(kind),
                                    allowed_mentions=discord.AllowedMentions.none())
            if kind == "progress_summary":
                self._summary_cards[self._progress[(key, request_id)].id] = history
        else:
            mentioned: list[int] = []
            mentioned_roles: list[int] = []
            if kind == "final":
                text, mentioned, mentioned_roles, rejected = await self._render_mentions(
                    request, text)
                if rejected:
                    # Keep membership validation private to the active Pi
                    # session. The runtime asks for a rewrite before any post.
                    raise ReplyRejected(text)
            import discord
            allowed_users = [discord.Object(id=user_id) for user_id in mentioned]
            allowed_roles = [discord.Object(id=role_id) for role_id in mentioned_roles]
            broadcasts = [(mention.start, mention.end) for mention in find_mentions(text)
                          if mention.server is None and mention.localpart.casefold() in {"everyone", "here"}]
            hop = min(self._scope_hops.get(key, 0) + 1, 3) if kind == "final" else self._scope_hops.get(key, 0)
            invitations = []
            cursor = 0
            if kind == "final" and self._scope_hops.get(key, 0) < 3:
                for mention in find_mentions(text):
                    if text[cursor:mention.start].strip(" \t\n,;([{\"'"):
                        break
                    if mention.raw.startswith("<@") and not mention.raw.startswith("<@&"):
                        invitations.append(mention.raw[2:-1])
                    cursor = mention.end
            embed = self._metadata_embed(kind, hop, invitations)
            offset = 0
            for index, chunk in enumerate(chunks(text)):
                allowed = discord.AllowedMentions(
                    users=allowed_users, roles=allowed_roles,
                    everyone=(kind == "final" and broadcast_allowed and any(
                        offset <= start and end <= offset + len(chunk) for start, end in broadcasts)),
                    replied_user=index == 0)
                if index == 0:
                    await request.reply(chunk, mention_author=True,
                                        allowed_mentions=allowed, embed=embed)
                else:
                    await request.channel.send(
                        chunk, allowed_mentions=allowed, embed=embed)
                offset += len(chunk)
            for target in [target for target in self._progress if target[0] == key and
                           (not request_error or target[1] == request_id)]:
                await self._seal_progress(*target, "finished" if kind == "final" else kind)
            for target in [target for target in self._requests if target[0] == key and
                           (not request_error or target[1] == request_id)]:
                self._requests.pop(target, None)
        await self.runtime.record_activity(key)
        return kind == "final"

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
        message = await self._reaction_message(key, message_id)
        await message.add_reaction(emoji)
        await self.runtime.record_activity(key)

    async def _reaction_message(self, key: SessionKey, message_id: str) -> Any:
        """Fetch only from an active approved channel (also used after restart)."""
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
        return message

    async def _recovery_scopes(self, deadline: float | None = None) -> tuple[list[Any], set[int], set[int]]:
        """Bound every REST call; one slow listing cannot erase completed discovery."""
        def budget(seconds: float) -> float:
            return seconds if deadline is None else min(seconds, max(0, deadline - time.monotonic()))

        blocked: set[int] = set()
        thread_blind: set[int] = set()
        threads = []
        try:
            guild = self.client.get_guild(self.settings.guild_id)
            if guild is None:
                guild = await asyncio.wait_for(self.client.fetch_guild(self.settings.guild_id), budget(10))
            active = await asyncio.wait_for(guild.active_threads(), budget(10))
            threads = [thread for thread in active
                       if getattr(thread, "parent_id", None) in self.settings.channel_ids]
        except Exception as error:
            thread_blind.update(self.settings.channel_ids)
            log.warning("Discord active-thread discovery paused cause=%s", type(error).__name__)
        parents = []
        for channel_id in self.settings.channel_ids:
            try:
                channel = self.client.get_channel(channel_id)
                if channel is None:
                    channel = await asyncio.wait_for(self.client.fetch_channel(channel_id), budget(5))
                if not self._approved_channel(channel, getattr(channel, "guild", None)):
                    raise RuntimeError("Discord recovery parent is outside the configured guild")
                parents.append(channel)
            except Exception as error:
                blocked.add(channel_id)
                log.warning("Discord parent recovery discovery paused scope_hash=%s cause=%s",
                            SessionKey(self.agent, "discord", str(channel_id)).scope_hash,
                            type(error).__name__)
        for channel in parents:
            if hasattr(channel, "archived_threads"):
                try:
                    async def archived() -> list[Any]:
                        return [thread async for thread in channel.archived_threads(limit=101)]
                    listing = await asyncio.wait_for(archived(), budget(5))
                    if len(listing) > 100:
                        raise RuntimeError("Discord archived-thread enumeration exceeds 100 per parent")
                    threads.extend(thread for thread in listing
                                   if self._approved_channel(thread, getattr(thread, "guild", None)))
                except Exception as error:
                    thread_blind.add(int(channel.id))
                    log.warning("Discord archived-thread discovery paused scope_hash=%s cause=%s",
                                SessionKey(self.agent, "discord", str(channel.id)).scope_hash,
                                type(error).__name__)
        scopes = {int(channel.id): channel for channel in (*parents, *threads)
                  if int(channel.id) not in blocked and
                  getattr(channel, "parent_id", None) not in blocked}
        ordered = sorted(scopes.values(), key=lambda channel: int(
            getattr(channel, "last_message_id", None) or channel.id), reverse=True)
        return ordered, blocked, thread_blind

    async def _recover_scope(self, channel: Any, deadline: float | None = None,
                             *, preserve_gap: bool = False) -> None:
        """Best-effort reconstruction, not a replay of edits or deleted messages."""
        import discord
        key = SessionKey(self.agent, "discord", str(channel.id))
        observed, accepted, status = await self.runtime.discord_recovery_state(key)
        if status == "checkpoint_error":
            raise RuntimeError("Discord scope needs checkpoint recovery")
        parent_id = getattr(channel, "parent_id", None)
        if not observed and not accepted and parent_id in self.settings.channel_ids:
            parent = SessionKey(self.agent, "discord", str(parent_id))
            parent_observed, _, _ = await self.runtime.discord_recovery_state(parent)
            if parent_observed:
                observed = str(max(int(parent_observed), int(channel.id)))
        # An old checkpoint predating this feature is not a delivery cursor.
        # Once an observation exists, the accepted ID also closes the crash
        # window after Pi admission but before the observation was persisted.
        cursor = (max(int(observed), int(accepted)) if observed and accepted else
                  int(observed) if observed else self._recovery_floor)
        cutoff = _snowflake_at(datetime.now(timezone.utc) - timedelta(hours=24))
        reset = False
        if not observed:
            reason = "first_enable" if accepted else "fresh_install"
            skipped = f"unknown since={accepted}" if accepted else "unknown"
            if await self.runtime.record_discord_gap(key, reason, cursor, skipped):
                self._gap_alert_pending = True
                log.warning("Discord baseline_reset scope_hash=%s reason=%s boundary=%d skipped=%s",
                            key.scope_hash, reason, cursor, skipped)
            reset = True
        if cursor < cutoff:
            if await self.runtime.record_discord_gap(key, "age", cutoff, "unknown"):
                self._gap_alert_pending = True
                log.warning("Discord baseline_reset scope_hash=%s reason=age boundary=%d skipped=unknown",
                            key.scope_hash, cutoff)
            cursor = cutoff
            reset = True
        # The first release has no observation watermark. The process-start
        # snowflake is the no-history baseline, not an invitation to old work.
        await self.runtime.record_discord_observed(key, str(cursor))
        after = discord.Object(id=cursor)
        async def fetch() -> list[Any]:
            return [message async for message in channel.history(
                after=after, limit=301, oldest_first=False)]
        remaining = None if deadline is None else deadline - time.monotonic()
        if remaining is not None and remaining <= 0:
            raise TimeoutError("Discord recovery fetch budget elapsed")
        messages = (await fetch() if remaining is None else
                    await asyncio.wait_for(fetch(), timeout=remaining))
        if len(messages) > 300:
            messages = messages[:300]
            boundary = min(int(message.id) for message in messages)
            if await self.runtime.record_discord_gap(key, "count", boundary, ">=1"):
                self._gap_alert_pending = True
                log.warning("Discord baseline_reset scope_hash=%s reason=count boundary=%d skipped>=1",
                            key.scope_hash, boundary)
            await self.runtime.record_discord_observed(key, str(boundary - 1))
            reset = True
        for message in sorted(messages, key=lambda item: int(item.id)):
            if deadline is not None and time.monotonic() >= deadline:
                # Never cancel a Pi admission merely to meet the REST budget.
                # The persisted watermark resumes this scope on the next READY.
                raise TimeoutError("Discord recovery admission budget elapsed")
            await self._on_message(message, recovering=True)
        if not reset and not preserve_gap:
            await self.runtime.clear_discord_gap(key)

    async def _reconcile_ready(self, *, retry: bool = False) -> None:
        """Hold live events until older REST history has entered each scope."""
        async with self._recovery_lock:
            self._recovery_ready.clear()
            prior_blocked = set(self._blocked_channels) if retry else set()
            prior_blind = set(self._blocked_thread_parents) if retry else set()
            blocked: set[int] = set()
            thread_blind: set[int] = set()
            failed_inspection: set[int] = set()
            self._gap_alert_pending = False
            deadline = time.monotonic() + 60
            try:
                scopes, discovery_blocked, thread_blind = await self._recovery_scopes(deadline)
                blocked.update(discovery_blocked)
                failed_inspection.update(discovery_blocked)
                if retry:
                    discovered = {int(channel.id) for channel in scopes}
                    missing_held = prior_blocked - discovered
                    blocked.update(missing_held)
                    failed_inspection.update(missing_held)
                    pending = prior_blocked | prior_blind
                    scopes = [channel for channel in scopes
                              if int(channel.id) in pending or
                              getattr(channel, "parent_id", None) in prior_blind]
                for channel in scopes[10:]:
                    blocked.add(int(channel.id))
                    key = SessionKey(self.agent, "discord", str(channel.id))
                    if await self.runtime.record_discord_gap(key, "limit", 0, "held"):
                        self._gap_alert_pending = True
                for channel in scopes[:10]:
                    try:
                        if time.monotonic() >= deadline:
                            raise TimeoutError("Discord recovery budget elapsed")
                        failed_inspection.add(int(channel.id))
                        await self._recover_scope(
                            channel, deadline, preserve_gap=int(channel.id) in thread_blind)
                    except Exception as error:
                        blocked.add(int(channel.id))
                        status = getattr(error, "status", None)
                        cause = ("permission" if status == 403 else
                                 "not_found" if status == 404 else
                                 "transient" if status == 429 or
                                 isinstance(status, int) and status >= 500 or
                                 isinstance(error, TimeoutError) else
                                 type(error).__name__)
                        key = SessionKey(self.agent, "discord", str(channel.id))
                        log.warning("Discord recovery paused scope_hash=%s cause=%s",
                                    key.scope_hash, cause)
                        marker_reason = cause if cause in {"permission", "not_found", "transient"} else "error"
                        if await self.runtime.record_discord_gap(key, marker_reason, 0, "held"):
                            self._gap_alert_pending = True
                for channel_id in discovery_blocked:
                    key = SessionKey(self.agent, "discord", str(channel_id))
                    if await self.runtime.record_discord_gap(key, "discovery", 0, "held"):
                        self._gap_alert_pending = True
                for channel_id in thread_blind - blocked:
                    key = SessionKey(self.agent, "discord", str(channel_id))
                    if await self.runtime.record_discord_gap(key, "thread_discovery", 0, "held"):
                        self._gap_alert_pending = True
            except Exception as error:
                blocked.update(self.settings.channel_ids)
                failed_inspection.update(self.settings.channel_ids)
                log.warning("Discord recovery discovery failed cause=%s", type(error).__name__)
                for channel_id in self.settings.channel_ids:
                    key = SessionKey(self.agent, "discord", str(channel_id))
                    if await self.runtime.record_discord_gap(key, "discovery", 0, "held"):
                        self._gap_alert_pending = True
            finally:
                try:
                    await self._bound_recovery_holds(blocked, thread_blind, failed_inspection)
                finally:
                    self._blocked_channels = blocked
                    self._blocked_thread_parents = thread_blind
                    self._recovery_ready.set()
            if blocked or thread_blind:
                log.warning("Discord recovery has %d paused scope(s), %d thread-blind parent(s)",
                            len(blocked), len(thread_blind))
            if self._gap_alert_pending and self.settings.startup_channel_id is not None:
                try:
                    import discord
                    channel_id = self.settings.startup_channel_id
                    channel = self.client.get_channel(channel_id) or await self.client.fetch_channel(channel_id)
                    await channel.send(
                        "agent-bridge Discord catch-up skipped or paused a scope; inspect private bridge logs.",
                        allowed_mentions=discord.AllowedMentions.none(),
                        embed=self._metadata_embed("recovery_alert"))
                except Exception:
                    log.warning("Discord recovery alert could not be delivered")
            if blocked or thread_blind:
                if self._retry_task is None or self._retry_task.done():
                    self._retry_task = asyncio.create_task(self._retry_recovery())
            else:
                self._retry_delay = 30
                if self._retry_task is not None and self._retry_task is not asyncio.current_task():
                    self._retry_task.cancel()

    async def _bound_recovery_holds(self, blocked: set[int], thread_blind: set[int],
                                    failed_inspection: set[int]) -> None:
        """Expire only failed inspections, never scopes merely waiting behind the cap."""
        present = {("channel", channel_id) for channel_id in blocked}
        present.update(("thread", parent_id) for parent_id in thread_blind - blocked)
        for token in tuple(self._hold_failures):
            if token not in present:
                del self._hold_failures[token]
        attempted = {("channel", channel_id) for channel_id in blocked & failed_inspection}
        attempted.update(("thread", parent_id) for parent_id in thread_blind - blocked)
        for kind, channel_id in sorted(attempted):
            token = (kind, channel_id)
            failures = self._hold_failures.get(token, 0) + 1
            if failures < 3:
                self._hold_failures[token] = failures
                continue
            key = SessionKey(self.agent, "discord", str(channel_id))
            boundary = _snowflake_at(datetime.now(timezone.utc))
            reason = "thread_hold_expired" if kind == "thread" else "hold_expired"
            if await self.runtime.record_discord_gap(key, reason, boundary, "unknown after=3"):
                self._gap_alert_pending = True
                log.warning("Discord baseline_reset scope_hash=%s reason=%s boundary=%d skipped=unknown",
                            key.scope_hash, reason, boundary)
            await self.runtime.record_discord_observed(key, str(boundary))
            if kind == "thread":
                thread_blind.discard(channel_id)
            else:
                blocked.discard(channel_id)
            del self._hold_failures[token]

    async def _retry_recovery(self) -> None:
        """Retry held scopes without waiting for another gateway reconnect."""
        while self._blocked_channels or self._blocked_thread_parents:
            delay = self._retry_delay
            log.warning("Discord recovery retry scheduled in %d seconds", delay)
            await asyncio.sleep(delay)
            self._retry_delay = min(delay * 2, 300)
            try:
                await self._reconcile_ready(retry=True)
            except Exception:
                log.exception("Discord recovery retry failed")

    async def _announce_startup(self) -> None:
        channel_id = self.settings.startup_channel_id
        if channel_id is None or self._startup_sent:
            return
        import discord
        channel = self.client.get_channel(channel_id) or await self.client.fetch_channel(channel_id)
        if getattr(getattr(channel, "guild", None), "id", None) != self.settings.guild_id:
            raise RuntimeError("Discord startup channel is not in the configured guild")
        # Discord's send API has no enforced idempotency key. Do not risk a
        # duplicate after an ambiguous response on reconnect.
        self._startup_sent = True
        await channel.send(
            f"agent-bridge v{__version__} connected — {self.agent} (bot {self.client.user.id})",
            allowed_mentions=discord.AllowedMentions.none(), embed=self._metadata_embed("startup"))

    async def run(self) -> None:
        import discord  # Optional for Matrix-only installations.

        intents = discord.Intents.default()
        intents.message_content = True
        client = discord.Client(intents=intents)
        tree = discord.app_commands.CommandTree(client)
        self.client = client

        self._recovery_floor = _snowflake_at(datetime.now(timezone.utc))
        self._recovery_ready.clear()

        @client.event
        async def on_message(message: discord.Message) -> None:
            try:
                await self._on_message(message)
            except Exception:
                log.exception("Discord message delivery failed")

        @client.event
        async def on_ready() -> None:
            log.info("Discord connected for %s", self.agent)
            await self._reconcile_ready()
            try:
                await tree.sync(guild=discord.Object(id=self.settings.guild_id))
            except Exception:
                log.exception("Discord command sync failed")
            try:
                await self._announce_startup()
            except Exception:
                log.exception("Discord startup notice failed")

        @client.event
        async def on_resumed() -> None:
            await self._reconcile_ready()

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
        try:
            await client.start(os.environ[self.settings.token_env])
        finally:
            if self._retry_task is not None:
                self._retry_task.cancel()

    async def stop(self) -> None:
        if self._retry_task is not None:
            self._retry_task.cancel()
        if self.client is not None:
            await self.client.close()
