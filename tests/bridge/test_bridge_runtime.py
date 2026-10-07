import asyncio
import stat
import tempfile
import unittest
from pathlib import Path

from bridge.runtime.pi_rpc import PiProviderError, PiRunResult, RpcImage
from bridge.runtime.session import BridgeRuntime, Incoming, ReplyRejected, _deliver_progress
from bridge.runtime.state import Checkpoint, SessionKey, read_checkpoint, write_checkpoint


class FakeRpc:
    def __init__(self, session_file):
        self.session_file = session_file
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.active = False
        self.prompts = []
        self.prompt_images = []
        self.steers = []
        self.aborted = False
        self.stopped = False

    async def prompt(self, message, on_event, images=None, *, on_accepted=None):
        self.active = True
        self.prompts.append(message)
        self.prompt_images.append(images)
        self.session_file.write_text("private full transcript\n")
        if on_accepted:
            await on_accepted()
        self.started.set()
        await on_event({"type": "tool_execution_start", "toolName": "shell",
                        "args": {"secret": "must never be posted"}})
        await self.release.wait()
        self.active = False
        return PiRunResult("Done")

    async def steer(self, message, images=None):
        self.steers.append(message)

    async def abort(self):
        self.aborted = True
        self.release.set()

    async def compact(self):
        return "safe saved summary"

    async def stop(self):
        self.stopped = True


class BridgeRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_rejected_matrix_final_is_rewritten_in_same_pi_session(self):
        with tempfile.TemporaryDirectory() as directory:
            clients = []
            delivered = []

            class RevisionRpc(FakeRpc):
                async def prompt(self, message, on_event, images=None, *, on_accepted=None):
                    self.prompts.append(message)
                    if on_accepted:
                        await on_accepted()
                    return PiRunResult("Ask @agent_two-agent about it." if len(self.prompts) == 1
                                       else "Ask Agent_two about it.")

            def factory(key, session_file):
                client = RevisionRpc(session_file)
                clients.append(client)
                return client

            async def send(key, kind, text, request_id):
                if kind == "final" and "@agent_two-agent" in text:
                    raise ReplyRejected("@agent_two-agent is not in this room. People here: Operator, Agent_three.")
                delivered.append((kind, text))

            runtime = BridgeRuntime(Path(directory), factory, send)
            key = SessionKey("agent_one", "matrix", "!direct:example.test:main")
            await runtime.submit(key, Incoming("Operator", "$request", "Ask Agent_two"))
            await asyncio.wait_for(runtime.wait_idle(key), timeout=1)
            self.assertEqual(len(clients), 1)
            self.assertEqual(len(clients[0].prompts), 2)
            self.assertIn("People here: Operator, Agent_three", clients[0].prompts[1])
            self.assertEqual(delivered, [("ack", "I’m working on it."),
                                         ("final", "Ask Agent_two about it.")])
            self.assertTrue(read_checkpoint(key.directory(Path(directory))).final_sent)

    async def test_repeated_mention_rejection_never_posts_public_error(self):
        with tempfile.TemporaryDirectory() as directory:
            clients = []
            delivered = []

            class RevisionRpc(FakeRpc):
                async def prompt(self, message, on_event, images=None, *, on_accepted=None):
                    self.prompts.append(message)
                    if on_accepted:
                        await on_accepted()
                    return PiRunResult("Ask @absent-agent about it.")

            def factory(key, session_file):
                client = RevisionRpc(session_file)
                clients.append(client)
                return client

            async def send(key, kind, text, request_id):
                if kind == "final":
                    raise ReplyRejected("@absent-agent is not in this room. People here: Operator.")
                delivered.append(kind)

            runtime = BridgeRuntime(Path(directory), factory, send)
            key = SessionKey("agent_one", "matrix", "!direct:example.test:main")
            await runtime.submit(key, Incoming("Operator", "$request", "Ask about it"))
            await asyncio.wait_for(runtime.wait_idle(key), timeout=1)
            self.assertEqual(len(clients[0].prompts), 3)
            self.assertEqual(delivered, ["ack", "cleanup"])
            self.assertEqual(read_checkpoint(key.directory(Path(directory))).status,
                             "delivery_error")

    async def test_bounded_wait_for_turn_does_not_cancel_live_pi(self):
        with tempfile.TemporaryDirectory() as directory:
            clients = []

            def factory(key, session_file):
                client = FakeRpc(session_file)
                clients.append(client)
                return client

            async def send(key, kind, text, request_id):
                pass

            runtime = BridgeRuntime(Path(directory), factory, send)
            key = SessionKey("agent_two", "matrix", "shared-room")
            await runtime.submit(key, Incoming("chair", "start", "Prepare"))
            await clients[0].started.wait()
            with self.assertRaises(TimeoutError):
                await asyncio.wait_for(runtime.wait_idle(key), timeout=0.01)
            self.assertTrue(clients[0].active)
            clients[0].release.set()
            await runtime.wait_idle(key)

    async def test_text_only_agent_gets_image_description_without_image_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            clients = []
            output = []
            described = []

            def factory(key, session_file):
                client = FakeRpc(session_file)
                clients.append(client)
                return client

            async def caption(key, images):
                described.append((key.agent, images))
                return "A red and blue diagram."

            async def send(key, kind, text, request_id):
                output.append((kind, text, request_id))

            runtime = BridgeRuntime(Path(directory), factory, send,
                                    image_captioner=caption)
            key = SessionKey("agent_three", "matrix", "image-room")
            image = RpcImage("image/png", b"synthetic")
            await runtime.submit(key, Incoming("Agent_one", "1", "Review", (image,)))
            await clients[0].started.wait()
            self.assertEqual(described, [("agent_three", (image,))])
            self.assertEqual(clients[0].prompt_images, [[]])
            self.assertIn("A red and blue diagram.", clients[0].prompts[0])
            self.assertEqual([kind for kind, _, _ in output if kind == "ack"], ["ack"])
            clients[0].release.set()
            await runtime.wait_idle(key)

    async def test_provider_5xx_error_retries_then_succeeds(self):
        with tempfile.TemporaryDirectory() as directory:
            clients = []
            output = []
            sleeps = []

            class FlakyRpc(FakeRpc):
                def __init__(self, session_file, *, fail_count=0):
                    super().__init__(session_file)
                    self.fail_count = fail_count
                    self.attempts = 0

                async def prompt(self, message, on_event, images=None, *, on_accepted=None):
                    self.attempts += 1
                    self.session_file.write_text(f"transcript attempt {self.attempts}\n")
                    if on_accepted:
                        await on_accepted()
                    if self.attempts <= self.fail_count:
                        raise PiProviderError(500)
                    self.active = True
                    self.prompts.append(message)
                    self.prompt_images.append(images)
                    self.started.set()
                    await self.release.wait()
                    self.active = False
                    return PiRunResult("Done after retry")

            def factory(key, session_file):
                # Each retry creates a fresh client. Fail the first two clients
                # once apiece, then succeed on the third.
                client = FlakyRpc(session_file, fail_count=1 if len(clients) < 2 else 0)
                clients.append(client)
                return client

            async def send(key, kind, text, request_id):
                output.append((kind, text, request_id))

            async def fake_sleep(delay):
                sleeps.append(delay)

            root = Path(directory)
            key = SessionKey("agent_two", "matrix", "image-room")
            write_checkpoint(key.directory(root), Checkpoint(summary="Earlier summary"))
            runtime = BridgeRuntime(root, factory, send, sleep=fake_sleep)
            await runtime.submit(key, Incoming("Agent_one", "1", "Review image"))
            # Wait for the retry loop to create the client that will succeed.
            while len(clients) < 3 or not clients[-1].started.is_set():
                await asyncio.sleep(0)
            self.assertEqual(len(clients), 3)
            self.assertEqual([c.attempts for c in clients], [1, 1, 1])
            self.assertTrue(all(c.stopped for c in clients[:-1]))
            self.assertEqual(sleeps, [1.0, 2.0])
            self.assertEqual([item for item in output if item[0] == "error"], [])
            clients[-1].release.set()
            await runtime.wait_idle(key)
            self.assertEqual([item for item in output if item[0] == "final"],
                             [("final", "Done after retry", "1")])
            # The successful transcript is not archived; only failed attempts are.
            for idx in range(1, 3):
                self.assertTrue((key.directory(root) / "archive" /
                                 f"conversation-{idx:08d}.jsonl").exists())

    async def test_provider_5xx_error_retries_exhausted_reports_attempts_and_restarts_from_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            clients = []
            output = []
            sleeps = []

            class FailingRpc(FakeRpc):
                async def prompt(self, message, on_event, images=None, *, on_accepted=None):
                    self.session_file.write_text("private failed transcript\n")
                    if on_accepted:
                        await on_accepted()
                    raise PiProviderError(500)

            def factory(key, session_file):
                # The first three clients exhaust retries on the first request;
                # any later client (second request) succeeds.
                client = FailingRpc(session_file) if len(clients) < 3 else FakeRpc(session_file)
                clients.append(client)
                return client

            async def send(key, kind, text, request_id):
                output.append((kind, text, request_id))

            async def fake_sleep(delay):
                sleeps.append(delay)

            root = Path(directory)
            key = SessionKey("agent_two", "matrix", "image-room")
            write_checkpoint(key.directory(root), Checkpoint(summary="Earlier summary"))
            runtime = BridgeRuntime(root, factory, send, sleep=fake_sleep)
            await runtime.submit(key, Incoming("Agent_one", "1", "Review image"))
            await runtime.wait_idle(key)
            self.assertEqual(len(clients), 3)
            self.assertTrue(all(c.stopped for c in clients))
            self.assertEqual(sleeps, [1.0, 2.0])
            self.assertEqual([item for item in output if item[0] == "error"],
                             [("error", "Model provider returned HTTP 500 after 3 attempts. Please retry.", "1")])
            for idx in range(1, 4):
                self.assertTrue((key.directory(root) / "archive" /
                                 f"conversation-{idx:08d}.jsonl").exists())
            await runtime.submit(key, Incoming("Agent_one", "2", "Try again"))
            # Wait for the new successful client to start.
            while not clients[-1].started.is_set():
                await asyncio.sleep(0)
            self.assertIsInstance(clients[-1], FakeRpc)
            self.assertIn("Earlier summary", clients[-1].prompts[0])
            self.assertNotIn("private failed transcript", clients[-1].prompts[0])
            clients[-1].release.set()
            await runtime.wait_idle(key)

    async def test_provider_4xx_error_does_not_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            clients = []
            output = []
            sleeps = []

            class BadRequestRpc(FakeRpc):
                async def prompt(self, message, on_event, images=None, *, on_accepted=None):
                    self.session_file.write_text("private failed transcript\n")
                    if on_accepted:
                        await on_accepted()
                    raise PiProviderError(400)

            def factory(key, session_file):
                client = BadRequestRpc(session_file)
                clients.append(client)
                return client

            async def send(key, kind, text, request_id):
                output.append((kind, text, request_id))

            async def fake_sleep(delay):
                sleeps.append(delay)

            root = Path(directory)
            key = SessionKey("agent_two", "matrix", "image-room")
            runtime = BridgeRuntime(root, factory, send, sleep=fake_sleep)
            await runtime.submit(key, Incoming("Agent_one", "1", "Review image"))
            await runtime.wait_idle(key)
            self.assertEqual(len(clients), 1)
            self.assertTrue(clients[0].stopped)
            self.assertEqual(sleeps, [])
            self.assertEqual([item for item in output if item[0] == "error"],
                             [("error", "Model provider returned HTTP 400. Please retry.", "1")])
            self.assertTrue((key.directory(root) / "archive" /
                             "conversation-00000001.jsonl").exists())

    async def test_provider_5xx_after_tool_does_not_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            clients = []
            output = []
            sleeps = []

            class ToolThenFailRpc(FakeRpc):
                async def prompt(self, message, on_event, images=None, *, on_accepted=None):
                    self.session_file.write_text("private transcript with tool\n")
                    if on_accepted:
                        await on_accepted()
                    await on_event({"type": "tool_execution_start", "toolName": "shell",
                                    "args": {"secret": "must never be posted"}})
                    raise PiProviderError(500)

            def factory(key, session_file):
                client = ToolThenFailRpc(session_file)
                clients.append(client)
                return client

            async def send(key, kind, text, request_id):
                output.append((kind, text, request_id))

            async def fake_sleep(delay):
                sleeps.append(delay)

            root = Path(directory)
            key = SessionKey("agent_two", "matrix", "tool-fail-room")
            runtime = BridgeRuntime(root, factory, send, sleep=fake_sleep)
            await runtime.submit(key, Incoming("Agent_one", "1", "Run a tool"))
            await runtime.wait_idle(key)
            self.assertEqual(len(clients), 1)
            self.assertTrue(clients[0].stopped)
            self.assertEqual(sleeps, [])
            self.assertEqual([item for item in output if item[0] == "error"],
                             [("error", "Model provider returned HTTP 500. A tool may have run; inspect its result before retrying.", "1")])
            self.assertTrue((key.directory(root) / "archive" /
                             "conversation-00000001.jsonl").exists())

    async def test_provider_5xx_retry_cancels_during_backoff(self):
        with tempfile.TemporaryDirectory() as directory:
            clients = []
            output = []
            sleeps = []
            sleep_started = asyncio.Event()
            sleep_cancelled = asyncio.Event()

            class FailOnceRpc(FakeRpc):
                def __init__(self, session_file):
                    super().__init__(session_file)
                    self.attempts = 0

                async def prompt(self, message, on_event, images=None, *, on_accepted=None):
                    self.attempts += 1
                    self.session_file.write_text(f"transcript attempt {self.attempts}\n")
                    if on_accepted:
                        await on_accepted()
                    raise PiProviderError(500)

            def factory(key, session_file):
                client = FailOnceRpc(session_file)
                clients.append(client)
                return client

            async def send(key, kind, text, request_id):
                output.append((kind, text, request_id))

            async def fake_sleep(delay):
                sleeps.append(delay)
                sleep_started.set()
                await sleep_cancelled.wait()

            root = Path(directory)
            key = SessionKey("agent_two", "matrix", "cancel-backoff-room")
            runtime = BridgeRuntime(root, factory, send, sleep=fake_sleep)
            await runtime.submit(key, Incoming("Agent_one", "1", "Cancel me"))
            await asyncio.wait_for(sleep_started.wait(), timeout=1)
            # The retry client is created before the backoff sleep begins.
            self.assertEqual(len(clients), 2)
            self.assertEqual(clients[1].prompts, [])
            self.assertTrue(await runtime.cancel(key, "Agent_one"))
            sleep_cancelled.set()
            await runtime.wait_idle(key)
            self.assertEqual([item for item in output if item[0] in {"final", "cancelled", "error"}],
                             [("cancelled", "Request cancelled.", "1")])
            self.assertTrue(clients[1].aborted)
            self.assertTrue(clients[1].stopped)

    async def test_short_session_preserves_prior_summary_and_archives_transcript(self):
        with tempfile.TemporaryDirectory() as directory:
            now = [1_000_000]
            clients = []

            def factory(key, session_file):
                client = FakeRpc(session_file)

                async def too_small():
                    return None

                client.compact = too_small
                clients.append(client)
                return client

            async def send(key, kind, text, request_id):
                pass

            runtime = BridgeRuntime(Path(directory), factory, send,
                                    now_ms=lambda: now[0])
            key = SessionKey("agent_two", "matrix", "short-room")
            write_checkpoint(key.directory(Path(directory)),
                             Checkpoint(status="idle", summary="Earlier summary"))
            await runtime.submit(key, Incoming("Agent_one", "1", "First"))
            await runtime.record_wake(key, "1")
            await clients[0].started.wait()
            clients[0].release.set()
            await runtime.wait_idle(key)
            now[0] += 181_000
            self.assertEqual(await runtime.sweep_idle(), 1)
            self.assertTrue(clients[0].stopped)
            scope_dir = key.directory(Path(directory))
            self.assertEqual(read_checkpoint(scope_dir).summary, "Earlier summary")
            self.assertEqual(read_checkpoint(scope_dir).last_wake_id, "")
            self.assertTrue((scope_dir / "archive" /
                             "conversation-00000001.jsonl").exists())
            await runtime.submit(key, Incoming("Agent_one", "2", "Second"))
            await clients[1].started.wait()
            self.assertIn("Earlier summary", clients[1].prompts[0])
            self.assertNotIn("private full transcript", clients[1].prompts[0])
            self.assertNotIn("First", clients[1].prompts[0])
            clients[1].release.set()
            await runtime.wait_idle(key)

    async def test_steering_activity_and_summary_only_revival(self):
        with tempfile.TemporaryDirectory() as directory:
            now = [1_000_000]
            clients = []
            output = []
            progress_seen = asyncio.Event()

            def factory(key, session_file):
                client = FakeRpc(session_file)
                clients.append(client)
                return client

            async def send(key, kind, text, request_id):
                output.append((kind, text, request_id))
                if kind == "progress":
                    progress_seen.set()

            runtime = BridgeRuntime(Path(directory), factory, send,
                                    now_ms=lambda: now[0])
            key = SessionKey("agent_two", "discord", "room-1")
            self.assertEqual(await runtime.submit(key, Incoming("Operator", "1", "First")),
                             "started")
            await clients[0].started.wait()
            await asyncio.wait_for(progress_seen.wait(), timeout=1)
            self.assertIn(("progress", "Using `shell`…", "1"), output)
            now[0] += 40_000
            self.assertEqual(await runtime.submit(key, Incoming("Agent_one", "2", "Second")),
                             "steered")
            self.assertIn("Agent_one", clients[0].steers[0])
            self.assertIn(("steered", "Added to the current thread.", "2"), output)
            self.assertEqual(await runtime.sweep_idle(), 0)
            clients[0].release.set()
            await runtime.wait_idle(key)
            self.assertEqual([item[0] for item in output if item[0] == "final"], ["final"])
            self.assertTrue(all("secret" not in item[1] for item in output))
            now[0] += 179_000
            self.assertEqual(await runtime.sweep_idle(), 0)
            now[0] += 2_000
            self.assertEqual(await runtime.sweep_idle(), 1)
            self.assertTrue(clients[0].stopped)
            state = read_checkpoint(key.directory(Path(directory)))
            self.assertEqual(state.summary, "safe saved summary")
            self.assertTrue((key.directory(Path(directory)) / "archive" /
                             "conversation-00000001.jsonl").exists())
            self.assertEqual(stat.S_IMODE((key.directory(Path(directory)) / "state.json").stat().st_mode), 0o600)
            self.assertEqual(await runtime.submit(key, Incoming("Operator", "3", "Third")),
                             "started")
            await clients[1].started.wait()
            self.assertIn("safe saved summary", clients[1].prompts[0])
            self.assertNotIn("private full transcript", clients[1].prompts[0])
            self.assertNotIn("First", clients[1].prompts[0])
            clients[1].release.set()
            await runtime.wait_idle(key)
            clients[1].release.clear()
            await runtime.submit(key, Incoming("Operator", "4", "Fourth"))
            await asyncio.sleep(0)
            self.assertEqual(len(clients), 2)
            self.assertNotIn("safe saved summary", clients[1].prompts[1])
            clients[1].release.set()
            await runtime.wait_idle(key)

    async def test_only_requester_can_cancel_and_one_status_is_sent(self):
        with tempfile.TemporaryDirectory() as directory:
            clients = []
            output = []

            def factory(key, session_file):
                client = FakeRpc(session_file)
                clients.append(client)
                return client

            async def send(key, kind, text, request_id):
                output.append((kind, text, request_id))

            runtime = BridgeRuntime(Path(directory), factory, send)
            key = SessionKey("agent_three", "matrix", "room-2")
            await runtime.submit(key, Incoming("Operator", "a", "Please check"))
            await clients[0].started.wait()
            self.assertFalse(await runtime.cancel(key, "Someone else"))
            self.assertTrue(await runtime.cancel(key, "Operator"))
            await runtime.wait_idle(key)
            self.assertTrue(clients[0].aborted)
            self.assertEqual([kind for kind, _, _ in output if kind in {"final", "cancelled", "error"}],
                             ["cancelled"])
            state = read_checkpoint(key.directory(Path(directory)))
            self.assertEqual(state.status, "idle")

    async def test_failed_progress_edit_does_not_abort_turn(self):
        with tempfile.TemporaryDirectory() as directory:
            clients = []
            delivered = []

            def factory(key, session_file):
                client = FakeRpc(session_file)
                clients.append(client)
                return client

            async def send(key, kind, text, request_id):
                if kind == "progress":
                    raise RuntimeError("temporary transport failure")
                delivered.append(kind)

            runtime = BridgeRuntime(Path(directory), factory, send)
            key = SessionKey("agent_two", "discord", "room")
            await runtime.submit(key, Incoming("Operator", "1", "Go"))
            await clients[0].started.wait()
            clients[0].release.set()
            await runtime.wait_idle(key)
            self.assertEqual(delivered, ["ack", "final"])

    async def test_slow_progress_edit_does_not_block_pi_events_or_final(self):
        with tempfile.TemporaryDirectory() as directory:
            progress_started = asyncio.Event()
            progress_cancelled = asyncio.Event()
            events_processed = asyncio.Event()
            delivered = []

            class FastRpc(FakeRpc):
                async def prompt(self, message, on_event, images=None, *, on_accepted=None):
                    if on_accepted:
                        await on_accepted()
                    await on_event({"type": "tool_execution_start", "toolName": "shell"})
                    await progress_started.wait()
                    await on_event({"type": "message_update"})
                    events_processed.set()
                    return PiRunResult("Done")

            async def send(key, kind, text, request_id):
                if kind == "progress":
                    progress_started.set()
                    try:
                        await asyncio.Event().wait()
                    except asyncio.CancelledError:
                        progress_cancelled.set()
                        raise
                delivered.append(kind)

            runtime = BridgeRuntime(Path(directory),
                                    lambda key, session_file: FastRpc(session_file), send)
            key = SessionKey("agent_three", "matrix", "slow-progress-room")
            await runtime.submit(key, Incoming("Operator", "1", "Go"))
            await asyncio.wait_for(runtime.wait_idle(key), timeout=1)
            self.assertTrue(events_processed.is_set())
            self.assertTrue(progress_cancelled.is_set())
            self.assertEqual(delivered, ["ack", "final"])

    async def test_progress_edits_are_ordered_and_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            first_started = asyncio.Event()
            release_first = asyncio.Event()
            last_started = asyncio.Event()
            edits = []

            class BurstRpc(FakeRpc):
                async def prompt(self, message, on_event, images=None, *, on_accepted=None):
                    if on_accepted:
                        await on_accepted()
                    await on_event({"type": "tool_execution_start", "toolName": "first"})
                    await first_started.wait()
                    await on_event({"type": "tool_execution_start", "toolName": "second"})
                    await on_event({"type": "tool_execution_start", "toolName": "third"})
                    release_first.set()
                    await last_started.wait()
                    return PiRunResult("Done")

            async def send(key, kind, text, request_id):
                if kind == "progress":
                    edits.append(text)
                    if text == "Using `first`…":
                        first_started.set()
                        await release_first.wait()
                    elif text == "Using `third`…":
                        last_started.set()

            runtime = BridgeRuntime(Path(directory),
                                    lambda key, session_file: BurstRpc(session_file), send)
            key = SessionKey("agent_two", "discord", "burst-progress-room")
            await runtime.submit(key, Incoming("Operator", "1", "Go"))
            await asyncio.wait_for(runtime.wait_idle(key), timeout=5)
            self.assertEqual(edits, ["Using `first`…", "Using `third`…"])

    async def test_progress_burst_collapses_into_one_edit(self):
        with tempfile.TemporaryDirectory() as directory:
            first_started = asyncio.Event()
            release_burst = asyncio.Event()
            last_started = asyncio.Event()
            edits = []

            class BurstRpc(FakeRpc):
                async def prompt(self, message, on_event, images=None, *, on_accepted=None):
                    if on_accepted:
                        await on_accepted()
                    await on_event({"type": "tool_execution_start", "toolName": "first"})
                    await first_started.wait()
                    for name in ("second", "third", "fourth", "fifth"):
                        await on_event({"type": "tool_execution_start", "toolName": name})
                    release_burst.set()
                    await last_started.wait()
                    return PiRunResult("Done")

            async def send(key, kind, text, request_id):
                if kind == "progress":
                    edits.append(text)
                    if text == "Using `first`…":
                        first_started.set()
                        await release_burst.wait()
                    elif text == "Using `fifth`…":
                        last_started.set()

            runtime = BridgeRuntime(Path(directory),
                                    lambda key, session_file: BurstRpc(session_file), send)
            key = SessionKey("agent_three", "matrix", "burst-window-room")
            await runtime.submit(key, Incoming("Operator", "1", "Go"))
            await asyncio.wait_for(runtime.wait_idle(key), timeout=5)
            self.assertEqual(edits, ["Using `first`…", "Using `fifth`…"])

    async def test_progress_worker_balances_task_done_when_cancelled_mid_coalesce(self):
        queue = asyncio.Queue(maxsize=1)
        sent = []
        send_started = asyncio.Event()
        release_send = asyncio.Event()

        async def send(message):
            sent.append(message)
            send_started.set()
            await release_send.wait()

        worker = asyncio.create_task(_deliver_progress(queue, send))

        # First edit is delivered at once and opens the coalesce window.
        await queue.put("first")
        await send_started.wait()
        release_send.set()
        await asyncio.sleep(0)  # let the worker finish and loop back to get()

        # A second update arrives inside the window, so the worker sits in the
        # coalesce wait; cancel it there.
        await queue.put("second")
        await asyncio.sleep(0)
        worker.cancel()
        try:
            await worker
        except asyncio.CancelledError:
            pass

        self.assertEqual(sent, ["first"])
        # The top-of-loop get is balanced by a task_done in the finally even
        # though cancellation landed mid-coalesce-wait; join() must not hang.
        await asyncio.wait_for(queue.join(), timeout=1)

    async def test_stop_session_kills_only_requesters_scope_without_compacting(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clients = {}
            def factory(key, session_file):
                client = FakeRpc(session_file)
                clients.setdefault(key.scope, []).append(client)
                return client
            async def send(key, kind, text, request_id):
                pass
            runtime = BridgeRuntime(root, factory, send)
            first = SessionKey("agent_one", "matrix", "first")
            second = SessionKey("agent_one", "matrix", "second")
            self.assertFalse(await runtime.stop_session(first, "Operator"))
            await runtime.submit(first, Incoming("Operator", "1", "First"))
            await clients["first"][0].started.wait()
            await runtime.submit(second, Incoming("Other", "2", "Second"))
            await clients["second"][0].started.wait()
            self.assertFalse(await runtime.stop_session(first, "Other"))
            self.assertTrue(await runtime.stop_session(first, "Operator"))
            self.assertTrue(clients["first"][0].stopped)
            self.assertFalse(clients["second"][0].stopped)
            self.assertEqual(read_checkpoint(first.directory(root)).status, "idle")
            self.assertTrue((first.directory(root) / "archive" /
                             "conversation-00000001.jsonl").exists())
            self.assertFalse(await runtime.stop_session(first, "Operator"))
            await runtime.submit(first, Incoming("Operator", "3", "New"))
            await clients["first"][1].started.wait()
            self.assertNotIn("private full transcript", clients["first"][1].prompts[0])
            clients["first"][1].release.set()
            clients["second"][0].release.set()
            await runtime.wait_idle(first)
            await runtime.wait_idle(second)

    async def test_failed_compaction_blocks_full_history_revival(self):
        with tempfile.TemporaryDirectory() as directory:
            now = [1_000_000]
            clients = []

            def factory(key, session_file):
                client = FakeRpc(session_file)
                async def failed_compact():
                    raise TimeoutError("compaction timed out")
                client.compact = failed_compact
                clients.append(client)
                return client

            async def send(key, kind, text, request_id):
                pass

            runtime = BridgeRuntime(Path(directory), factory, send,
                                    now_ms=lambda: now[0])
            key = SessionKey("agent_three", "matrix", "room")
            await runtime.submit(key, Incoming("Operator", "1", "Go"))
            await clients[0].started.wait()
            clients[0].release.set()
            await runtime.wait_idle(key)
            now[0] += 181_000
            self.assertEqual(await runtime.sweep_idle(), 0)
            self.assertTrue(clients[0].stopped)
            self.assertEqual(read_checkpoint(key.directory(Path(directory))).status,
                             "checkpoint_error")
            with self.assertRaisesRegex(RuntimeError, "checkpoint recovery"):
                await runtime.submit(key, Incoming("Operator", "2", "Again"))
            self.assertFalse(await runtime.recover(key, "Other"))
            self.assertTrue(await runtime.recover(key, "Operator"))
            self.assertEqual(read_checkpoint(key.directory(Path(directory))).status, "idle")
            await runtime.submit(key, Incoming("Operator", "3", "New"))
            await clients[1].started.wait()
            self.assertNotIn("private full transcript", clients[1].prompts[0])
            clients[1].release.set()
            await runtime.wait_idle(key)

    async def test_forced_checkpoint_skips_active_and_closes_recent_idle_scopes(self):
        with tempfile.TemporaryDirectory() as directory:
            now = [1_000_000]
            clients = {}

            def factory(key, session_file):
                client = FakeRpc(session_file)
                clients[key.scope] = client
                return client

            async def send(key, kind, text, request_id):
                pass

            root = Path(directory)
            runtime = BridgeRuntime(root, factory, send, now_ms=lambda: now[0])
            idle = SessionKey("agent_one", "matrix", "idle")
            busy = SessionKey("agent_one", "matrix", "busy")
            await runtime.submit(idle, Incoming("Operator", "1", "First"))
            await clients["idle"].started.wait()
            clients["idle"].release.set()
            await runtime.wait_idle(idle)
            await runtime.submit(busy, Incoming("Operator", "2", "In progress"))
            await clients["busy"].started.wait()

            self.assertEqual(await runtime.sweep_idle(), 0)
            self.assertEqual(await runtime.sweep_idle(force=True), 1)
            self.assertTrue(clients["idle"].stopped)
            self.assertEqual(read_checkpoint(idle.directory(root)).summary,
                             "safe saved summary")
            self.assertFalse(clients["busy"].stopped)
            self.assertEqual(read_checkpoint(busy.directory(root)).status, "running")
            self.assertEqual(await runtime.sweep_idle(force=True), 0)
            clients["busy"].release.set()
            await runtime.wait_idle(busy)
            self.assertEqual(await runtime.sweep_idle(force=True), 1)
            self.assertTrue(clients["busy"].stopped)

    async def test_room_handoff_checkpoints_only_idle_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            clients = {}

            def factory(key, session_file):
                client = FakeRpc(session_file)
                clients[key.scope] = client
                return client

            async def send(key, kind, text, request_id):
                pass

            runtime = BridgeRuntime(Path(directory), factory, send)
            idle = SessionKey("agent_one", "matrix", "!room:example.test:main")
            thread = SessionKey("agent_one", "matrix", "!room:example.test:thread:$root")
            busy = SessionKey("agent_one", "matrix", "busy")
            await runtime.submit(idle, Incoming("Operator", "1", "First"))
            await clients[idle.scope].started.wait()
            clients[idle.scope].release.set()
            await runtime.wait_idle(idle)
            await runtime.submit(thread, Incoming("Operator", "3", "Thread"))
            await clients[thread.scope].started.wait()
            clients[thread.scope].release.set()
            await runtime.wait_idle(thread)
            await runtime.submit(busy, Incoming("Operator", "2", "Still running"))
            await clients["busy"].started.wait()
            self.assertFalse(await runtime.checkpoint_for_handoff(busy))
            self.assertTrue(await runtime.checkpoint_for_handoff(idle))
            self.assertTrue(clients[idle.scope].stopped)
            self.assertTrue(clients[thread.scope].stopped)
            self.assertFalse(clients["busy"].stopped)
            clients["busy"].release.set()
            await runtime.wait_idle(busy)

    async def test_wake_waits_for_inflight_compaction_and_uses_saved_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            now = [1_000_000]
            compact_started = asyncio.Event()
            finish_compact = asyncio.Event()
            clients = []

            class SlowCompactRpc(FakeRpc):
                async def compact(self):
                    compact_started.set()
                    await finish_compact.wait()
                    return "summary before the new wake"

            def factory(key, session_file):
                client = SlowCompactRpc(session_file)
                clients.append(client)
                return client

            async def send(key, kind, text, request_id):
                pass

            runtime = BridgeRuntime(Path(directory), factory, send,
                                    now_ms=lambda: now[0])
            key = SessionKey("agent_three", "matrix", "compaction-race-room")
            await runtime.submit(key, Incoming("Operator", "1", "First"))
            await clients[0].started.wait()
            clients[0].release.set()
            await runtime.wait_idle(key)
            now[0] += 181_000
            sweep = asyncio.create_task(runtime.sweep_idle())
            await asyncio.wait_for(compact_started.wait(), timeout=1)
            wake = asyncio.create_task(runtime.submit(key, Incoming("Operator", "2", "Second")))
            await asyncio.sleep(0)
            self.assertFalse(wake.done())
            finish_compact.set()
            self.assertEqual(await asyncio.wait_for(sweep, timeout=1), 1)
            self.assertEqual(await asyncio.wait_for(wake, timeout=1), "started")
            await clients[1].started.wait()
            self.assertIn("summary before the new wake", clients[1].prompts[0])
            clients[1].release.set()
            await runtime.wait_idle(key)

    async def test_restart_archives_interrupted_transcript(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key = SessionKey("agent_three", "matrix", "room")
            scope_dir = key.directory(root)
            write_checkpoint(scope_dir, Checkpoint(epoch=1, status="running",
                                                    requester="Operator"))
            (scope_dir / "conversation-00000001.jsonl").write_text("private transcript")
            clients = []

            def factory(key, session_file):
                client = FakeRpc(session_file)
                clients.append(client)
                return client

            async def send(key, kind, text, request_id):
                pass

            runtime = BridgeRuntime(root, factory, send)
            await runtime.submit(key, Incoming("Operator", "2", "New request"))
            await clients[0].started.wait()
            self.assertTrue((scope_dir / "archive" / "conversation-00000001.jsonl").exists())
            self.assertNotIn("private transcript", clients[0].prompts[0])
            clients[0].release.set()
            await runtime.wait_idle(key)

    async def test_damaged_checkpoint_can_be_explicitly_recovered(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key = SessionKey("agent_two", "discord", "room")
            scope_dir = key.directory(root)
            scope_dir.mkdir(parents=True)
            (scope_dir / "state.json").write_text("{broken")
            (scope_dir / "conversation-00000001.jsonl").write_text("private transcript")
            clients = []

            def factory(key, session_file):
                client = FakeRpc(session_file)
                clients.append(client)
                return client

            async def send(key, kind, text, request_id):
                pass

            runtime = BridgeRuntime(root, factory, send)
            with self.assertRaisesRegex(RuntimeError, "checkpoint recovery"):
                await runtime.submit(key, Incoming("Operator", "2", "New request"))
            self.assertTrue(list((scope_dir / "archive").glob("corrupt-state-*.json")))
            self.assertTrue((scope_dir / "archive" / "conversation-00000001.jsonl").exists())
            self.assertTrue(await runtime.recover(key, "Operator"))
            await runtime.submit(key, Incoming("Operator", "3", "Again"))
            await clients[0].started.wait()
            self.assertNotIn("private transcript", clients[0].prompts[0])
            clients[0].release.set()
            await runtime.wait_idle(key)


if __name__ == "__main__":
    unittest.main()
