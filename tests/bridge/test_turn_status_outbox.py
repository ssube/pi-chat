"""Real process-loss recovery plus deterministic cadence/epoch checks."""
import asyncio
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from urllib.parse import unquote

from bridge.runtime.state import SessionKey
from bridge.turn_status import Deferred, Indicator, TurnIndicators
from test_bridge_delivery import OPERATOR, discord_adapter, matrix_adapter, matrix_key, matrix_runtime


# This is a real child process with a file-backed fake transport. No live agent,
# network, model, or service is stopped. SIGKILL cannot run a cleanup/finally.
CHILD = r'''
import asyncio, json, sys
from pathlib import Path
from bridge.runtime.state import SessionKey
from bridge.turn_status import TurnIndicators

async def main():
    path, mode, scope = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
    agent, transport_name, request_id = sys.argv[4:7]
    async def emit(record, phase):
        transport = path.with_name("transport.json")
        transport.write_text(json.dumps({"$owned": ["⏳", "bot"], "$other": ["⏳", "other"]}))
        transport.chmod(0o600)
        if mode == "unknown":
            print("ready", flush=True)  # Server accepted it; acknowledgement is lost.
            await asyncio.Event().wait()
        return record.request_id if record.key.transport == "discord" else "$owned"
    store = TurnIndicators(path, agent, emit, interval=0)
    key = SessionKey(agent, transport_name, scope)
    start = asyncio.create_task(store.update(key, request_id, "running", 7))
    await asyncio.sleep(0)
    await store.flush_once()
    await start
    if mode in {"confirmed", "clear", "terminal-emitted", "clear-emitted"}:
        phase = "clear" if mode in {"clear", "clear-emitted"} else "done"
        asyncio.create_task(store.update(key, request_id, phase, 7))
        await asyncio.sleep(0)  # Intent persisted, no terminal HTTP yet.
        if mode.endswith("-emitted"):
            record = next(iter(store.records.values()))
            if transport_name == "matrix":
                sys.path.insert(0, str(Path.cwd() / "tests" / "bridge"))
                from test_turn_status_outbox import FileMatrix, matrix_adapter, matrix_runtime
                server = FileMatrix(path.with_name("transport.json"))
                adapter = matrix_adapter(path.parent, matrix_runtime())
                adapter._request = server.request
                await adapter.turn_status(record, phase)
            else:
                events = {"$other": ["⏳", "other"]}
                if phase == "done":
                    events["$terminal"] = ["🤖", "bot"]
                path.with_name("transport.json").write_text(json.dumps(events))
            # Deliberately bypass the outbox's post-HTTP commit, then SIGKILL.
    print("ready", flush=True)
    await asyncio.Event().wait()
asyncio.run(main())
'''


ACK_CHILD = r'''
import asyncio, json, sys
from pathlib import Path
from unittest.mock import AsyncMock
sys.path.insert(0, str(Path.cwd() / "tests" / "bridge"))
from bridge.runtime.session import BridgeRuntime, Incoming
from bridge.runtime.state import SessionKey
from bridge.turn_status import TurnIndicators
from test_turn_status import StatusRpc

async def main():
    path, mode, scope = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
    agent, transport_name, request_id = sys.argv[4:7]
    key = SessionKey(agent, transport_name, scope)
    async def emit(record, phase):
        server = path.with_name("transport.json")
        server.write_text(json.dumps({"$owned": ["⏳", "bot"], "$other": ["⏳", "other"]}))
        server.chmod(0o600)
        return request_id if transport_name == "discord" else "$owned"
    store = TurnIndicators(path, agent, emit, interval=0)
    async def output(key, kind, text, request):
        if kind == "ack":
            print("ready", flush=True)
            await asyncio.Event().wait()  # Real SIGKILL while ack HTTP is pending.
        return True
    runtime = BridgeRuntime(path.parent / "runtime", lambda key, file: StatusRpc(file),
                            output, turn_status=store.update)
    if mode == "ack-image":
        runtime.image_captioner = AsyncMock(return_value="fixture")
    asyncio.create_task(store.run())
    images = ({"type": "image", "data": "fixture", "mimeType": "image/png"},) if mode == "ack-image" else ()
    await runtime.submit(key, Incoming("human", request_id, "question", images=images))
asyncio.run(main())
'''


