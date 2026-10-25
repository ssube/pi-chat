import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from bridge.metrics import render_metrics
from bridge.runtime.pi_rpc import PiRpcClient
from bridge.workload import KEY, WorkloadSnapshot, aggregate


def frame(tasks=None, subagents=None):
    return {"type": "extension_ui_request", "method": "setStatus", "statusKey": KEY,
            "statusText": json.dumps({"version": 1, "tasks": tasks, "subagents": subagents})}


TASKS = {"pending": 2, "progress": 1, "blocked": 3, "question": 1, "complete": 4}
SUBAGENTS = {"active": 5, "capacity_used": 2, "capacity_limit": 4}


class WorkloadTests(unittest.TestCase):
    def test_zero_is_distinct_from_absent_unavailable_and_stale(self):
        clock = [10.0]
        snapshot = WorkloadSnapshot(now=lambda: clock[0])
        client = SimpleNamespace(running=True, workload_snapshot=snapshot)
        missing = aggregate([client])
        self.assertEqual(missing["coverage"]["tasks"]["absent"], 1)
        self.assertIsNone(missing["counts"]["tasks"])
        snapshot.consume(frame(dict.fromkeys(TASKS, 0), None))
        observed = aggregate([client])
        self.assertEqual(observed["counts"]["tasks"], dict.fromkeys(TASKS, 0))
        self.assertEqual(observed["coverage"]["subagents"]["unavailable"], 1)
        clock[0] += 76
        self.assertEqual(aggregate([client])["coverage"]["tasks"]["stale"], 1)
        self.assertIsNone(aggregate([client])["counts"]["tasks"])
        client.running = False
        self.assertEqual(aggregate([client])["coverage"]["tasks"]["absent"], 1)

    def test_partial_coverage_omits_totals_but_sources_are_independent(self):
        one, two = WorkloadSnapshot(), WorkloadSnapshot()
        one.consume(frame(TASKS, SUBAGENTS))
        two.consume(frame(TASKS, None))
        summary = aggregate([SimpleNamespace(running=True, workload_snapshot=one), SimpleNamespace(running=True, workload_snapshot=two)])
        self.assertEqual(summary["counts"]["tasks"]["pending"], 4)
        self.assertIsNone(summary["counts"]["subagents"])
        body = render_metrics("test", {"workload": summary}, 1.0)
        self.assertIn('agent_bridge_tasks{agent="test",state="question"} 2', body)
        self.assertNotIn('agent_bridge_subagents_active{', body)
        self.assertIn('source="subagents",status="unavailable"} 1', body)
        self.assertIn("NOT running processes or capacity utilization", body)

    def test_invalid_reserved_replacements_clear_old_data_and_do_not_leak(self):
        snapshot = WorkloadSnapshot()
        invalid = [None, "bad-json", "[1]", json.dumps({"version": True, "tasks": TASKS, "subagents": SUBAGENTS}), "x" * 2049]
        for raw in invalid:
            snapshot.consume(frame(TASKS, SUBAGENTS))
            self.assertTrue(snapshot.consume({"type": "extension_ui_request", "method": "setStatus", "statusKey": KEY, "statusText": raw}))
            self.assertIsNotNone(snapshot.received)
            self.assertEqual(snapshot.counts, {"tasks": None, "subagents": None})
        for value in [-1, True, 1.5, 2**53, "PRIVATE"]:
            snapshot.consume(frame({**TASKS, "pending": value}, SUBAGENTS))
            self.assertIsNone(snapshot.counts["tasks"])
            self.assertEqual(snapshot.counts["subagents"], SUBAGENTS)
        snapshot.consume(frame({**TASKS, "PRIVATE": 1}, None))
        self.assertIsNone(snapshot.counts["tasks"])
        self.assertFalse(snapshot.consume({"type": "extension_ui_request", "method": "setStatus", "statusKey": "normal-human-status", "statusText": "PRIVATE"}))

    def test_empty_resident_set_is_known_zero_and_shutdown_omits_observer_totals(self):
        body = render_metrics("test", {"workload": aggregate([])}, 1.0)
        self.assertIn('agent_bridge_tasks{agent="test",state="pending"} 0', body)
        shutdown = render_metrics("test", {"workload": aggregate([])}, 2.0, disconnected=True)
        self.assertNotIn('agent_bridge_tasks{', shutdown)
        self.assertNotIn('agent_bridge_subagents_active{', shutdown)

    def test_session_states_are_exclusive_bounded_and_independent(self):
        body = render_metrics("test", {"state_breakdown": {"running": 2, "compacting": 1, "idle": 3, "PRIVATE": 1}}, 1.0)
        self.assertIn('agent_bridge_sessions{agent="test",state="running"} 2', body)
        self.assertIn('agent_bridge_sessions{agent="test",state="compacting"} 1', body)
        self.assertIn('agent_bridge_sessions{agent="test",state="unknown"} 1', body)
        self.assertNotIn("PRIVATE", body)
        samples = [int(line.rsplit(" ", 1)[1]) for line in body.splitlines() if line.startswith("agent_bridge_sessions{")]
        self.assertEqual(sum(samples), 7)
        shutdown = render_metrics("test", {"state_breakdown": {"running": 2}}, 2.0, disconnected=True)
        self.assertIn('agent_bridge_sessions{agent="test",state="running"} 0', shutdown)


