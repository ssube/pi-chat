import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from bridge import __version__
from bridge.adapters.discord import DiscordAdapter, chunks
from bridge.adapters.matrix import (MATRIX_MAX_ATTEMPTS, MatrixAdapter, _describe_429, _retry_delay, addressed_to,
                                    agent_runtime_notice, format_context, explicit_ping,
                                    leading_agent_mentions, matrix_scope,
                                    progress_html)
from bridge.config import DiscordSettings, MatrixSettings, Profile
from bridge.runtime.session import ReplyRejected
from bridge.runtime.state import SessionKey


class BridgeAdapterTests(unittest.TestCase):
    def test_damaged_matrix_seen_journal_does_not_block_startup(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            (state_dir / "matrix-seen.json").write_text("{broken")
            settings = MatrixSettings("https://example.test", "@agent:example.test",
                                      "TEST_MATRIX_TOKEN", ("!room:example.test",))
            with patch.dict(os.environ, {"TEST_MATRIX_TOKEN": "test"}):
                adapter = MatrixAdapter("agent_one", settings, None, state_dir)
            self.assertEqual(adapter._seen, {})
            adapter._remember("$new")
            self.assertEqual(json.loads((state_dir / "matrix-seen.json").read_text()),
                             ["$new"])

    def test_runtime_hookshot_notice_is_room_and_sender_scoped(self):
        room = "!updates:example.test"
        settings = MatrixSettings("https://example.test", "@agent_two:example.test",
                                  "TEST_MATRIX_TOKEN", (room,),
                                  hookshot_user_id="@hookshot:matrix.example.invalid", hookshot_notice_rooms=(room,))
        event = {"type": "m.room.message", "event_id": "$build",
                 "sender": "@hookshot:matrix.example.invalid",
                 "content": {"msgtype": "m.notice",
                             "body": "Agent runtime CI: @agent_two:example.test new edge image",
                             "uk.half-shot.hookshot.webhook_data": {
                                 "username": "Agent runtime CI"}}}
        self.assertTrue(agent_runtime_notice(event, room, settings))
        self.assertFalse(agent_runtime_notice(event, "!other:example.test", settings))
        event["sender"] = "@random:example.test"
        self.assertFalse(agent_runtime_notice(event, room, settings))
        event["sender"] = "@hookshot:matrix.example.invalid"
        event["content"]["uk.half-shot.hookshot.webhook_data"]["username"] = "other"
        self.assertFalse(agent_runtime_notice(event, room, settings))
        event["content"]["uk.half-shot.hookshot.webhook_data"]["username"] = "Agent runtime CI"
        event["content"]["body"] = "Agent runtime CI: no addressee"
        self.assertFalse(agent_runtime_notice(event, room, settings))

    def test_matrix_ping_is_independent_of_reply(self):
        target = "@agent_one-agent:example.test"
        mentions = {"m.mentions": {"user_ids": [target]}}
        parent = {"m.relates_to": {"m.in_reply_to": {"event_id": "$parent"}}}
        link = f'<a href="https://matrix.to/#/{target}">Agent_one</a>'
        self.assertTrue(explicit_ping({**mentions, "body": "Agent_one: ping"}, target))
        self.assertTrue(explicit_ping({**mentions, "body": "Please ask Agent_one to check this"}, target))
        self.assertTrue(explicit_ping({"body": f"Ping {target} now"}, target))
        self.assertFalse(explicit_ping({**mentions, **parent, "body": "this is a reply"}, target))
        self.assertFalse(explicit_ping({**mentions, **parent, "body": "this is a reply",
                                        "formatted_body": f"<mx-reply>{link}</mx-reply>this is a reply"}, target))
        self.assertTrue(explicit_ping({**mentions, **parent, "body": "Agent_one: reply and ping",
                                       "formatted_body": f"<mx-reply>{link}</mx-reply>{link}: reply and ping"}, target))
        self.assertFalse(explicit_ping({**mentions, **parent,
                                        "body": f"> <{target}> earlier\n\njust replying"}, target))
        self.assertFalse(explicit_ping({"body": "Agent_one: plain text only"}, target))
        self.assertFalse(explicit_ping({"body": "Please ask someone else"}, target))

    def test_pr_review_requires_structured_recipient(self):
        from bridge.adapters.matrix import targeted_pr_review
        room = "!reviews:example.test"
        settings = MatrixSettings("https://example.test", "@agent_two:example.test",
                                  "TEST_MATRIX_TOKEN", (room,), hookshot_user_id="@hookshot:matrix.example.invalid", pr_review_rooms=(room,))
        event = {"sender": "@hookshot:matrix.example.invalid",
                 "content": {"msgtype": "m.text",
                             "body": "@agent_three:example.test appears in the PR title",
                             "m.mentions": {"user_ids": ["@agent_two:example.test"]}}}
        self.assertTrue(targeted_pr_review(event, room, settings))
        event["content"]["m.mentions"]["user_ids"] = ["@agent_three:example.test"]
        self.assertFalse(targeted_pr_review(event, room, settings))
        event["content"]["m.mentions"]["user_ids"] = ["@agent_two:example.test", "@agent_three:example.test"]
        self.assertTrue(targeted_pr_review(event, room, settings))
        event["content"]["m.mentions"] = {}
        self.assertFalse(targeted_pr_review(event, room, settings))

    def test_matrix_mentions_context_and_thread_scope(self):
        self.assertEqual(leading_agent_mentions(
            "@agent_two:example.test, @agent_three:example.test please compare notes"),
            ["@agent_two:example.test", "@agent_three:example.test"])
        self.assertEqual(leading_agent_mentions(
            "I quoted @agent_two:example.test in the report"), [])
        for suffix in (".", ":", "?", ")", "\""):
            self.assertEqual(leading_agent_mentions(
                "@agent_two:example.test" + suffix + " please take this"),
                ["@agent_two:example.test"])
        self.assertEqual(leading_agent_mentions(
            "(@agent_two:example.test) please take this"), ["@agent_two:example.test"])
        event = {"event_id": "$two", "type": "m.room.message",
                 "sender": "@operator:example.test",
                 "content": {"body": "Can you help?", "msgtype": "m.text",
                             "m.mentions": {"user_ids": ["@agent_two:example.test"]},
                             "m.relates_to": {"rel_type": "m.thread", "event_id": "$root"}}}
        self.assertTrue(addressed_to(event, "@agent_two:example.test"))
        self.assertFalse(addressed_to(event, "@agent_three:example.test"))
        event["content"]["body"] = "Quoted @agent_three:example.test while pinging Agent_two"
        self.assertTrue(addressed_to(event, "@agent_three:example.test"))
        self.assertEqual(matrix_scope("!room:example.test", event),
                         "!room:example.test:thread:$root")
        context = format_context([
            {"event_id": "$one", "type": "m.room.message", "sender": "@agent_one:example.test",
             "content": {"msgtype": "m.text", "body": "Earlier detail"}}, event], "$two")
        self.assertIn("[timestamp unknown] @agent_one:example.test [$one]: Earlier detail", context)

    def test_matrix_context_keeps_decisions_past_progress_chatter(self):
        decision = {"event_id": "$decision", "type": "m.room.message",
                    "sender": "@agent_three:example.test",
                    "content": {"msgtype": "m.text",
                                "body": "I verified Operator directly; the gallery may proceed."}}
        progress = [{"event_id": f"$progress-{i}", "type": "m.room.message",
                     "sender": "@agent_three:example.test",
                     "content": {"msgtype": "m.text", "body": "Still working…",
                                 "org.example.pi_bridge": {"kind": "progress"}}}
                    for i in range(35)]
        image = {"event_id": "$image", "type": "m.room.message",
                 "sender": "@agent_one:example.test",
                 "content": {"msgtype": "m.image", "body": "Canonical draft 1"}}
        context = format_context([decision, *progress, image], "$current")
        self.assertIn("@agent_three:example.test [$decision]: I verified Operator directly", context)
        self.assertIn("@agent_one:example.test [$image]: [image] Canonical draft 1", context)
        self.assertNotIn("Still working", context)

    def test_matrix_context_counts_bounded_omissions_and_dates(self):
        def event(index):
            return {"event_id": f"${index}", "type": "m.room.message",
                    "sender": "@agent_three:example.test", "origin_server_ts": 1_600_000_000_000 + index * 1000,
                    "content": {"msgtype": "m.text", "body": f"Message {index}"}}

        events = [event(index) for index in range(40)]
        exact = format_context(events, "$current", last_id="$0",
                               summary_saved_ms=1_600_000_000_000)
        self.assertIn("[skipping 9 messages]", exact)
        self.assertIn("previous private conversation summary saved 2020-09-13T12:26:40+00:00", exact)
        self.assertIn("[2020-09-13T12:27:19+00:00] @agent_three:example.test [$39]", exact)
        self.assertNotIn("Message 1\n", exact)
        unknown = format_context(events, "$current", last_id="$missing",
                                 summary_saved_ms=1_600_000_000_000)
        self.assertIn("earlier unseen message count unknown", unknown)
        self.assertIn("[$39]", unknown)

    def test_discord_chunks_preserve_text(self):
        original = "one two three four five six seven"
        pieces = chunks(original, limit=11)
        self.assertTrue(all(len(part) <= 11 for part in pieces))
        self.assertEqual("".join(pieces), original)
        self.assertTrue(all(len(part) <= 11 for part in chunks("", limit=11)))
        self.assertEqual("".join(chunks("line one\n  line two", limit=9)),
                         "line one\n  line two")

    def test_child_environment_excludes_transport_tokens(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "profile.json"
            base = {"provider": "test", "model": "test-model", "agent": "agent_three", "work_dir": directory,
                    "state_dir": directory, "pi_executable": "/usr/bin/pi",
                    "pi_agent_dir": directory, "pi_path_prefix": [directory],
                    "pi_extra_args": ["--approve"],
                    "pi_pass_env": ["DOMAIN_TOOL_TOKEN"],
                    "matrix": {"homeserver": "https://example.test",
                               "user_id": "@agent_three:example.test",
                               "token_env": "TEST_MATRIX_TOKEN",
                               "rooms": ["!room:example.test"]}}
            path.write_text(json.dumps(base))
            with patch.dict(os.environ, {"UNUSED_ROOM_ID": "!shared:example.test",
                                      "TEST_MATRIX_TOKEN": "transport-secret",
                                      "DOMAIN_TOOL_TOKEN": "domain-tool-secret"}):
                profile = Profile.load(path)
                self.assertEqual(profile.matrix.direct_rooms, ())
                client = profile.pi_client(SessionKey("agent_three", "matrix", "room"),
                                           root / "conversation.jsonl",
                                           history_capability="x" * 43)
                self.assertIn("To invite another Matrix agent",
                              (root / "bridge-system.txt").read_text())
                self.assertIn("do not invoke a `bridge.send_cli`",
                              (root / "bridge-system.txt").read_text())
                self.assertIn("there is no such module",
                              (root / "bridge-system.txt").read_text())
                profile.pi_client(SessionKey("agent_three", "discord", "room"),
                                  root / "discord-conversation.jsonl")
                self.assertNotIn("To invite another Matrix agent",
                                 (root / "bridge-system.txt").read_text())
                self.assertIn("do not invoke a `bridge.send_cli`",
                              (root / "bridge-system.txt").read_text())
                self.assertIn("there is no such module",
                              (root / "bridge-system.txt").read_text())
                self.assertNotIn("TEST_MATRIX_TOKEN", client._env)
                self.assertEqual(client._env["DOMAIN_TOOL_TOKEN"], "domain-tool-secret")
                self.assertEqual(client._env["AGENT_BRIDGE_INVOCATION"], "1")
                self.assertEqual(client._env["AGENT_BRIDGE_HISTORY_CAPABILITY"], "x" * 43)
                self.assertEqual(client._env["AGENT_BRIDGE_HISTORY_SCOPE"], "room")
                self.assertTrue(client._env["PATH"].startswith(directory + os.pathsep))
                self.assertNotIn("--no-tools", client._command)
                self.assertNotIn("--no-extensions", client._command)
                self.assertIn("--approve", client._command)
                (root / "auth.json").write_text('{"test":true}')
                (root / "models.json").write_text('{"test":true}')
                smoke_client = profile.pi_client(SessionKey("agent_three", "matrix", "smoke"),
                                                 root / "smoke.jsonl", safe_mode=True,
                                                 timeout_seconds=120)
                self.assertIn("--no-tools", smoke_client._command)
                self.assertIn("--no-extensions", smoke_client._command)
                self.assertEqual(smoke_client._timeout_seconds, 120)
                self.assertNotIn("--approve", smoke_client._command)
                self.assertEqual(smoke_client._env["PI_CODING_AGENT_DIR"], str(root / "agent"))
                self.assertEqual(smoke_client._env["HOME"], str(root))
                self.assertEqual((root / "agent" / "auth.json").stat().st_mode & 0o777, 0o600)
                self.assertEqual(json.loads((root / "agent" / "settings.json").read_text()),
                                 {"compaction": {"keepRecentTokens": 1}})
                self.assertEqual((root / "agent" / "settings.json").stat().st_mode & 0o777,
                                 0o600)
                self.assertIn("--session-dir", smoke_client._command)
                base["pi_pass_env"] = ["TEST_MATRIX_TOKEN"]
                path.write_text(json.dumps(base))
                with self.assertRaisesRegex(ValueError, "Transport credentials"):
                    Profile.load(path).pi_client(SessionKey("agent_three", "matrix", "room"),
                                                 root / "conversation.jsonl")
                base["matrix"]["token_env"] = "PATH"
                base["pi_pass_env"] = ["PATH"]
                path.write_text(json.dumps(base))
                with self.assertRaisesRegex(ValueError, "Transport credentials"):
                    Profile.load(path).pi_client(SessionKey("agent_three", "matrix", "room"),
                                                 root / "conversation.jsonl")

    def test_startup_channels_must_be_enabled_targets(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profile.json"
            base = {"agent": "agent_two", "work_dir": directory, "state_dir": directory,
                    "provider": "test", "model": "test-model", "pi_executable": "/usr/bin/pi", "pi_agent_dir": directory,
                    "matrix": {"homeserver": "https://example.test",
                               "user_id": "@agent_two:example.test", "token_env": "TEST_MATRIX_TOKEN",
                               "rooms": ["!main:example.test"]},
                    "discord": {"token_env": "TEST_DISCORD_TOKEN",
                                "guild_id": 123, "channel_ids": [456]}}
            with patch.dict(os.environ, {
                                      "TEST_MATRIX_TOKEN": "test", "TEST_DISCORD_TOKEN": "test"}):
                base["matrix"]["startup_room_id"] = "!main:example.test"
                path.write_text(json.dumps(base))
                self.assertEqual(Profile.load(path).matrix.startup_room_id,
                                 "!main:example.test")
                base["discord"]["startup_channel_id"] = 456
                path.write_text(json.dumps(base))
                self.assertEqual(Profile.load(path).matrix.startup_room_id,
                                 "!main:example.test")
                self.assertEqual(Profile.load(path).discord.startup_channel_id, 456)
                base["matrix"].pop("startup_room_id")
                path.write_text(json.dumps(base))
                self.assertEqual(Profile.load(path).discord.startup_channel_id, 456)
                base["discord"]["startup_channel_id"] = 999
                path.write_text(json.dumps(base))
                with self.assertRaisesRegex(ValueError, "enabled channel"):
                    Profile.load(path)
                base["discord"].pop("startup_channel_id")
                base["matrix"]["startup_room_id"] = "!other:example.test"
                path.write_text(json.dumps(base))
                with self.assertRaisesRegex(ValueError, "enabled room"):
                    Profile.load(path)



class MatrixDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_compaction_notice_precedes_context_and_does_not_touch_checkpoint(self):
        runtime = SimpleNamespace(
            is_awake=AsyncMock(return_value=True),
            last_wake=AsyncMock(return_value=("", 0)),
            submit=AsyncMock(), record_wake=AsyncMock(), record_activity=AsyncMock())
        order = []

        async def admit(key, can_wake, *, on_wait=None):
            await on_wait()
            order.append("finished")
            return True

        runtime.admit = admit
        settings = MatrixSettings("https://example.test", "@agent_one:example.test",
                                  "TEST_MATRIX_TOKEN", ("!room:example.test",))
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"TEST_MATRIX_TOKEN": "test"}):
                adapter = MatrixAdapter("agent_one", settings, runtime, Path(directory))

            async def request(method, path, **kwargs):
                content = kwargs["data"]
                self.assertEqual(content["body"], "Compacting, one moment...")
                self.assertEqual(content["m.mentions"]["user_ids"], [])
                self.assertEqual(content["org.example.pi_bridge"]["invitations"], [])
                order.append("notice")
                return {"event_id": "$notice"}

            adapter._request = request

            async def history(*args, **kwargs):
                self.assertEqual(order, ["notice", "finished"])
                order.append("context")
                return "Earlier posts"

            adapter._history = history
            # The initial awake check is true; compaction closes Pi before the
            # adapter decides whether to read history.
            runtime.is_awake.side_effect = [True, False]
            await adapter._handle_event("!room:example.test", {
                "type": "m.room.message", "event_id": "$new",
                "sender": "@operator:example.test",
                "content": {"msgtype": "m.text", "body": "@agent_one:example.test next"}})
        self.assertEqual(order, ["notice", "finished", "context"])
        self.assertIn("Earlier posts", runtime.submit.await_args.args[1].text)
        runtime.record_activity.assert_not_awaited()


    async def test_submit_compaction_notice_never_mentions_registered_requester(self):
        runtime = SimpleNamespace(record_activity=AsyncMock())
        settings = MatrixSettings("https://example.test", "@agent_one:example.test",
                                  "TEST_MATRIX_TOKEN", ("!room:example.test",))
        with tempfile.TemporaryDirectory() as directory, patch.dict(
                os.environ, {"TEST_MATRIX_TOKEN": "test"}):
            adapter = MatrixAdapter("agent_one", settings, runtime, Path(directory))
            key = SessionKey("agent_one", "matrix", "!room:example.test:main")
            adapter._requesters[(key, "$new")] = "@owner:example.test"
            adapter._request = AsyncMock(return_value={"event_id": "$notice"})
            await adapter.output(key, "compacting", "Compacting, one moment...", "$new")
            content = adapter._request.await_args.kwargs["data"]
            self.assertEqual(content["m.mentions"], {"user_ids": [], "room": False})
            self.assertEqual(content["org.example.pi_bridge"]["invitations"], [])
            self.assertEqual(adapter._requesters[(key, "$new")], "@owner:example.test")
            runtime.record_activity.assert_not_awaited()

    async def test_startup_notice_after_first_sync_once_with_stable_transaction(self):
        room = "!main:example.test"
        settings = MatrixSettings("https://example.test", "@agent_one:example.test",
                                  "TEST_MATRIX_TOKEN", (room,), startup_room_id=room)
        with tempfile.TemporaryDirectory() as directory, patch.dict(
                os.environ, {"TEST_MATRIX_TOKEN": "test"}):
            adapter = MatrixAdapter("agent_one", settings, None, Path(directory))
            session = SimpleNamespace(close=AsyncMock())
            async def request(method, path, **kwargs):
                if method == "GET":
                    return {"next_batch": "next"}
                self.assertTrue((Path(directory) / "matrix-since.txt").exists())
                self.assertEqual(kwargs["data"]["msgtype"], "m.notice")
                self.assertIn(f"agent-bridge v{__version__} connected — agent_one (@agent_one:example.test)",
                              kwargs["data"]["body"])
                self.assertEqual(kwargs["data"]["m.mentions"],
                                 {"user_ids": [], "room": False})
                return {"event_id": "$startup"}
            adapter._request = AsyncMock(side_effect=request)
            original_write = adapter._write_since
            def finish_sync(token):
                original_write(token)
                adapter._stopping.set()
            adapter._write_since = finish_sync
            with patch.dict(sys.modules, {"aiohttp": SimpleNamespace(ClientSession=lambda **kw: session)}):
                await adapter.run()
            self.assertEqual([call.args[0] for call in adapter._request.call_args_list],
                             ["GET", "PUT"])
            self.assertTrue(adapter._startup_sent)
            await adapter._announce_startup()
            self.assertEqual(adapter._request.call_count, 2)

    async def test_matrix_startup_retry_keeps_transaction_id(self):
        room = "!main:example.test"
        settings = MatrixSettings("https://example.test", "@agent_one:example.test",
                                  "TEST_MATRIX_TOKEN", (room,), startup_room_id=room)
        with tempfile.TemporaryDirectory() as directory, patch.dict(
                os.environ, {"TEST_MATRIX_TOKEN": "test"}):
            adapter = MatrixAdapter("agent_one", settings, None, Path(directory))
            adapter._request = AsyncMock(side_effect=[TimeoutError(), {"event_id": "$startup"}])
            with self.assertRaises(TimeoutError):
                await adapter._announce_startup()
            await adapter._announce_startup()
            await adapter._announce_startup()
            self.assertEqual(adapter._request.call_count, 2)
            first, second = adapter._request.call_args_list
            self.assertEqual(first.args[1], second.args[1])

    async def test_active_pr_room_ignores_hookshot_request_for_other_reviewer(self):
        room = "!reviews:example.test"
        settings = MatrixSettings("https://example.test", "@agent_two:example.test",
                                  "TEST_MATRIX_TOKEN", (room,), hookshot_user_id="@hookshot:matrix.example.invalid", pr_review_rooms=(room,))
        runtime = SimpleNamespace(is_awake=AsyncMock(return_value=True),
                                  admit=AsyncMock(return_value=True),
                                  submit=AsyncMock())
        event = {"type": "m.room.message", "event_id": "$wrong-reviewer",
                 "sender": "@hookshot:matrix.example.invalid",
                 "content": {"msgtype": "m.text",
                             "body": "@agent_two:example.test appears in the PR title",
                             "m.mentions": {"user_ids": ["@agent_three:example.test"]}}}
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"TEST_MATRIX_TOKEN": "test"}):
                adapter = MatrixAdapter("agent_two", settings, runtime, Path(directory))
            await adapter._handle_event(room, event)
            runtime.is_awake.assert_not_awaited()
            runtime.admit.assert_not_awaited()
            runtime.submit.assert_not_awaited()
            self.assertIn("$wrong-reviewer", adapter._seen)


    async def test_named_wake_on_any_room_and_post_admission_context(self):
        class Runtime:
            def __init__(self):
                self.awake_checks = 0
                self.received = []

            async def is_awake(self, key):
                self.awake_checks += 1
                return self.awake_checks == 1  # Idle sweep closes before admit.

            async def admit(self, key, can_wake, *, on_wait=None):
                self.can_wake = can_wake
                return can_wake

            async def last_wake(self, key):
                return "$old", 1_000_000

            async def submit(self, key, incoming):
                self.received.append(incoming)

            async def record_wake(self, key, event_id):
                pass

        with tempfile.TemporaryDirectory() as directory:
            room = "!room:example.test"
            settings = MatrixSettings("https://example.test", "@agent:example.test",
                                      "TEST_MATRIX_TOKEN", (room,),
                                      wake_on_any_room_ids=(room,))
            runtime = Runtime()
            with patch.dict(os.environ, {"TEST_MATRIX_TOKEN": "test"}):
                adapter = MatrixAdapter("agent_one", settings, runtime, Path(directory))

            async def history(*args, **kwargs):
                return "[skipping 5 messages]\nEarlier context"

            adapter._history = history
            event = {"type": "m.room.message", "event_id": "$new",
                     "sender": "@operator:example.test",
                     "content": {"msgtype": "m.text", "body": "ordinary"}}
            await adapter._handle_event(room, event)
            self.assertTrue(runtime.can_wake)
            self.assertEqual(len(runtime.received), 1)
            self.assertTrue(runtime.received[0].optional)
            self.assertIn("Earlier context", runtime.received[0].text)

    async def test_history_page_is_scoped_and_paginated(self):
        with tempfile.TemporaryDirectory() as directory:
            room = "!room:example.test"
            settings = MatrixSettings("https://example.test", "@agent:example.test",
                                      "TEST_MATRIX_TOKEN", (room,))
            with patch.dict(os.environ, {"TEST_MATRIX_TOKEN": "test"}):
                adapter = MatrixAdapter("agent_one", settings, None, Path(directory))
            calls = []

            async def request(method, path, *, params=None, **kwargs):
                calls.append((path, params))
                return {"chunk": [
                    {"event_id": "$one", "type": "m.room.message",
                     "sender": "@operator:example.test", "origin_server_ts": 1_600_000_000_000,
                     "content": {"msgtype": "m.text", "body": "older"}},
                    {"event_id": "$thread", "type": "m.room.message",
                     "sender": "@operator:example.test",
                     "content": {"msgtype": "m.text", "body": "thread reply",
                                 "m.relates_to": {"rel_type": "m.thread", "event_id": "$root"}}}],
                        "end": "older-token"}

            adapter._request = request
            key = SessionKey("agent_one", "matrix", room + ":main")
            page = await adapter.history_page(key, "cursor", 2)
            self.assertEqual([message["id"] for message in page["messages"]], ["$one"])
            self.assertEqual(page["next_before"], "older-token")
            self.assertEqual(calls[0][1]["from"], "cursor")
            self.assertEqual(page["messages"][0]["timestamp"], "2020-09-13T12:26:40+00:00")
            with self.assertRaisesRegex(ValueError, "not configured"):
                await adapter.history_page(SessionKey("agent_one", "matrix", "!other:example.test:main"),
                                           None, 50)

    async def test_ci_hookshot_notice_reaches_pi_only_when_addressed(self):
        class FakeRuntime:
            def __init__(self):
                self.submitted = []

            async def is_active(self, key):
                return False

            async def is_awake(self, key):
                return False

            async def admit(self, key, can_wake, *, on_wait=None):
                return can_wake

            async def last_wake(self, key):
                return None, None

            async def record_wake(self, key, event_id):
                pass

            async def submit(self, key, incoming):
                self.submitted.append(incoming.message_id)

        with tempfile.TemporaryDirectory() as directory:
            room = "!updates:example.test"
            settings = MatrixSettings("https://example.test", "@agent_three:example.test",
                                      "TEST_MATRIX_TOKEN", (room,),
                                      hookshot_user_id="@hookshot:matrix.example.invalid", hookshot_notice_rooms=(room,))
            with patch.dict(os.environ, {"TEST_MATRIX_TOKEN": "test"}):
                runtime = FakeRuntime()
                adapter = MatrixAdapter("agent_three", settings, runtime, Path(directory))

            async def no_history(room_id, current_id, thread_root, **kwargs):
                return ""

            adapter._history = no_history
            event = {"type": "m.room.message", "event_id": "$ci",
                     "sender": "@hookshot:matrix.example.invalid",
                     "content": {"msgtype": "m.notice",
                                 "body": "Agent runtime CI: @agent_three:example.test image ready",
                                 "uk.half-shot.hookshot.webhook_data": {
                                     "username": "Agent runtime CI"}}}
            await adapter._handle_event(room, event)
            self.assertEqual(runtime.submitted, ["$ci"])
            event["event_id"] = "$unaddressed"
            event["content"]["body"] = "Agent runtime CI: image ready"
            await adapter._handle_event(room, event)
            self.assertEqual(runtime.submitted, ["$ci"])

    def test_retry_delay_honors_server_hint(self):
        self.assertEqual(_retry_delay({"retry_after_ms": 1500}, {}, 1), 1.5)
        self.assertEqual(_retry_delay(None, {"Retry-After": "2"}, 1), 2.0)
        self.assertEqual(_retry_delay(None, {}, 3), 2.0)
        self.assertEqual(_retry_delay({"retry_after_ms": 999_999}, {}, 1), 30.0)

    def test_describe_429_separates_limiter_from_dead_backend(self):
        """Synapse's limiter and the proxy's no-backend 429 need opposite answers."""
        self.assertEqual(
            _describe_429({"errcode": "M_LIMIT_EXCEEDED", "error": "Too Many Requests",
                           "retry_after_ms": 1200}),
            "errcode=M_LIMIT_EXCEEDED, error=Too Many Requests")
        self.assertEqual(
            _describe_429({"errcode": "M_UNKNOWN", "error": "Server is unavailable"}),
            "errcode=M_UNKNOWN, error=Server is unavailable")
        self.assertEqual(_describe_429({}), "empty JSON body")
        self.assertEqual(_describe_429(None), "no JSON body")
        self.assertEqual(_describe_429({"errcode": "M_UNKNOWN"}), "errcode=M_UNKNOWN")
        long_error = _describe_429({"error": "x" * 500})
        self.assertEqual(long_error, "error=" + "x" * 120)

    async def test_rate_limited_request_retries_then_succeeds(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = MatrixSettings("https://example.test", "@agent_two:example.test",
                                      "TEST_MATRIX_TOKEN", ("!room:example.test",))
            runtime = SimpleNamespace(record_activity=AsyncMock())
            with patch.dict(os.environ, {"TEST_MATRIX_TOKEN": "test"}):
                adapter = MatrixAdapter("agent_two", settings, runtime, Path(directory))

            class FakeResponse:
                def __init__(self, status, payload, headers=None):
                    self.status = status
                    self._payload = payload
                    self.headers = headers or {}

                async def json(self, content_type=None):
                    return self._payload

                async def __aenter__(self):
                    return self

                async def __aexit__(self, *exc):
                    return False

            responses = [FakeResponse(429, {"retry_after_ms": 0,
                                            "errcode": "M_LIMIT_EXCEEDED",
                                            "error": "Too Many Requests"}),
                         FakeResponse(200, {"event_id": "$ok"})]
            adapter.session = SimpleNamespace(request=lambda *a, **k: responses.pop(0))

            with self.assertLogs("agent_bridge.matrix", level="WARNING") as logged:
                result = await adapter._request(
                    "GET", "/_matrix/client/v3/account/whoami", timeout=5)

            self.assertEqual(result, {"event_id": "$ok"})
            self.assertEqual(responses, [])
            # A limiter breach has to be identifiable from the log alone.
            self.assertEqual(len(logged.records), 1)
            message = logged.records[0].getMessage()
            self.assertIn("attempt 1/4", message)
            self.assertIn("errcode=M_LIMIT_EXCEEDED", message)
            self.assertIn("error=Too Many Requests", message)

    async def test_rate_limited_request_logs_errcode_when_giving_up(self):
        """The 429 that ends a turn must say which 429 it was."""
        with tempfile.TemporaryDirectory() as directory:
            settings = MatrixSettings("https://example.test", "@agent_two:example.test",
                                      "TEST_MATRIX_TOKEN", ("!room:example.test",))
            runtime = SimpleNamespace(record_activity=AsyncMock())
            with patch.dict(os.environ, {"TEST_MATRIX_TOKEN": "test"}):
                adapter = MatrixAdapter("agent_two", settings, runtime, Path(directory))

            class FakeResponse:
                status = 429
                headers = {"Retry-After": "0"}

                async def json(self, content_type=None):
                    return {"errcode": "M_UNKNOWN", "error": "Server is unavailable"}

                async def __aenter__(self):
                    return self

                async def __aexit__(self, *exc):
                    return False

            adapter.session = SimpleNamespace(request=lambda *a, **k: FakeResponse())

            with self.assertLogs("agent_bridge.matrix", level="WARNING") as logged:
                with self.assertRaisesRegex(RuntimeError, "HTTP 429"):
                    await adapter._request("PUT", "/_matrix/client/v3/rooms/x/send/y/z",
                                           data={}, timeout=5)

            last = logged.records[-1].getMessage()
            self.assertIn("after 4 attempt(s)", last)
            self.assertIn("errcode=M_UNKNOWN", last)
            self.assertIn("error=Server is unavailable", last)

    async def test_rate_limited_request_gives_up_with_status(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = MatrixSettings("https://example.test", "@agent_two:example.test",
                                      "TEST_MATRIX_TOKEN", ("!room:example.test",))
            runtime = SimpleNamespace(record_activity=AsyncMock())
            with patch.dict(os.environ, {"TEST_MATRIX_TOKEN": "test"}):
                adapter = MatrixAdapter("agent_two", settings, runtime, Path(directory))

            class FakeResponse:
                status = 429
                headers = {"Retry-After": "0"}

                async def json(self, content_type=None):
                    return {}

                async def __aenter__(self):
                    return self

                async def __aexit__(self, *exc):
                    return False

            adapter.session = SimpleNamespace(request=lambda *a, **k: FakeResponse())

            with self.assertRaisesRegex(RuntimeError, "HTTP 429"):
                await adapter._request("GET", "/_matrix/client/v3/account/whoami", timeout=5)

    async def test_tool_progress_uses_matrix_code_formatting(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = MatrixSettings("https://example.test", "@agent_two:example.test",
                                      "TEST_MATRIX_TOKEN", ("!room:example.test",))
            runtime = SimpleNamespace(record_activity=AsyncMock())
            with patch.dict(os.environ, {"TEST_MATRIX_TOKEN": "test"}):
                adapter = MatrixAdapter("agent_two", settings, runtime, Path(directory))
            key = SessionKey("agent_two", "matrix", "!room:example.test:main")
            adapter._acks[(key, "$request")] = "$ack"
            adapter._request = AsyncMock(return_value={"event_id": "$edit"})

            await adapter.output(key, "progress", "Using `bash`…", "$request")

            content = adapter._request.await_args.kwargs["data"]
            self.assertEqual(content["body"], "Using `bash`…")
            self.assertEqual(content["formatted_body"], "Using <code>bash</code>…")
            self.assertEqual(content["format"], "org.matrix.custom.html")
            self.assertEqual(content["m.new_content"]["formatted_body"],
                             "Using <code>bash</code>…")
            self.assertEqual(progress_html("Using `<unsafe&>`…"),
                             "Using <code>&lt;unsafe&amp;&gt;</code>…")

    async def test_declared_direct_room_starts_without_mention(self):
        class FakeRuntime:
            def __init__(self):
                self.messages = []
                self.active = False

            async def is_active(self, key):
                return self.active

            async def is_awake(self, key):
                return self.active

            async def admit(self, key, can_wake, *, on_wait=None):
                return can_wake or self.active

            async def last_wake(self, key):
                return None, None

            async def record_wake(self, key, event_id):
                pass

            async def submit(self, key, incoming):
                self.messages.append(incoming.text)

        event = {"type": "m.room.message", "event_id": "$plain",
                 "sender": "@agent_one:example.test",
                 "content": {"msgtype": "m.text", "body": "hello"}}
        with tempfile.TemporaryDirectory() as directory:
            settings = MatrixSettings("https://example.test", "@agent_two:example.test",
                                      "TEST_MATRIX_TOKEN", ("!dm:example.test",),
                                      ("!dm:example.test",))
            runtime = FakeRuntime()
            with patch.dict(os.environ, {"TEST_MATRIX_TOKEN": "test"}):
                adapter = MatrixAdapter("agent_two", settings, runtime, Path(directory))

            async def history(room_id, current_id, thread_root=None, **kwargs):
                return "@agent_one:example.test: earlier detail"

            adapter._history = history
            await adapter._handle_event("!dm:example.test", event)
            self.assertEqual(len(runtime.messages), 1)
            self.assertIn("earlier detail", runtime.messages[0])
            self.assertIn("hello", runtime.messages[0])
            event["event_id"] = "$other-mentioned"
            event["content"]["m.mentions"] = {"user_ids": ["@third:example.test"]}
            await adapter._handle_event("!dm:example.test", event)
            self.assertEqual(len(runtime.messages), 2)
            event["event_id"] = "$quoted-third"
            event["content"]["body"] = "I quoted @agent_two:example.test here"
            await adapter._handle_event("!dm:example.test", event)
            self.assertEqual(len(runtime.messages), 3)
            event["event_id"] = "$other-bridge"
            event["content"]["m.mentions"] = {"user_ids": ["@agent_two:example.test"]}
            event["content"]["org.example.pi_bridge"] = {
                "kind": "final", "hop": 1, "invitations": []}
            await adapter._handle_event("!dm:example.test", event)
            self.assertEqual(len(runtime.messages), 4)
            event["event_id"] = "$intentional-bridge-mention"
            event["content"]["body"] = "@agent_two:example.test, could you check this?"
            event["content"]["m.mentions"] = {"user_ids": ["@agent_one:example.test",
                                                           "@agent_two:example.test"]}
            event["content"]["org.example.pi_bridge"]["invitations"] = [
                "@agent_two:example.test"]
            await adapter._handle_event("!dm:example.test", event)
            self.assertEqual(len(runtime.messages), 5)
            event["event_id"] = "$quoted-id"
            event["content"]["body"] = "I quoted @agent_two:example.test here."
            event["content"]["org.example.pi_bridge"]["invitations"] = []
            await adapter._handle_event("!dm:example.test", event)
            self.assertEqual(len(runtime.messages), 6)
            runtime.active = True
            event["event_id"] = "$active-final"
            event["content"]["m.mentions"] = {"user_ids": ["@agent_two:example.test"]}
            await adapter._handle_event("!dm:example.test", event)
            self.assertEqual(len(runtime.messages), 7)
            event["event_id"] = "$over-limit"
            event["content"]["org.example.pi_bridge"]["hop"] = 4
            event["content"]["org.example.pi_bridge"]["kind"] = "final"
            await adapter._handle_event("!dm:example.test", event)
            self.assertEqual(len(runtime.messages), 7)
            event["event_id"] = "$active-progress"
            event["content"]["org.example.pi_bridge"]["hop"] = 1
            event["content"]["org.example.pi_bridge"]["kind"] = "progress"
            await adapter._handle_event("!dm:example.test", event)
            self.assertEqual(len(runtime.messages), 7)
            event["event_id"] = "$legacy-final"
            event["content"]["org.example.pi_bridge"] = {"kind": "final",
                                                                  "invitations": []}
            await adapter._handle_event("!dm:example.test", event)
            self.assertEqual(len(runtime.messages), 8)
            runtime.active = False
            event["event_id"] = "$new-chain"
            event["content"]["body"] = "@agent_two:example.test new task"
            event["content"]["org.example.pi_bridge"] = {
                "kind": "final", "hop": 1,
                "invitations": ["@agent_two:example.test"]}
            key = SessionKey("agent_two", "matrix", "!dm:example.test:main")
            adapter._scope_hops[key] = 3
            await adapter._handle_event("!dm:example.test", event)
            self.assertEqual(adapter._scope_hops[key], 1)
            self.assertEqual(len(runtime.messages), 9)
            event["event_id"] = "$zero-hop-invite"
            event["content"]["org.example.pi_bridge"]["hop"] = 0
            await adapter._handle_event("!dm:example.test", event)
            self.assertEqual(len(runtime.messages), 10)
            event["event_id"] = "$legacy-invite"
            del event["content"]["org.example.pi_bridge"]["hop"]
            await adapter._handle_event("!dm:example.test", event)
            self.assertEqual(len(runtime.messages), 11)
            self.assertEqual(adapter._scope_hops[key], 3)
            event["event_id"] = "$last-hop-invite"
            event["content"]["org.example.pi_bridge"]["hop"] = 3
            await adapter._handle_event("!dm:example.test", event)
            self.assertEqual(len(runtime.messages), 12)
            self.assertEqual(adapter._scope_hops[key], 3)

        with tempfile.TemporaryDirectory() as directory:
            settings = MatrixSettings("https://example.test", "@agent_two:example.test",
                                      "TEST_MATRIX_TOKEN", ("!dm:example.test",))
            runtime = FakeRuntime()
            with patch.dict(os.environ, {"TEST_MATRIX_TOKEN": "test"}):
                adapter = MatrixAdapter("agent_two", settings, runtime, Path(directory))
            plain_event = {"type": "m.room.message", "event_id": "$plain-again",
                           "sender": "@agent_one:example.test",
                           "content": {"msgtype": "m.text", "body": "hello"}}
            await adapter._handle_event("!dm:example.test", plain_event)
            self.assertEqual(runtime.messages, [])
            async def no_history(*args, **kwargs):
                return ""
            adapter._history = no_history
            plain_event["event_id"] = "$body-mention-with-other-attached"
            plain_event["content"]["body"] = "@agent_two:example.test please take this"
            plain_event["content"]["m.mentions"] = {"user_ids": ["@third:example.test"]}
            await adapter._handle_event("!dm:example.test", plain_event)
            self.assertEqual(len(runtime.messages), 1)

    async def test_peer_bridge_ping_expects_no_reaction(self):
        class FakeRuntime:
            def __init__(self):
                self.incoming = []

            async def is_active(self, key):
                return False

            async def is_awake(self, key):
                return False

            async def admit(self, key, can_wake, *, on_wait=None):
                return can_wake

            async def last_wake(self, key):
                return None, None

            async def record_wake(self, key, event_id):
                pass

            async def submit(self, key, incoming):
                self.incoming.append(incoming)

        def event(event_id, bridge=False):
            content = {"msgtype": "m.text", "body": "hello",
                       "m.mentions": {"user_ids": ["@agent_two:example.test"]}}
            if bridge:
                content["org.example.pi_bridge"] = {
                    "kind": "final", "hop": 1, "invitations": ["@agent_two:example.test"]}
            return {"type": "m.room.message", "event_id": event_id,
                    "sender": "@agent_one:example.test", "content": content}

        with tempfile.TemporaryDirectory() as directory:
            settings = MatrixSettings("https://example.test", "@agent_two:example.test",
                                      "TEST_MATRIX_TOKEN", ("!room:example.test",))
            runtime = FakeRuntime()
            with patch.dict(os.environ, {"TEST_MATRIX_TOKEN": "test"}):
                adapter = MatrixAdapter("agent_two", settings, runtime, Path(directory))

            async def history(room_id, current_id, thread_root=None, **kwargs):
                return ""

            adapter._history = history
            await adapter._handle_event("!room:example.test", event("$human"))
            await adapter._handle_event("!room:example.test", event("$peer", bridge=True))
            self.assertEqual([incoming.message_id for incoming in runtime.incoming],
                             ["$human", "$peer"])
            self.assertTrue(runtime.incoming[0].ack_required)
            self.assertFalse(runtime.incoming[1].ack_required)

    async def test_thread_reply_and_seen_event_journal(self):
        class FakeRuntime:
            def __init__(self):
                self.activity = []
                self.submitted = []

            async def is_active(self, key):
                return False

            async def is_awake(self, key):
                return False

            async def admit(self, key, can_wake, *, on_wait=None):
                return can_wake

            async def last_wake(self, key):
                return None, None

            async def record_wake(self, key, event_id):
                pass

            async def submit(self, key, incoming):
                self.submitted.append(incoming.message_id)

            async def record_activity(self, key):
                self.activity.append(key)

        with tempfile.TemporaryDirectory() as directory:
            runtime = FakeRuntime()
            settings = MatrixSettings("https://example.test", "@agent:example.test",
                                      "TEST_MATRIX_TOKEN", ("!room:example.test",))
            with patch.dict(os.environ, {"TEST_MATRIX_TOKEN": "test"}):
                adapter = MatrixAdapter("agent_one", settings, runtime, Path(directory))
            calls = []

            async def request(method, path, *, data=None, params=None, timeout=40,
                                    attempts=MATRIX_MAX_ATTEMPTS, budget=None, stop=None):
                calls.append((method, path, data))
                if method == "GET":
                    return {"chunk": []}
                return {"event_id": "$reply"}

            adapter._request = request
            event = {"type": "m.room.message", "event_id": "$new",
                     "sender": "@operator:example.test",
                     "content": {"msgtype": "m.text", "body": "@agent:example.test hello",
                                 "m.relates_to": {"rel_type": "m.thread",
                                                  "event_id": "$root"}}}
            await adapter._handle_event("!room:example.test", event)
            await adapter._handle_event("!room:example.test", event)
            self.assertEqual(runtime.submitted, ["$new"])
            self.assertTrue(any("/relations/" in path for _, path, _ in calls))
            self.assertEqual((Path(directory) / "matrix-seen.json").stat().st_mode & 0o777, 0o600)
            key = SessionKey("agent_one", "matrix", "!room:example.test:thread:$root")
            await adapter.output(key, "final", "Hello", "$new")
            relation = calls[-1][2]["m.relates_to"]
            self.assertEqual(calls[-1][2]["org.example.pi_bridge"]["kind"],
                             "final")
            self.assertEqual(relation["rel_type"], "m.thread")
            self.assertEqual(relation["event_id"], "$root")
            self.assertEqual(relation["m.in_reply_to"]["event_id"], "$new")
            adapter._rosters["!room:example.test"] = (float("inf"), {"@agent_two:example.test": {}})
            await adapter.output(key, "final", "@agent_two:example.test please take this", "$new2")
            self.assertEqual(calls[-1][2]["m.mentions"]["user_ids"],
                             ["@agent_two:example.test"])
            self.assertEqual(calls[-1][2]["org.example.pi_bridge"]["invitations"],
                             ["@agent_two:example.test"])
            self.assertEqual(calls[-1][2]["org.example.pi_bridge"]["hop"], 1)
            adapter._scope_hops[key] = 3
            adapter._requesters[(key, "$new3")] = "@operator:example.test"
            await adapter.output(key, "final", "@agent_two:example.test hand off again", "$new3")
            self.assertEqual(calls[-1][2]["org.example.pi_bridge"]["invitations"],
                             [])
            self.assertEqual(calls[-1][2]["m.mentions"]["user_ids"],
                             ["@operator:example.test", "@agent_two:example.test"])
            invitation = {"type": "m.room.message", "event_id": "$invite",
                          "sender": "@agent_two:example.test",
                          "content": {"msgtype": "m.text",
                                      "body": "@agent:example.test please take this",
                                      "m.mentions": {"user_ids": ["@agent:example.test"]},
                                      "org.example.pi_bridge": {
                                          "kind": "final",
                                          "hop": 1,
                                          "invitations": ["@agent:example.test"]}}}
            await adapter._handle_event("!room:example.test", invitation)
            self.assertEqual(runtime.submitted, ["$new", "$invite"])
            with patch.dict(os.environ, {"TEST_MATRIX_TOKEN": "test"}):
                restarted = MatrixAdapter("agent_one", settings, runtime, Path(directory))
            self.assertIn("$new", restarted._seen)

    async def test_stop_is_awaitable(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = MatrixSettings("https://example.test", "@agent:example.test",
                                      "TEST_MATRIX_TOKEN", ("!room:example.test",))
            with patch.dict(os.environ, {"TEST_MATRIX_TOKEN": "test"}):
                adapter = MatrixAdapter("agent_one", settings, None, Path(directory))
            await adapter.stop()
            self.assertTrue(adapter._stopping.is_set())

    async def test_poisoned_event_is_seen_and_next_event_runs(self):
        class FakeRuntime:
            def __init__(self):
                self.events = []

            async def is_active(self, key):
                return False

            async def is_awake(self, key):
                return False

            async def admit(self, key, can_wake, *, on_wait=None):
                return can_wake

            async def last_wake(self, key):
                return None, None

            async def record_wake(self, key, event_id):
                pass

            async def submit(self, key, incoming):
                self.events.append(incoming.message_id)
                if incoming.message_id == "$bad":
                    raise RuntimeError("checkpoint_error")

            async def record_activity(self, key):
                pass

        with tempfile.TemporaryDirectory() as directory:
            settings = MatrixSettings("https://example.test", "@agent:example.test",
                                      "TEST_MATRIX_TOKEN", ("!room:example.test",))
            runtime = FakeRuntime()
            with patch.dict(os.environ, {"TEST_MATRIX_TOKEN": "test"}):
                adapter = MatrixAdapter("agent_one", settings, runtime, Path(directory))

            async def request(method, path, *, data=None, params=None, timeout=40,
                                    attempts=MATRIX_MAX_ATTEMPTS, budget=None, stop=None):
                if method == "GET":
                    return {"chunk": []}
                return {"event_id": "$out"}

            adapter._request = request
            for event_id in ("$bad", "$good"):
                event = {"type": "m.room.message", "event_id": event_id,
                         "sender": "@operator:example.test",
                         "content": {"msgtype": "m.text",
                                     "body": "@agent:example.test help"}}
                await adapter._handle_event("!room:example.test", event)
            self.assertEqual(runtime.events, ["$bad", "$good"])
            self.assertIn("$bad", adapter._seen)
            malformed = {"content": {"body": "hello", "m.mentions": {"user_ids": 5}}}
            self.assertFalse(addressed_to(malformed, "@agent:example.test"))


class DiscordDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_compaction_notice_waits_before_history_and_submit(self):
        order = []
        runtime = SimpleNamespace(
            is_awake=AsyncMock(return_value=False), submit=AsyncMock(),
            record_wake=AsyncMock())

        async def admit(key, can_wake, *, on_wait=None):
            await on_wait()
            order.append("finished")
            return True

        runtime.admit = admit
        bot = SimpleNamespace(id=9)
        guild = SimpleNamespace(id=1)
        channel = SimpleNamespace(id=2, guild=guild, parent_id=None)
        message = SimpleNamespace(
            id=12, channel=channel, guild=guild,
            author=SimpleNamespace(id=3, display_name="Operator", bot=False),
            mentions=[bot], content="<@9> next", attachments=[], reply=AsyncMock())
        adapter = DiscordAdapter("agent_two", DiscordSettings("TOKEN", 1, (2,)), runtime)
        adapter.client = SimpleNamespace(user=bot)

        async def reply(text, **kwargs):
            self.assertEqual(text, "Compacting, one moment...")
            self.assertFalse(kwargs["mention_author"])
            order.append("notice")

        message.reply.side_effect = reply

        async def context(incoming):
            self.assertEqual(order, ["notice", "finished"])
            order.append("context")
            return "Earlier posts"

        adapter._context = context
        await adapter._on_message(message)
        self.assertEqual(order, ["notice", "finished", "context"])
        self.assertIn("Earlier posts", runtime.submit.await_args.args[1].text)


    async def test_discord_startup_notice_once_across_ready_reconnect(self):
        channel = SimpleNamespace(guild=SimpleNamespace(id=123), send=AsyncMock())
        client = SimpleNamespace(user=SimpleNamespace(id=789),
                                 get_channel=Mock(return_value=channel),
                                 fetch_channel=AsyncMock(), close=AsyncMock())
        events = {}
        def event(handler):
            events[handler.__name__] = handler
            return handler
        client.event = event
        async def start(token):
            await events["on_ready"]()
            await events["on_ready"]()
        client.start = start
        tree = SimpleNamespace(sync=AsyncMock(), command=lambda **kw: lambda handler: handler)
        discord = SimpleNamespace(
            Intents=SimpleNamespace(default=lambda: SimpleNamespace()),
            Client=lambda intents: client,
            app_commands=SimpleNamespace(CommandTree=lambda client: tree),
            Object=lambda id: SimpleNamespace(id=id),
            Embed=__import__("discord").Embed,
            AllowedMentions=SimpleNamespace(none=lambda: "no-mentions"))
        adapter = DiscordAdapter("agent_two", DiscordSettings("TEST_DISCORD_TOKEN", 123,
                                 (456,), startup_channel_id=456), None)
        with patch.dict(sys.modules, {"discord": discord}), patch.dict(
                os.environ, {"TEST_DISCORD_TOKEN": "test"}):
            await adapter.run()
        channel.send.assert_awaited_once()
        args, kwargs = channel.send.call_args
        self.assertIn(f"agent-bridge v{__version__} connected — agent_two (bot 789)", args[0])
        self.assertEqual(kwargs["allowed_mentions"], "no-mentions")
        self.assertEqual(tree.sync.await_count, 2)

    async def test_runtime_compaction_notice_retains_discord_request_without_activity(self):
        runtime = SimpleNamespace(record_activity=AsyncMock())
        adapter = DiscordAdapter("agent_two", DiscordSettings("TOKEN", 1, (2,)), runtime)
        key = SessionKey("agent_two", "discord", "2")
        request = SimpleNamespace(reply=AsyncMock(), channel=SimpleNamespace(id=2))
        adapter._requests[(key, "10")] = request
        await adapter.output(key, "compacting", "Compacting, one moment...", "10")
        self.assertEqual(request.reply.await_args.args, ("Compacting, one moment...",))
        self.assertFalse(request.reply.await_args.kwargs["allowed_mentions"].everyone)
        self.assertIn("compacting", request.reply.await_args.kwargs["embed"].footer.text)
        self.assertIs(adapter._requests[(key, "10")], request)
        runtime.record_activity.assert_not_awaited()

    async def test_ack_uses_discord_typing_api_and_posts_progress(self):
        adapter = DiscordAdapter("agent_two", DiscordSettings("TOKEN", 1, (2,)),
                                 SimpleNamespace(record_activity=AsyncMock()))
        key = SessionKey("agent_two", "discord", "2")
        typing = AsyncMock()
        progress = object()
        reply = AsyncMock(return_value=progress)
        adapter._requests[(key, "10")] = SimpleNamespace(
            channel=SimpleNamespace(typing=typing), reply=reply)

        await adapter.output(key, "ack", "I’m working on it.", "10")

        typing.assert_awaited_once_with()
        # The runtime's acknowledgement text (“I’m reading the image…”) reaches
        # the room, matching what Matrix posts; the fallback covers empty text.
        self.assertEqual(reply.await_args.args, ("I’m working on it.",))
        self.assertFalse(reply.await_args.kwargs["allowed_mentions"].everyone)
        self.assertIs(adapter._progress[(key, "10")], progress)

        reply.reset_mock()
        adapter._requests[(key, "11")] = adapter._requests.pop((key, "10"))
        await adapter.output(key, "ack", "", "11")
        self.assertEqual(reply.await_args.args, ("Working…",))

    async def test_scope_delivery_preserves_arrival_order_during_context_read(self):
        class FakeRuntime:
            def __init__(self):
                self.received = []

            async def is_awake(self, key):
                return False

            async def admit(self, key, can_wake, *, on_wait=None):
                return can_wake

            async def record_wake(self, key, event_id):
                pass

            async def submit(self, key, incoming):
                self.received.append(incoming.message_id)

        runtime = FakeRuntime()
        adapter = DiscordAdapter("agent_two", DiscordSettings("TOKEN", 1, (2,)), runtime)
        bot = SimpleNamespace(id=9)
        adapter.client = SimpleNamespace(user=bot)
        first_context = asyncio.Event()

        async def context(message):
            if message.id == 10:
                await first_context.wait()
            return ""

        adapter._context = context
        channel = SimpleNamespace(id=2, parent_id=None)
        guild = SimpleNamespace(id=1)
        author = SimpleNamespace(id=3, display_name="Operator", bot=False)

        def message(number):
            return SimpleNamespace(id=number, channel=channel, guild=guild,
                                   author=author, mentions=[bot], content="help",
                                   attachments=[])

        first = asyncio.create_task(adapter._on_message(message(10)))
        await asyncio.sleep(0)
        second = asyncio.create_task(adapter._on_message(message(11)))
        await asyncio.sleep(0)
        first_context.set()
        await asyncio.gather(first, second)
        self.assertEqual(runtime.received, ["10", "11"])

    async def test_peer_bot_ping_expects_no_reaction(self):
        class FakeRuntime:
            def __init__(self):
                self.received = []

            async def is_awake(self, key):
                return True

            async def admit(self, key, can_wake, *, on_wait=None):
                return can_wake

            async def record_wake(self, key, event_id):
                pass

            async def submit(self, key, incoming):
                self.received.append(incoming)

        runtime = FakeRuntime()
        adapter = DiscordAdapter("agent_two", DiscordSettings("TOKEN", 1, (2,)), runtime)
        bot = SimpleNamespace(id=9)
        adapter.client = SimpleNamespace(user=bot)
        channel = SimpleNamespace(id=2, parent_id=None)
        guild = SimpleNamespace(id=1)

        def message(number, author):
            return SimpleNamespace(id=number, channel=channel, guild=guild,
                                   author=author, mentions=[bot], content="ping",
                                   attachments=[])

        await adapter._on_message(message(10, SimpleNamespace(id=3, display_name="Operator", bot=False)))
        await adapter._on_message(message(11, SimpleNamespace(id=4, display_name="Peer", bot=True)))
        self.assertEqual([incoming.message_id for incoming in runtime.received], ["10", "11"])
        self.assertTrue(runtime.received[0].ack_required)
        self.assertFalse(runtime.received[1].ack_required)


class MentionPingTests(unittest.IsolatedAsyncioTestCase):
    def _adapter(self, directory, agent="agent_one"):
        settings = MatrixSettings("https://example.test", f"@{agent}:example.test",
                                  "TEST_MATRIX_TOKEN", ("!room:example.test",))
        runtime = SimpleNamespace(record_activity=AsyncMock())
        with patch.dict(os.environ, {"TEST_MATRIX_TOKEN": "test"}):
            return MatrixAdapter(agent, settings, runtime, Path(directory))

    async def test_matrix_final_rewrites_handle_into_a_real_mention(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter = self._adapter(directory)
            calls = []

            async def request(method, path, *, data=None, params=None, timeout=40,
                                    attempts=MATRIX_MAX_ATTEMPTS, budget=None, stop=None):
                calls.append((method, path, data))
                if method == "GET":
                    return {"joined": {
                        "@agent_two-peer:example.test": {"display_name": "Agent_two"},
                        "@operator:example.test": {"display_name": "Operator"}}}
                return {"event_id": "$out"}

            adapter._request = request
            key = SessionKey("agent_one", "matrix", "!room:example.test:main")
            await adapter.output(key, "final", "Approved, @agent_two-peer — nice work.", "$req")
            content = calls[-1][2]
            self.assertEqual(content["body"],
                             "Approved, @agent_two-peer:example.test — nice work.")
            self.assertIn("@agent_two-peer:example.test",
                          content["m.mentions"]["user_ids"])
            self.assertEqual(content["org.example.pi_bridge"]["kind"], "final")

    async def test_matrix_full_id_and_repeated_handle_render_once_each(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter = self._adapter(directory)
            calls = []

            async def request(method, path, *, data=None, params=None, timeout=40,
                                    attempts=MATRIX_MAX_ATTEMPTS, budget=None, stop=None):
                calls.append((method, path, data))
                if method == "GET":
                    return {"joined": {
                        "@agent_two-peer:example.test": {"display_name": "Agent_two"}}}
                return {"event_id": "$out"}

            adapter._request = request
            key = SessionKey("agent_one", "matrix", "!room:example.test:main")
            await adapter.output(key, "final",
                                 "Ping @agent_two-peer:example.test. Then @agent_two-peer.",
                                 "$req")
            content = calls[-1][2]
            self.assertEqual(content["body"],
                             "Ping @agent_two-peer:example.test. Then "
                             "@agent_two-peer:example.test.")
            self.assertEqual(content["m.mentions"]["user_ids"],
                             ["@agent_two-peer:example.test"])

    async def test_matrix_unknown_handle_returns_private_roster_feedback(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter = self._adapter(directory)
            calls = []

            async def request(method, path, *, data=None, params=None, timeout=40,
                                    attempts=MATRIX_MAX_ATTEMPTS, budget=None, stop=None):
                calls.append((method, path, data))
                if method == "GET":
                    return {"joined": {
                        "@agent_two-peer:example.test": {"display_name": "Agent_two"},
                        "@operator:example.test": {"display_name": "Operator"}}}
                return {"event_id": "$out"}

            adapter._request = request
            key = SessionKey("agent_one", "matrix", "!room:example.test:main")
            with self.assertRaises(ReplyRejected) as rejected:
                await adapter.output(key, "final", "Ask @ghost about it.", "$req")
            self.assertIn("Canonical targets: agent_two-peer (@agent_two-peer:example.test), operator (@operator:example.test)", str(rejected.exception))
            self.assertEqual([method for method, _, _ in calls], ["GET", "GET"])

    async def test_matrix_leaves_code_handles_and_known_ids_alone(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter = self._adapter(directory)
            calls = []

            async def request(method, path, *, data=None, params=None, timeout=40,
                                    attempts=MATRIX_MAX_ATTEMPTS, budget=None, stop=None):
                calls.append((method, path, data))
                if method == "GET":
                    return {"joined": {
                        "@operator:example.test": {"display_name": "Operator"}}}
                return {"event_id": "$out"}

            adapter._request = request
            key = SessionKey("agent_one", "matrix", "!room:example.test:main")
            await adapter.output(key, "final", "Use `@property`, then ask @operator.", "$req")
            content = calls[-1][2]
            self.assertIn("`@property`", content["body"])
            self.assertIn("@operator:example.test", content["m.mentions"]["user_ids"])

    async def test_matrix_returns_private_feedback_when_the_roster_is_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter = self._adapter(directory)
            calls = []

            async def request(method, path, *, data=None, params=None, timeout=40,
                                    attempts=MATRIX_MAX_ATTEMPTS, budget=None, stop=None):
                calls.append((method, path, data))
                if method == "GET":
                    return {"chunk": []}
                return {"event_id": "$out"}

            adapter._request = request
            key = SessionKey("agent_one", "matrix", "!room:example.test:main")
            with self.assertRaisesRegex(ReplyRejected, "Membership is unavailable"):
                await adapter.output(key, "final", "Ask @ghost about it.", "$req")
            self.assertTrue(all(method == "GET" for method, _, _ in calls))

    async def test_matrix_raw_room_token_never_requests_a_room_ping(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter = self._adapter(directory)
            calls = []

            async def request(method, path, *, data=None, params=None, timeout=40,
                                    attempts=MATRIX_MAX_ATTEMPTS, budget=None, stop=None):
                calls.append((method, path, data))
                if method == "GET":
                    return {"chunk": []}  # roster unavailable; raw text remains
                return {"event_id": "$out"}

            adapter._request = request
            key = SessionKey("agent_one", "matrix", "!room:example.test:main")
            await adapter.output(key, "final", r"Please check, \@room", "$req")
            content = calls[-1][2]
            self.assertEqual(content["body"], r"Please check, \@room")
            self.assertEqual(content["m.mentions"],
                             {"user_ids": [], "room": False})

    async def test_discord_resolves_display_names_and_rejects_unknown(self):
        class Member:
            def __init__(self, identifier, name, display_name, global_name=None):
                self.id = identifier
                self.name = name
                self.display_name = display_name
                self.global_name = global_name

        adapter = DiscordAdapter("agent_two", DiscordSettings("TOKEN", 1, (2,)),
                                 SimpleNamespace(record_activity=AsyncMock()))
        members = [Member(3, "operator", "Operator"), Member(4, "agent_two", "Agent_two")]
        request = SimpleNamespace(guild=SimpleNamespace(members=members),
                                  channel=SimpleNamespace(members=[]))
        text, user_ids, role_ids, rejected = await adapter._render_mentions(request, "ping @Operator")
        self.assertEqual((text, user_ids, role_ids, rejected),
                         ("ping <@3>", [3], [], False))
        text, user_ids, role_ids, rejected = await adapter._render_mentions(request, "ping @ghost")
        self.assertTrue(rejected)
        self.assertIn("Canonical targets: operator (<@3>), agent_two (<@4>)", text)
        self.assertEqual(user_ids, [])
        self.assertEqual(role_ids, [])

    async def test_discord_leaves_handles_alone_without_a_member_cache(self):
        adapter = DiscordAdapter("agent_two", DiscordSettings("TOKEN", 1, (2,)),
                                 SimpleNamespace(record_activity=AsyncMock()))
        request = SimpleNamespace(guild=SimpleNamespace(members=[]),
                                  channel=SimpleNamespace(members=[]))
        text, users, roles, rejected = await adapter._render_mentions(request, "ping @Operator")
        self.assertTrue(rejected)
        self.assertIn("Membership is unavailable", text)
        self.assertEqual((users, roles), ([], []))

    async def test_discord_final_allows_only_resolved_user_pings(self):
        adapter = DiscordAdapter("agent_two", DiscordSettings("TOKEN", 1, (2,)),
                                 SimpleNamespace(record_activity=AsyncMock()))
        key = SessionKey("agent_two", "discord", "2")
        reply = AsyncMock()
        request = SimpleNamespace(
            guild=SimpleNamespace(members=[]),
            channel=SimpleNamespace(members=[], send=AsyncMock()), reply=reply)
        adapter._requests[(key, "10")] = request

        # Broadcasts are rejected even without a member cache, before any post.
        with self.assertRaisesRegex(ReplyRejected, "Broadcast pings are not allowed"):
            await adapter.output(key, "final", "Status for @everyone and <@&123>", "10")
        reply.assert_not_awaited()

        member = SimpleNamespace(id=3, name="operator", global_name=None,
                                 display_name="Operator")
        request.guild.members = [member]
        adapter._requests[(key, "11")] = request
        reply.reset_mock()
        await adapter.output(key, "final", "Please check this, @Operator.", "11")
        args, kwargs = reply.await_args
        self.assertEqual(args[0], "Please check this, <@3>.")
        allowed = kwargs["allowed_mentions"]
        self.assertEqual(allowed.to_dict(),
                         {"users": [3], "roles": [], "replied_user": True,
                          "parse": []})

    async def test_discord_allows_named_roles_but_never_broadcast_roles(self):
        def role(identifier, name, mentionable=True, default=False):
            return SimpleNamespace(id=identifier, name=name, mentionable=mentionable,
                                   is_default=lambda: default)

        adapter = DiscordAdapter("agent_two", DiscordSettings("TOKEN", 1, (2,)),
                                 SimpleNamespace(record_activity=AsyncMock()))
        key = SessionKey("agent_two", "discord", "2")
        reply = AsyncMock()
        guild = SimpleNamespace(id=1, members=[], roles=[
            role(1, "@everyone", default=True), role(4, "Ops Team"),
            role(5, "Private", mentionable=False), role(6, "here"),
            role(7, "channel")])
        request = SimpleNamespace(
            guild=guild, channel=SimpleNamespace(members=[], send=AsyncMock()),
            reply=reply)
        adapter._requests[(key, "20")] = request
        await adapter.output(key, "final", "Ping @Ops-Team now.", "20")
        args, kwargs = reply.await_args
        self.assertEqual(args[0], "Ping <@&4> now.")
        self.assertEqual(kwargs["allowed_mentions"].to_dict(),
                         {"roles": [4], "users": [], "replied_user": True,
                          "parse": []})

        adapter._requests[(key, "20a")] = request
        reply.reset_mock()
        await adapter.output(key, "final", "Meet here for the update.", "20a")
        args, kwargs = reply.await_args
        self.assertEqual(args[0], "Meet here for the update.")
        self.assertEqual(kwargs["allowed_mentions"].to_dict(),
                         {"roles": [], "users": [], "replied_user": True,
                          "parse": []})

        for index, broadcast in enumerate(("@everyone", "@here", "@channel",
                                           "<@&1>", "<@&5>"), start=21):
            adapter._requests[(key, str(index))] = request
            reply.reset_mock()
            if broadcast in {"@everyone", "@here", "@channel"}:
                with self.assertRaisesRegex(ReplyRejected, "Broadcast pings are not allowed"):
                    await adapter.output(key, "final", f"Ping {broadcast} now.", str(index))
                reply.assert_not_awaited()
                continue
            with self.assertRaises(ReplyRejected):
                await adapter.output(key, "final", f"Ping {broadcast} now.", str(index))
            reply.assert_not_awaited()
