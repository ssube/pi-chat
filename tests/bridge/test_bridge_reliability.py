"""Paired transport contracts and deterministic concurrency regressions."""
import asyncio
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bridge.filtering import SILENT_KINDS, metadata_rejection
from bridge.mentions import Member, Roster
from bridge.runtime.pi_rpc import PiRpcError, RpcImage
from bridge.runtime.session import BridgeRuntime, Incoming, ReplyRejected
from bridge.runtime.state import SessionKey, read_checkpoint
from reliability_helpers import ControlledRpc, Harness

TRANSPORTS = ("matrix", "discord")


class TransportContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_awake_scope_rejects_uninvited_saturated_handoff_on_both_transports(self):
        for transport in TRANSPORTS:
            with self.subTest(transport=transport), tempfile.TemporaryDirectory() as directory:
                h = Harness(Path(directory), transport)
                self.addAsyncCleanup(h.close)
                await h.deliver(h.event(1, "Start a conversation"))
                await h.idle()
                await h.deliver(h.event(2, "completed", ping=False,
                                        meta={"kind": "final", "hop": 3, "invitations": []},
                                        peer=True))
                self.assertEqual(len(h.clients[0].prompts), 1)
                self.assertEqual(h.runtime.telemetry.counts[transport, "filter", "hop_limit"], 1)

    async def test_discord_silent_metadata_is_filtered_before_roster_scan(self):
        with tempfile.TemporaryDirectory() as directory:
            h = Harness(Path(directory), "discord")
            self.addAsyncCleanup(h.close)
            with patch.object(h.adapter, "_roster", side_effect=AssertionError("roster scan")):
                await h.deliver(h.event(1, "working", ping=False,
                                        meta={"kind": "progress", "hop": 0}, peer=True))
            self.assertEqual(h.runtime.telemetry.counts["discord", "filter", "machine_status"], 1)

    async def test_two_agents_exchange_substantive_results_with_bounded_handoffs(self):
        from bridge.filtering import discord_metadata
        from bridge.adapters.matrix import BRIDGE_META

        for transport in TRANSPORTS:
            with self.subTest(transport=transport), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                first, second = Harness(root / "first", transport), Harness(root / "second", transport)
                self.addAsyncCleanup(first.close)
                self.addAsyncCleanup(second.close)
                if transport == "matrix":
                    second.adapter.settings = replace(second.adapter.settings, user_id="@bob:example.test")
                    members = {"@bridge:example.test": {}, "@bob:example.test": {}, "@alice:example.test": {}}
                    for h in (first, second):
                        h.adapter._rosters[h.room] = (float("inf"), members)
                    answers = ("@bob please check this result.", "@bridge checked; here is my answer.")
                else:
                    first.adapter.client.user.name = "bridge"
                    second.adapter.client.user = second.guild.members[1]
                    second.adapter.client.user.bot = True
                    bridge_member = SimpleNamespace(id=9, name="bridge", display_name="Bridge", global_name=None)
                    first.guild.members.append(bridge_member)
                    second.guild.members.append(bridge_member)
                    answers = ("@bob please check this result.", "@bridge checked; here is my answer.")
                for h, answer in zip((first, second), answers):
                    original = h.runtime.client_factory

                    def factory(key, path, original=original, answer=answer):
                        client = original(key, path)
                        client.answer = answer
                        return client

                    h.runtime.client_factory = factory
                await first.deliver(first.event(1, "Please collaborate"))
                await first.idle()
                source, destination = first, second
                delivered = 0
                for serial in range(2, 8):
                    post = source.sent[-1]
                    if transport == "matrix":
                        meta = post[BRIDGE_META]
                    else:
                        meta = discord_metadata(SimpleNamespace(author=SimpleNamespace(bot=True), embeds=[post["embed"]]))
                    # A final without an invitation stops the automatic chain.
                    if not meta.get("invitations"):
                        break
                    event = destination.event(serial, post["body"], meta=meta, peer=True)
                    if transport == "matrix":
                        event["sender"] = source.adapter.settings.user_id
                        event["content"]["m.mentions"] = post["m.mentions"]
                    else:
                        user = source.adapter.client.user
                        event.author = SimpleNamespace(id=user.id, name=user.name,
                            display_name=getattr(user, "display_name", user.name), bot=True)
                    await destination.deliver(event)
                    await destination.idle()
                    delivered += 1
                    source, destination = destination, source
                self.assertEqual(delivered, 3)
                self.assertTrue(any("here is my answer" in post["body"] for post in second.sent))

    async def test_incoming_alias_and_structured_pings_wake_but_code_does_not(self):
        for transport in TRANSPORTS:
            with self.subTest(transport=transport), tempfile.TemporaryDirectory() as directory:
                h = Harness(Path(directory), transport)
                self.addAsyncCleanup(h.close)
                if transport == "matrix":
                    alias = "bridge"
                else:
                    h.adapter.client.user.name = "bridge"
                    h.adapter.client.user.display_name = "Bridge Agent"
                    alias = "Bridge Agent"
                await h.deliver(h.event(1, "`@" + alias + "` is an example", ping=False))
                self.assertEqual(h.clients, [])
                await h.deliver(h.event(2, "@" + alias + " can you review?", ping=False))
                await h.idle()
                self.assertEqual(len(h.clients), 1)
                await h.deliver(h.event(3, "Please review", ping=True))
                await h.idle()
                self.assertEqual(len(h.clients[0].prompts), 2)

    async def test_matrix_refreshes_cached_absence_before_rejecting(self):
        with tempfile.TemporaryDirectory() as directory:
            h = Harness(Path(directory), "matrix")
            self.addAsyncCleanup(h.close)
            h.adapter._rosters[h.room] = (float("inf"), {"@bob:example.test": {}})
            text, targets, rejected = await h.adapter._render_mentions(h.room, "Ask @Alice Smith.")
            self.assertFalse(rejected)
            self.assertEqual(targets, ["@alice:example.test"])
            self.assertEqual(text, "Ask @alice:example.test.")

    async def test_canonical_members_and_aliases_share_resolver(self):
        for transport in TRANSPORTS:
            with self.subTest(transport=transport), tempfile.TemporaryDirectory() as directory:
                h = Harness(Path(directory), transport)
                self.addAsyncCleanup(h.close)
                roster = await h.adapter.member_roster(h.key)
                self.assertTrue(roster.available)
                for member in roster.members:
                    self.assertEqual(roster.plan(member.id).user_ids, [member.id])
                    for alias in member.aliases:
                        if alias.startswith("!"):
                            continue
                        self.assertEqual(roster.plan("@" + alias).user_ids, [member.id])
                plan = roster.plan("Thanks @Alice Smith and @Bob.")
                self.assertEqual(len(plan.user_ids), 2)
                self.assertFalse(plan.unknown)

    async def test_validation_returns_to_agent_without_public_error(self):
        for transport in TRANSPORTS:
            with self.subTest(transport=transport), tempfile.TemporaryDirectory() as directory:
                h = Harness(Path(directory), transport)
                self.addAsyncCleanup(h.close)
                original = h.runtime.client_factory

                def factory(key, session_file):
                    client = original(key, session_file)
                    prompt = client.prompt

                    async def revised(message, on_event, images=None, *, on_accepted=None):
                        client.answer = "Ask @missing." if not client.prompts else "Ask @Alice Smith."
                        return await prompt(message, on_event, images, on_accepted=on_accepted)

                    client.prompt = revised
                    return client

                h.runtime.client_factory = factory
                await h.deliver(h.event(1))
                await h.idle()
                self.assertEqual(len(h.clients[0].prompts), 2)
                feedback = h.clients[0].prompts[1][0]
                self.assertIn("Canonical targets:", feedback)
                self.assertNotIn("posted to Matrix", feedback)
                self.assertFalse(any("unknown or ambiguous" in item["body"] for item in h.sent))
                self.assertTrue(read_checkpoint(h.key.directory(h.runtime.state_root)).final_sent)

    async def test_status_filtered_but_substantive_peer_flow_preserved(self):
        for transport in TRANSPORTS:
            for wake_any in (False, True):
                with self.subTest(transport=transport, wake_any=wake_any), tempfile.TemporaryDirectory() as directory:
                    h = Harness(Path(directory), transport, wake_any=wake_any)
                    self.addAsyncCleanup(h.close)
                    for index, kind in enumerate(sorted(SILENT_KINDS), 1):
                        await h.deliver(h.event(index, "@bridge working", meta={"kind": kind}, peer=True))
                    self.assertEqual(h.clients, [])
                    await h.deliver(h.event(20, "The status check found an error; can you review?",
                                            ping=False, peer=True))
                    self.assertEqual(len(h.clients), int(wake_any))
                    if wake_any:
                        await h.idle()
                    await h.deliver(h.event(21, "Can you check this?", peer=True))
                    await h.idle()
                    await h.deliver(h.event(22, "Here is the result; what do you think?", ping=False,
                                            meta={"kind": "final", "hop": 1}, peer=True))
                    await h.idle()
                    self.assertTrue(any("what do you think" in prompt for prompt, _ in h.clients[-1].prompts))
                    await h.deliver(h.event(23, "Still working…", meta={"kind": "progress"}, peer=True))
                    self.assertFalse(any(prompt == "Still working…" for prompt, _ in h.clients[-1].prompts))

    async def test_duplicate_input_and_native_mentions(self):
        for transport in TRANSPORTS:
            with self.subTest(transport=transport), tempfile.TemporaryDirectory() as directory:
                h = Harness(Path(directory), transport)
                self.addAsyncCleanup(h.close)
                event = h.event(1)
                await asyncio.wait_for(asyncio.gather(h.deliver(event), h.deliver(event)), 2)
                await h.idle()
                self.assertEqual(len(h.clients[0].prompts), 1)
                if transport == "discord":
                    text, users, _, rejected = await h.adapter._render_mentions(event, "Hi <@!3> and <@3>.")
                    self.assertFalse(rejected)
                    self.assertEqual(users, [3])
                    self.assertEqual(text, "Hi <@3> and <@3>.")

    async def test_image_preparation_cannot_be_overtaken_by_text(self):
        for transport in TRANSPORTS:
            with self.subTest(transport=transport), tempfile.TemporaryDirectory() as directory:
                h = Harness(Path(directory), transport, hold=True)
                self.addAsyncCleanup(h.close)
                image_started, release_image = asyncio.Event(), asyncio.Event()
                image = RpcImage("image/png", b"synthetic")

                async def download(*args):
                    image_started.set()
                    await release_image.wait()
                    return image if transport == "matrix" else (image,)

                first = h.event(1, "first image")
                if transport == "matrix":
                    first["content"]["msgtype"] = "m.image"
                    h.adapter._image = download
                else:
                    h.adapter._images = download
                image_task = asyncio.create_task(h.deliver(first))
                await asyncio.wait_for(image_started.wait(), 1)
                text_task = asyncio.create_task(h.deliver(h.event(2, "second text")))
                self.assertFalse(text_task.done())
                release_image.set()
                # Restore normal image extraction for the second message.
                await asyncio.wait_for(image_task, 1)
                await asyncio.wait_for(text_task, 1)
                client = h.clients[0]
                self.assertIn("first image", client.prompts[0][0])
                self.assertEqual(client.prompts[0][1], [image])
                self.assertIn("second text", client.steers[0][0])
                client.release.set()
                await h.idle()

    async def test_discord_threads_accept_work_independently_of_blocked_channel(self):
        with tempfile.TemporaryDirectory() as directory:
            h = Harness(Path(directory), "discord")
            self.addAsyncCleanup(h.close)
            started, release = asyncio.Event(), asyncio.Event()

            async def images(message):
                if message.channel.id == 2:
                    started.set()
                    await release.wait()
                return ()

            h.adapter._images = images
            blocked = asyncio.create_task(h.deliver(h.event(1)))
            await asyncio.wait_for(started.wait(), 1)
            thread = h.event(2)
            thread.channel = SimpleNamespace(id=20, parent_id=2, guild=h.guild, members=h.guild.members,
                                             typing=AsyncMock(), send=AsyncMock())
            await asyncio.wait_for(h.deliver(thread), 1)
            await asyncio.wait_for(h.runtime.wait_idle(SessionKey("agent_two", "discord", "20")), 1)
            self.assertFalse(blocked.done())
            release.set()
            await asyncio.wait_for(blocked, 1)
            await h.idle()

    async def test_unavailable_roster_never_posts_unresolved_ping(self):
        for transport in TRANSPORTS:
            with self.subTest(transport=transport), tempfile.TemporaryDirectory() as directory:
                h = Harness(Path(directory), transport)
                self.addAsyncCleanup(h.close)
                h.adapter.member_roster = AsyncMock(return_value=Roster([], complete=False, available=False))
                if transport == "discord":
                    h.adapter._roster = lambda *args: Roster([], complete=False, available=False)
                    h.adapter._requests[(h.key, "1")] = h.event(1)
                with self.assertRaisesRegex(ReplyRejected, "Membership is unavailable"):
                    await h.adapter.output(h.key, "final", "Ask @missing", "1")
                self.assertEqual(h.sent, [])


class LifecycleConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_compaction_blocks_same_scope_but_not_another(self):
        for transport in TRANSPORTS:
            with self.subTest(transport=transport), tempfile.TemporaryDirectory() as directory:
                h = Harness(Path(directory), transport)
                self.addAsyncCleanup(h.close)
                h.runtime.output = AsyncMock()
                await h.runtime.submit(h.key, Incoming("owner", "1", "first", optional=True))
                await h.idle()
                client = h.clients[0]
                client.compact_release.clear()
                sweep = asyncio.create_task(h.runtime.sweep_idle(force=True))
                await asyncio.wait_for(client.compact_started.wait(), 1)
                same = asyncio.create_task(h.runtime.submit(h.key, Incoming("owner", "2", "image", (RpcImage("image/png", b"x"),), optional=True)))
                other_key = SessionKey("agent_two", transport, "other")
                await asyncio.wait_for(h.runtime.submit(other_key, Incoming("owner", "3", "independent", optional=True)), 1)
                self.assertFalse(same.done())
                async with h.runtime.external_job() as reserved:
                    self.assertFalse(reserved)
                client.compact_release.set()
                await asyncio.wait_for(sweep, 1)
                await asyncio.wait_for(same, 1)
                await h.idle()
                self.assertIn("Sound summary", h.clients[-1].prompts[0][0])
                self.assertEqual(h.clients[-1].prompts[0][1][0].data, b"x")

    async def test_captioning_allows_independent_scope_and_excludes_external_job(self):
        with tempfile.TemporaryDirectory() as directory:
            started, release = asyncio.Event(), asyncio.Event()
            clients = []

            def factory(key, path):
                client = ControlledRpc(path)
                clients.append(client)
                return client

            async def caption(key, images):
                started.set()
                await release.wait()
                return "A synthetic image"

            runtime = BridgeRuntime(Path(directory), factory, AsyncMock(), image_captioner=caption)
            self.addAsyncCleanup(runtime.close)
            first = SessionKey("agent_two", "matrix", "one")
            task = asyncio.create_task(runtime.submit(first, Incoming("owner", "1", "image", (RpcImage("image/png", b"x"),))))
            await asyncio.wait_for(started.wait(), 1)
            other = SessionKey("agent_two", "discord", "two")
            await asyncio.wait_for(runtime.submit(other, Incoming("owner", "2", "text")), 1)
            async with runtime.external_job() as reserved:
                self.assertFalse(reserved)
            release.set()
            await asyncio.wait_for(task, 1)
            await asyncio.wait_for(runtime.wait_idle(first), 1)
            self.assertEqual(clients[-1].prompts[0][1], [])

    async def test_shutdown_cancels_active_work_and_leaves_no_progress_tasks(self):
        with tempfile.TemporaryDirectory() as directory:
            h = Harness(Path(directory), "discord", hold=True)
            await h.deliver(h.event(1))
            await asyncio.wait_for(h.clients[0].accepted.wait(), 1)
            await asyncio.wait_for(h.close(), 1)
            self.assertTrue(h.clients[0].stopped)
            self.assertFalse(await h.runtime.is_active(h.key))
            with self.assertRaisesRegex(RuntimeError, "closing"):
                await h.runtime.submit(h.key, Incoming("owner", "2", "late"))

    async def test_compaction_failure_preserves_checkpoint_and_requires_recovery(self):
        for transport in TRANSPORTS:
            with self.subTest(transport=transport), tempfile.TemporaryDirectory() as directory:
                h = Harness(Path(directory), transport)
                self.addAsyncCleanup(h.close)
                h.runtime.output = AsyncMock()
                await h.runtime.submit(h.key, Incoming("owner", "1", "first", optional=True))
                await h.idle()
                scope = h.runtime._scopes[h.key]
                scope.checkpoint.summary = "Last sound summary"
                h.clients[0].compact = AsyncMock(side_effect=PiRpcError("synthetic"))
                await h.runtime.sweep_idle(force=True)
                self.assertTrue(h.clients[0].stopped)
                self.assertEqual(scope.checkpoint.summary, "Last sound summary")
                with self.assertRaisesRegex(RuntimeError, "recovery"):
                    await h.runtime.submit(h.key, Incoming("owner", "2", "retry"))
                self.assertFalse(await h.runtime.recover(h.key, "other"))
                self.assertTrue(await h.runtime.recover(h.key, "owner"))


