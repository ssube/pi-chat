"""Delivery reactions never borrow the editable progress/acknowledgement path."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bridge.adapters.discord import DiscordAdapter
from bridge.adapters.matrix import MatrixAdapter
from bridge.config import DiscordSettings, MatrixSettings
from bridge.reaction_service import validate_request
from bridge.runtime.pi_rpc import PiRunResult
from bridge.runtime.session import BridgeRuntime, Incoming, ReplyRejected
from bridge.runtime.state import SessionKey
from bridge.turn_status import Deferred, Indicator, MAX_TRANSPORT_RETRY_SECONDS, TURN_EMOJI, TurnIndicators
from test_bridge_runtime import FakeRpc


class StatusRpc(FakeRpc):
    final_text = "Done"
    linger_active = False

    async def prompt(self, text, on_event, images=None, *, on_accepted=None):
        self.on_event = on_event
        await super().prompt(text, on_event, images, on_accepted=on_accepted)
        if self.linger_active:
            self.active = True
        return PiRunResult(self.final_text)


class RuntimeStatusTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.key = SessionKey("agent_one", "matrix", "!room:test:main")
        self.statuses = []
        self.clients = []
        self.runtime_count = 0

    async def status(self, key, request_id, status, epoch=0):
        self.statuses.append((request_id, status))

    def runtime(self, output, status=None):
        def factory(key, path):
            client = StatusRpc(path)
            self.clients.append(client)
            return client
        self.runtime_count += 1
        runtime = BridgeRuntime(Path(self.temp.name) / str(self.runtime_count), factory, output,
                                turn_status=status or self.status)
        self.addAsyncCleanup(runtime.close)
        return runtime

    async def start(self, runtime, **kwargs):
        await runtime.submit(self.key, Incoming("human", "$first", "hello", **kwargs))
        await asyncio.wait_for(self.clients[0].started.wait(), 1)
        return self.clients[0]

    async def finish(self, runtime, client):
        client.release.set()
        await asyncio.wait_for(runtime.wait_idle(self.key), 2)

    async def test_complete_waits_for_final_delivery_and_never_counts_as_answer(self):
        final_entered, final_release = asyncio.Event(), asyncio.Event()
        async def output(key, kind, text, request_id):
            if kind == "final":
                final_entered.set()
                await final_release.wait()
                return True
        runtime = self.runtime(output)
        client = await self.start(runtime)
        await client.on_event({"type": "auto_compaction_start"})
        await client.on_event({"type": "auto_compaction_end"})
        self.assertEqual(self.statuses, [("$first", "running")])
        client.release.set()
        await asyncio.wait_for(final_entered.wait(), 1)
        self.assertEqual(self.statuses, [("$first", "running")])
        final_release.set()
        await asyncio.wait_for(runtime.wait_idle(self.key), 1)
        self.assertEqual(self.statuses[-1], ("$first", "done"))
        self.assertEqual((await runtime._scope(self.key)).reacted_ids, set())

    async def test_pending_terminal_update_remains_active_for_deploy_gate(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def status(key, request_id, status, epoch=0):
            if status == "done":
                entered.set()
                await release.wait()
        runtime = self.runtime(AsyncMock(return_value=True), status)
        client = await self.start(runtime)
        client.release.set()
        await asyncio.wait_for(entered.wait(), 1)
        self.assertEqual((await runtime.session_summary())["active"], 1)
        release.set()
        await asyncio.wait_for(runtime.wait_idle(self.key), 1)
        self.assertEqual((await runtime.session_summary())["active"], 0)

    async def test_late_input_waits_for_terminal_update_then_starts_its_own_turn(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def status(key, request_id, status, epoch=0):
            self.statuses.append((request_id, status))
            if request_id == "$first" and status == "done":
                entered.set()
                await release.wait()
        runtime = self.runtime(AsyncMock(return_value=True), status)
        client = await self.start(runtime)
        # A client may still advertise active after returning its final result.
        # The runtime's terminal phase, not that advisory flag, owns admission.
        client.linger_active = True
        client.release.set()
        await asyncio.wait_for(entered.wait(), 1)
        late = asyncio.create_task(runtime.submit(self.key, Incoming("human", "$late", "new request")))
        self.addAsyncCleanup(lambda: asyncio.gather(late, return_exceptions=True))
        try:
            done, _ = await asyncio.wait({late}, timeout=0.05)
            self.assertFalse(done, "a late request must not steer into an already answered turn")
            self.assertEqual(client.steers, [])
            self.assertNotIn(("$late", "running"), self.statuses)
        finally:
            release.set()
        self.assertEqual(await asyncio.wait_for(late, 1), "started")
        await asyncio.wait_for(runtime.wait_idle(self.key), 1)
        self.assertEqual(len(client.prompts), 2)
        self.assertIn(("$late", "done"), self.statuses)

    async def test_late_input_cannot_steer_while_completed_prompt_drains_progress(self):
        runtime = self.runtime(AsyncMock(return_value=True))
        client = await self.start(runtime)
        scope = await runtime._scope(self.key)
        client.linger_active = True
        await scope.progress_lock.acquire()
        try:
            client.release.set()

            async def finalizing():
                while scope.steer_epoch is not None:
                    await asyncio.sleep(0)

            await asyncio.wait_for(finalizing(), 1)
            late = asyncio.create_task(runtime.submit(self.key, Incoming("human", "$late", "later")))
            done, _ = await asyncio.wait([late], timeout=0.02)
            self.assertFalse(done)
            self.assertFalse(client.steers)
        finally:
            scope.progress_lock.release()
        self.assertEqual(await asyncio.wait_for(late, 1), "started")
        await self.finish(runtime, client)
        self.assertEqual(self.statuses, [("$first", "running"), ("$first", "done"),
                                        ("$late", "running"), ("$late", "done")])

    async def test_admission_is_durable_before_cosmetic_indicator_wait(self):
        observed = []

        async def status(key, request_id, phase, epoch=0):
            if phase == "running":
                scope = await runtime._scope(key)
                saved = json.loads((scope.directory / "state.json").read_text())
                observed.append((request_id, saved["last_message_id"], request_id in scope.required_ids))
            await self.status(key, request_id, phase, epoch)

        runtime = self.runtime(AsyncMock(return_value=True), status)
        client = await self.start(runtime)
        await runtime.submit(self.key, Incoming("human", "$steer", "more"))
        self.assertEqual(observed, [("$first", "$first", True), ("$steer", "$steer", True)])
        await self.finish(runtime, client)

    async def test_ack_failure_marks_unstarted_text_and_image_failed_on_both_transports(self):
        for transport, scope in (("matrix", "!room:test:main"), ("discord", "2")):
            for image in (False, True):
                with self.subTest(transport=transport, image=image):
                    self.statuses.clear()

                    async def output(key, kind, text, request_id):
                        if kind == "ack":
                            raise RuntimeError("ack unavailable")
                        return True

                    runtime = self.runtime(output)
                    runtime.image_captioner = AsyncMock(return_value="fixture")
                    images = ({"type": "image", "data": "fixture", "mimeType": "image/png"},) if image else ()
                    with self.assertRaisesRegex(RuntimeError, "ack unavailable"):
                        await runtime.submit(SessionKey("agent_one", transport, scope),
                                             Incoming("human", "$ack", "question", images=images))
                    self.assertEqual(self.statuses, [("$ack", "running"), ("$ack", "failed")])
                    runtime.image_captioner.assert_not_awaited()
                    self.assertTrue(all(not client.prompts for client in self.clients))

    async def test_image_steer_retains_its_preparation_epoch_after_rpc_epoch_advance(self):
        for transport, scope_id in (("matrix", "!room:test:main"), ("discord", "2")):
            with self.subTest(transport=transport):
                self.key = SessionKey("agent_one", transport, scope_id)
                epochs = []

                async def status(key, request, phase, epoch):
                    epochs.append((request, phase, epoch))

                runtime = self.runtime(AsyncMock(return_value=True), status)
                await self.start(runtime)
                scope = await runtime._scope(self.key)
                accepted_epoch = scope.checkpoint.epoch

                async def caption(key, images):
                    # Isolate the post-caption admission branch after an RPC
                    # retry advances its epoch/window; no live RPC is involved.
                    scope.checkpoint.epoch += 1
                    scope.steer_epoch = scope.checkpoint.epoch
                    return "fixture"

                runtime.image_captioner = AsyncMock(side_effect=caption)
                outcome = await runtime.submit(self.key, Incoming("human", "$image", "photo", images=(
                    {"type": "image", "data": "fixture", "mimeType": "image/png"},)))
                self.assertEqual(outcome, "steered")
                self.assertEqual([epoch for request, phase, epoch in epochs
                                  if request == "$image" and phase == "running"], [accepted_epoch])
                self.assertEqual(scope.status_ids["$image"], accepted_epoch)
                await runtime.close()
                self.clients.clear()

    async def test_prepared_image_continues_into_its_own_main_turn(self):
        runtime = self.runtime(AsyncMock(return_value=True))
        runtime.image_captioner = AsyncMock(return_value="A fixture image")
        outcome = await runtime.submit(self.key, Incoming("human", "$image", "photo", images=(
            {"type": "image", "data": "fixture", "mimeType": "image/png"},)))
        self.assertEqual(outcome, "started")
        client = self.clients[0]
        await asyncio.wait_for(client.started.wait(), 1)
        await self.finish(runtime, client)
        self.assertEqual(self.statuses, [("$image", "running"), ("$image", "done")])

    async def test_caption_failure_marks_only_the_unstarted_image_request(self):
        runtime = self.runtime(AsyncMock(return_value=True))
        client = await self.start(runtime)
        prepared = []

        async def caption(key, images):
            scope = await runtime._scope(key)
            prepared.append(json.loads((scope.directory / "state.json").read_text())["last_message_id"])
            raise RuntimeError("caption unavailable")

        runtime.image_captioner = AsyncMock(side_effect=caption)
        await runtime.submit(self.key, Incoming("human", "$image", "photo", images=(
            {"type": "image", "data": "fixture", "mimeType": "image/png"},)))
        self.assertEqual(prepared, ["$image"])
        self.assertEqual(self.statuses, [("$first", "running"), ("$image", "running"),
                                         ("$image", "failed")])
        await self.finish(runtime, client)
        self.assertEqual(self.statuses[-1], ("$first", "done"))

    async def test_none_receipt_and_delivery_failure_never_confirm_completion(self):
        for failure in (False, True):
            with self.subTest(failure=failure):
                async def output(key, kind, text, request_id):
                    if kind == "final" and failure:
                        raise RuntimeError("ambiguous delivery")
                    return None
                runtime = self.runtime(output)
                client = await self.start(runtime)
                await self.finish(runtime, client)
                self.assertEqual(self.statuses[-1], ("$first", "failed"))
                self.assertNotIn(("$first", "done"), self.statuses)
                await runtime.close()
                self.clients.clear()
                self.statuses.clear()

    async def test_steers_finish_with_the_turn_not_their_added_notice(self):
        runtime = self.runtime(AsyncMock(return_value=True))
        client = await self.start(runtime)
        await runtime.submit(self.key, Incoming("human", "$second", "also this"))
        self.assertCountEqual(self.statuses, [("$first", "running"), ("$second", "running")])
        await self.finish(runtime, client)
        self.assertCountEqual(self.statuses[-2:], [("$first", "done"), ("$second", "done")])

    async def test_pass_clears_pending_without_claiming_delivery_or_failure(self):
        runtime = self.runtime(AsyncMock(return_value=True))
        client = await self.start(runtime)
        client.final_text = "[[bridge:pass]]"
        await self.finish(runtime, client)
        self.assertEqual(self.statuses, [("$first", "running"), ("$first", "clear")])

    async def test_peer_and_optional_inputs_never_get_lifecycle_reactions(self):
        for args in ({"ack_required": False}, {"optional": True}):
            runtime = self.runtime(AsyncMock(return_value=True))
            client = await self.start(runtime, **args)
            await self.finish(runtime, client)
            self.assertEqual(self.statuses, [])
            await runtime.close()
            self.clients.clear()

    async def test_stop_cancel_and_marker_failure_never_replay_a_turn(self):
        runtime = self.runtime(AsyncMock(return_value=True))
        client = await self.start(runtime)
        await runtime.stop_session(self.key, "human")
        self.assertEqual(self.statuses[-1], ("$first", "failed"))
        self.assertNotIn(("$first", "done"), self.statuses)
        self.clients.clear()
        runtime = self.runtime(AsyncMock(return_value=True), AsyncMock(side_effect=RuntimeError("status unavailable")))
        client = await self.start(runtime)
        await self.finish(runtime, client)
        self.assertEqual(len(client.prompts), 1)

    async def test_private_rewrite_keeps_pending_until_the_replacement_posts(self):
        attempts = 0
        async def output(key, kind, text, request_id):
            nonlocal attempts
            if kind == "final":
                attempts += 1
                if attempts == 1:
                    self.assertEqual(self.statuses[-1], ("$first", "running"))
                    raise ReplyRejected("rewrite this reply")
                return True
        runtime = self.runtime(output)
        client = await self.start(runtime)
        await self.finish(runtime, client)
        self.assertEqual(attempts, 2)
        self.assertEqual(self.statuses, [("$first", "running"), ("$first", "done")])


class AdapterStatusTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.runtime = SimpleNamespace(record_activity=AsyncMock(), record_reaction=AsyncMock())
        self.key = SessionKey("agent_one", "matrix", "!room:test:main")

    def matrix(self):
        settings = MatrixSettings("https://example.test", "@agent_one:test", "TEST_TOKEN", ("!room:test",))
        with patch.dict("os.environ", {"TEST_TOKEN": "fixture"}):
            adapter = MatrixAdapter("agent_one", settings, self.runtime, Path(self.temp.name))
        async def request(method, path, data=None, **kwargs):
            if method == "GET":
                return {"event_id": "$first", "type": "m.room.message", "sender": "@human:test",
                        "content": {"msgtype": "m.text", "body": "hello"}}
            return {"event_id": f"$reaction-{adapter._request.await_count}"}
        adapter._request = AsyncMock(side_effect=request)
        self.indicators = TurnIndicators(Path(self.temp.name) / "matrix-status.json", adapter.agent,
                                         adapter.turn_status, interval=0)
        return adapter

    async def apply(self, key, request_id, status, epoch=0):
        update = asyncio.create_task(self.indicators.update(key, request_id, status, epoch))
        await asyncio.sleep(0)
        await self.indicators.flush_once()
        await update

    async def test_matrix_progress_and_history_do_not_replace_pending_or_done(self):
        adapter = self.matrix()
        await self.apply(self.key, "$first", "running")
        original = dict(self.indicators.records)
        for kind in ("ack", "progress", "progress_summary", "compacting"):
            await adapter.output(self.key, kind, "Working\nSummary", "$first")
            self.assertEqual(self.indicators.records, original)
        receipt = await adapter.output(self.key, "final", "Full answer", "$first")
        self.assertIs(receipt, True)
        await self.apply(self.key, "$first", "done")
        for kind in ("progress", "progress_summary", "compacting"):
            await adapter.output(self.key, kind, "Late summary", "$first")
        emojis = [call.kwargs["data"]["m.relates_to"]["key"]
                  for call in adapter._request.await_args_list
                  if "/send/m.reaction/" in call.args[1]]
        self.assertEqual(emojis, ["⏳", "🤖"])
        self.assertEqual(self.indicators.records, {})
        self.runtime.record_reaction.assert_not_awaited()

    async def test_matrix_missing_event_receipt_is_not_confirmed(self):
        adapter = self.matrix()
        adapter._request = AsyncMock(return_value={})
        self.assertIs(await adapter.output(self.key, "final", "answer", "$first"), False)

    async def test_matrix_status_has_separate_idempotent_transactions_and_no_activity(self):
        adapter = self.matrix()
        await self.apply(self.key, "$first", "running")
        status_path = adapter._request.await_args_list[-1].args[1]
        count = adapter._request.await_count
        await self.apply(self.key, "$first", "running")
        self.assertEqual(adapter._request.await_count, count)
        await adapter.react(self.key, "$first", "⏳")
        self.assertNotEqual(adapter._request.await_args_list[-1].args[1], status_path)
        self.runtime.record_activity.reset_mock()
        await self.apply(self.key, "$first", "failed")
        self.runtime.record_activity.assert_not_awaited()
        self.runtime.record_reaction.assert_not_awaited()
        self.assertIn("/redact/", adapter._request.await_args_list[-1].args[1])

    async def test_matrix_clear_emits_no_terminal_and_no_receipt_on_noop(self):
        adapter = self.matrix()
        await self.apply(self.key, "$first", "running")
        await self.apply(self.key, "$first", "clear")
        emojis = [call.kwargs["data"]["m.relates_to"]["key"]
                  for call in adapter._request.await_args_list if "/send/m.reaction/" in call.args[1]]
        self.assertEqual(emojis, ["⏳"])
        self.assertIsNone(await adapter.output(SessionKey("agent_one", "discord", "2"), "final", "x", "20"))
        self.assertIsNone(await adapter.output(self.key, "progress", "no card", "$missing"))

    def discord(self):
        adapter = DiscordAdapter("agent_two", DiscordSettings("TOKEN", 1, (2,)), self.runtime)
        adapter.client = SimpleNamespace(user=SimpleNamespace(id=9))
        card = SimpleNamespace(id=10, edit=AsyncMock(), delete=AsyncMock())
        channel = SimpleNamespace(id=2, typing=AsyncMock(), send=AsyncMock())
        request = SimpleNamespace(channel=channel, reply=AsyncMock(return_value=card),
                                  add_reaction=AsyncMock(), remove_reaction=AsyncMock())
        key = SessionKey("agent_two", "discord", "2")
        adapter._requests[(key, "20")] = request
        self.indicators = TurnIndicators(Path(self.temp.name) / "discord-status.json", adapter.agent,
                                         adapter.turn_status, interval=0)
        return adapter, key, request

    async def test_discord_public_rate_limit_defers_and_client_uses_supported_bound(self):
        import discord
        adapter, key, request = self.discord()
        request.add_reaction.side_effect = discord.RateLimited(45.0)
        record = Indicator(key, "20", 1)
        self.assertEqual(await adapter.turn_status(record, "running"), Deferred("discord", 45.0))
        request.add_reaction.assert_awaited_once_with("⏳")

        options = {}

        def refuse_network(**kwargs):
            options.update(kwargs)
            raise RuntimeError("client construction captured")

        with patch("discord.Client", side_effect=refuse_network):
            with self.assertRaisesRegex(RuntimeError, "client construction captured"):
                await adapter.run()
        self.assertEqual(options["max_ratelimit_timeout"], MAX_TRANSPORT_RETRY_SECONDS)
        client = discord.Client(intents=discord.Intents.default(),
                                max_ratelimit_timeout=MAX_TRANSPORT_RETRY_SECONDS)
        self.assertGreaterEqual(client.http.max_ratelimit_timeout, MAX_TRANSPORT_RETRY_SECONDS)
        await client.close()
        from bridge.__main__ import ADAPTER_STOP_SECONDS, INDICATOR_DRAIN_SECONDS
        self.assertLess(INDICATOR_DRAIN_SECONDS + ADAPTER_STOP_SECONDS, 20.0)

    async def test_discord_history_does_not_replace_lifecycle_reactions(self):
        adapter, key, request = self.discord()
        await adapter.output(key, "ack", "Working", "20")
        await self.apply(key, "20", "running")
        for kind in ("progress", "progress_summary", "compacting"):
            await adapter.output(key, kind, "Working\nHistory", "20")
        request.add_reaction.assert_awaited_once_with("⏳")
        request.remove_reaction.assert_not_awaited()
        adapter._render_mentions = AsyncMock(return_value=("answer", [], [], False))
        self.assertIs(await adapter.output(key, "final", "answer", "20"), True)
        self.runtime.record_activity.reset_mock()
        await self.apply(key, "20", "done")
        self.assertEqual([call.args[0] for call in request.add_reaction.await_args_list], ["⏳", "🤖"])
        request.remove_reaction.assert_awaited_once_with("⏳", adapter.client.user)
        self.runtime.record_activity.assert_not_awaited()
        self.runtime.record_reaction.assert_not_awaited()

    async def test_discord_partial_final_never_returns_a_complete_receipt(self):
        adapter, key, request = self.discord()
        await self.apply(key, "20", "running")
        adapter._render_mentions = AsyncMock(return_value=("x" * 4500, [], [], False))
        request.channel.send.side_effect = [None, RuntimeError("third chunk failed")]
        with self.assertRaisesRegex(RuntimeError, "third chunk"):
            await adapter.output(key, "final", "x" * 4500, "20")
        self.assertEqual(request.channel.send.await_count, 2)
        request.add_reaction.assert_awaited_once_with("⏳")
        await self.apply(key, "20", "failed")
        self.assertEqual([call.args[0] for call in request.add_reaction.await_args_list], ["⏳", "⚠️"])

    async def test_discord_runtime_marks_partial_delivery_failed_not_done(self):
        adapter, key, request = self.discord()
        clients = []
        def factory(key, path):
            client = StatusRpc(path)
            client.final_text = "x" * 4500
            clients.append(client)
            return client
        runtime = BridgeRuntime(Path(self.temp.name), factory, adapter.output,
                                turn_status=self.apply)
        adapter.runtime = runtime
        self.addAsyncCleanup(runtime.close)
        adapter._render_mentions = AsyncMock(return_value=("x" * 4500, [], [], False))
        request.channel.send.side_effect = [None, RuntimeError("third chunk failed")]
        await runtime.submit(key, Incoming("human", "20", "hello"))
        await asyncio.wait_for(clients[0].started.wait(), 1)
        self.assertEqual((await runtime._scope(key)).reacted_ids, set())
        clients[0].release.set()
        await asyncio.wait_for(runtime.wait_idle(key), 2)
        self.assertEqual([call.args[0] for call in request.add_reaction.await_args_list], ["⏳", "⚠️"])
        self.assertEqual((await runtime._scope(key)).checkpoint.status, "delivery_error")

    def test_standard_unicode_keys(self):
        for emoji in TURN_EMOJI.values():
            request = {"transport": "matrix", "scope": "!room:test:main", "message_id": "$first",
                       "capability": "x" * 40, "emoji": emoji}
            self.assertEqual(validate_request(request)["emoji"], emoji)


class IndicatorFailureTests(unittest.IsolatedAsyncioTestCase):
    """A failed cosmetic emit releases its caller; the intent is replayed alone."""

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.key = SessionKey("agent_one", "matrix", "!room:test:main")
        self.now = 1000.0
        self.attempts = []

    def indicators(self, *, fail_on):
        async def emit(record, status):
            self.attempts.append(status)
            if len(self.attempts) == fail_on:
                raise RuntimeError("transport unavailable")
            return "receipt"
        indicators = TurnIndicators(Path(self.temp.name) / "status.json", "agent_one", emit,
                                    interval=0, now=lambda: self.now)
        self.addCleanup(indicators.close)
        return indicators

    async def apply(self, indicators, status, epoch=1):
        update = asyncio.create_task(indicators.update(self.key, "$first", status, epoch))
        await asyncio.sleep(0)
        await indicators.flush_once()
        await asyncio.wait_for(update, 1)

    async def test_failed_running_emit_releases_the_waiter_and_retries_alone(self):
        indicators = self.indicators(fail_on=1)
        await self.apply(indicators, "running")
        record = indicators.records[(self.key, "$first", 1)]
        self.assertEqual((record.desired, record.applied), ("running", None))
        self.now += 3
        await indicators.flush_once()
        self.assertEqual(self.attempts, ["running", "running"])
        self.assertEqual(record.applied, "running")

    async def test_failed_terminal_emit_releases_the_waiter_and_retries_alone(self):
        indicators = self.indicators(fail_on=2)
        await self.apply(indicators, "running")
        await self.apply(indicators, "done")
        self.assertEqual(self.attempts, ["running", "done"])
        self.assertEqual(indicators.records[(self.key, "$first", 1)].applied, "running")
        self.now += 3
        await indicators.flush_once()
        self.assertEqual(self.attempts, ["running", "done", "done"])
        self.assertNotIn((self.key, "$first", 1), indicators.records)
