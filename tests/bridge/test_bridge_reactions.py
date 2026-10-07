import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from bridge.adapters.discord import DiscordAdapter
from bridge.adapters.matrix import MatrixAdapter
from bridge.config import DiscordSettings, MatrixSettings
from bridge.react_cli import send
from bridge.reaction_service import ReactionRejected, ReactionService, validate_request
from bridge.runtime.pi_rpc import PiRunResult
from bridge.runtime.session import BridgeRuntime, Incoming, split_pass
from bridge.runtime.state import SessionKey


def reaction_request(emoji="👍", **changes):
    request = {"transport": "matrix", "scope": "!room:example.test:main",
               "message_id": "$message", "emoji": emoji, "capability": "x" * 40}
    request.update(changes)
    return request


class EmojiValidationTests(unittest.TestCase):
    def test_standard_emoji_families_and_presentation(self):
        for form in ("😀", "👍", "👎", "👍🏽", "🍷", "☕", "📓", "🕒", "🇺🇸", "👩🏽‍💻"):
            with self.subTest(form=form):
                self.assertEqual(validate_request(reaction_request(form))["emoji"], form)
        self.assertEqual(validate_request(reaction_request("❤"))["emoji"], "❤️")

    def test_rejects_text_alias_multiple_emoji_and_components(self):
        for form in ("", "hello", ":thumbsup:", "👍👎", "👍 thanks", "🏽", "<:custom:1>"):
            with self.subTest(form=form), self.assertRaises(ValueError):
                validate_request(reaction_request(form))