class FilteringContractTests(unittest.TestCase):
    def test_loop_limits_and_terminal_failures(self):
        for awake in (False, True):
            for hop in (None, -1, True, "1", 4, [], {}):
                self.assertEqual(metadata_rejection({"kind": "final", "hop": hop},
                    awake=awake, addressed=True, wake_any=True), "invalid_hop")
        for kind in ("error", "cancelled"):
            self.assertIsNone(metadata_rejection({"kind": kind}, awake=True, addressed=True, wake_any=True))
            self.assertEqual(metadata_rejection({"kind": kind}, awake=False, addressed=True, wake_any=True), "terminal_idle")
        self.assertEqual(metadata_rejection({"kind": "final", "hop": 3}, awake=False,
                                           addressed=False, wake_any=True), "hop_limit")
        self.assertEqual(metadata_rejection({"kind": "final", "hop": 3}, awake=True,
                                           addressed=False, wake_any=True), "hop_limit")
        self.assertIsNone(metadata_rejection({"kind": "final", "hop": 2}, awake=True,
                                             addressed=False, wake_any=False))

    def test_alias_collision_never_selects_first_member(self):
        members = [Member("@alice:one.test", "alice", ("alice", "Al")),
                   Member("@alice:two.test", "alice", ("alice", "Al"))]
        for order in (members, list(reversed(members))):
            roster = Roster(order)
            self.assertFalse(roster.plan("@Al").known)
            self.assertFalse(roster.plan("@alice").known)
            self.assertEqual(roster.plan("@alice:two.test").user_ids, ["@alice:two.test"])
            self.assertIn("@alice:one.test", roster.feedback(roster.plan("@Al")))
