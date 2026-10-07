"""Delivery behaviors the transports owe the operator beyond command parsing.

Progress cards must be created, edited, and cleaned up in order; context and
history readers must degrade to honest omissions instead of crashes; image and
reaction intake must stay inside the approved scope. A stuck "Working…" card
or a silently dropped attachment reads like a crashed bridge, so these paths
are pinned before a deploy trusts them live.
"""

import datetime
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bridge.adapters.discord import DiscordAdapter, chunks
from bridge.adapters.matrix import MatrixAdapter, progress_html
from bridge.config import DiscordSettings, MatrixSettings
from bridge.reaction_service import ReactionRejected
from bridge.runtime.session import SessionKey

ROOM = "!room:example.test"
AGENT_ID = "@agent_one:example.test"
OPERATOR = "@operator:example.test"
STAMP = datetime.datetime(2025, 1, 1, tzinfo=datetime.timezone.utc)


def matrix_runtime(**overrides):
    runtime = SimpleNamespace(
        is_awake=AsyncMock(return_value=True),
        admit=AsyncMock(return_value=True),
        submit=AsyncMock(),
        last_wake=AsyncMock(return_value=("", 0)),
        record_wake=AsyncMock(),
        record_activity=AsyncMock(),
    )
    for name, value in overrides.items():
        setattr(runtime, name, value)
    return runtime


def matrix_adapter(directory, runtime):
    settings = MatrixSettings("https://example.test", AGENT_ID, "TEST_MATRIX_TOKEN",
                              (ROOM,))
    with patch.dict(os.environ, {"TEST_MATRIX_TOKEN": "test"}):
        return MatrixAdapter("agent_one", settings, runtime, Path(directory))


def matrix_key():
    return SessionKey("agent_one", "matrix", f"{ROOM}:main")


class MatrixPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_since_token_survives_atomic_rewrite(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter = matrix_adapter(directory, matrix_runtime())
            adapter._write_since("tok-42")
            self.assertEqual(adapter._since_file.read_text().strip(), "tok-42")
            adapter._write_since("tok-43")
            self.assertEqual(adapter._since_file.read_text().strip(), "tok-43")
            self.assertEqual(os.stat(adapter._since_file).st_mode & 0o777, 0o600)

    async def test_progress_status_renders_tool_names_as_html(self):
        self.assertEqual(progress_html("Using `Bash(ls)`…"),
                         "Using <code>Bash(ls)</code>…")
        self.assertIsNone(progress_html("plain progress"))
        self.assertIsNotNone(progress_html("Using `<script>`…"))
        self.assertNotIn("<script>", progress_html("Using `<script>`…"))


class MatrixHistoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_context_failure_degrades_to_no_context(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter = matrix_adapter(directory, matrix_runtime())
            adapter._request = AsyncMock(side_effect=RuntimeError("boom"))
            self.assertEqual(await adapter._history(ROOM, "$e"), "")

    async def test_history_page_rejects_foreign_scopes(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter = matrix_adapter(directory, matrix_runtime())
            with self.assertRaises(ValueError):
                await adapter.history_page(
                    SessionKey("agent_one", "discord", "2"), None, 10)
            with self.assertRaises(ValueError):
                await adapter.history_page(
                    SessionKey("agent_two", "matrix", f"{ROOM}:main"), None, 10)
            with self.assertRaises(ValueError):
                await adapter.history_page(
                    SessionKey("agent_one", "matrix", "!other:example.test:main"), None, 10)

    async def test_history_page_reads_main_and_thread_scopes(self):
        text = {"type": "m.room.message", "event_id": "$1", "sender": OPERATOR,
                "origin_server_ts": 1_700_000_000_000,
                "content": {"msgtype": "m.text", "body": "hello"}}
        image = {"type": "m.room.message", "event_id": "$2", "sender": OPERATOR,
                 "origin_server_ts": 1_700_000_001_000,
                 "content": {"msgtype": "m.image", "body": "photo"}}
        off_thread = {"type": "m.room.message", "event_id": "$3", "sender": OPERATOR,
                      "origin_server_ts": 1_700_000_002_000,
                      "content": {"msgtype": "m.text", "body": "elsewhere",
                                  "m.relates_to": {"rel_type": "m.thread",
                                                   "event_id": "$other"}}}
        with tempfile.TemporaryDirectory() as directory:
            adapter = matrix_adapter(directory, matrix_runtime())
            adapter._request = AsyncMock(return_value={"chunk": [text, image, off_thread],
                                                       "end": "tok"})
            key = matrix_key()
            page = await adapter.history_page(key, None, 10)
            self.assertEqual([item["content"] for item in page["messages"]],
                             ["hello", "[image] photo"])
            self.assertTrue(page["has_more"])
            self.assertEqual(page["next_before"], "tok")
            request_path = adapter._request.await_args.args[1]
            self.assertIn("/messages", request_path)

            adapter._request = AsyncMock(return_value={"chunk": [text, off_thread],
                                                       "end": ""})
            thread_key = SessionKey("agent_one", "matrix", f"{ROOM}:thread:$root")
            text["content"]["m.relates_to"] = {"rel_type": "m.thread",
                                               "event_id": "$root"}
            page = await adapter.history_page(thread_key, None, 10)
            self.assertEqual([item["content"] for item in page["messages"]], ["hello"])
            self.assertFalse(page["has_more"])
            self.assertEqual(page["next_before"], "")
            request_path = adapter._request.await_args.args[1]
            self.assertIn("/relations/", request_path)


class MatrixReactionTests(unittest.IsolatedAsyncioTestCase):
    async def test_react_stays_in_the_active_configured_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter = matrix_adapter(directory, matrix_runtime())
            key = matrix_key()
            target = {"event_id": "$m1", "room_id": ROOM, "type": "m.room.message",
                      "sender": OPERATOR, "origin_server_ts": 1_700_000_000_000,
                      "content": {"msgtype": "m.text", "body": "hi"}}
            adapter._request = AsyncMock(side_effect=[target, {}])
            with patch.dict(os.environ, {}):
                await adapter.react(key, "$m1", "👍")
            calls = adapter._request.await_args_list
            self.assertEqual(calls[0].args[0], "GET")
            put = calls[1]
            self.assertEqual(put.args[0], "PUT")
            self.assertIn("m.reaction", put.args[1])
            adapter.runtime.record_activity.assert_awaited_once_with(key)

    async def test_react_rejects_out_of_scope_targets(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter = matrix_adapter(directory, matrix_runtime())
            with self.assertRaisesRegex(ReactionRejected, "Invalid Matrix reaction target"):
                await adapter.react(SessionKey("agent_one", "discord", "2"), "$m", "👍")
            with self.assertRaisesRegex(ReactionRejected, "Invalid Matrix reaction target"):
                await adapter.react(matrix_key(), "plain-id", "👍")
            with self.assertRaisesRegex(ReactionRejected, "Room is not configured"):
                await adapter.react(
                    SessionKey("agent_one", "matrix", "!other:example.test:main"),
                    "$m", "👍")
            adapter._request = AsyncMock(return_value={"event_id": "$m",
                                                       "room_id": "!other:example.test"})
            with self.assertRaisesRegex(ReactionRejected, "outside this room or thread"):
                await adapter.react(matrix_key(), "$m", "👍")

    async def test_react_in_thread_checks_the_thread_root(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter = matrix_adapter(directory, matrix_runtime())
            key = SessionKey("agent_one", "matrix", f"{ROOM}:thread:$root")
            thread_target = {"event_id": "$m2", "room_id": ROOM,
                             "type": "m.room.message", "sender": OPERATOR,
                             "origin_server_ts": 1_700_000_000_000,
                             "content": {"msgtype": "m.text", "body": "hi",
                                         "m.relates_to": {"rel_type": "m.thread",
                                                          "event_id": "$root"}}}
            adapter._request = AsyncMock(side_effect=[thread_target, {}])
            await adapter.react(SessionKey("agent_one", "matrix",
                                           f"{ROOM}:thread:$root"), "$m2", "👍")
            self.assertEqual(adapter._request.await_count, 2)

            adapter._request = AsyncMock(return_value={
                "event_id": "$m2", "room_id": ROOM, "type": "m.room.message",
                "sender": OPERATOR, "origin_server_ts": 1_700_000_000_000,
                "content": {"msgtype": "m.text", "body": "hi",
                            "m.relates_to": {"rel_type": "m.thread",
                                             "event_id": "$other"}}})
            with self.assertRaisesRegex(ReactionRejected, "outside this room or thread"):
                await adapter.react(key, "$m2", "👍")


class FakeImageResponse:
    def __init__(self, status, content_type, data):
        self.status = status
        self.headers = {"Content-Type": content_type}
        self.content = SimpleNamespace(read=AsyncMock(return_value=data))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class MatrixImageTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_mxc_image_urls_are_downloaded(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter = matrix_adapter(directory, matrix_runtime())
            self.assertIsNone(await adapter._image({}))
            self.assertIsNone(await adapter._image({"url": "https://example.test/x.png"}))
            self.assertIsNone(await adapter._image({"url": "mxc://no-media-id"}))

    async def test_image_download_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter = matrix_adapter(directory, matrix_runtime())
            content = {"msgtype": "m.image", "body": "photo",
                       "url": "mxc://example.test/media1"}
            for status, mime, data, expected in (
                    (500, "image/png", b"x", None),
                    (200, "text/html", b"x", None),
                    (200, "image/png", b"x" * 20_000_001, None),
                    (200, "image/png; charset=binary", b"png-bytes", "png"),
            ):
                with self.subTest(status=status, mime=mime):
                    response = FakeImageResponse(status, mime, data)
                    adapter.session = SimpleNamespace(get=lambda *args, **kwargs: response)
                    image = await adapter._image(content)
                    if expected is None:
                        self.assertIsNone(image)
                    else:
                        self.assertEqual(image.mime_type, "image/png")
                        self.assertEqual(image.data, b"png-bytes")


class MatrixEventGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_non_message_and_malformed_events_are_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = matrix_runtime()
            adapter = matrix_adapter(directory, runtime)
            await adapter._handle_event(ROOM, {"type": "m.reaction", "sender": OPERATOR})
            await adapter._handle_event(ROOM, {"type": "m.room.message", "sender": OPERATOR})
            await adapter._handle_event(
                ROOM, {"type": "m.room.message", "sender": OPERATOR, "event_id": "$e1",
                       "content": "not-a-dict"})
            runtime.admit.assert_not_awaited()
            self.assertNotIn("$e1", adapter._seen)

    async def test_duplicate_events_are_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = matrix_runtime()
            adapter = matrix_adapter(directory, runtime)
            adapter._seen["$dup"] = None
            await adapter._handle_event(ROOM, {"type": "m.room.message",
                                               "sender": OPERATOR, "event_id": "$dup",
                                               "content": {"msgtype": "m.text",
                                                           "body": "hi"}})
            runtime.admit.assert_not_awaited()


class MatrixSubmitFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_submit_failure_tells_the_sender_and_forgets_nothing_else(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = matrix_runtime(submit=AsyncMock(side_effect=RuntimeError("down")))
            adapter = matrix_adapter(directory, runtime)
            adapter.output = AsyncMock()
            adapter._history = AsyncMock(return_value="")
            event = {"type": "m.room.message", "event_id": "$fail", "sender": OPERATOR,
                     "content": {"msgtype": "m.text",
                                 "body": f"{AGENT_ID} please deploy"}}
            await adapter._handle_event(ROOM, event)
            self.assertEqual(adapter.output.await_args.args[1], "error")
            self.assertIn("recover", adapter.output.await_args.args[2])
            self.assertIn("$fail", adapter._seen)

    async def test_submit_failure_with_undeliverable_error_drops_the_requester(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = matrix_runtime(submit=AsyncMock(side_effect=RuntimeError("down")))
            adapter = matrix_adapter(directory, runtime)
            adapter.output = AsyncMock(side_effect=RuntimeError("no room"))
            adapter._history = AsyncMock(return_value="")
            event = {"type": "m.room.message", "event_id": "$fail2", "sender": OPERATOR,
                     "content": {"msgtype": "m.text", "body": f"{AGENT_ID} hi"}}
            await adapter._handle_event(ROOM, event)
            self.assertNotIn((matrix_key(), "$fail2"), adapter._requesters)
            self.assertIn("$fail2", adapter._seen)


class MatrixOutputTests(unittest.IsolatedAsyncioTestCase):
    async def test_ack_cards_are_registered_and_final_output_clears_them(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = matrix_runtime()
            adapter = matrix_adapter(directory, runtime)
            adapter._request = AsyncMock(side_effect=[{"event_id": "$ack-9"},
                                                      {"event_id": "$final-9"}])
            key = matrix_key()
            adapter._requesters[(key, "$e9")] = OPERATOR
            await adapter.output(key, "ack", "Working…", "$e9")
            self.assertEqual(adapter._acks[(key, "$e9")], "$ack-9")

            await adapter.output(key, "final", "Done, all set.", "$e9")
            self.assertNotIn((key, "$e9"), adapter._acks)
            self.assertNotIn((key, "$e9"), adapter._requesters)
            final_put = adapter._request.await_args_list[-1]
            content = final_put.kwargs["data"]
            self.assertEqual(content["m.mentions"]["user_ids"], [OPERATOR])
            self.assertFalse(content["m.mentions"]["room"])
            runtime.record_activity.assert_awaited()

    async def test_progress_requires_an_ack_and_edits_in_place(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = matrix_runtime()
            adapter = matrix_adapter(directory, runtime)
            adapter._request = AsyncMock(return_value={"event_id": "$out"})
            key = matrix_key()
            adapter._requesters[(key, "$p")] = OPERATOR

            await adapter.output(key, "progress", "Using `Read`…", "$p")
            adapter._request.assert_not_awaited()  # no ack to edit in place

            adapter._acks[(key, "$p")] = "$ack-card"
            await adapter.output(key, "progress", "Using `Read`…", "$p")
            content = adapter._request.await_args.kwargs["data"]
            self.assertEqual(content["m.relates_to"],
                             {"rel_type": "m.replace", "event_id": "$ack-card"})
            self.assertEqual(content["m.new_content"]["format"],
                             "org.matrix.custom.html")
            self.assertIn("<code>Read</code>", content["m.new_content"]["formatted_body"])

    async def test_steered_output_drops_the_pending_requester(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = matrix_runtime()
            adapter = matrix_adapter(directory, runtime)
            adapter._request = AsyncMock(return_value={"event_id": "$steered-1"})
            key = matrix_key()
            adapter._requesters[(key, "$s")] = OPERATOR
            await adapter.output(key, "steered", "Handled elsewhere.", "$s")
            self.assertNotIn((key, "$s"), adapter._requesters)

    async def test_cleanup_redacts_the_ack_card_and_tolerates_redact_failures(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = matrix_runtime()
            adapter = matrix_adapter(directory, runtime)
            key = matrix_key()
            adapter._acks[(key, "$c")] = "$ack-card"
            adapter._requesters[(key, "$c")] = OPERATOR
            adapter._request = AsyncMock(return_value={})
            await adapter.output(key, "cleanup", "", "$c")
            self.assertIn("/redact/", adapter._request.await_args.args[1])
            self.assertNotIn((key, "$c"), adapter._acks)
            self.assertNotIn((key, "$c"), adapter._requesters)

            adapter._acks[(key, "$c2")] = "$ack-card-2"
            adapter._request = AsyncMock(side_effect=RuntimeError("no power"))
            await adapter.output(key, "cleanup", "", "$c2")
            self.assertNotIn((key, "$c2"), adapter._acks)

    async def test_output_ignores_other_transports(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = matrix_runtime()
            adapter = matrix_adapter(directory, runtime)
            await adapter.output(SessionKey("agent_one", "discord", "2"),
                                 "ack", "Working…", "$x")
            runtime.record_activity.assert_not_awaited()

    async def test_room_member_lookup_failure_yields_no_roster(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter = matrix_adapter(directory, matrix_runtime())
            adapter._request = AsyncMock(side_effect=RuntimeError("nope"))
            self.assertIsNone(await adapter._room_members(ROOM))
            roster = {"@b:example.test": {"display_name": "Briana"}}
            adapter._rosters[ROOM] = (time.monotonic() + 60, roster)
            adapter._request = AsyncMock(side_effect=AssertionError("cached"))
            self.assertIs(await adapter._room_members(ROOM), roster)


def discord_adapter(runtime):
    adapter = DiscordAdapter("agent_two", DiscordSettings("TOKEN", 1, (2,)), runtime)
    adapter.client = SimpleNamespace(user=SimpleNamespace(id=9))
    return adapter


class FakeDiscordChannel:
    def __init__(self, items=None, id=2, error=False):
        self.id = id
        self.parent_id = None
        self.guild = SimpleNamespace(id=1)
        self.items = items or []
        self.error = error
        self.send = AsyncMock()
        self.typing = AsyncMock()

    def history(self, **_kwargs):
        if self.error:
            raise RuntimeError("history unavailable")

        async def iterate():
            for item in self.items:
                yield item
        return iterate()


def discord_item(id, content, author_id=3, name="Operator"):
    return SimpleNamespace(id=id, content=content, created_at=STAMP,
                           author=SimpleNamespace(id=author_id, display_name=name))


class DiscordChunkTests(unittest.TestCase):
    def test_chunk_limit_must_be_positive(self):
        with self.assertRaises(ValueError):
            chunks("hello", 0)

    def test_long_replies_split_at_line_boundaries(self):
        text = "\n".join(f"line {index}" for index in range(400))
        pieces = chunks(text)
        self.assertEqual("".join(pieces).replace("", ""), text)
        self.assertTrue(all(len(piece) <= 1900 for piece in pieces))
        self.assertGreater(len(pieces), 1)


class DiscordContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_context_failure_degrades_to_no_context(self):
        adapter = discord_adapter(matrix_runtime())
        message = SimpleNamespace(id=10, channel=FakeDiscordChannel(error=True),
                                  attachments=[])
        self.assertEqual(await adapter._context(message), "")

    async def test_context_skips_own_and_blank_messages(self):
        adapter = discord_adapter(matrix_runtime())
        items = [discord_item(1, "first"), discord_item(2, ""),
                 discord_item(3, "self", author_id=9), discord_item(4, "second")]
        message = SimpleNamespace(id=10, channel=FakeDiscordChannel(items),
                                  attachments=[])
        context = await adapter._context(message)
        self.assertIn("first", context)
        self.assertIn("second", context)
        self.assertNotIn("self", context)

    async def test_missing_previous_wake_marker_is_disclosed(self):
        runtime = matrix_runtime(last_wake=AsyncMock(return_value=("999", 0)))
        adapter = discord_adapter(runtime)
        message = SimpleNamespace(id=10,
                                  channel=FakeDiscordChannel([discord_item(1, "hi")]),
                                  attachments=[])
        context = await adapter._context(message)
        self.assertIn("[earlier messages may be omitted; count unknown]", context)

    async def test_context_trims_to_the_character_budget(self):
        runtime = matrix_runtime(last_wake=AsyncMock(return_value=("", 0)))
        adapter = discord_adapter(runtime)
        items = [discord_item(index, "x" * 1500, author_id=4 + index)
                 for index in range(1, 31)]
        message = SimpleNamespace(id=100, channel=FakeDiscordChannel(items),
                                  attachments=[])
        context = await adapter._context(message)
        self.assertLessEqual(len(context), 26_000)
        self.assertIn("[older messages omitted; count unknown]", context)


class DiscordImageTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_safe_discord_images_are_downloaded(self):
        adapter = discord_adapter(matrix_runtime())
        good = SimpleNamespace(content_type="image/png", size=5,
                               read=AsyncMock(return_value=b"png"))
        skipped = [SimpleNamespace(content_type="application/pdf", size=5,
                                   read=AsyncMock(side_effect=AssertionError("read"))),
                   SimpleNamespace(content_type="image/png", size=20_000_001,
                                   read=AsyncMock(side_effect=AssertionError("read"))),
                   SimpleNamespace(content_type="image/png", size=5,
                                   read=AsyncMock(side_effect=RuntimeError("gone")))]
        message = SimpleNamespace(attachments=[good, *skipped])
        images = await adapter._images(message)
        self.assertEqual([(image.mime_type, image.data) for image in images],
                         [("image/png", b"png")])


class DiscordMessageGuardsTests(unittest.IsolatedAsyncioTestCase):
    async def test_unmentioned_bot_traffic_is_ignored(self):
        runtime = matrix_runtime()
        adapter = discord_adapter(runtime)
        message = SimpleNamespace(
            id=10, content="hello", mentions=[], attachments=[],
            channel=FakeDiscordChannel(), guild=SimpleNamespace(id=1),
            author=SimpleNamespace(id=7, bot=True, display_name="WebHook"),
            reply=AsyncMock())
        await adapter._on_message(message)
        runtime.admit.assert_not_awaited()
        message.reply.assert_not_awaited()

    async def test_submit_failure_reports_to_the_channel(self):
        runtime = matrix_runtime(submit=AsyncMock(side_effect=RuntimeError("down")))
        adapter = discord_adapter(runtime)
        adapter.output = AsyncMock()
        message = SimpleNamespace(
            id=10, content="<@9> deploy", mentions=[SimpleNamespace(id=9)],
            attachments=[], channel=FakeDiscordChannel(),
            guild=SimpleNamespace(id=1),
            author=SimpleNamespace(id=3, bot=False, display_name="Operator"),
            reply=AsyncMock())
        await adapter._on_message(message)
        self.assertEqual(adapter.output.await_args.args[1], "error")
        self.assertIn("bridge_recover", adapter.output.await_args.args[2])

    async def test_submit_failure_with_undeliverable_error_drops_the_request(self):
        runtime = matrix_runtime(submit=AsyncMock(side_effect=RuntimeError("down")))
        adapter = discord_adapter(runtime)
        adapter.output = AsyncMock(side_effect=RuntimeError("gone"))
        message = SimpleNamespace(
            id=11, content="<@9> deploy", mentions=[SimpleNamespace(id=9)],
            attachments=[], channel=FakeDiscordChannel(),
            guild=SimpleNamespace(id=1),
            author=SimpleNamespace(id=3, bot=False, display_name="Operator"),
            reply=AsyncMock())
        await adapter._on_message(message)
        key = SessionKey("agent_two", "discord", "2")
        self.assertNotIn((key, "11"), adapter._requests)


class DiscordOutputTests(unittest.IsolatedAsyncioTestCase):
    async def test_output_ignores_other_transports(self):
        runtime = matrix_runtime()
        adapter = discord_adapter(runtime)
        await adapter.output(SessionKey("agent_two", "matrix", "!r:main"),
                             "ack", "Working…", "10")
        runtime.record_activity.assert_not_awaited()

    async def test_output_without_a_request_is_a_bug_report(self):
        adapter = discord_adapter(matrix_runtime())
        key = SessionKey("agent_two", "discord", "2")
        with self.assertRaises(RuntimeError):
            await adapter.output(key, "final", "hi", "404")

    async def test_progress_posts_once_then_edits(self):
        adapter = discord_adapter(matrix_runtime())
        key = SessionKey("agent_two", "discord", "2")
        card = SimpleNamespace(edit=AsyncMock())
        reply = AsyncMock(return_value=card)
        request = SimpleNamespace(channel=FakeDiscordChannel(), reply=reply)
        adapter._requests[(key, "10")] = request

        await adapter.output(key, "progress", "Using `Read` …", "10")
        reply.assert_awaited_once()
        await adapter.output(key, "progress", "Using `Bash` …", "10")
        card.edit.assert_awaited_once_with(content="Using `Bash` …")
        self.assertEqual(reply.await_count, 1)

    async def test_steered_replies_close_the_request(self):
        adapter = discord_adapter(matrix_runtime())
        key = SessionKey("agent_two", "discord", "2")
        reply = AsyncMock()
        adapter._requests[(key, "10")] = SimpleNamespace(reply=reply)
        await adapter.output(key, "steered", "Handled in Matrix.", "10")
        reply.assert_awaited_once_with("Handled in Matrix.", mention_author=False)
        self.assertNotIn((key, "10"), adapter._requests)

    async def test_final_replies_chunk_and_clear_progress(self):
        runtime = matrix_runtime()
        adapter = discord_adapter(runtime)
        key = SessionKey("agent_two", "discord", "2")
        card = SimpleNamespace(delete=AsyncMock())
        reply = AsyncMock()
        channel = FakeDiscordChannel()
        adapter._requests[(key, "10")] = SimpleNamespace(channel=channel, reply=reply)
        adapter._progress[(key, "10")] = card

        await adapter.output(key, "final", "all good " * 380, "10")
        self.assertEqual(reply.await_count, 1)
        self.assertEqual(channel.send.await_count, 1)
        card.delete.assert_awaited_once_with()
        self.assertNotIn((key, "10"), adapter._requests)
        self.assertNotIn((key, "10"), adapter._progress)
        runtime.record_activity.assert_awaited()

    async def test_final_survives_a_progress_card_that_cannot_be_deleted(self):
        adapter = discord_adapter(matrix_runtime())
        key = SessionKey("agent_two", "discord", "2")
        card = SimpleNamespace(delete=AsyncMock(side_effect=RuntimeError("403")))
        adapter._requests[(key, "10")] = SimpleNamespace(channel=FakeDiscordChannel(),
                                                         reply=AsyncMock())
        adapter._progress[(key, "10")] = card
        await adapter.output(key, "final", "done", "10")
        self.assertNotIn((key, "10"), adapter._requests)

    async def test_cleanup_deletes_progress_and_tolerates_failures(self):
        adapter = discord_adapter(matrix_runtime())
        key = SessionKey("agent_two", "discord", "2")
        card = SimpleNamespace(delete=AsyncMock(side_effect=RuntimeError("403")))
        adapter._requests[(key, "10")] = SimpleNamespace(reply=AsyncMock())
        adapter._progress[(key, "10")] = card
        await adapter.output(key, "cleanup", "", "10")
        self.assertNotIn((key, "10"), adapter._requests)
        self.assertNotIn((key, "10"), adapter._progress)


class DiscordHistoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_history_page_rejects_invalid_scopes(self):
        adapter = discord_adapter(matrix_runtime())
        with self.assertRaises(ValueError):
            await adapter.history_page(SessionKey("agent_two", "matrix", "2"), None, 5)
        with self.assertRaises(ValueError):
            await adapter.history_page(SessionKey("agent_two", "discord", "abc"),
                                       None, 5)

    async def test_history_page_fetches_missing_channels_and_checks_them(self):
        adapter = discord_adapter(matrix_runtime())
        adapter.client = SimpleNamespace(
            user=SimpleNamespace(id=9), get_channel=lambda _id: None,
            fetch_channel=AsyncMock(return_value=None))
        with self.assertRaises(ValueError):
            await adapter.history_page(SessionKey("agent_two", "discord", "2"), None, 5)

        disabled = SimpleNamespace(id=99, parent_id=None, guild=SimpleNamespace(id=1),
                                   history=None)
        adapter.client.fetch_channel = AsyncMock(return_value=disabled)
        with self.assertRaises(ValueError):
            await adapter.history_page(SessionKey("agent_two", "discord", "99"), None, 5)

    async def test_history_page_windows_and_pages_back(self):
        adapter = discord_adapter(matrix_runtime())
        items = [discord_item(index, f"message {index}") for index in range(1, 7)]
        adapter.client = SimpleNamespace(
            user=SimpleNamespace(id=9), get_channel=lambda _id: None,
            fetch_channel=AsyncMock(return_value=FakeDiscordChannel(items)))
        page = await adapter.history_page(SessionKey("agent_two", "discord", "2"),
                                          "9", 5)
        self.assertTrue(page["has_more"])
        self.assertEqual(len(page["messages"]), 5)
        self.assertEqual(page["next_before"], str(items[4].id))
        self.assertEqual(page["messages"][0]["content"], "message 1")


class DiscordReactionTests(unittest.IsolatedAsyncioTestCase):
    async def test_react_stays_in_an_approved_channel_and_scope(self):
        runtime = matrix_runtime()
        adapter = discord_adapter(runtime)
        target = SimpleNamespace(channel=SimpleNamespace(id=2),
                                 add_reaction=AsyncMock())
        channel = SimpleNamespace(id=2, parent_id=None,
                                  guild=SimpleNamespace(id=1),
                                  fetch_message=AsyncMock(return_value=target))
        adapter.client = SimpleNamespace(
            user=SimpleNamespace(id=9), get_channel=lambda _id: None,
            fetch_channel=AsyncMock(return_value=channel))
        key = SessionKey("agent_two", "discord", "2")
        await adapter.react(key, "77", "👍")
        target.add_reaction.assert_awaited_once_with("👍")
        adapter.runtime.record_activity.assert_awaited_once_with(key)

    async def test_react_rejects_out_of_scope_targets(self):
        adapter = discord_adapter(matrix_runtime())
        with self.assertRaisesRegex(ReactionRejected, "Invalid Discord reaction target"):
            await adapter.react(SessionKey("agent_two", "matrix", "2"), "77", "👍")
        with self.assertRaisesRegex(ReactionRejected, "Invalid Discord reaction target"):
            await adapter.react(SessionKey("agent_one", "discord", "2"), "77", "👍")
        with self.assertRaisesRegex(ReactionRejected, "Invalid Discord reaction target"):
            await adapter.react(SessionKey("agent_two", "discord", "2"), "bad", "👍")

        adapter.client = SimpleNamespace(user=SimpleNamespace(id=9),
                                         get_channel=lambda _id: None,
                                         fetch_channel=AsyncMock(return_value=None))
        with self.assertRaisesRegex(ReactionRejected, "Discord channel is not enabled"):
            await adapter.react(SessionKey("agent_two", "discord", "2"), "77", "👍")

        cached = SimpleNamespace(id=2, parent_id=None, guild=SimpleNamespace(id=1),
                                 fetch_message=AsyncMock(return_value=SimpleNamespace(
                                     channel=SimpleNamespace(id=99))))
        adapter.client = SimpleNamespace(user=SimpleNamespace(id=9),
                                         get_channel=lambda _id: cached)
        with self.assertRaisesRegex(ReactionRejected, "outside this channel"):
            await adapter.react(SessionKey("agent_two", "discord", "2"), "77", "👍")


class DiscordActivityTests(unittest.IsolatedAsyncioTestCase):
    async def test_presence_publish_requires_a_ready_client(self):
        from bridge.activity import Activity, Presence
        adapter = discord_adapter(matrix_runtime())
        adapter.client = None
        self.assertFalse(await adapter.publish_activity(
            Activity(text="working", presence=Presence.AWAKE)))


if __name__ == "__main__":
    unittest.main()