class ReactionSocketTests(unittest.IsolatedAsyncioTestCase):
    async def test_private_socket_and_cli_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            calls = []

            async def react(request):
                calls.append(request)

            service = ReactionService(Path(directory) / "react.sock", react)
            await service.start()
            try:
                self.assertEqual(service.socket_path.stat().st_mode & 0o777, 0o600)
                response = await asyncio.to_thread(
                    send, str(service.socket_path), "matrix", "!room:example.test:main",
                    "x" * 40, "$message", "❤")
                self.assertEqual(response, {"ok": True, "emoji": "❤️"})
                self.assertEqual(calls[0]["emoji"], "❤️")
                with self.assertRaisesRegex(RuntimeError, "standard Unicode emoji"):
                    await asyncio.to_thread(
                        send, str(service.socket_path), "matrix", "!room:example.test:main",
                        "x" * 40, "$message", "word")
            finally:
                await service.close()
            self.assertFalse(service.socket_path.exists())

    async def test_delivery_failure_logs_safe_cause_without_remote_details(self):
        for error_class in (RuntimeError, ValueError):
            with self.subTest(error_class=error_class.__name__):
                async def react(request):
                    raise error_class("remote body token=transport-secret")

                with tempfile.TemporaryDirectory() as directory:
                    service = ReactionService(Path(directory) / "react.sock", react)
                    await service.start()
                    try:
                        with self.assertLogs("agent_bridge.reaction", level="WARNING") as captured:
                            with self.assertRaisesRegex(RuntimeError, "reaction delivery failed"):
                                await asyncio.to_thread(
                                    send, str(service.socket_path), "matrix", "!room:example.test",
                                    "capability-secret-" + "x" * 40, "$message", "👀")
                    finally:
                        await service.close()
                self.assertEqual(len(captured.output), 1)
                self.assertIn("transport=matrix error_class=" + error_class.__name__,
                              captured.output[0])
                self.assertNotIn("transport-secret", captured.output[0])
                self.assertNotIn("capability-secret", captured.output[0])
                self.assertNotIn("!room:example.test", captured.output[0])

    async def test_unexpected_validation_failure_returns_generic_error(self):
        with tempfile.TemporaryDirectory() as directory:
            service = ReactionService(Path(directory) / "react.sock", AsyncMock())
            await service.start()
            try:
                with patch("bridge.reaction_service.validate_request",
                           side_effect=RuntimeError("token=validation-secret")):
                    with self.assertLogs("agent_bridge.reaction", level="WARNING") as captured:
                        with self.assertRaisesRegex(RuntimeError, "reaction request failed"):
                            await asyncio.to_thread(
                                send, str(service.socket_path), "matrix",
                                "!room:example.test", "x" * 40, "$message", "👀")
            finally:
                await service.close()
        self.assertIn("error_class=RuntimeError", captured.output[0])
        self.assertNotIn("validation-secret", captured.output[0])

    async def test_local_rejection_keeps_fixed_actionable_message(self):
        for reason, expected in (
                ("capability", "reaction capability is not valid for this conversation"),
                ("awake", "reactions are available only for an awake conversation")):
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as directory:
                async def reject(request):
                    raise ReactionRejected(reason)

                service = ReactionService(Path(directory) / "react.sock", reject)
                await service.start()
                try:
                    with self.assertRaisesRegex(RuntimeError, expected):
                        await asyncio.to_thread(
                            send, str(service.socket_path), "matrix",
                            "!room:example.test", "x" * 40, "$message", "👀")
                finally:
                    await service.close()

    async def test_matrix_adapter_rejections_keep_fixed_socket_messages(self):
        class Runtime:
            record_activity = AsyncMock()

        settings = MatrixSettings("https://example.test", "@agent_one:example.test",
                                  "TEST_MATRIX_TOKEN", ("!room:example.test",))
        with tempfile.TemporaryDirectory() as directory, patch.dict(
                os.environ, {"TEST_MATRIX_TOKEN": "test"}):
            adapter = MatrixAdapter("agent_one", settings, Runtime(), Path(directory))

            async def react(request):
                key = SessionKey("agent_one", request["transport"], request["scope"])
                await adapter.react(key, request["message_id"], request["emoji"])

            service = ReactionService(Path(directory) / "react.sock", react)
            await service.start()
            try:
                adapter._request = AsyncMock(return_value={"event_id": "$different"})
                with self.assertRaisesRegex(RuntimeError,
                                            "Reaction target is outside this room or thread"):
                    await asyncio.to_thread(send, str(service.socket_path), "matrix",
                                            "!room:example.test:main", "x" * 40, "$message", "👀")
                with self.assertRaisesRegex(RuntimeError,
                                            "Room is not configured for this bridge"):
                    await asyncio.to_thread(send, str(service.socket_path), "matrix",
                                            "!other:example.test:main", "x" * 40, "$message", "👀")
                adapter._request = AsyncMock(side_effect=ValueError("token=remote-secret"))
                with self.assertLogs("agent_bridge.reaction", level="WARNING") as captured:
                    with self.assertRaisesRegex(RuntimeError, "reaction delivery failed") as failure:
                        await asyncio.to_thread(send, str(service.socket_path), "matrix",
                                                "!room:example.test:main", "x" * 40,
                                                "$message", "👀")
                self.assertNotIn("remote-secret", str(failure.exception))
                self.assertIn("error_class=ValueError", captured.output[0])
                self.assertNotIn("remote-secret", captured.output[0])
            finally:
                await service.close()


