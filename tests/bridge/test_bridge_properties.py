"""Generated inputs and operation sequences complement controlled race schedules."""
import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from hypothesis import given, seed, settings, strategies as st

from bridge.filtering import metadata_rejection
from bridge.mentions import Member, Roster, find_mentions, replace_mentions
from bridge.runtime.pi_rpc import PiRpcProtocolError, decode_json_object
from bridge.runtime.session import BridgeRuntime, Incoming
from bridge.runtime.state import SessionKey
from reliability_helpers import ControlledRpc

settings.register_profile("bridge", max_examples=60, deadline=None, print_blob=True)
settings.load_profile("bridge")


class MentionProperties(unittest.TestCase):
    @seed(int(os.environ.get("BRIDGE_TEST_SEED", "20261012")))
    @given(st.text())
    def test_spans_are_ordered_and_replacement_is_lossless(self, text):
        mentions = find_mentions(text)
        previous = 0
        for mention in mentions:
            self.assertGreaterEqual(mention.start, previous)
            self.assertEqual(text[mention.start:mention.end], mention.raw)
            previous = mention.end
        roster = Roster([Member("@alice:example.test", "alice", ("alice", "Alice Smith", "Élodie"))])
        plan = roster.plan(text)
        self.assertEqual(replace_mentions(text, plan, lambda mention, _target: mention.raw), text)

    @seed(int(os.environ.get("BRIDGE_TEST_SEED", "20261012")))
    @given(st.sampled_from(["Alice Smith", "Élodie", "alice", "ops-team"]), st.integers(1, 5))
    def test_escape_and_code_never_notify(self, name, count):
        roster = Roster([Member("@alice:example.test", "alice", (name,))])
        for text in ("`@" + name + "`", "```\n@" + name + "\n```", "\\@" + name):
            self.assertEqual(roster.plan(text).user_ids, [])
        self.assertEqual(roster.plan(("@" + name + " ") * count).user_ids, ["@alice:example.test"])

    @seed(int(os.environ.get("BRIDGE_TEST_SEED", "20261012")))
    @given(st.lists(st.sampled_from(["a", "b", "c"]), min_size=2, max_size=8))
    def test_alias_collisions_are_order_independent(self, names):
        members = [Member(f"@user{index}:example.test", f"user{index}", (name,))
                   for index, name in enumerate(names)]
        first, reverse = Roster(members), Roster(reversed(members))
        for name in names:
            expected = 1 if names.count(name) == 1 else 0
            self.assertEqual(first.plan("@" + name).user_ids, reverse.plan("@" + name).user_ids)
            self.assertEqual(len(first.plan("@" + name).user_ids), expected)

    def test_full_ids_ports_unicode_and_mention_limit(self):
        for target in ("@alice:example.test:8448", "@alice:[::1]:8448", "@Élodie:example.test"):
            roster = Roster([Member(target, "canonical", ())])
            self.assertEqual(roster.plan(target + ".").user_ids, [target])
        roster = Roster([Member("@alice:example.test", "alice", ("alice",))])
        self.assertIn("limit", roster.feedback(roster.plan("@alice " * 33)))


class ProtocolProperties(unittest.TestCase):
    @seed(int(os.environ.get("BRIDGE_TEST_SEED", "20261012")))
    @given(st.one_of(st.none(), st.booleans(), st.integers(), st.text(),
                     st.lists(st.integers()), st.dictionaries(st.text(), st.text())))
    def test_rpc_decoder_accepts_only_objects(self, value):
        encoded = json.dumps(value).encode()
        if isinstance(value, dict):
            self.assertEqual(decode_json_object(encoded), value)
        else:
            with self.assertRaises(PiRpcProtocolError):
                decode_json_object(encoded)

    @seed(int(os.environ.get("BRIDGE_TEST_SEED", "20261012")))
    @given(st.recursive(st.one_of(st.none(), st.booleans(), st.integers(), st.text()),
                        lambda children: st.lists(children) | st.dictionaries(st.text(), children), max_leaves=10))
    def test_filter_metadata_is_total(self, value):
        result = metadata_rejection({"kind": value, "hop": value}, awake=False,
                                    addressed=False, wake_any=True)
        self.assertIsInstance(result, str)


class LifecycleProperties(unittest.TestCase):
    @settings(max_examples=30)
    @seed(int(os.environ.get("BRIDGE_TEST_SEED", "20261012")))
    @given(st.lists(st.tuples(st.sampled_from(["matrix", "discord"]),
                             st.sampled_from(["submit", "duplicate", "complete", "compact", "cancel"])),
                    min_size=1, max_size=25))
    def test_generated_lifecycle_sequences(self, operations):
        async def exercise(root):
            clients = []
            output = AsyncMock()

            def factory(key, path):
                client = ControlledRpc(path, hold=True)
                clients.append((key, client))
                return client

            runtime = BridgeRuntime(root, factory, output)
            keys = {name: SessionKey("agent_two", name, "synthetic") for name in ("matrix", "discord")}
            previous = {}
            serial = 0
            try:
                for transport, action in operations:
                    key = keys[transport]
                    scope = runtime._scopes.get(key)
                    if action == "submit" or action == "duplicate" and transport not in previous:
                        serial += 1
                        previous[transport] = str(serial)
                        await runtime.submit(key, Incoming("owner", str(serial), f"message {serial}"))
                        scope = runtime._scopes[key]
                        await asyncio.wait_for(scope.accepted.wait(), 1)
                    elif action == "duplicate":
                        self.assertEqual(await runtime.submit(key, Incoming("owner", previous[transport], "repeat")), "duplicate")
                    elif action == "complete" and scope and scope.client:
                        scope.client.release.set()
                        await asyncio.wait_for(runtime.wait_idle(key), 1)
                    elif action == "cancel" and scope and scope.active:
                        self.assertTrue(await runtime.cancel(key, "owner"))
                        await asyncio.wait_for(runtime.wait_idle(key), 1)
                    elif action == "compact":
                        await runtime.sweep_idle(force=True)
                    for scope in runtime._scopes.values():
                        if scope.active:
                            self.assertIsNotNone(scope.client)
                for scope in runtime._scopes.values():
                    if scope.client:
                        scope.client.release.set()
                await asyncio.gather(*(runtime.wait_idle(key) for key in keys.values()))
                terminal = [(call.args[0], call.args[3]) for call in output.await_args_list
                            if call.args[1] in {"final", "error", "cancelled"}]
                self.assertEqual(len(terminal), len(set(terminal)))
                self.assertTrue(all(count == 0 for count in runtime.telemetry.inflight.values()))
            finally:
                await runtime.close()
        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(asyncio.wait_for(exercise(Path(directory)), 5))
