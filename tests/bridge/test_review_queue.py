import asyncio
import hashlib
import json
import os
from functools import partial
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bridge.adapters.matrix import MatrixAdapter
from bridge.config import MatrixSettings, ReviewSettings
from bridge.review_queue import (ForgejoReviews, ReviewCliFailure, ReviewQueue,
                                 _bounded_tail, _redact_output, review_link as raw_review_link)

from bridge.runtime.session import BridgeRuntime, Incoming
from bridge.runtime.state import SessionKey


review_link = partial(raw_review_link, base_url="https://git.example.invalid",
                      repositories=("team/project", "team/other"))


class FakeForgejo(ForgejoReviews):
    def __init__(self):
        super().__init__("test-token", "agent_two", "https://git.example.invalid", ("team/project", "team/other"))
        self.pr = {
            "state": "open", "base": {"ref": "main", "repo": {"full_name": "team/project"}},
            "head": {"sha": "a" * 40, "repo": {"full_name": "team/project"}},
            "user": {"login": "agent_three"}, "requested_reviewers": [{"login": "agent_two"}],
        }
        self.timeline = [{"type": "review_request", "id": 10,
                          "assignee": {"login": "agent_two"},
                          "created_at": "2026-10-02T10:00:00Z"}]
        self.reviews = []

    def _get(self, repo, path):
        assert repo == "team/project"
        if path.startswith("/pulls/") and "/reviews" not in path:
            return self.pr
        if "/timeline" in path:
            page = int(path.split("page=")[1].split("&")[0])
            return self.timeline[(page - 1) * 50:page * 50]
        if "/reviews" in path:
            page = int(path.split("page=")[1].split("&")[0])
            return self.reviews[(page - 1) * 50:page * 50]
        raise AssertionError(path)


class FakeRuntime:
    def __init__(self):
        self.active = False
        self.running = False
        self.overlap = False

    @asynccontextmanager
    async def external_job(self):
        if self.active:
            yield False
            return
        self.running = True
        try:
            yield True
        finally:
            self.running = False