class AdapterReactionTests(unittest.IsolatedAsyncioTestCase):
    async def test_matrix_reaction_checks_room_and_thread_then_annotations(self):
        class Runtime:
            record_activity = AsyncMock()

        settings = MatrixSettings("https://example.test", "@agent_one:example.test",
                                  "TEST_MATRIX_TOKEN", ("!room:example.test",))
        with tempfile.TemporaryDirectory() as directory, patch.dict(
                os.environ, {"TEST_MATRIX_TOKEN": "test"}):
            adapter = MatrixAdapter("agent_one", settings, Runtime(), Path(directory))
            target = {"event_id": "$message", "room_id": "!room:example.test",
                      "type": "m.room.message", "content": {
                          "msgtype": "m.text", "body": "hello",
                          "m.relates_to": {"rel_type": "m.thread", "event_id": "$root"}}}
            adapter._request = AsyncMock(side_effect=[target, {"event_id": "$reaction"}])
            key = SessionKey("agent_one", "matrix", "!room:example.test:thread:$root")
            await adapter.react(key, "$message", "☕")
            self.assertEqual(adapter._request.await_args_list[1].kwargs["data"], {
                "m.relates_to": {"rel_type": "m.annotation", "event_id": "$message",
                                 "key": "☕"}})
            self.assertIn("/send/m.reaction/", adapter._request.await_args_list[1].args[1])
            adapter._request = AsyncMock(return_value=target)
            with self.assertRaisesRegex(ValueError, "outside this room or thread"):
                await adapter.react(SessionKey("agent_one", "matrix", "!room:example.test:main"),
                                    "$message", "☕")

    async def test_reaction_only_cleans_matrix_ack(self):
        class Runtime:
            record_activity = AsyncMock()

        settings = MatrixSettings("https://example.test", "@agent_one:example.test",
                                  "TEST_MATRIX_TOKEN", ("!room:example.test",))
        with tempfile.TemporaryDirectory() as directory, patch.dict(
                os.environ, {"TEST_MATRIX_TOKEN": "test"}):
            adapter = MatrixAdapter("agent_one", settings, Runtime(), Path(directory))
            key = SessionKey("agent_one", "matrix", "!room:example.test:main")
            adapter._acks[(key, "$message")] = "$ack"
            adapter._request = AsyncMock(return_value={})
            await adapter.output(key, "cleanup", "", "$message")
            self.assertIn("/redact/%24ack/", adapter._request.await_args.args[1])
            self.assertNotIn((key, "$message"), adapter._acks)

    async def test_discord_reaction_fetches_within_current_channel(self):
        class Runtime:
            record_activity = AsyncMock()

        message = SimpleNamespace(id=22, channel=SimpleNamespace(id=2),
                                  add_reaction=AsyncMock())
        channel = SimpleNamespace(id=2, parent_id=None,
                                  guild=SimpleNamespace(id=1),
                                  fetch_message=AsyncMock(return_value=message))
        client = SimpleNamespace(get_channel=Mock(return_value=channel))
        adapter = DiscordAdapter("agent_two", DiscordSettings("TOKEN", 1, (2,)), Runtime())
        adapter.client = client
        key = SessionKey("agent_two", "discord", "2")
        await adapter.react(key, "22", "🍷")
        message.add_reaction.assert_awaited_once_with("🍷")
        message.channel.id = 3
        with self.assertRaisesRegex(ValueError, "outside this channel"):
            await adapter.react(key, "22", "🍷")
        self.assertEqual(message.add_reaction.await_count, 1)

    async def test_reaction_only_cleans_discord_progress(self):
        adapter = DiscordAdapter("agent_two", DiscordSettings("TOKEN", 1, (2,)),
                                 SimpleNamespace())
        key = SessionKey("agent_two", "discord", "2")
        progress = SimpleNamespace(delete=AsyncMock())
        adapter._progress[(key, "22")] = progress
        await adapter.output(key, "cleanup", "", "22")
        progress.delete.assert_awaited_once()


class ReactionOnlyRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_reaction_can_replace_direct_reply_or_accompany_text(self):
        class Pi:
            active = False

            def __init__(self):
                self.result = "[[bridge:pass]]"
                self.started = asyncio.Event()
                self.release = asyncio.Event()

            async def prompt(self, prompt, on_event, images=None, *, on_accepted=None):
                self.active = True
                if on_accepted:
                    await on_accepted()
                self.started.set()
                await self.release.wait()
                self.active = False
                return PiRunResult(self.result)

            async def compact(self):
                return "summary"

            async def stop(self):
                pass

        with tempfile.TemporaryDirectory() as directory:
            pi = Pi()
            outputs = []

            async def output(key, kind, text, request_id):
                outputs.append((kind, text, request_id))

            runtime = BridgeRuntime(Path(directory), lambda key, path: pi, output)
            key = SessionKey("agent_one", "matrix", "!room:example.test:main")
            await runtime.submit(key, Incoming("Operator", "$one", "Please review"))
            await pi.started.wait()
            await runtime.record_reaction(key, "$one")
            pi.release.set()
            await runtime.wait_idle(key)
            self.assertIn(("cleanup", "", "$one"), outputs)
            self.assertFalse(any(kind == "final" for kind, _, _ in outputs))

            pi.started.clear()
            pi.release.clear()
            pi.result = "Done too."
            await runtime.submit(key, Incoming("Operator", "$two", "Check this"))
            await pi.started.wait()
            await runtime.record_reaction(key, "$two")
            pi.release.set()
            await runtime.wait_idle(key)
            self.assertIn(("final", "Done too.", "$two"), outputs)

            pi.started.clear()
            pi.release.clear()
            pi.result = "[[bridge:pass]]"
            await runtime.submit(key, Incoming("Operator", "$three", "One more"))
            await pi.started.wait()
            await runtime.record_reaction(key, "$two")
            pi.release.set()
            await runtime.wait_idle(key)
            self.assertIn(("cleanup", "", "$three"), outputs)
            self.assertFalse(any(kind == "error" for kind, _, _ in outputs))
            self.assertFalse(any(text == "[[bridge:pass]]" for _, text, _ in outputs))

    async def test_peer_ping_needs_no_reaction(self):
        class Pi:
            active = False

            def __init__(self):
                self.result = "[[bridge:pass]]"
                self.started = asyncio.Event()
                self.release = asyncio.Event()

            async def prompt(self, prompt, on_event, images=None, *, on_accepted=None):
                self.active = True
                if on_accepted:
                    await on_accepted()
                self.started.set()
                await self.release.wait()
                self.active = False
                return PiRunResult(self.result)

            async def compact(self):
                return "summary"

            async def stop(self):
                pass

        with tempfile.TemporaryDirectory() as directory:
            pi = Pi()
            outputs = []

            async def output(key, kind, text, request_id):
                outputs.append((kind, text, request_id))

            runtime = BridgeRuntime(Path(directory), lambda key, path: pi, output)
            key = SessionKey("agent_one", "matrix", "!room:example.test:main")
            await runtime.submit(key, Incoming("Agent_two", "$peer", "PR #23 review, please",
                                               ack_required=False))
            await pi.started.wait()
            pi.release.set()
            await runtime.wait_idle(key)
            self.assertIn(("cleanup", "", "$peer"), outputs)
            self.assertFalse(any(kind == "error" for kind, _, _ in outputs))