class FileMatrix:
    def __init__(self, path):
        self.path = path
        self.events = json.loads(path.read_text())
        self.transaction_file = path.with_suffix(".transactions.json")
        self.transactions = (json.loads(self.transaction_file.read_text())
                             if self.transaction_file.exists() else {})
        self.redacted = []
        self.count = len(self.transactions)

    async def request(self, method, path, data=None, *, defer_on_limit=False):
        if method == "GET":
            return {"event_id": unquote(path.split("/event/", 1)[1]),
                    "type": "m.room.message", "sender": OPERATOR,
                    "content": {"msgtype": "m.text", "body": "Question"}}
        if "/send/m.reaction/" in path:
            emoji = data["m.relates_to"]["key"]
            if emoji == "⏳" and "$owned" in self.events:
                event = "$owned"  # Replay of the same accepted pending transaction.
            else:
                if path not in self.transactions:
                    self.count += 1
                    self.transactions[path] = f"$receipt-{self.count}"
                    self.transaction_file.write_text(json.dumps(self.transactions))
                event = self.transactions[path]
                self.events[event] = [emoji, "bot"]
        elif "/redact/" in path:
            event = unquote(path.split("/redact/", 1)[1].split("/", 1)[0])
            if event == "$other":
                raise AssertionError("must never remove another participant's reaction")
            self.redacted.append(event)
            self.events.pop(event, None)
        else:
            raise AssertionError("unexpected transport operation")
        self.path.write_text(json.dumps(self.events))
        return {"event_id": event}


async def enqueue(store, key, request, phase, epoch=1):
    task = asyncio.create_task(store.update(key, request, phase, epoch))
    await asyncio.sleep(0)
    return task


