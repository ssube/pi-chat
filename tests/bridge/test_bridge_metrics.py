import asyncio
import unittest
from unittest.mock import AsyncMock

from bridge.metrics import MetricsService, render_metrics


class MetricsTests(unittest.IsolatedAsyncioTestCase):
    def test_agent_metric_label_is_escaped(self):
        body = render_metrics('agent"\\name\n', {}, 1.0)
        self.assertIn('agent="agent\\"\\\\name\\n"', body)

    def test_render_marks_awake_when_a_turn_is_running(self):
        body = render_metrics("agent_three", {"total_scopes": 3, "active": 1, "awake": 1,
                                         "active_non_idle": 1}, 1000.0)
        self.assertIn('agent_bridge_presence{agent="agent_three",state="awake"} 1', body)
        self.assertIn('agent_bridge_presence{agent="agent_three",state="connected"} 0', body)
        self.assertIn('agent_bridge_presence{agent="agent_three",state="disconnected"} 0', body)
        self.assertIn('agent_bridge_scopes{agent="agent_three",state="total"} 3', body)
        self.assertIn('agent_bridge_scopes{agent="agent_three",state="active"} 1', body)
        self.assertIn('agent_bridge_scopes{agent="agent_three",state="critical"} 1', body)

    def test_render_marks_connected_when_every_scope_is_asleep(self):
        body = render_metrics("agent_one", {"total_scopes": 2, "active": 0, "awake": 0,
                                         "active_non_idle": 0}, 2000.0)
        self.assertIn('agent_bridge_presence{agent="agent_one",state="awake"} 0', body)
        self.assertIn('agent_bridge_presence{agent="agent_one",state="connected"} 1', body)
        self.assertIn('agent_bridge_scopes{agent="agent_one",state="awake"} 0', body)

    def test_render_marks_disconnected_and_zeroes_scopes(self):
        body = render_metrics("agent_two", {}, 3000.0, disconnected=True)
        self.assertIn('agent_bridge_presence{agent="agent_two",state="disconnected"} 1', body)
        self.assertIn('agent_bridge_presence{agent="agent_two",state="connected"} 0', body)
        self.assertIn('agent_bridge_scopes{agent="agent_two",state="total"} 0', body)
        self.assertIn('agent_bridge_last_push_timestamp_seconds{agent="agent_two"} 3000.0', body)

    async def test_refresh_pushes_a_rendered_snapshot(self):
        runtime = AsyncMock()
        runtime.session_summary = AsyncMock(return_value={"total_scopes": 1,
                                                          "active": 0, "awake": 0,
                                                          "active_non_idle": 0})
        service = MetricsService("agent_three", runtime, "https://pushgateway.example",
                                 now=lambda: 1234.0)
        service._push = AsyncMock()
        await service.refresh()
        body = service._push.await_args.args[0]
        self.assertIn('agent_bridge_presence{agent="agent_three",state="connected"} 1', body)
        self.assertIn('agent_bridge_last_push_timestamp_seconds{agent="agent_three"} 1234.0', body)

    async def test_shutdown_pushes_disconnected_then_closes_session(self):
        runtime = AsyncMock()
        service = MetricsService("agent_three", runtime, "https://pushgateway.example",
                                 now=lambda: 9.0)
        service._push = AsyncMock()
        service._session = None
        await service.shutdown()
        self.assertIn('state="disconnected"} 1', service._push.await_args.args[0])

    async def test_shutdown_cancels_inflight_refresh_before_disconnected(self):
        runtime = AsyncMock()
        runtime.session_summary = AsyncMock(return_value={"total_scopes": 1,
                                                          "active": 1, "awake": 1,
                                                          "active_non_idle": 1})
        service = MetricsService("agent_three", runtime, "https://pushgateway.example",
                                 now=lambda: 1234.0)
        release = asyncio.Event()
        pushed: list[str] = []

        async def fake_push(body):
            pushed.append(body)
            if 'state="disconnected"} 1' not in body:
                await release.wait()  # block the regular refresh PUT
            return None

        service._push = fake_push
        poll = asyncio.create_task(service.run())
        for _ in range(1000):
            if pushed:
                break
            await asyncio.sleep(0)
        self.assertEqual(len(pushed), 1)  # a connected/awake PUT is in flight
        await service.shutdown()
        self.assertTrue(poll.done())
        self.assertIn('state="disconnected"} 1', pushed[-1])

    async def test_push_times_out_on_stalled_pushgateway(self):
        runtime = AsyncMock()
        service = MetricsService("agent_three", runtime, "https://pushgateway.example",
                                 push_timeout=0.05)

        class StalledResponse:
            status = 200

            async def __aenter__(self):
                await asyncio.sleep(3600)  # the request stalls here
                return self

            async def __aexit__(self, *args):
                return False

        class StalledSession:
            closed = False

            def put(self, url, data):
                return StalledResponse()

            async def close(self):
                self.closed = True

        service._session = StalledSession()
        with self.assertRaises(TimeoutError):
            await service._push("body")


if __name__ == "__main__":
    unittest.main()