class AcknowledgementPromptTests(unittest.TestCase):
    def test_reaction_is_encouraged_for_a_ping_and_absent_without_one(self):
        direct = Incoming("Operator", "$one", "Please review")
        self.assertIn("encouraged", direct.prompt())
        peer = Incoming("Agent_two", "$two", "Please review", ack_required=False)
        self.assertNotIn("encouraged", peer.prompt())
        ambient = Incoming("Operator", "$three", "chat", optional=True)
        self.assertIn("without a direct ping", ambient.prompt())
        self.assertNotIn("encouraged", ambient.prompt())


class PassMarkerTests(unittest.TestCase):
    def test_split_pass_removes_only_standalone_markers(self):
        self.assertEqual(split_pass("[[bridge:pass]]"), ("", True))
        self.assertEqual(split_pass("Done.\n\n[[bridge:pass]]"), ("Done.", True))
        self.assertEqual(split_pass("[[bridge:pass]]\n\nActually, one caveat."),
                         ("Actually, one caveat.", True))
        self.assertEqual(split_pass("Done."), ("Done.", False))
        self.assertEqual(split_pass("I returned `[[bridge:pass]]` there."),
                         ("I returned `[[bridge:pass]]` there.", False))


class PassMarkerRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_text_with_a_trailing_pass_marker_posts_the_text_only(self):
        class Pi:
            active = False

            def __init__(self):
                self.result = "Understood. No action needed.\n\n[[bridge:pass]]"
                self.started = asyncio.Event()
                self.release = asyncio.Event()

            async def prompt(self, prompt, on_event, images=None, *, on_accepted=None):
                self.active = True
                if on_accepted:
                    await on_accepted()
                self.started.set()
                await self.release.wait()
                self.active = False
                return PiRunResult(self.result)

            async def compact(self):
                return "summary"

            async def stop(self):
                pass

        with tempfile.TemporaryDirectory() as directory:
            pi = Pi()
            outputs = []

            async def output(key, kind, text, request_id):
                outputs.append((kind, text, request_id))

            runtime = BridgeRuntime(Path(directory), lambda key, path: pi, output)
            key = SessionKey("agent_one", "matrix", "!room:example.test:main")
            await runtime.submit(key, Incoming("Operator", "$one", "Anything new?"))
            await pi.started.wait()
            pi.release.set()
            await runtime.wait_idle(key)
            self.assertIn(("final", "Understood. No action needed.", "$one"), outputs)
            self.assertFalse(any("[[bridge:pass]]" in text for _, text, _ in outputs))


if __name__ == "__main__":
    unittest.main()
