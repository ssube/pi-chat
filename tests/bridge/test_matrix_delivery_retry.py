"""Offline HTTP and active-turn regressions for transient Matrix failures."""
import asyncio
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from bridge.adapters.matrix import (MATRIX_ACK_ATTEMPTS, MATRIX_ACK_RETRY_BUDGET_SECONDS, MATRIX_MAX_ATTEMPTS,
                                    MATRIX_STOP_POLL_SECONDS, MatrixAdapter, _retry_delay)
from bridge.config import MatrixSettings
from bridge.runtime.session import BridgeRuntime, Incoming, StopRequested
from bridge.runtime.state import SessionKey
from test_bridge_awareness import FakePi


class Response:
    def __init__(self, status, payload=None):
        self.status = status
        self.payload = payload or {}
        self.headers = {}
        self.closed = False

    async def json(self, content_type=None):
        return self.payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True


class MatrixDeliveryRetryTests(unittest.IsolatedAsyncioTestCase):
    def adapter(self, directory, request):
        settings = MatrixSettings("https://example.test", "@agent:example.test",
                                  "TEST_MATRIX_TOKEN", ("!room:example.test",))
        with patch.dict(os.environ, {"TEST_MATRIX_TOKEN": "test"}):
            adapter = MatrixAdapter("agent_one", settings, None, Path(directory))
        adapter.session = SimpleNamespace(request=request)
        return adapter

    def idle_runtime(self, **overrides):
        """Runtime stub for direct `output` calls: no stop is queued on the scope."""
        stubs = {"record_activity": AsyncMock(),
                 "stop_requested": lambda key, requester=None: False}
        stubs.update(overrides)
        return SimpleNamespace(**stubs)

    def test_nonfinite_retry_hints_use_finite_backoff(self):
        for hint in ["NaN", "Infinity", "-Infinity"]:
            with self.subTest(hint=hint):
                self.assertEqual(_retry_delay({"retry_after_ms": hint}, {}, 1), 0.5)
                self.assertEqual(_retry_delay(None, {"Retry-After": hint}, 1), 0.5)

    async def test_put_reuses_transaction_and_body_through_429_and_5xx(self):
        with tempfile.TemporaryDirectory() as directory:
            responses = [Response(429, {"retry_after_ms": 1250}), Response(503),
                         Response(200, {"event_id": "$delivered"})]
            pending = list(responses)
            calls, delays = [], []

            def request(method, url, **kwargs):
                calls.append((method, url, kwargs["json"]))
                return pending.pop(0)

            async def sleep(delay):
                # Backoff releases each response, so sync/other sends can use
                # the connector while this delivery waits.
                self.assertTrue(responses[len(calls) - 1].closed)
                delays.append(delay)

            adapter = self.adapter(directory, request)
            payload = {"body": "one logical notice"}
            with patch("bridge.adapters.matrix.asyncio.sleep", side_effect=sleep):
                result = await adapter._request(
                    "PUT", "/_matrix/client/v3/rooms/room/send/m.room.message/fixed-txid",
                    data=payload)
            self.assertEqual(result, {"event_id": "$delivered"})
            self.assertEqual(len(calls), 3)
            self.assertEqual(calls, [calls[0]] * 3)
            self.assertTrue(all(call[2] is payload for call in calls))
            self.assertEqual(delays, [1.25, 1.0])

    async def test_no_post_replay_and_no_auth_failure_retry(self):
        for method, status in [("POST", 429), ("POST", 503), ("PUT", 401), ("PUT", 403)]:
            with self.subTest(method=method, status=status), tempfile.TemporaryDirectory() as directory:
                calls = []

                def request(*args, **kwargs):
                    calls.append(args)
                    return Response(status)

                adapter = self.adapter(directory, request)
                with patch("bridge.adapters.matrix.asyncio.sleep", new_callable=AsyncMock) as sleep:
                    with self.assertRaisesRegex(RuntimeError, f"HTTP {status}"):
                        await adapter._request(method, "/test", data={"body": "once"})
                    sleep.assert_not_awaited()
                self.assertEqual(len(calls), 1)

    async def test_server_failure_retry_budget_and_delay_are_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            calls = []

            def request(*args, **kwargs):
                calls.append(args)
                return Response(502, {"retry_after_ms": 999999999})

            adapter = self.adapter(directory, request)
            with patch("bridge.adapters.matrix.asyncio.sleep", new_callable=AsyncMock) as sleep:
                with self.assertRaisesRegex(RuntimeError, "HTTP 502"):
                    await adapter._request("PUT", "/fixed-transaction", data={"body": "once"})
                self.assertEqual(sleep.await_args_list, [unittest.mock.call(30.0)] * 3)
            self.assertEqual(len(calls), MATRIX_MAX_ATTEMPTS)

    async def test_ack_put_outlasts_a_rate_limit_storm_beyond_the_default_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            calls, delays = [], []

            def request(method, url, **kwargs):
                calls.append((method, url, kwargs["json"]))
                # A 429 storm longer than the default four-attempt budget. The
                # acknowledgement is what aborts an addressed request, so it
                # keeps retrying while other writes would already have failed.
                if len(calls) <= MATRIX_MAX_ATTEMPTS:
                    return Response(429, {"retry_after_ms": 1000})
                return Response(200, {"event_id": "$delivered"})

            async def sleep(delay):
                delays.append(delay)

            adapter = self.adapter(directory, request)
            adapter.runtime = self.idle_runtime()
            key = SessionKey("agent_one", "matrix", "!room:example.test:main")
            adapter._requesters[(key, "$first")] = "Operator"
            with patch("bridge.adapters.matrix.asyncio.sleep", side_effect=sleep):
                await adapter.output(key, "ack", "I’m working on it.", "$first")
            self.assertEqual(len(calls), MATRIX_MAX_ATTEMPTS + 1)
            # The full server hint is honoured, in stop-aware slices rather than
            # one sleep, so an emergency stop queued mid-wait is noticed at once.
            self.assertEqual(sum(delays), 1.0 * MATRIX_MAX_ATTEMPTS)
            self.assertEqual(set(delays), {MATRIX_STOP_POLL_SECONDS})
            self.assertEqual(adapter._acks[(key, "$first")], "$delivered")
            self.assertEqual([url for _, url, _ in calls].count(calls[0][1]), len(calls))

    async def test_exhausted_notice_does_not_reject_or_replay_accepted_steer(self):
        with tempfile.TemporaryDirectory() as directory:
            calls = []
            notice_started, release_notice = asyncio.Event(), asyncio.Event()

            def request(method, url, **kwargs):
                payload = kwargs["json"]
                calls.append((url, payload))
                if payload.get("body") == "Added to the current thread.":
                    return Response(429, {"retry_after_ms": 0})
                return Response(200, {"event_id": "$delivered"})

            adapter = self.adapter(directory, request)
            pi = FakePi("finished the steered request")
            runtime = BridgeRuntime(Path(directory), lambda key, path: pi, adapter.output)
            adapter.runtime = runtime
            adapter._render_mentions = AsyncMock(side_effect=lambda room, text: (text, [], []))
            key = SessionKey("agent_one", "matrix", "!room:example.test:main")
            await runtime.submit(key, Incoming("Operator", "$first", "Start work"))
            await pi.started.wait()

            async def backoff(_delay):
                notice_started.set()
                await release_notice.wait()

            with patch("bridge.adapters.matrix.asyncio.sleep", side_effect=backoff):
                with self.assertLogs("agent_bridge.runtime", level="ERROR") as logs:
                    steered = asyncio.create_task(runtime.submit(
                        key, Incoming("Operator", "$second", "Additional work")))
                    await notice_started.wait()
                    # The steer has already reached Pi even though its notice
                    # is still rate limited. Retry the notice, never the steer.
                    self.assertEqual(len(pi.steers), 1)
                    release_notice.set()
                    self.assertEqual(await steered, "steered")
            self.assertTrue(any("Steer accepted but delivery notice failed" in line
                                and "HTTP 429" in line for line in logs.output))
            self.assertEqual(await runtime.submit(
                key, Incoming("Operator", "$second", "Additional work")), "duplicate")
            self.assertEqual(len(pi.steers), 1)
            notices = [(url, body) for url, body in calls
                       if body.get("body") == "Added to the current thread."]
            self.assertEqual(len(notices), MATRIX_MAX_ATTEMPTS)
            self.assertEqual(notices, [notices[0]] * MATRIX_MAX_ATTEMPTS)
            pi.release.set()
            await runtime.wait_idle(key)
            self.assertTrue(any(body.get("body") == "finished the steered request"
                                for _, body in calls))
            await runtime.close()

    async def test_ack_retry_window_is_bounded_by_the_retry_budget(self):
        """A long rate-limit hint must not hold the scope for the whole attempt ceiling.

        The acknowledgement is written while the runtime holds the per-scope lock,
        and an emergency stop has to wait for that lock. Eight attempts honouring
        seven 30s hints would hold it for minutes; the budget caps the whole window.
        """
        with tempfile.TemporaryDirectory() as directory:
            calls, delays = [], []
            clock, timeouts = [0.0], []

            def request(*args, **kwargs):
                calls.append(args)
                timeouts.append(kwargs["timeout"].total)
                return Response(429, {"retry_after_ms": 30_000,
                                      "errcode": "M_LIMIT_EXCEEDED"})

            async def sleep(delay):
                delays.append(delay)
                clock[0] += delay

            adapter = self.adapter(directory, request)
            adapter.runtime = self.idle_runtime()
            key = SessionKey("agent_one", "matrix", "!room:example.test:main")
            adapter._requesters[(key, "$first")] = "Operator"
            with patch("bridge.adapters.matrix._now", side_effect=lambda: clock[0]):
                with patch("bridge.adapters.matrix.asyncio.sleep", side_effect=sleep):
                    with self.assertLogs("agent_bridge.matrix", level="WARNING") as logged:
                        with self.assertRaisesRegex(RuntimeError, "HTTP 429"):
                            await adapter.output(key, "ack", "I’m working on it.", "$first")

            # The window, not the attempt ceiling, ends the retry loop, and no
            # attempt is even issued once the budget is spent: two attempts, each
            # clamped to the budget left when it started.
            self.assertEqual(delays, [MATRIX_STOP_POLL_SECONDS] * 240)
            self.assertEqual(len(calls), 2)
            self.assertEqual(timeouts, [40, 30])
            self.assertEqual(sum(delays), MATRIX_ACK_RETRY_BUDGET_SECONDS)
            self.assertLess(len(calls), MATRIX_ACK_ATTEMPTS)
            self.assertIn("retry budget of 60s spent", logged.records[-1].getMessage())
            self.assertNotIn((key, "$first"), adapter._acks)

    async def test_a_bare_429_keeps_the_default_ceiling_for_the_acknowledgement(self):
        """Only a limiter that names itself earns the deeper attempt budget.

        The front door answers 429 with an empty body while Synapse has no live
        backend. That is an outage, not a limiter, so the acknowledgement stops at
        the default ceiling instead of spending the deeper budget on a dead
        backend -- and it must not read a limiter into a code Synapse never sent.
        """
        cases = {
            "empty body": {},
            "unavailable backend": {"errcode": "M_UNKNOWN", "error": "Server is unavailable"},
        }
        for name, body in cases.items():
            with self.subTest(body=name), tempfile.TemporaryDirectory() as directory:
                calls = []

                def request(*args, **kwargs):
                    calls.append(args)
                    return Response(429, body)

                adapter = self.adapter(directory, request)
                with patch("bridge.adapters.matrix.asyncio.sleep",
                           new_callable=AsyncMock) as sleep:
                    with self.assertRaisesRegex(RuntimeError, "HTTP 429"):
                        await adapter._request("PUT", "/fixed-transaction", data={"body": "once"},
                                               attempts=MATRIX_ACK_ATTEMPTS,
                                               budget=MATRIX_ACK_RETRY_BUDGET_SECONDS)
                self.assertEqual(len(calls), MATRIX_MAX_ATTEMPTS)
                self.assertEqual(sleep.await_args_list,
                                 [unittest.mock.call(0.5), unittest.mock.call(1.0),
                                  unittest.mock.call(2.0)])

    async def test_a_limiter_429_that_names_itself_still_earns_the_deeper_budget(self):
        """Either limiter signal alone is enough: an errcode or a retry hint."""
        cases = {
            "errcode": ({"errcode": "M_LIMIT_EXCEEDED"}, {}),
            "retry hint": ({}, {"Retry-After": "1"}),
        }
        for name, (body, headers) in cases.items():
            with self.subTest(signal=name), tempfile.TemporaryDirectory() as directory:
                calls = []

                def request(*args, **kwargs):
                    calls.append(args)
                    response = Response(429, body)
                    response.headers = headers
                    return response

                adapter = self.adapter(directory, request)
                # A stopped clock keeps the wall-clock budget out of the way so the
                # attempt ceiling is what ends the loop.
                with patch("bridge.adapters.matrix._now", return_value=0.0):
                    with patch("bridge.adapters.matrix.asyncio.sleep",
                               new_callable=AsyncMock) as sleep:
                        with self.assertRaisesRegex(RuntimeError, "HTTP 429"):
                            await adapter._request("PUT", "/fixed-transaction",
                                                   data={"body": "once"},
                                                   attempts=MATRIX_ACK_ATTEMPTS,
                                                   budget=MATRIX_ACK_RETRY_BUDGET_SECONDS)
                self.assertEqual(len(calls), MATRIX_ACK_ATTEMPTS)
                self.assertEqual(sleep.await_count, MATRIX_ACK_ATTEMPTS - 1)

    async def test_ack_retry_budget_confines_extra_attempts_to_rate_limiting(self):
        """A 5xx backend keeps the default ceiling even under the deeper ack budget."""
        with tempfile.TemporaryDirectory() as directory:
            calls = []

            def request(*args, **kwargs):
                calls.append(args)
                return Response(503, {"retry_after_ms": 1000})

            adapter = self.adapter(directory, request)
            with patch("bridge.adapters.matrix.asyncio.sleep", new_callable=AsyncMock) as sleep:
                with self.assertRaisesRegex(RuntimeError, "HTTP 503"):
                    await adapter._request("PUT", "/fixed-transaction", data={"body": "once"},
                                           attempts=MATRIX_ACK_ATTEMPTS,
                                           budget=MATRIX_ACK_RETRY_BUDGET_SECONDS)
            self.assertEqual(len(calls), MATRIX_MAX_ATTEMPTS)
            self.assertEqual(sleep.await_args_list,
                             [unittest.mock.call(1.0)] * (MATRIX_MAX_ATTEMPTS - 1))

    async def test_ack_output_passes_the_bounded_budget_and_other_writes_do_not(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter = self.adapter(directory, lambda *a, **k: Response(200, {"event_id": "$ok"}))
            adapter.runtime = self.idle_runtime(stop_requested=Mock(return_value=False))
            key = SessionKey("agent_one", "matrix", "!room:example.test:main")
            adapter._request = AsyncMock(return_value={"event_id": "$ack"})
            adapter._requesters[(key, "$first")] = "Operator"

            await adapter.output(key, "ack", "I’m working on it.", "$first")
            self.assertEqual(adapter._request.await_args.kwargs["attempts"], MATRIX_ACK_ATTEMPTS)
            self.assertEqual(adapter._request.await_args.kwargs["budget"],
                             MATRIX_ACK_RETRY_BUDGET_SECONDS)
            stop = adapter._request.await_args.kwargs["stop"]
            self.assertFalse(stop())
            # Polled as this write's own requester: a stop queued for anyone else
            # can never make this acknowledgement give way.
            self.assertEqual(adapter.runtime.stop_requested.call_args,
                             unittest.mock.call(key, "Operator"))

            await adapter.output(key, "compacting", "Compacting, one moment…", "$first")
            self.assertEqual(adapter._request.await_args.kwargs["attempts"], MATRIX_MAX_ATTEMPTS)
            self.assertIsNone(adapter._request.await_args.kwargs["budget"])
            # Only the acknowledgement holds the scope lock across its retries, so
            # only it has to give way to a queued stop.
            self.assertIsNone(adapter._request.await_args.kwargs["stop"])

    async def test_emergency_stop_aborts_the_ack_retry_that_is_holding_the_scope(self):
        """A stop queued during the ack's backoff takes the lock instead of outwaiting it.

        The acknowledgement PUT retries while holding the per-scope lock the stop
        needs, so a 5s rate-limit hint used to be 5s of the stop's time. The write
        must give way within a slice, and must not report a failed turn on its way
        out -- that report would take the lock again, under the stop.
        """
        with tempfile.TemporaryDirectory() as directory:
            calls, ack_started = [], asyncio.Event()
            reports, report_started, release_report = [], asyncio.Event(), asyncio.Event()

            def request(method, url, **kwargs):
                body = (kwargs.get("json") or {}).get("body")
                calls.append(body)
                if body == "I’m working on it.":
                    ack_started.set()
                    return Response(429, {"retry_after_ms": 5000,
                                          "errcode": "M_LIMIT_EXCEEDED"})
                return Response(200, {"event_id": "$ok"})

            async def turn_status(key, request_id, status, epoch):
                reports.append((request_id, status))
                if status == "failed":
                    report_started.set()
                    await release_report.wait()

            adapter = self.adapter(directory, request)
            pi = FakePi("finished")
            runtime = BridgeRuntime(Path(directory), lambda key, path: pi, adapter.output,
                                    turn_status=turn_status)
            adapter.runtime = runtime
            adapter._render_mentions = AsyncMock(side_effect=lambda room, text: (text, [], []))
            key = SessionKey("agent_one", "matrix", "!room:example.test:main")

            pending = asyncio.create_task(runtime.submit(
                key, Incoming("Operator", "$first", "Start work")))
            await asyncio.wait_for(ack_started.wait(), 2)
            started = time.monotonic()
            # Generous timeout on purpose: a regression fails on the elapsed check
            # below with the hint's length visible, not as a bare timeout.
            self.assertTrue(await asyncio.wait_for(runtime.stop_session(key, "Operator"), 6))
            self.assertLess(time.monotonic() - started, 1.0)
            # The aborted request's indicator is still resolved, and the report is
            # in flight while the stop already owns the scope: it is what replaced
            # the failure report that used to be written under the lock.
            self.assertEqual(reports, [("$first", "running"), ("$first", "failed")])
            await asyncio.wait_for(report_started.wait(), 1)
            release_report.set()
            with self.assertRaises(StopRequested):
                await pending
            self.assertEqual(calls, ["I’m working on it."])
            self.assertNotIn((key, "$first"), adapter._acks)
            # The flag is scoped to the stop that set it, not sticky.
            self.assertFalse(runtime.stop_requested(key))
            pi.release.set()
            await runtime.wait_idle(key)
            await runtime.close()

    async def test_a_refused_stop_or_cancel_never_ends_the_requests_own_write(self):
        """A denied third-party stop must not abort the answer it was refused.

        The cooperative-stop flag used to be raised before the requester was
        authorized, so `stop_session(..., "Mallory")` was correctly denied by the
        runtime *and* made Operator's acknowledgement give way one PUT into its
        retries: someone with no authority over the request ended it.
        """
        with tempfile.TemporaryDirectory() as directory:
            ack = "I’m working on it."
            calls, ack_started = [], asyncio.Event()

            def request(method, url, **kwargs):
                body = (kwargs.get("json") or {}).get("body")
                if body == ack:
                    calls.append(body)
                    if len(calls) == 1:
                        ack_started.set()
                        return Response(429, {"retry_after_ms": 750,
                                              "errcode": "M_LIMIT_EXCEEDED"})
                return Response(200, {"event_id": "$ok"})

            adapter = self.adapter(directory, request)
            pi = FakePi("finished")
            runtime = BridgeRuntime(Path(directory), lambda key, path: pi, adapter.output)
            adapter.runtime = runtime
            adapter._render_mentions = AsyncMock(side_effect=lambda room, text: (text, [], []))
            key = SessionKey("agent_one", "matrix", "!room:example.test:main")

            pending = asyncio.create_task(runtime.submit(
                key, Incoming("Operator", "$first", "Start work")))
            await asyncio.wait_for(ack_started.wait(), 2)
            # Both controls are exercised while the acknowledgement retries a
            # limiter 429 holding the scope lock, which is when the flag mattered.
            self.assertFalse(await asyncio.wait_for(runtime.stop_session(key, "Mallory"), 3))
            self.assertFalse(runtime.stop_requested(key))
            self.assertFalse(await asyncio.wait_for(runtime.cancel(key, "Mallory"), 3))
            self.assertFalse(runtime.stop_requested(key))
            # Operator's request was never stopped by anybody: it retried and started.
            self.assertEqual(await asyncio.wait_for(pending, 3), "started")
            self.assertEqual(calls, [ack, ack])
            self.assertIn((key, "$first"), adapter._acks)
            pi.release.set()
            await runtime.wait_idle(key)
            await runtime.close()

    async def test_the_cooperative_stop_signal_is_scoped_to_the_requester_it_authorized(self):
        """A queued stop reaches only the request it was authorized to interrupt.

        The acknowledgement polls this signal to decide whether to give way, so a
        stop queued for one requester must not abandon a write belonging to
        another.
        """
        with tempfile.TemporaryDirectory() as directory:
            async def send(key, kind, text, request_id):
                return None

            runtime = BridgeRuntime(Path(directory), lambda key, path: FakePi("finished"), send)
            key = SessionKey("agent_one", "matrix", "!room:example.test:main")
            scope = await runtime._scope(key)
            scope.stop_requests["Operator"] = 1
            self.assertTrue(runtime.stop_requested(key, "Operator"))
            self.assertFalse(runtime.stop_requested(key, "Mallory"))
            # Omitting the requester still asks about the whole scope.
            self.assertTrue(runtime.stop_requested(key))
            scope.stop_requests.clear()
            self.assertFalse(runtime.stop_requested(key, "Operator"))
            await runtime.close()

    async def test_a_stop_queued_between_attempts_is_never_sent_another_one(self):
        """The retry loop re-checks the queued stop before it issues an attempt."""
        with tempfile.TemporaryDirectory() as directory:
            calls, stopping = [], {"now": False}

            def request(method, url, **kwargs):
                calls.append(kwargs["json"]["body"])
                return Response(429, {"retry_after_ms": 250, "errcode": "M_LIMIT_EXCEEDED"})

            async def sleep(delay):
                # The stop lands while the acknowledgement waits out its hint.
                stopping["now"] = True

            adapter = self.adapter(directory, request)
            adapter.runtime = self.idle_runtime(
                stop_requested=lambda key, requester=None: stopping["now"])
            key = SessionKey("agent_one", "matrix", "!room:example.test:main")
            adapter._requesters[(key, "$first")] = "Operator"
            with patch("bridge.adapters.matrix.asyncio.sleep", side_effect=sleep):
                with self.assertLogs("agent_bridge.matrix", level="WARNING") as logged:
                    with self.assertRaisesRegex(StopRequested, "abandoned for a queued scope stop"):
                        await adapter.output(key, "ack", "I’m working on it.", "$first")

            # One attempt was sent, then the queued stop ended the loop: the
            # deeper attempt ceiling is not spent on a request nobody wants.
            self.assertEqual(calls, ["I’m working on it."])
            self.assertTrue(any("because a cancel or stop is queued" in record.getMessage()
                                for record in logged.records))

    async def test_a_stopped_request_is_not_answered_with_a_recovery_hint(self):
        """The room is not told to `recover` from a request the stop itself ended."""
        with tempfile.TemporaryDirectory() as directory:
            class Runtime:
                submitted: list[str] = []

                async def is_active(self, key):
                    return False

                async def is_awake(self, key):
                    return True

                async def admit(self, key, can_wake, *, on_wait=None):
                    return can_wake

                async def last_wake(self, key):
                    return "", None

                async def record_wake(self, key, event_id):
                    pass

                async def record_activity(self, key):
                    pass

                async def submit(self, key, incoming):
                    Runtime.submitted.append(incoming.message_id)
                    raise StopRequested("Matrix PUT abandoned for a queued scope stop")

            settings = MatrixSettings("https://example.test", "@agent:example.test",
                                      "TEST_MATRIX_TOKEN", ("!room:example.test",))
            with patch.dict(os.environ, {"TEST_MATRIX_TOKEN": "test"}):
                adapter = MatrixAdapter("agent_one", settings, Runtime(), Path(directory))
            posts = []

            async def request(method, path, **kwargs):
                if method == "GET":
                    return {"chunk": []}
                posts.append(kwargs.get("data"))
                return {"event_id": "$reply"}

            adapter._request = request
            adapter._history = AsyncMock(return_value="")
            await adapter._handle_event("!room:example.test", {
                "type": "m.room.message", "event_id": "$new", "sender": "@operator:example.test",
                "content": {"msgtype": "m.text", "body": "@agent:example.test hello"}})
            # The attempt really did reach submission and abort there, and the
            # room was left with neither an error card nor a stale requester: the
            # stop is the whole outcome, and a later reply needs no name to answer.
            self.assertEqual(Runtime.submitted, ["$new"])
            self.assertEqual(posts, [])
            self.assertEqual(adapter._requesters, {})