class ReviewQueueTests(unittest.IsolatedAsyncioTestCase):
    def test_link_rejects_unapproved_or_ambiguous_pr(self):
        self.assertEqual(review_link("[PR](https://git.example.invalid/team/project/pulls/38)"),
                         ("team/project", 38))
        self.assertIsNone(review_link("https://evil.test/team/project/pulls/38"))
        self.assertIsNone(review_link("PR #38"))
        self.assertIsNone(review_link("https://git.example.invalid/team/project/pulls/38 "
                                      "https://git.example.invalid/team/other/pulls/38"))

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.forgejo = FakeForgejo()
        self.runtime = FakeRuntime()
        self.matrix = MatrixSettings(
            "https://matrix.example.test", "@agent_two-agent:example.test",
            "AGENT_TWO_AGENT_MATRIX_TOKEN", ("!reviews:example.test",),
            pr_review_rooms=("!reviews:example.test",),
            hookshot_user_id="@hookshot:matrix.example.invalid")
        self.settings = ReviewSettings("https://git.example.invalid", "agent_two", "REVIEW_TOKEN",
                                       ("team/project", "team/other"), ("/bin/true",), self.path)
        self.queue = ReviewQueue("agent_two", self.path, self.settings, self.runtime,
                                 self.forgejo, self.matrix)

    async def asyncTearDown(self):
        self.queue.close()
        self.temp.cleanup()


    async def test_successful_cli_with_inherited_pipe_does_not_retry(self):
        stdout = asyncio.StreamReader()
        stderr = asyncio.StreamReader()
        stdout.feed_eof()
        # A descendant can inherit stderr after the CLI has exited successfully.
        process = SimpleNamespace(wait=AsyncMock(return_value=0), stdout=stdout, stderr=stderr)
        with (patch.dict(os.environ, {"UNUSED_REVIEW_USER": "agent_two",
                                   "AGENT_TWO_AGENT_MATRIX_TOKEN": "test-token"}),
              patch("bridge.review_queue.OUTPUT_DRAIN_TIMEOUT", 0.01),
              patch("bridge.review_queue.asyncio.create_subprocess_exec",
                    new_callable=AsyncMock, return_value=process)):
            self.assertTrue(await self.queue._run_cli("team/other", 57))

    def test_quoted_cli_flags_redact_entire_value(self):
        output = (b'--token "first secret with spaces" '
                  b"--api-key='second secret with spaces' "
                  b'--user "third secret with spaces"')
        redacted = _redact_output(output, False)
        self.assertNotIn("secret with spaces", redacted)
        self.assertEqual(redacted.count("[REDACTED]"), 3)

    async def test_truncated_no_newline_has_explicit_marker(self):
        stream = asyncio.StreamReader()
        stream.feed_data(b"x" * 20000)
        stream.feed_eof()
        data, truncated = await _bounded_tail(stream)
        self.assertTrue(truncated)
        self.assertEqual(data, b"")
        self.assertIn("no complete line retained", _redact_output(data, truncated))

    async def test_failed_cli_retains_bounded_redacted_details_for_retry(self):
        await self.queue.enqueue("$failed", "https://git.example.invalid/team/project/pulls/38")
        stdout = asyncio.StreamReader()
        stderr = asyncio.StreamReader()
        stdout.feed_data(b"a" * 20000 + b"\nordinary progress\n")
        stdout.feed_eof()
        stderr.feed_data(b"Authorization: Bearer sensitive-value\n"
                         b"MATRIX_TOKEN=secret-value\n"
                         b"review failed: stale account\n")
        stderr.feed_eof()
        process = SimpleNamespace(wait=AsyncMock(return_value=7), stdout=stdout,
                                  stderr=stderr, returncode=7)
        with (patch.dict(os.environ, {
                  "UNUSED_REVIEW_USER": "agent_two", "AGENT_TWO_AGENT_MATRIX_TOKEN": "secret-value",
              }),
              patch("bridge.review_queue.asyncio.create_subprocess_exec",
                    new_callable=AsyncMock, return_value=process)):
            worker = asyncio.create_task(self.queue.run())
            try:
                for _ in range(50):
                    row = self.queue.db.execute("SELECT attempts, failure_ref, failure_sha256 "
                                                "FROM jobs").fetchone()
                    if row and row[0] == 1 and row[1]:
                        break
                    await asyncio.sleep(0.02)
                self.assertEqual(row[0], 1)
                detail_path = Path(row[1])
                data = detail_path.read_bytes()
                self.assertEqual(row[2], hashlib.sha256(data).hexdigest())
                self.assertEqual(detail_path.stat().st_mode & 0o777, 0o600)
                detail = json.loads(data)
                self.assertEqual((detail["job_id"], detail["repo"], detail["pr"],
                                  detail["head"], detail["request_id"], detail["attempt"],
                                  detail["exit_code"]),
                                 (1, "team/project", 38, "a" * 40, 10, 1, 7))
                self.assertIn("ordinary progress", detail["stdout"])
                self.assertIn("stale account", detail["stderr"])
                self.assertNotIn("sensitive-value", data.decode())
                self.assertNotIn("secret-value", data.decode())
                self.assertLess(len(data), 20000)
            finally:
                worker.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await worker

    async def test_bad_shared_redaction_config_discards_streams(self):
        await self.queue.enqueue("$redaction", "https://git.example.invalid/team/project/pulls/38")
        redaction_file = self.path / "redaction.json"
        redaction_file.write_text('{"secrets":"invalid"}')
        with patch.dict(os.environ, {"PI_CAPTURE_REDACTION_FILE": str(redaction_file)}):
            self.queue._record_failure(1, "team/project", 38, "a" * 40, 10, 1,
                                       ReviewCliFailure("nonzero_exit", 1,
                                                        stderr=b"raw private output"))
        detail_ref = self.queue.db.execute("SELECT failure_ref FROM jobs").fetchone()[0]
        detail = Path(detail_ref).read_text()
        self.assertIn('"reason":"redaction_unavailable"', detail)
        self.assertNotIn("raw private output", detail)

    async def test_retention_prunes_old_attempts_but_keeps_current_pointer(self):
        await self.queue.enqueue("$retention", "https://git.example.invalid/team/project/pulls/38")
        with patch("bridge.review_queue.FAILURE_RETENTION_COUNT", 1):
            refs = []
            for attempt in range(1, 4):
                self.queue._record_failure(1, "team/project", 38, "a" * 40, 10,
                                           attempt, ReviewCliFailure("nonzero_exit", 1))
                refs.append(self.queue.db.execute(
                    "SELECT failure_ref FROM jobs WHERE id=1").fetchone()[0])
        self.assertFalse(Path(refs[0]).exists())
        self.assertTrue(Path(refs[1]).exists())
        self.assertTrue(Path(refs[2]).exists())
        self.assertEqual(self.queue.db.execute(
            "SELECT COUNT(*) FROM failure_artifacts").fetchone()[0], 2)
        self.queue.db.execute("UPDATE failure_artifacts SET created_at=0 WHERE path=?",
                              (refs[1],))
        self.queue.db.commit()
        self.queue._prune_failures()
        self.assertFalse(Path(refs[1]).exists())
        self.assertTrue(Path(refs[2]).exists())

    async def test_retention_cleanup_is_batched(self):
        await self.queue.enqueue("$batch", "https://git.example.invalid/team/project/pulls/38")
        for attempt in range(1, 5):
            self.queue._record_failure(1, "team/project", 38, "a" * 40, 10,
                                       attempt, ReviewCliFailure("nonzero_exit", 1))
        self.queue.db.execute("UPDATE failure_artifacts SET created_at=0")
        self.queue.db.commit()
        with patch("bridge.review_queue.FAILURE_PRUNE_BATCH", 1):
            self.queue._prune_failures()
        self.assertEqual(self.queue.db.execute(
            "SELECT COUNT(*) FROM failure_artifacts").fetchone()[0], 3)
        current_ref = self.queue.db.execute(
            "SELECT failure_ref FROM jobs WHERE id=1").fetchone()[0]
        self.assertTrue(Path(current_ref).exists())

    async def test_retention_size_budget_does_not_delete_referenced_file(self):
        await self.queue.enqueue("$size", "https://git.example.invalid/team/project/pulls/38")
        self.queue._record_failure(1, "team/project", 38, "a" * 40, 10,
                                   1, ReviewCliFailure("nonzero_exit", 1))
        previous = self.queue.db.execute("SELECT failure_ref FROM jobs WHERE id=1").fetchone()[0]
        with patch("bridge.review_queue.FAILURE_RETENTION_BYTES", 0):
            self.queue._record_failure(1, "team/project", 38, "a" * 40, 10,
                                       2, ReviewCliFailure("nonzero_exit", 1))
        current = self.queue.db.execute("SELECT failure_ref FROM jobs WHERE id=1").fetchone()[0]
        self.assertFalse(Path(previous).exists())
        self.assertTrue(Path(current).exists())

    def test_live_generation_and_exact_head_review(self):
        self.assertEqual(self.forgejo.status("team/project", 38),
                         ("requested", "a" * 40, 10, False))
        self.forgejo.reviews = [{"user": {"login": "agent_two"}, "state": "APPROVED",
                                 "commit_id": "a" * 40, "submitted_at": "2026-10-02T10:01:00Z"}]
        self.assertTrue(self.forgejo.status("team/project", 38)[3])
        self.forgejo.pr["requested_reviewers"] = []
        self.assertEqual(self.forgejo.status("team/project", 38),
                         ("unrequested", "a" * 40, 10, True))
        self.forgejo.pr["requested_reviewers"] = [{"login": "agent_two"}]
        self.forgejo.timeline.append({"type": "review_request", "id": 11,
                                      "assignee": {"login": "agent_two"},
                                      "created_at": "2026-10-02T10:02:00Z"})
        self.assertEqual(self.forgejo.status("team/project", 38)[2:], (11, False))
        self.forgejo.pr["head"]["sha"] = "b" * 40
        self.assertFalse(self.forgejo.status("team/project", 38)[3])
        self.forgejo.pr["requested_reviewers"] = []
        self.assertEqual(self.forgejo.status("team/project", 38)[0], "unrequested")

    def test_only_current_approval_qualifies_for_checkmark(self):
        review = {"user": {"login": "agent_two"}, "state": "COMMENT", "commit_id": "a" * 40,
                  "submitted_at": "2026-10-02T10:01:00Z"}
        self.forgejo.reviews = [review]
        self.assertFalse(self.forgejo.approved("team/project", 38, "a" * 40, 10))
        review["state"] = "APPROVED"
        self.assertTrue(self.forgejo.approved("team/project", 38, "a" * 40, 10))
        review["stale"] = True
        self.assertFalse(self.forgejo.approved("team/project", 38, "a" * 40, 10))
        review["stale"] = False
        self.forgejo.timeline[0]["created_at"] = "2026-10-02T10:02:00Z"
        self.assertFalse(self.forgejo.approved("team/project", 38, "a" * 40, 10))

    def test_latest_verdict_and_paginated_request_control_checkmark(self):
        self.forgejo.timeline = [{"id": n, "type": "comment", "created_at": "2026-10-02T09:00:00Z"}
                                 for n in range(60)] + self.forgejo.timeline
        self.forgejo.reviews = [
            {"id": 1, "user": {"login": "agent_two"}, "state": "APPROVED",
             "commit_id": "a" * 40, "submitted_at": "2026-10-02T10:01:00Z"},
            {"id": 2, "user": {"login": "agent_two"}, "state": "REQUEST_CHANGES",
             "commit_id": "a" * 40, "submitted_at": "2026-10-02T10:05:00Z"}]
        self.assertFalse(self.forgejo.approved("team/project", 38, "a" * 40, 10))
        self.forgejo.reviews[1]["state"] = "APPROVED"
        self.assertTrue(self.forgejo.approved("team/project", 38, "a" * 40, 10))

    async def test_confirmation_read_failure_keeps_completed_job_done(self):
        room = "!reviews:example.test"
        await self.queue.enqueue("$read-failure", "https://git.example.invalid/team/project/pulls/38", room)
        self.queue.react = AsyncMock()
        async def review(repo, number, room_id):
            self.assertEqual(room_id, room)
            self.forgejo.reviews = [{"user": {"login": "agent_two"}, "state": "APPROVED",
                                     "commit_id": "a" * 40,
                                     "submitted_at": "2026-10-02T10:01:00Z"}]
            return True
        self.queue._run_cli = review
        self.forgejo.approved = lambda *args: (_ for _ in ()).throw(OSError("unavailable"))
        worker = asyncio.create_task(self.queue.run())
        try:
            for _ in range(100):
                if self.queue.db.execute("SELECT status FROM jobs").fetchone()[0] == "done":
                    break
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.02)
            self.assertEqual(self.queue.db.execute("SELECT status, attempts FROM jobs").fetchone(),
                             ("done", 0))
            self.assertEqual(self.queue.react.await_count, 1)  # 👀, not ✅
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    async def test_review_queue_reacts_only_after_verified_approval(self):
        room = "!reviews:example.test"
        link = "https://git.example.invalid/team/project/pulls/38"
        await self.queue.enqueue("$review", link, room)
        self.queue.react = AsyncMock()
        async def review(repo, number, room_id):
            self.assertEqual(room_id, room)
            self.forgejo.reviews = [{"user": {"login": "agent_two"}, "state": "APPROVED",
                                     "commit_id": "a" * 40,
                                     "submitted_at": "2026-10-02T10:01:00Z"}]
            return True
        self.queue._run_cli = review
        worker = asyncio.create_task(self.queue.run())
        try:
            for _ in range(100):
                if self.queue.db.execute("SELECT status FROM jobs").fetchone()[0] == "done":
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(self.queue.db.execute("SELECT status FROM jobs").fetchone()[0], "done")
            for _ in range(100):
                if self.queue.react.await_count == 2:
                    break
                await asyncio.sleep(0.01)
            self.queue.react.assert_any_await(room, "$review", "👀")
            self.queue.react.assert_any_await(room, "$review", "✅")
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    async def test_comment_review_never_gets_approval_reaction(self):
        room = "!reviews:example.test"
        await self.queue.enqueue("$comment", "https://git.example.invalid/team/project/pulls/38", room)
        self.queue.react = AsyncMock()
        async def review(repo, number, room_id):
            self.assertEqual(room_id, room)
            self.forgejo.reviews = [{"user": {"login": "agent_two"}, "state": "COMMENT",
                                     "commit_id": "a" * 40,
                                     "submitted_at": "2026-10-02T10:01:00Z"}]
            return True
        self.queue._run_cli = review
        worker = asyncio.create_task(self.queue.run())
        try:
            for _ in range(100):
                if self.queue.db.execute("SELECT status FROM jobs").fetchone()[0] == "done":
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(self.queue.react.await_args_list[0].args,
                             (room, "$comment", "👀"))
            self.assertEqual(self.queue.react.await_count, 1)
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    async def test_targeted_hookshot_is_durable_and_never_reaches_pi(self):
        room = "!reviews:example.test"
        settings = MatrixSettings("https://example.test", "@agent_two-agent:example.test",
                                  "TEST_MATRIX_TOKEN", (room,), pr_review_rooms=(room,),
                                  hookshot_user_id="@hookshot:matrix.example.invalid")
        runtime = AsyncMock()
        with patch.dict(os.environ, {"TEST_MATRIX_TOKEN": "test"}):
            adapter = MatrixAdapter("agent_two", settings, runtime, self.path / "matrix", self.queue)
        event = {"type": "m.room.message", "event_id": "$hookshot-1",
                 "sender": "@hookshot:matrix.example.invalid",
                 "content": {"msgtype": "m.text", "m.mentions": {
                     "user_ids": ["@agent_two-agent:example.test"]},
                     "body": "Review https://git.example.invalid/team/project/pulls/38"}}
        await adapter._handle_event(room, event)
        self.assertEqual(self.queue.db.execute("SELECT status, room_id FROM jobs").fetchall(),
                         [("pending", room)])
        runtime.submit.assert_not_called()
        adapter.review_queue = type("BrokenQueue", (), {
            "enqueue": AsyncMock(side_effect=OSError("disk full"))})()
        failed = {**event, "event_id": "$hookshot-2"}
        with self.assertRaises(OSError):
            await adapter._handle_event(room, failed)
        self.assertNotIn("$hookshot-2", adapter._seen)
        adapter.review_queue = self.queue
        await adapter._handle_event(room, event)
        self.assertEqual(self.queue.db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 1)
        self.queue.close()
        self.settings = ReviewSettings("https://git.example.invalid", "agent_two", "REVIEW_TOKEN",
                                       ("team/project", "team/other"), ("/bin/true",), self.path)
        self.queue = ReviewQueue("agent_two", self.path, self.settings, self.runtime,
                                 self.forgejo, self.matrix)
        self.assertEqual(self.queue.db.execute("SELECT status FROM jobs").fetchall(),
                         [("pending",)])

    async def test_burst_waits_for_idle_and_serializes_jobs(self):
        link = "https://git.example.invalid/team/project/pulls/38"
        await self.queue.enqueue("$one", link, "!reviews:example.test")
        await self.queue.enqueue("$two", link, "!reviews:example.test")
        self.runtime.active = True
        calls = []

        async def review(repo, number, room_id):
            self.assertEqual(room_id, "!reviews:example.test")
            self.assertTrue(self.runtime.running)
            calls.append((repo, number))
            self.forgejo.reviews = [{"user": {"login": "agent_two"}, "state": "APPROVED",
                                     "commit_id": "a" * 40,
                                     "submitted_at": "2026-10-02T10:01:00Z"}]
            self.forgejo.pr["requested_reviewers"] = []
            return True

        self.queue._run_cli = review
        worker = asyncio.create_task(self.queue.run())
        try:
            await asyncio.sleep(0.1)
            self.assertEqual(calls, [])
            self.runtime.active = False
            self.queue.db.execute("UPDATE jobs SET not_before=0")
            self.queue.db.commit()
            self.queue.wake.set()
            for _ in range(50):
                if calls:
                    break
                await asyncio.sleep(0.02)
            self.assertEqual(calls, [("team/project", 38)])
            for _ in range(50):
                states = [row[0] for row in self.queue.db.execute("SELECT status FROM jobs")]
                if all(state in {"done", "duplicate"} for state in states):
                    break
                await asyncio.sleep(0.02)
            self.assertEqual(states, ["done", "duplicate"])
        finally:
            worker.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await worker

    async def test_external_job_blocks_new_turns_across_transports(self):
        runtime = BridgeRuntime(self.path / "sessions", lambda *_: None,
                                AsyncMock())
        runtime._submit_unlocked = AsyncMock(return_value="started")
        matrix = SessionKey("agent_two", "matrix", "room")
        discord = SessionKey("agent_two", "discord", "channel")
        runtime._scopes[discord] = SimpleNamespace(
            active=True, checkpoint=SimpleNamespace(status="running"))
        async with runtime.external_job() as reserved:
            self.assertFalse(reserved)
        runtime._scopes.clear()
        async with runtime.external_job() as reserved:
            self.assertTrue(reserved)
            turn = asyncio.create_task(runtime.submit(matrix, Incoming("Operator", "1", "hello")))
            await asyncio.sleep(0)
            self.assertFalse(turn.done())
            runtime._sweep_scopes = AsyncMock(return_value=1)
            self.assertEqual(await runtime.sweep_idle(force=True), 0)
            runtime._sweep_scopes.assert_not_called()
        self.assertEqual(await asyncio.wait_for(turn, timeout=1), "started")
        self.assertEqual(await runtime.sweep_idle(force=True), 1)
