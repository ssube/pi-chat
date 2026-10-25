"""Named modes select existing Pi models without inference or credential changes."""

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bridge.adapters.matrix import addressed_command_body
from bridge.config import Profile
from bridge.debug import debug_status
from bridge.modes import ModelSelection, ModeSwitchError, mode_command, mode_label, read_modes
from bridge.runtime.pi_rpc import PiProviderError, PiRpcError, PiRunResult, RpcImage
from bridge.runtime.session import BridgeRuntime, Incoming, StopRequested
from bridge.runtime.state import SessionKey, read_checkpoint
from test_bridge_commands import (AGENT_ID, ROOM, discord_adapter, discord_message,
                                  discord_runtime, matrix_adapter, matrix_event, matrix_runtime)

MODES = {"fast": ModelSelection("test", "flash"),
         "smart": ModelSelection("openai-codex", "gpt-smart"),
         "research": ModelSelection("other-provider", "research-model")}


class ModeConfigTests(unittest.TestCase):
    def test_legacy_fallback_and_extensible_map(self):
        self.assertEqual(read_modes(None, "legacy", "fast"),
                         {"fast": ModelSelection("test", "legacy")})
        raw = {name: {"provider": value.provider, "model": value.model}
               for name, value in MODES.items()}
        self.assertEqual(read_modes(raw, "unused", "smart"), MODES)
        self.assertEqual(mode_label("smart"), "🧠 smart")
        self.assertEqual(mode_label("fast"), "🐇 fast")
        self.assertEqual(mode_label("research"), "⚙️ research")

    def test_invalid_maps_names_selections_and_defaults(self):
        for value in ([], {}, {"stop": {}}, {"debug": {}}, {"cancel": {}}, {"recover": {}},
                      {"mode": {}}, {"bridge_debug": {}}, {"bridge_stop": {}},
                      {"bridge_cancel": {}}, {"bridge_recover": {}}, {"UPPER": {}}, {"with space": {}}, {"a" * 33: {}},
                      {"smart": "model"}, {"smart": {"model": "model"}},
                      {"smart": {"provider": "p", "modle": "m"}},
                      {"smart": {"provider": "p", "model": "m", "extra": True}},
                      {"smart": {"provider": "", "model": "m"}},
                      {"smart": {"provider": "p", "model": "m\nsecret"}},
                      {"smart": {"provider": None, "model": "m"}}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                read_modes(value, "legacy", "smart")
        with self.assertRaisesRegex(ValueError, "default_mode"):
            read_modes(None, "legacy", "missing")

    def test_prefix_parser_only_matches_leading_configured_modes(self):
        for name in MODES:
            for prefix in ("!", "/"):
                self.assertEqual(mode_command(f"{prefix}{name.upper()} Keep My CASE\nSecond line", MODES),
                                 (name, "Keep My CASE\nSecond line"))
                self.assertEqual(mode_command(f"{prefix}{name}   ", MODES), (name, ""))
        for text in ("quoted !smart", "> !smart", "`!smart`", "!smarter hello", "!stop", "!other hi"):
            self.assertIsNone(mode_command(text, MODES))

    def test_profile_modes_drive_only_normal_clients(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile_path = root / "profile.json"
            raw = {"agent": "agent_one", "work_dir": directory, "state_dir": directory,
                   "pi_executable": "/usr/bin/pi", "pi_agent_dir": directory,
                   "provider": "test", "model": "legacy-auxiliary", "default_mode": "smart",
                   "modes": {name: {"provider": value.provider, "model": value.model}
                             for name, value in MODES.items()},
                   "matrix": {"homeserver": "https://example.test", "user_id": AGENT_ID,
                              "token_env": "TEST_TOKEN", "rooms": [ROOM]}}
            profile_path.write_text(json.dumps(raw))
            with patch.dict(os.environ, {"COUNCIL_MATRIX_ROOM_ID": "!council:example.test"}):
                profile = Profile.load(profile_path, require_transport_credentials=False)
                key = SessionKey("agent_one", "matrix", "test")
                for options in ({}, {"safe_mode": True}, {"status_mode": True},
                                {"progress_mode": True}, {"vision_mode": True, "model_override": "vision"}):
                    with self.subTest(options=options):
                        client = profile.pi_client(key, root / "test.jsonl", **options)
                        command = client._command
                        expected = MODES["smart"] if not options else ModelSelection(
                            "test", options.get("model_override", "legacy-auxiliary"))
                        self.assertEqual(command[command.index("--provider") + 1], expected.provider)
                        self.assertEqual(command[command.index("--model") + 1], expected.model)
                        self.assertNotIn("TEST_TOKEN", client._env)
                raw["default_mode"] = "missing"
                profile_path.write_text(json.dumps(raw))
                with self.assertRaisesRegex(ValueError, "default_mode"):
                    Profile.load(profile_path, require_transport_credentials=False)


class ModeRpc:
    def __init__(self, path):
        self.path = path
        self.selections = []
        self.prompts = []
        self.active = False
        self.stopped = False
        self.aborted = False
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()
        self.compact_started = asyncio.Event()
        self.compact_release = asyncio.Event()
        self.compact_release.set()
        self.compact_selections = []
        self.fail_switch = False
        self.failure = None
        self.auto_compact = False

    async def set_model(self, provider, model):
        self.selections.append((provider, model))
        if self.fail_switch:
            raise PiRpcError("private provider diagnostic")

    async def prompt(self, message, on_event, images=None, *, on_accepted=None):
        self.path.write_text("private transcript")
        self.prompts.append(message)
        if self.failure:
            raise self.failure
        self.active = True
        if on_accepted:
            await on_accepted()
        self.started.set()
        if self.auto_compact:
            await on_event({"type": "auto_compaction_start", "reason": "threshold"})
            await on_event({"type": "auto_compaction_end"})
        await self.release.wait()
        self.active = False
        return PiRunResult("Done")

    async def compact(self):
        self.compact_selections.append(self.selections[-1])
        self.compact_started.set()
        await self.compact_release.wait()
        return "saved summary"

    async def stop(self):
        self.stopped = True

    async def abort(self):
        self.aborted = True
        self.release.set()


class ModeRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.clients = []
        self.posts = []
        self.first_switch_fails = False
        self.first_prompt_fails = False

        def factory(key, path):
            client = ModeRpc(path)
            if not self.clients:
                client.fail_switch = self.first_switch_fails
                client.failure = PiProviderError(503) if self.first_prompt_fails else None
            self.clients.append(client)
            return client

        async def send(key, kind, text, request):
            self.posts.append((kind, text))
            return kind == "final"

        self.runtime = BridgeRuntime(Path(self.directory.name), factory, send,
                                     modes=MODES, sleep=AsyncMock())
        self.addAsyncCleanup(self.runtime.close)
        self.key = SessionKey("agent_one", "matrix", "!room:test:main")

    async def submit(self, text="request", mode=None, request="$request"):
        await self.runtime.submit(self.key, Incoming("operator", request, text, mode=mode))
        await self.runtime.wait_idle(self.key)

    async def test_bare_switch_does_not_construct_pi_or_persist_mode(self):
        notice = await self.runtime.change_mode(self.key, "smart")
        self.assertIn("🧠 smart", notice)
        self.assertEqual(self.clients, [])
        result = await debug_status(self.runtime, self.key)
        self.assertIn("🧠 smart", result)
        self.assertIn("openai-codex/gpt-smart", result)
        self.assertIn("sleeping", result)
        checkpoint = json.loads((self.key.directory(Path(self.directory.name)) / "state.json").read_text())
        self.assertNotIn("mode", checkpoint)
        await self.submit()
        self.assertEqual(self.clients[0].selections, [("openai-codex", "gpt-smart")])

    async def test_debug_lists_modes_without_loading_a_scope(self):
        text = await debug_status(self.runtime, self.key)
        self.assertIn("Configured modes:", text)
        self.assertIn("🧠 smart", text)
        self.assertIn("🐇 fast", text)
        self.assertIn("research", text)
        self.assertFalse(self.runtime._scopes)
        self.assertFalse(self.clients)

    async def test_prefix_applies_before_prompt_and_same_session_switch_preserves_epoch(self):
        await self.submit(mode="smart")
        client = self.clients[0]
        epoch = self.runtime._scopes[self.key].checkpoint.epoch
        await self.runtime.change_mode(self.key, "fast")
        await self.submit("next request", request="$next")
        self.assertEqual(len(self.clients), 1)
        self.assertEqual(client.selections, [("openai-codex", "gpt-smart"), ("test", "flash")])
        self.assertEqual(self.runtime._scopes[self.key].checkpoint.epoch, epoch)
        self.assertFalse(client.stopped)
        self.assertEqual(len(client.prompts), 2)
        self.assertIn("🧠 smart", self.posts[0][1])

    async def test_active_switch_is_refused_without_abort_or_attached_prompt(self):
        await self.submit()
        client = self.clients[0]
        client.release.clear()
        await self.runtime.submit(self.key, Incoming("operator", "$working", "work"))
        await asyncio.sleep(0)
        with self.assertRaisesRegex(ModeSwitchError, "turn is running"):
            await self.runtime.submit(self.key, Incoming("operator", "$switch", "new", mode="smart"))
        self.assertFalse(client.aborted)
        self.assertEqual(self.runtime._scopes[self.key].mode, "fast")
        self.assertEqual(len(client.prompts), 2)
        client.release.set()
        await self.runtime.wait_idle(self.key)

    async def test_sleep_compacts_with_current_model_then_resets_default(self):
        await self.submit(mode="smart")
        client = self.clients[0]
        client.compact_release.clear()
        sweep = asyncio.create_task(self.runtime.sweep_idle(force=True))
        await client.compact_started.wait()
        self.assertIn("🧠 smart", await debug_status(self.runtime, self.key))
        self.assertEqual(client.compact_selections, [("openai-codex", "gpt-smart")])
        client.compact_release.set()
        self.assertEqual(await sweep, 1)
        self.assertEqual(self.runtime._scopes[self.key].mode, "fast")
        await self.submit("wake", request="$wake")
        self.assertEqual(self.clients[1].selections, [("test", "flash")])
        self.assertIn("saved summary", self.clients[1].prompts[0])

    async def test_auto_compaction_keeps_mode(self):
        await self.submit(mode="smart")
        self.clients[0].auto_compact = True
        await self.submit("more", request="$more")
        self.assertEqual(self.runtime._scopes[self.key].mode, "smart")
        self.assertFalse(self.clients[0].stopped)

    async def test_provider_retry_reapplies_mode_at_client_creation(self):
        self.first_prompt_fails = True
        await self.submit(mode="smart")
        self.assertEqual(len(self.clients), 2)
        self.assertEqual(self.clients[1].selections, [("openai-codex", "gpt-smart")])
        self.assertEqual(self.runtime._scopes[self.key].mode, "smart")

    async def test_unconfigured_and_unavailable_modes_fail_closed(self):
        with self.assertRaisesRegex(ModeSwitchError, "not configured"):
            await self.runtime.change_mode(self.key, "missing")
        self.assertEqual(self.clients, [])
        self.first_switch_fails = True
        with self.assertRaisesRegex(ModeSwitchError, "could not start") as failure:
            await self.submit(mode="smart")
        self.assertNotIn("private", str(failure.exception))
        self.assertFalse(self.clients[0].prompts)
        self.assertTrue(self.clients[0].stopped)
        self.assertEqual(self.runtime._scopes[self.key].mode, "fast")

    async def test_failed_live_switch_restores_default_on_next_plain_request(self):
        await self.submit()
        client = self.clients[0]
        client.fail_switch = True
        with self.assertRaisesRegex(ModeSwitchError, "could not confirm"):
            await self.runtime.change_mode(self.key, "smart")
        self.assertTrue(client.stopped)
        self.assertIsNone(self.runtime._scopes[self.key].client)
        self.assertEqual(self.runtime._scopes[self.key].mode, "fast")
        await self.submit("next ordinary request", request="$next")
        self.assertEqual(len(self.clients), 2)
        self.assertEqual(self.clients[1].selections, [("test", "flash")])
        self.assertEqual(len(self.clients[1].prompts), 1)

    async def test_failed_start_restores_default_without_explicit_selection(self):
        self.first_switch_fails = True
        with self.assertRaisesRegex(ModeSwitchError, "could not start"):
            await self.submit(mode="smart")
        self.assertEqual(self.runtime._scopes[self.key].mode, "fast")
        await self.submit("ordinary retry", request="$retry")
        self.assertEqual(self.clients[1].selections, [("test", "flash")])
        self.assertEqual(len(self.clients[1].prompts), 1)

    async def test_timed_out_start_restores_default_on_next_request(self):
        original = self.runtime.client_factory
        def factory(key, path):
            client = original(key, path)
            if len(self.clients) == 1:
                async def stalled(provider, model):
                    await asyncio.Event().wait()
                client.set_model = stalled
            return client
        self.runtime.client_factory = factory
        with patch("bridge.runtime.session.MODE_SWITCH_TIMEOUT_SECONDS", .01):
            with self.assertRaises(ModeSwitchError):
                await self.submit(mode="smart")
        self.assertTrue(self.clients[0].stopped)
        self.assertEqual(self.runtime._scopes[self.key].mode, "fast")
        await self.submit("ordinary retry", request="$retry")
        self.assertEqual(self.clients[1].selections, [("test", "flash")])

    async def test_process_start_oserror_discards_client_and_revalidates_next_request(self):
        original = self.runtime.client_factory
        def factory(key, path):
            client = original(key, path)
            if len(self.clients) == 1:
                async def unavailable(provider, model):
                    raise OSError("private executable path")
                client.set_model = unavailable
            return client
        self.runtime.client_factory = factory
        with self.assertRaisesRegex(ModeSwitchError, "could not start"):
            await self.submit(mode="smart")
        self.assertTrue(self.clients[0].stopped)
        self.assertIsNone(self.runtime._scopes[self.key].client)
        await self.submit("retry", request="$retry")
        self.assertEqual(self.clients[1].selections, [("test", "flash")])

    async def test_failed_switch_uses_configured_default_not_hardcoded_fast(self):
        self.runtime.default_mode = "research"
        await self.submit(mode="smart")
        self.clients[0].fail_switch = True
        with self.assertRaises(ModeSwitchError):
            await self.runtime.change_mode(self.key, "fast")
        self.assertEqual(self.runtime._scopes[self.key].mode, "research")
        await self.submit("ordinary retry", request="$retry")
        self.assertEqual(self.clients[1].selections, [("other-provider", "research-model")])

    async def test_reload_removing_sleeping_selection_uses_default_before_client_creation(self):
        await self.runtime.change_mode(self.key, "smart")
        self.runtime.modes = {"fast": MODES["fast"]}
        await self.submit("after reload")
        self.assertEqual(self.runtime._scopes[self.key].mode, "fast")
        self.assertEqual(self.clients[0].selections, [("test", "flash")])

    async def test_reload_removing_idle_selection_discards_old_client_and_uses_default(self):
        await self.submit(mode="smart")
        old = self.clients[0]
        self.runtime.modes = {"fast": MODES["fast"]}
        await self.submit("after reload", request="$reload")
        self.assertTrue(old.stopped)
        self.assertEqual(self.runtime._scopes[self.key].mode, "fast")
        self.assertEqual(self.clients[1].selections, [("test", "flash")])

    async def test_reload_does_not_interrupt_active_turn_before_default_reset(self):
        await self.submit(mode="smart")
        old = self.clients[0]
        old.release.clear()
        await self.runtime.submit(self.key, Incoming("operator", "$active", "work"))
        await asyncio.sleep(0)
        self.runtime.modes = {"fast": MODES["fast"]}
        pending = asyncio.create_task(self.runtime.submit(self.key, Incoming("operator", "$next", "next")))
        await asyncio.sleep(0)
        self.assertFalse(old.stopped)
        old.release.set()
        await asyncio.wait_for(pending, 1)
        await self.runtime.wait_idle(self.key)
        self.assertTrue(old.stopped)
        self.assertEqual(self.clients[1].selections, [("test", "flash")])

    async def test_bare_peer_mode_change_does_not_take_turn_ownership(self):
        await self.submit()
        await self.runtime.change_mode(self.key, "smart", "@peer-agent:example.test")
        self.assertEqual(self.runtime._scopes[self.key].checkpoint.requester, "operator")
        self.assertFalse(await self.runtime.stop_session(self.key, "@peer-agent:example.test"))
        self.assertTrue(await self.runtime.stop_session(self.key, "operator"))

    async def test_reload_during_captioning_preserves_summary_for_actual_new_epoch(self):
        await self.submit(mode="smart")
        scope = self.runtime._scopes[self.key]
        scope.checkpoint.summary = "previous saved context"
        async def caption(key, images):
            self.runtime.modes = {"fast": MODES["fast"]}
            return "image caption"
        self.runtime.image_captioner = caption
        await self.runtime.submit(self.key, Incoming("operator", "$image", "inspect", (RpcImage("image/png", "QQ=="),)))
        await self.runtime.wait_idle(self.key)
        self.assertEqual(len(self.clients), 2)
        self.assertEqual(self.clients[1].selections, [("test", "flash")])
        self.assertIn("previous saved context", self.clients[1].prompts[0])

    async def test_unavailable_default_reports_default_error_without_retry_invitation(self):
        self.first_switch_fails = True
        with self.assertRaisesRegex(ModeSwitchError, "configured default itself is unavailable") as result:
            await self.submit()
        self.assertNotIn("next request will use it", str(result.exception))
        self.assertEqual(len(self.clients), 1)
        self.assertTrue(self.clients[0].stopped)
        self.assertIsNone(self.runtime._scopes[self.key].client)
        self.assertEqual(self.runtime._scopes[self.key].mode, "fast")

    async def test_failed_switch_to_default_identifies_unavailable_default(self):
        await self.submit(mode="smart")
        self.clients[0].fail_switch = True
        with self.assertRaisesRegex(ModeSwitchError, "configured default itself is unavailable"):
            await self.runtime.change_mode(self.key, "fast")
        self.assertIsNone(self.runtime._scopes[self.key].client)

    async def test_duplicate_after_reload_does_not_retire_client_or_change_mode(self):
        await self.submit(mode="smart")
        old = self.clients[0]
        scope = self.runtime._scopes[self.key]
        epoch = scope.checkpoint.epoch
        self.runtime.modes = {"fast": MODES["fast"]}
        result = await self.runtime.submit(self.key, Incoming("operator", "$request", "request"))
        self.assertEqual(result, "duplicate")
        self.assertIs(scope.client, old)
        self.assertFalse(old.stopped)
        self.assertEqual(scope.mode, "smart")
        self.assertEqual(scope.checkpoint.epoch, epoch)

    async def test_queued_stop_exception_retires_unconfirmed_selection(self):
        await self.submit()
        with patch.object(self.runtime, "_apply_model", side_effect=StopRequested("queued stop")):
            with self.assertRaises(StopRequested):
                await self.runtime.change_mode(self.key, "smart")
        self.assertIsNone(self.runtime._scopes[self.key].client)
        self.assertEqual(self.runtime._scopes[self.key].mode, "fast")

    async def test_matrix_reload_replacement_fetches_recent_posts_without_summary(self):
        self.key = SessionKey("agent_one", "matrix", ROOM + ":main")
        await self.submit(mode="smart")
        self.runtime.modes = {"fast": MODES["fast"]}
        adapter = matrix_adapter(self.directory.name, self.runtime)
        adapter._history.return_value = "previous room context"
        await adapter._handle_event(ROOM, matrix_event("continue", event_id="$next", mentions=[AGENT_ID]))
        await self.runtime.wait_idle(self.key)
        adapter._history.assert_awaited_once()
        self.assertIn("previous room context", self.clients[1].prompts[0])

    async def test_discord_bot_reload_replacement_fetches_recent_posts_without_summary(self):
        self.key = SessionKey("agent_two", "discord", "2")
        await self.submit(mode="smart")
        self.runtime.modes = {"fast": MODES["fast"]}
        adapter = discord_adapter(self.runtime)
        adapter._context = AsyncMock(return_value="previous channel context")
        adapter._reply_context = AsyncMock(return_value="")
        adapter._images = AsyncMock(return_value=())
        await adapter._on_message(discord_message("<@9> continue", bot=True))
        await self.runtime.wait_idle(self.key)
        adapter._context.assert_awaited_once()
        self.assertIn("previous channel context", self.clients[1].prompts[0])

    async def test_previous_requester_cannot_abort_new_mode_bearing_turn(self):
        await self.submit()
        old = self.clients[0]
        entered = asyncio.Event()
        release = asyncio.Event()
        original = old.set_model
        async def delayed_switch(provider, model):
            entered.set()
            await release.wait()
            await original(provider, model)
        old.set_model = delayed_switch
        new_turn = asyncio.create_task(self.runtime.submit(
            self.key, Incoming("peer", "$peer", "peer work", mode="smart")))
        await asyncio.wait_for(entered.wait(), 1)
        stop = asyncio.create_task(self.runtime.stop_session(self.key, "operator"))
        await asyncio.sleep(0)
        release.set()
        stopped, result = await asyncio.gather(stop, new_turn, return_exceptions=True)
        self.assertFalse(stopped)
        self.assertNotIsInstance(result, BaseException)
        await self.runtime.wait_idle(self.key)
        self.assertFalse(old.stopped)
        self.assertIn("peer work", old.prompts[-1])
        self.assertEqual(self.runtime._scopes[self.key].checkpoint.requester, "peer")

    async def assert_retry_mode_failure_reported(self, failure):
        self.first_prompt_fails = True
        original = self.runtime.client_factory
        def factory(key, path):
            client = original(key, path)
            if len(self.clients) == 2:
                client.set_model = AsyncMock(side_effect=failure)
            return client
        self.runtime.client_factory = factory
        await self.submit(mode="smart")
        scope = self.runtime._scopes[self.key]
        self.assertEqual(scope.checkpoint.status, "error")
        self.assertEqual(scope.mode, "fast")
        self.assertIsNone(scope.client)
        self.assertTrue(any(kind == "error" and "configured default" in text
                            for kind, text in self.posts))
        self.assertEqual(self.clients[1].prompts, [])
        await self.submit("new request", request="$after-retry-failure")
        self.assertEqual(self.clients[2].selections, [("test", "flash")])
        self.assertEqual(len(self.clients[2].prompts), 1)
        self.assertIn("new request", self.clients[2].prompts[0])

    async def test_provider_retry_rpc_mode_failure_reports_error_and_recovers_default(self):
        await self.assert_retry_mode_failure_reported(PiRpcError("private diagnostic"))

    async def test_provider_retry_os_mode_failure_reports_error_and_recovers_default(self):
        await self.assert_retry_mode_failure_reported(OSError("private startup failure"))

    async def test_other_scopes_and_restart_do_not_inherit_selected_mode(self):
        await self.submit(mode="smart")
        other = SessionKey("agent_one", "matrix", "!other:test:main")
        await self.runtime.submit(other, Incoming("operator", "$other", "other"))
        await self.runtime.wait_idle(other)
        self.assertEqual(self.runtime._scopes[other].mode, "fast")
        self.assertEqual(self.runtime._scopes[self.key].mode, "smart")
        await self.runtime.stop_session(self.key, "operator")
        self.assertEqual(self.runtime._scopes[self.key].mode, "fast")
        checkpoint = read_checkpoint(self.key.directory(Path(self.directory.name)))
        self.assertEqual(checkpoint.status, "idle")

    async def test_stop_interrupts_stalled_initial_mode_application(self):
        entered = asyncio.Event()
        original = self.runtime.client_factory

        async def stalled(provider, model):
            entered.set()
            await asyncio.Event().wait()

        def factory(key, path):
            client = original(key, path)
            client.set_model = stalled
            return client

        self.runtime.client_factory = factory
        submit = asyncio.create_task(self.runtime.submit(
            self.key, Incoming("operator", "$stalled", "work", mode="smart")))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            stopped = await asyncio.wait_for(self.runtime.stop_session(self.key, "operator"), .2)
            self.assertTrue(stopped)
            self.assertFalse(self.clients[0].prompts)
            self.assertTrue(self.clients[0].stopped)
        finally:
            submit.cancel()
            await asyncio.gather(submit, return_exceptions=True)

    async def test_cancel_interrupts_stalled_live_mode_application(self):
        await self.submit()
        entered = asyncio.Event()

        async def stalled(provider, model):
            entered.set()
            await asyncio.Event().wait()

        self.clients[0].set_model = stalled
        change = asyncio.create_task(self.runtime.change_mode(self.key, "smart", "operator"))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            self.assertTrue(await asyncio.wait_for(self.runtime.cancel(self.key, "operator"), .2))
            self.assertTrue(self.clients[0].stopped)
            self.assertEqual(len(self.clients[0].prompts), 1)
        finally:
            change.cancel()
            await asyncio.gather(change, return_exceptions=True)

    async def test_stalled_selection_has_a_dedicated_short_deadline(self):
        await self.submit()

        async def stalled(provider, model):
            await asyncio.Event().wait()

        self.clients[0].set_model = stalled
        with patch("bridge.runtime.session.MODE_SWITCH_TIMEOUT_SECONDS", .01):
            with self.assertRaisesRegex(ModeSwitchError, "could not confirm"):
                await asyncio.wait_for(self.runtime.change_mode(self.key, "smart", "operator"), .2)
        self.assertEqual(self.runtime._scopes[self.key].mode, "fast")

    async def test_admitted_peer_can_select_mode_without_reaction_requirement(self):
        await self.runtime.submit(self.key, Incoming("peer", "$peer", "work", mode="smart", ack_required=False))
        await self.runtime.wait_idle(self.key)
        self.assertEqual(self.runtime._scopes[self.key].mode, "smart")
        self.assertEqual(self.clients[0].selections, [("openai-codex", "gpt-smart")])

    async def test_ambient_input_cannot_smuggle_mode_selection(self):
        with self.assertRaisesRegex(ModeSwitchError, "explicit mode command"):
            await self.runtime.submit(self.key, Incoming("peer", "$ambient", "text", mode="smart", optional=True))
        self.assertFalse(self.clients)

    async def test_timed_out_live_switch_discards_uncertain_client_and_next_request_recovers(self):
        await self.submit()
        client = self.clients[0]
        async def stalled(provider, model):
            await asyncio.Event().wait()
        client.set_model = stalled
        with patch("bridge.runtime.session.MODE_SWITCH_TIMEOUT_SECONDS", .01):
            with self.assertRaises(ModeSwitchError):
                await self.runtime.change_mode(self.key, "smart")
        self.assertTrue(client.stopped)
        self.assertEqual(self.runtime._scopes[self.key].mode, "fast")
        await self.submit("retry", request="$retry")
        self.assertEqual(self.clients[1].selections, [("test", "flash")])



class ModeAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_matrix_prefixes_strip_address_and_token_preserving_request_case(self):
        bodies = ["/smart Keep CASE", "/fast Keep CASE", "!smart Keep CASE", f"{AGENT_ID} !fast Keep CASE", "Agent_one: !research Keep CASE",
                  f"[{AGENT_ID}](https://matrix.to/#/{AGENT_ID}) !smart Keep CASE"]
        for body in bodies:
            with self.subTest(body=body), tempfile.TemporaryDirectory() as directory:
                runtime = matrix_runtime()
                runtime.modes = MODES
                adapter = matrix_adapter(directory, runtime)
                await adapter._handle_event(ROOM, matrix_event(body, mentions=[AGENT_ID]))
                incoming = runtime.submit.await_args.args[1]
                self.assertEqual(incoming.text, "Keep CASE")
                self.assertFalse(incoming.optional)
                self.assertIn(incoming.mode, MODES)
                self.assertNotIn("!smart", incoming.text)
                self.assertNotIn("!fast", incoming.text)

    async def test_matrix_bare_mode_no_prompt_and_unaddressed_sleeping_room_no_switch(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = matrix_runtime()
            runtime.modes = MODES
            runtime.change_mode = AsyncMock(return_value="🧠 smart mode")
            adapter = matrix_adapter(directory, runtime)
            await adapter._handle_event(ROOM, matrix_event("!smart", mentions=[AGENT_ID]))
            runtime.change_mode.assert_awaited_once()
            runtime.submit.assert_not_awaited()
            self.assertIn("🧠", adapter.output.await_args.args[2])
            runtime.admit.return_value = False
            await adapter._handle_event(ROOM, matrix_event("!fast", event_id="$asleep"))
            self.assertEqual(runtime.change_mode.await_count, 1)

    async def test_discord_both_prefixes_and_bare_switch(self):
        for name in MODES:
            runtime = discord_runtime()
            runtime.modes = MODES
            runtime.change_mode = AsyncMock(return_value=mode_label(name))
            adapter = discord_adapter(runtime)
            message = discord_message(f"<@9> !{name} Keep CASE")
            await adapter._on_message(message)
            incoming = runtime.submit.await_args.args[1]
            self.assertEqual(incoming.mode, name)
            self.assertIn("Keep CASE", incoming.text)
            self.assertNotIn(f"!{name}", incoming.text)
            self.assertFalse(incoming.optional)
            runtime.submit.reset_mock()
            bare = discord_message(f"<@9> !{name}")
            bare.id = 11
            await adapter._on_message(bare)
            runtime.change_mode.assert_awaited_once()
            runtime.submit.assert_not_awaited()

    async def test_admitted_discord_bot_can_select_prefixed_and_bare_modes(self):
        runtime = discord_runtime()
        runtime.modes = MODES
        runtime.change_mode = AsyncMock(return_value="smart")
        adapter = discord_adapter(runtime)
        await adapter._on_message(discord_message("<@9> !smart text", bot=True))
        self.assertEqual(runtime.submit.await_args.args[1].mode, "smart")
        self.assertFalse(runtime.submit.await_args.args[1].ack_required)
        runtime.submit.reset_mock()
        bare = discord_message("<@9> !fast", bot=True)
        bare.id = 11
        await adapter._on_message(bare)
        runtime.change_mode.assert_awaited_once()
        runtime.submit.assert_not_awaited()

    async def test_admitted_matrix_peer_metadata_can_select_prefixed_and_bare_modes(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = matrix_runtime()
            runtime.modes = MODES
            runtime.change_mode = AsyncMock(return_value="smart")
            adapter = matrix_adapter(directory, runtime)
            for index, body in enumerate(("!smart work", "!fast")):
                event = matrix_event(body, event_id=f"$peer{index}", mentions=[AGENT_ID])
                event["sender"] = "@peer-agent:example.test"
                event["content"]["org.example.pi_bridge"] = {"kind":"final", "hop":1, "invitations":[AGENT_ID]}
                await adapter._handle_event(ROOM, event)
            incoming = runtime.submit.await_args.args[1]
            self.assertEqual(incoming.mode, "smart")
            self.assertFalse(incoming.ack_required)
            runtime.change_mode.assert_awaited_once()

    async def test_explicit_other_recipient_is_not_a_mode_command_for_humans_or_peers(self):
        for peer in (False, True):
            with self.subTest(peer=peer), tempfile.TemporaryDirectory() as directory:
                runtime = matrix_runtime()
                runtime.is_awake.return_value = True
                runtime.modes = MODES
                runtime.change_mode = AsyncMock()
                adapter = matrix_adapter(directory, runtime)
                event = matrix_event("@other:example.test !smart work", mentions=["@other:example.test"])
                if peer:
                    event["sender"] = "@peer-agent:example.test"
                    event["content"]["org.example.pi_bridge"] = {"kind":"final", "hop":1, "invitations":["@other:example.test"]}
                await adapter._handle_event(ROOM, event)
                runtime.change_mode.assert_not_awaited()
                self.assertIsNone(runtime.submit.await_args.args[1].mode)

    async def test_incidental_mentions_do_not_suppress_mode_commands(self):
        for peer in (False, True):
            with self.subTest(peer=peer), tempfile.TemporaryDirectory() as directory:
                runtime = matrix_runtime()
                runtime.is_awake.return_value = True
                runtime.modes = MODES
                adapter = matrix_adapter(directory, runtime)
                event = matrix_event("!smart Summarize @other:example.test note", mentions=["@other:example.test"])
                if peer:
                    event["sender"] = "@peer-agent:example.test"
                    event["content"]["org.example.pi_bridge"] = {"kind":"final", "hop":1, "invitations":[]}
                await adapter._handle_event(ROOM, event)
                self.assertEqual(runtime.submit.await_args.args[1].mode, "smart")
            runtime = discord_runtime()
            runtime.modes = MODES
            adapter = discord_adapter(runtime, wake_on_any_channel_ids=(2,))
            message = discord_message("!smart Summarize <@42> note", mentioned=False, bot=peer)
            message.mentions = [SimpleNamespace(id=42)]
            await adapter._on_message(message)
            self.assertEqual(runtime.submit.await_args.args[1].mode, "smart")

    async def test_discord_mode_notice_is_nonterminal_and_keeps_request_for_ack(self):
        runtime = discord_runtime(record_activity=AsyncMock())
        adapter = discord_adapter(runtime)
        key = SessionKey("agent_two", "discord", "2")
        message = discord_message("<@9> !smart work")
        message.channel.typing = AsyncMock()
        message.reply.return_value = SimpleNamespace(id=42)
        adapter._requests[(key, "10")] = message
        await adapter.output(key, "mode", "🧠 smart", "10")
        self.assertIs(adapter._requests[(key, "10")], message)
        await adapter.output(key, "ack", "Working", "10")
        self.assertEqual(message.reply.await_count, 2)
        self.assertIs(adapter._requests[(key, "10")], message)

    async def test_matrix_mode_refusal_uses_request_scoped_error(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = matrix_runtime()
            runtime.change_mode = AsyncMock(side_effect=ModeSwitchError("turn is running"))
            adapter = matrix_adapter(directory, runtime)
            await adapter._handle_event(ROOM, matrix_event("!smart", mentions=[AGENT_ID]))
            self.assertEqual(adapter.output.await_args.args[1], "request_error")
            runtime.submit.assert_not_awaited()

    def test_matrix_leading_address_extraction_does_not_parse_prose(self):
        for body in ("hello Agent_one: !smart", "> !fast", "Agent_two: !smart"):
            self.assertIsNone(addressed_command_body(matrix_event(body, mentions=[AGENT_ID])["content"],
                                                     AGENT_ID))