class OutboxTests(unittest.IsolatedAsyncioTestCase):
    async def killed(self, directory, key, request_id, mode):
        path = Path(directory) / "turn-indicators.json"
        script = ACK_CHILD if mode.startswith("ack-") else CHILD
        child = subprocess.Popen([sys.executable, "-u", "-c", script, str(path), mode, key.scope,
                                  key.agent, key.transport, request_id],
                                 cwd=Path(__file__).resolve().parents[2],
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        try:
            ready = await asyncio.wait_for(asyncio.to_thread(child.stdout.readline), 5)
            self.assertEqual(ready.strip(), "ready")
            if mode.startswith("ack-"):
                states = list((Path(directory) / "runtime").rglob("state.json"))
                self.assertEqual(len(states), 1)
                self.assertEqual(json.loads(states[0].read_text())["last_message_id"], request_id)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
            os.kill(child.pid, signal.SIGKILL)
            self.assertEqual(await asyncio.to_thread(child.wait, 5), -signal.SIGKILL)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)
            child.stdout.close()
        return path

    @unittest.skipUnless(hasattr(signal, "SIGKILL"), "requires POSIX SIGKILL")
    async def test_real_sigkill_restart_reconciles_without_duplicate_or_stranded_marker(self):
        for mode, expected in (("running", "⚠️"), ("unknown", "⚠️"),
                               ("confirmed", "🤖"), ("clear", None),
                               ("terminal-emitted", "🤖"), ("clear-emitted", None),
                               ("ack-text", "⚠️"), ("ack-image", "⚠️")):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                key = matrix_key()
                path = await self.killed(directory, key, "$request", mode)
                transport = FileMatrix(path.with_name("transport.json"))
                if mode.endswith("-emitted"):
                    self.assertNotIn("$owned", transport.events)
                else:
                    self.assertEqual(transport.events["$owned"], ["⏳", "bot"])
                adapter = matrix_adapter(directory, matrix_runtime())
                adapter._request = AsyncMock(side_effect=transport.request)
                recovered = TurnIndicators(path, "agent_one", adapter.turn_status, interval=0)
                recovered.recover()
                await recovered.flush_once()
                own = [emoji for emoji, sender in transport.events.values() if sender == "bot"]
                self.assertEqual(own, [] if expected is None else [expected])
                self.assertEqual(transport.events["$other"], ["⏳", "other"])
                self.assertEqual(set(transport.redacted), {"$owned"})
                self.assertFalse(recovered.records)
                count = adapter._request.await_count
                # A second restart must not emit a duplicate terminal reaction.
                again = TurnIndicators(path, "agent_one", adapter.turn_status, interval=0)
                again.recover()
                await again.flush_once()
                self.assertEqual(adapter._request.await_count, count)

    @unittest.skipUnless(hasattr(signal, "SIGKILL"), "requires POSIX SIGKILL")
    async def test_discord_real_sigkill_recovery_fetches_target_and_removes_only_self(self):
        for mode, expected in (("running", "⚠️"), ("unknown", "⚠️"),
                               ("confirmed", "🤖"), ("clear", None),
                               ("terminal-emitted", "🤖"), ("clear-emitted", None),
                               ("ack-text", "⚠️"), ("ack-image", "⚠️")):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                key = SessionKey("agent_two", "discord", "2")
                path = await self.killed(directory, key, "20", mode)
                events = json.loads(path.with_name("transport.json").read_text())
                events["$manual"] = ["✅", "bot"]
                bot = SimpleNamespace(id=9)

                async def add(emoji):
                    if [emoji, "bot"] not in events.values():
                        events[f"bot:{emoji}"] = [emoji, "bot"]

                async def remove(emoji, user):
                    import discord
                    self.assertIs(user, bot)
                    found = False
                    for event, value in list(events.items()):
                        if value == [emoji, "bot"]:
                            events.pop(event)
                            found = True
                    if not found:
                        raise discord.NotFound(SimpleNamespace(status=404, reason="Not Found"),
                                               {"code": 10014, "message": "Unknown reaction"})

                channel = SimpleNamespace(id=2, guild=SimpleNamespace(id=1))
                message = SimpleNamespace(channel=channel, add_reaction=AsyncMock(side_effect=add),
                                          remove_reaction=AsyncMock(side_effect=remove))
                channel.fetch_message = AsyncMock(return_value=message)
                adapter = discord_adapter(matrix_runtime())
                adapter._status_requests = {}
                adapter.client = SimpleNamespace(user=bot, get_channel=Mock(return_value=channel))
                recovered = TurnIndicators(path, "agent_two", adapter.turn_status, interval=0)
                recovered.recover()
                await recovered.flush_once()
                channel.fetch_message.assert_awaited_once_with(20)
                message.remove_reaction.assert_awaited_once_with("⏳", bot)
                own = [emoji for emoji, sender in events.values() if sender == "bot" and emoji != "✅"]
                self.assertEqual(own, [] if expected is None else [expected])
                self.assertEqual(events["$other"], ["⏳", "other"])
                self.assertEqual(events["$manual"], ["✅", "bot"])
                self.assertFalse(recovered.records)
                count = message.add_reaction.await_count
                again = TurnIndicators(path, "agent_two", adapter.turn_status, interval=0)
                again.recover()
                await again.flush_once()
                self.assertEqual(message.add_reaction.await_count, count)

    async def test_matrix_keeps_unadmitted_event_replayable_until_runtime_returns(self):
        with tempfile.TemporaryDirectory() as directory:
            entered, release = asyncio.Event(), asyncio.Event()

            async def submit(key, incoming):
                entered.set()
                await release.wait()

            runtime = matrix_runtime(submit=AsyncMock(side_effect=submit))
            adapter = matrix_adapter(directory, runtime)
            adapter._request = AsyncMock(return_value={"event_id": "$notice"})
            event = {"event_id": "$request", "type": "m.room.message", "sender": OPERATOR,
                     "content": {"msgtype": "m.text", "body": "Question",
                                 "m.mentions": {"user_ids": [adapter.settings.user_id]}}}
            delivery = asyncio.create_task(adapter._deliver_event(
                matrix_key().scope.removesuffix(":main"), event))
            try:
                await asyncio.wait_for(entered.wait(), 1)
                self.assertFalse(adapter._seen_file.exists())
            finally:
                release.set()
                await delivery
            self.assertIn("$request", json.loads(adapter._seen_file.read_text()))

    async def test_one_phase_change_per_scope_per_interval_and_no_running_heartbeat(self):
        with tempfile.TemporaryDirectory() as directory:
            clock = [100.0]
            calls = []

            async def emit(record, phase):
                calls.append((clock[0], record.request_id, phase))
                return "$pending"

            store = TurnIndicators(Path(directory) / "status.json", "agent_one", emit,
                                   interval=2, now=lambda: clock[0])
            key = matrix_key()
            first = await enqueue(store, key, "$first", "running")
            await store.flush_once()
            await first
            second = await enqueue(store, key, "$second", "running")
            for tick in range(1, 20):
                clock[0] = 100 + tick / 10
                await store.update(key, "$first", "running", 1)
                await store.flush_once()
            self.assertEqual(len(calls), 1)
            clock[0] = 102
            await store.flush_once()
            await second
            done1 = await enqueue(store, key, "$first", "done")
            done2 = await enqueue(store, key, "$second", "done")
            for moment in (102.1, 103.9, 104, 104.1, 105.9, 106):
                clock[0] = moment
                await store.flush_once()
            await asyncio.gather(done1, done2)
            self.assertEqual([call[2] for call in calls], ["running", "running", "done", "done"])
            self.assertTrue(all(b[0] - a[0] >= 2 for a, b in zip(calls, calls[1:])))
            self.assertFalse(store.records)

    async def test_flush_pass_does_not_overlap_targets_in_one_bucket(self):
        for key in (matrix_key(), SessionKey("agent_one", "discord", "2")):
            with self.subTest(transport=key.transport), tempfile.TemporaryDirectory() as directory:
                entered, release = asyncio.Event(), asyncio.Event()
                calls = []

                async def emit(record, phase):
                    calls.append((record.request_id, phase))
                    if record.request_id == "first":
                        entered.set()
                        await release.wait()
                    return "$pending"

                store = TurnIndicators(Path(directory) / "status.json", "agent_one", emit, interval=0)
                first = await enqueue(store, key, "first", "running")
                second = await enqueue(store, key, "second", "running")
                flushing = asyncio.create_task(store.flush_once())
                try:
                    await asyncio.wait_for(entered.wait(), 1)
                    await asyncio.sleep(0)
                    self.assertEqual(calls, [("first", "running")])
                finally:
                    release.set()
                await asyncio.wait_for(flushing, 1)
                await asyncio.gather(first, second)
                self.assertEqual(calls, [("first", "running"), ("second", "running")])

    async def test_validated_transport_deferral_persists_without_spending_scope_cooldown(self):
        with tempfile.TemporaryDirectory() as directory:
            clock = [100.0]
            calls = []
            matrix_first = matrix_key()
            matrix_second = SessionKey("agent_one", "matrix", "!other:test:main")
            discord = SessionKey("agent_one", "discord", "2")

            async def emit(record, phase):
                calls.append((record.key.transport, record.key.scope, phase))
                if record.key == matrix_first and len(calls) == 1:
                    return Deferred("matrix", 5.0)
                return "$pending"

            path = Path(directory) / "status.json"
            store = TurnIndicators(path, "agent_one", emit, interval=2, now=lambda: clock[0])
            first = await enqueue(store, matrix_first, "$first", "running")
            second = await enqueue(store, matrix_second, "$second", "running")
            third = await enqueue(store, discord, "20", "running")
            await store.flush_once()
            await third
            self.assertEqual(calls, [("matrix", matrix_first.scope, "running"),
                                     ("discord", discord.scope, "running")])
            self.assertEqual(store.transport_not_before, {"matrix": 105.0})
            self.assertNotIn(matrix_first, store.last_scope_attempt)
            self.assertEqual(store.records[(matrix_first, "$first", 1)].last_attempt, 0)
            self.assertFalse(store.records[(matrix_first, "$first", 1)].pending_attempted)
            self.assertFalse(store.failures)
            saved = json.loads(path.read_text())
            self.assertEqual(saved["schema"], 2)
            self.assertEqual(saved["transport_not_before"], [{"transport": "matrix", "at": 105.0}])
            restored = TurnIndicators(path, "agent_one", emit, interval=2, now=lambda: clock[0])
            self.assertEqual(restored.transport_not_before, {"matrix": 105.0})
            done = await enqueue(store, matrix_first, "$first", "done")
            clock[0] = 104.0
            await store.flush_once()
            self.assertEqual(len(calls), 2)
            clock[0] = 106.0
            await store.flush_once()
            await asyncio.gather(first, second, done)
            self.assertEqual(calls[2:], [("matrix", matrix_first.scope, "done"),
                                         ("matrix", matrix_second.scope, "running")])
            self.assertFalse(store.transport_not_before)

    async def test_v1_indicator_state_loads_and_upgrades_on_next_save(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "status.json"
            path.write_text(json.dumps({"records": [], "last_updates": []}))
            store = TurnIndicators(path, "agent_one", AsyncMock(), now=lambda: 100.0)
            self.assertFalse(store.load_error)
            store.recover()
            saved = json.loads(path.read_text())
            self.assertEqual(saved["schema"], 2)
            self.assertEqual(saved["transport_not_before"], [])
            self.assertEqual(saved["records"], [])

    async def test_future_transport_gate_is_bounded_on_load_without_rewriting_source(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "status.json"
            raw = {"schema": 2, "records": [], "last_updates": [],
                   "transport_not_before": [{"transport": "matrix", "at": 10000.0}]}
            path.write_text(json.dumps(raw))
            clock = [100.0]
            store = TurnIndicators(path, "agent_one", AsyncMock(), now=lambda: clock[0])
            self.assertFalse(store.load_error)
            self.assertEqual(store.transport_not_before, {"matrix": 130.0})
            self.assertEqual(json.loads(path.read_text()), raw)
            store.recover()  # An unrelated state write must not shorten the raw deadline.
            self.assertEqual(json.loads(path.read_text()), raw)
            clock[0] = 131.0
            store._save()
            self.assertEqual(json.loads(path.read_text())["transport_not_before"], [])
            for malformed in ({**raw, "unexpected": True}, {**raw, "schema": True},
                              {**raw, "transport_not_before": [{"transport": "matrix", "at": True}]}):
                with self.subTest(malformed=malformed):
                    path.write_text(json.dumps(malformed))
                    invalid = TurnIndicators(path, "agent_one", AsyncMock(), now=lambda: 100.0)
                    self.assertTrue(invalid.load_error)
                    self.assertEqual(json.loads(path.read_text()), malformed)

    async def test_mid_pass_other_bucket_proceeds_and_repeated_ticks_do_not_duplicate(self):
        for transport, first_scope, second_scope in (
                ("matrix", "!first:test:main", "!second:test:main"),
                ("discord", "1", "2")):
            with self.subTest(transport=transport), tempfile.TemporaryDirectory() as directory:
                first_key = SessionKey("agent_one", transport, first_scope)
                second_key = SessionKey("agent_one", transport, second_scope)
                entered, release = asyncio.Event(), asyncio.Event()
                calls = []

                async def emit(record, phase):
                    calls.append((record.key.scope, phase))
                    if record.key == first_key:
                        entered.set()
                        await release.wait()
                    return "$pending"

                store = TurnIndicators(Path(directory) / "status.json", "agent_one", emit, interval=0)
                first = await enqueue(store, first_key, "first", "running")
                store.schedule_once()
                try:
                    await asyncio.wait_for(entered.wait(), 1)
                    second = await enqueue(store, second_key, "second", "running")
                    for _ in range(4):
                        store.schedule_once()
                    await asyncio.wait_for(second, 1)
                    self.assertEqual(calls.count((first_scope, "running")), 1)
                    self.assertEqual(calls.count((second_scope, "running")), 1)
                    self.assertFalse(first.done())
                    path = Path(directory) / "status.json"
                    saved = json.loads(path.read_text())
                    self.assertEqual({row["request_id"] for row in saved["records"]},
                                     {"first", "second"})
                    restored = TurnIndicators(path, "agent_one", emit, interval=0)
                    self.assertFalse(restored.load_error)
                    self.assertEqual(set(restored.records),
                                     {(first_key, "first", 1), (second_key, "second", 1)})
                finally:
                    release.set()
                await asyncio.wait_for(first, 1)
                store.close()

    async def test_matrix_threads_share_one_bucket_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            first_key = SessionKey("agent_one", "matrix", "!room:test:thread:$one")
            second_key = SessionKey("agent_one", "matrix", "!room:test:thread:$two")
            entered, release = asyncio.Event(), asyncio.Event()
            calls = []

            async def emit(record, phase):
                calls.append(record.request_id)
                if record.request_id == "first":
                    entered.set()
                    await release.wait()
                return "$pending"

            store = TurnIndicators(Path(directory) / "status.json", "agent_one", emit, interval=0)
            first = await enqueue(store, first_key, "first", "running")
            store.schedule_once()
            try:
                await asyncio.wait_for(entered.wait(), 1)
                second = await enqueue(store, second_key, "second", "running")
                for _ in range(4):
                    store.schedule_once()
                await asyncio.sleep(0)
                self.assertEqual(calls, ["first"])
            finally:
                release.set()
            await asyncio.wait_for(first, 1)
            store.schedule_once()
            await asyncio.wait_for(second, 1)
            self.assertEqual(calls, ["first", "second"])
            store.close()

    async def test_drain_cancels_stragglers_without_failure_or_orphan_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            entered = asyncio.Event()

            async def emit(record, phase):
                entered.set()
                await asyncio.Event().wait()

            path = Path(directory) / "status.json"
            store = TurnIndicators(path, "agent_one", emit, interval=0)
            record = Indicator(SessionKey("agent_one", "discord", "2"), "20", 1)
            store.records[record.target] = record
            store._save()
            store._save = Mock(wraps=store._save)
            store.schedule_once()
            await asyncio.wait_for(entered.wait(), 1)
            await store.drain(timeout=0.01)
            calls_after_drain = store._save.call_count
            self.assertFalse(store.failures)
            self.assertEqual(json.loads(path.read_text())["records"][0]["desired"], "running")
            store.close()
            store.close()
            await asyncio.sleep(0)
            self.assertEqual(store._save.call_count, calls_after_drain)
            self.assertEqual(asyncio.all_tasks(), {asyncio.current_task()})

    async def test_coalesces_before_tick_and_discards_stale_epoch_terminal(self):
        with tempfile.TemporaryDirectory() as directory:
            emit = AsyncMock(return_value="$pending")
            store = TurnIndicators(Path(directory) / "status.json", "agent_one", emit, interval=0)
            key = matrix_key()
            pending = await enqueue(store, key, "$request", "running", 4)
            done = await enqueue(store, key, "$request", "done", 4)
            await store.flush_once()
            await asyncio.gather(pending, done)
            self.assertEqual(emit.await_args.args[1], "done")
            self.assertEqual(emit.await_count, 1)  # No needless intermediate hourglass.
            pending = await enqueue(store, key, "$next", "running", 5)
            await store.flush_once()
            await pending
            await store.update(key, "$next", "failed", 4)
            await store.flush_once()
            self.assertEqual(emit.await_count, 2)
            self.assertEqual(store.records[(key, "$next", 5)].desired, "running")

    async def test_retry_preserves_confirmed_intent_and_respects_backoff(self):
        with tempfile.TemporaryDirectory() as directory:
            clock = [100.0]
            emit = AsyncMock(side_effect=["$pending", RuntimeError("offline"), None])
            path = Path(directory) / "status.json"
            store = TurnIndicators(path, "agent_one", emit, interval=0, now=lambda: clock[0])
            pending = await enqueue(store, matrix_key(), "$request", "running")
            await store.flush_once()
            await pending
            done = await enqueue(store, matrix_key(), "$request", "done")
            await store.flush_once()
            self.assertEqual(json.loads(path.read_text())["records"][0]["desired"], "done")
            await store.flush_once()
            self.assertEqual(emit.await_count, 2)
            clock[0] += 2
            await store.flush_once()
            await done
            self.assertEqual(emit.await_count, 3)
            self.assertFalse(store.records)