class RpcObservationTests(unittest.IsolatedAsyncioTestCase):
    async def test_idle_rpc_observation_does_not_start_turn_and_restart_clears_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            script = "import sys,json\nprint(" + repr(json.dumps(frame(TASKS, SUBAGENTS))) + ",flush=True)\nfor line in sys.stdin:\n r=json.loads(line);print(json.dumps({'type':'response','id':r['id'],'success':True,'data':{}}),flush=True)\n"
            client = PiRpcClient([sys.executable, "-u", "-c", script], Path(directory), {})
            try:
                await client.start()
                for _ in range(100):
                    if client.workload_snapshot.received is not None:
                        break
                    await asyncio.sleep(0.01)
                self.assertEqual(client.workload_snapshot.counts["tasks"], TASKS)
                self.assertFalse(client.active)
                self.assertEqual(client._request_number, 0, "no polling commands or prompts")
                await client.stop()
                self.assertIsNone(client.workload_snapshot.received)
                await client.start()
                self.assertIsNone(client.workload_snapshot.received, "previous process observation must not survive restart")
            finally:
                await client.stop()


class GroupReplacementTests(unittest.IsolatedAsyncioTestCase):
    def test_legacy_golden_and_complete_family_headers_on_all_branches(self):
        from bridge.metrics import compose_metrics
        from bridge.telemetry import Telemetry
        legacy = (Path(__file__).parent.parent / 'fixtures' / 'metrics-legacy-v070.prom').read_text()
        summary = {"total_scopes": 3, "active": 1, "awake": 2, "active_non_idle": 1,
                   "state_breakdown": {"running": 1, "idle": 1, "compacting": 1}}
        body = render_metrics('test', summary, 1000.0)
        old_names = ('agent_bridge_presence', 'agent_bridge_scopes', 'agent_bridge_last_push_timestamp_seconds')
        filtered = '\n'.join(line for line in body.splitlines() if any(line.startswith(name + '{') or line.startswith('# HELP '+name+' ') or line.startswith('# TYPE '+name+' ') for name in old_names))+'\n'
        self.assertEqual(filtered, legacy)
        telemetry = Telemetry()
        branches = [compose_metrics('test', summary, 1.0, telemetry),
                    compose_metrics('test', {**summary, 'workload': aggregate([])}, 1.0, telemetry),
                    compose_metrics('test', {}, 1.0, telemetry, disconnected=True)]
        families = [{line.split()[2] for line in body.splitlines() if line.startswith('# TYPE ')} for body in branches]
        self.assertEqual(families[0], families[1])
        self.assertEqual(families[0], families[2])
        self.assertNotIn('agent_bridge_tasks{', branches[0])
        self.assertNotIn('agent_bridge_subagents_active{', branches[0])
        self.assertEqual(sum(int(line.rsplit(' ',1)[1]) for line in body.splitlines() if line.startswith('agent_bridge_sessions{')), 3)

    async def test_real_http_put_replaces_complete_partial_and_shutdown_groups(self):
        from aiohttp import web
        from bridge.metrics import MetricsService
        from unittest.mock import AsyncMock
        bodies, methods = [], []
        async def put(request):
            methods.append(request.method)
            bodies.append(await request.text())
            return web.Response(text='')
        app = web.Application()
        app.router.add_route('*', '/metrics/job/agent_bridge/instance/test', put)
        runner = web.AppRunner(app)
        await runner.setup()
        server = __import__('socket').socket()
        server.bind(('127.0.0.1', 0))
        server.listen(8)
        site = web.SockSite(runner, server)
        await site.start()
        snapshot = WorkloadSnapshot()
        snapshot.consume(frame(TASKS, SUBAGENTS))
        client = SimpleNamespace(running=True, workload_snapshot=snapshot)
        runtime = SimpleNamespace(session_summary=AsyncMock(return_value={'workload':aggregate([client])}))
        service = MetricsService('test', runtime, f'http://127.0.0.1:{server.getsockname()[1]}')
        try:
            await service.refresh()
            self.assertIn('agent_bridge_tasks{', bodies[-1])
            snapshot.consume(frame(None, SUBAGENTS))
            runtime.session_summary.return_value = {'workload':aggregate([client])}
            await service.refresh()
            self.assertNotIn('agent_bridge_tasks{', bodies[-1])
            self.assertIn('agent_bridge_subagents_active{', bodies[-1])
            self.assertIn('source="tasks"} 0', bodies[-1])
            await service.shutdown()
            self.assertNotIn('agent_bridge_subagents_active{', bodies[-1])
            self.assertIn('state="disconnected"} 1', bodies[-1])
            self.assertEqual(methods, ['PUT','PUT','PUT'])
            families = [{line.split()[2] for line in body.splitlines() if line.startswith('# TYPE ')} for body in bodies]
            self.assertEqual(families[0], families[1])
            self.assertEqual(families[0], families[2])
        finally:
            await runner.cleanup()


class RpcFaultIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_bad_duplicate_and_midturn_frames_do_not_fail_pending_or_leak(self):
        from unittest.mock import AsyncMock, patch
        frames = [frame(TASKS, SUBAGENTS), frame(TASKS, SUBAGENTS),
                  {"type":"extension_ui_request","method":"setStatus","statusKey":KEY},
                  {"type":"extension_ui_request","method":"setStatus","statusKey":KEY,"statusText":"PRIVATE"},
                  {"type":"extension_ui_request","method":"setStatus","statusKey":KEY,"statusText":"x"*2049},
                  {"type":"extension_ui_request","method":"setStatus","statusKey":KEY+"-unknown","statusText":"PRIVATE"}]
        script = "import json,sys\nframes="+repr(frames)+"\nfor line in sys.stdin:\n r=json.loads(line)\n for f in frames: print(json.dumps(f),flush=True)\n print(json.dumps({'type':'response','id':r['id'],'success':True,'data':{}}),flush=True)\n if r['type']=='prompt':\n  print(json.dumps({'type':'message_end','message':{'role':'assistant','content':[{'type':'text','text':'OK'}]}}),flush=True)\n  print(json.dumps({'type':'agent_end'}),flush=True)\n"
        with tempfile.TemporaryDirectory() as directory:
            client = PiRpcClient([sys.executable, '-u', '-c', script], Path(directory), {}, settle_on_agent_end=True)
            try:
                with patch.object(client, '_fail_pending', wraps=client._fail_pending) as failure, patch('bridge.runtime.pi_rpc.log') as log:
                    await client.get_state()
                    result = await client.prompt('Offline test', AsyncMock())
                    self.assertEqual(result.text, 'OK')
                    failure.assert_not_called()
                    self.assertNotIn('PRIVATE', repr(log.mock_calls))
                    self.assertTrue(client.running)
                with patch.object(client.workload_snapshot, 'consume', side_effect=RuntimeError('PRIVATE')), patch.object(client, '_fail_pending', wraps=client._fail_pending) as failure:
                    self.assertEqual(await client.get_state(), {})
                    failure.assert_not_called()
                    self.assertTrue(client.running)
            finally:
                await client.stop()
