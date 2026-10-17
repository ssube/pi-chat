"""Opt-in installed-Pi RPC integration with a loopback-only synthetic provider.

BRIDGE_TEST_PI_CLI=/absolute/path/to/pi/dist/cli.js python -m unittest discover \
    -s tests -p test_pi_compaction_integration.py -v

No real credentials, models, extensions, personal config, or live bridge.
Only the image case enables the read tool, against its synthetic PNG fixture.
"""
import asyncio
import json
import os
import shutil
import struct
import tempfile
import threading
import unittest
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from bridge.runtime.pi_rpc import PiRpcClient
from bridge.runtime.session import BridgeRuntime, Incoming
from bridge.runtime.state import SessionKey


PI_CLI = os.environ.get("BRIDGE_TEST_PI_CLI", "")


class SyntheticProvider:
    def __init__(self, mode):
        self.mode = mode
        self.release_summary = threading.Event()
        self.summary_started = threading.Event()
        self.requests = []
        self.regular_calls = 0
        self.image_path = None
        provider = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                if self.path != "/v1/chat/completions":
                    self.send_error(404)
                    return
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                messages = request.get("messages", [])
                serialized = json.dumps(messages)
                summary = "You are a context summarization assistant." in serialized
                provider.requests.append((summary, serialized))
                if summary:
                    provider.summary_started.set()
                    if not provider.release_summary.wait(10):
                        self.send_error(504)
                        return
                else:
                    provider.regular_calls += 1
                if (provider.mode == "overflow" and not summary and provider.regular_calls == 1):
                    payload = json.dumps({"error": {"message": "context_length_exceeded",
                                                   "type": "invalid_request_error",
                                                   "code": "context_length_exceeded"}}).encode()
                    self.send_response(400)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Connection", "close")
                self.end_headers()
                chunk = {"id": "synthetic", "object": "chat.completion.chunk", "created": 1,
                         "model": "synthetic", "choices": [{"index": 0, "delta": {
                             "role": "assistant", "content": "Synthetic checkpoint" if summary
                             else "Synthetic answer"}, "finish_reason": None}]}
                image_read = provider.mode == "image" and not summary and provider.regular_calls == 1
                if image_read:
                    chunk["choices"][0]["delta"] = {"role": "assistant", "tool_calls": [{
                        "index": 0, "id": "synthetic-read", "type": "function",
                        "function": {"name": "read", "arguments": json.dumps({"path": provider.image_path})}}]}
                self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
                chunk["choices"] = [{"index": 0, "delta": {},
                                     "finish_reason": "tool_calls" if image_read else "stop"}]
                self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
                chunk["choices"] = []
                chunk["usage"] = {"prompt_tokens": 3500 if provider.mode in {"threshold", "image"}
                                  and not summary and provider.regular_calls == 1 else 64,
                                  "completion_tokens": 3, "total_tokens": 3503 if provider.mode
                                  in {"threshold", "image"} and not summary and provider.regular_calls == 1 else 67}
                self.wfile.write(("data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n").encode())
                self.wfile.flush()
                self.close_connection = True

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.01),
                                       daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.release_summary.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


@unittest.skipUnless(PI_CLI and Path(PI_CLI).is_file(), "set BRIDGE_TEST_PI_CLI for isolated real-Pi tests")
class InstalledPiCompactionTests(unittest.IsolatedAsyncioTestCase):
    async def test_compaction_queueing_and_saved_summary_reuse_on_real_pi(self):
        for mode in ("threshold", "overflow", "manual", "image"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory, SyntheticProvider(mode) as provider:
                root = Path(directory)
                if mode == "image":
                    provider.image_path = str(root / "synthetic.png")
                    def png_chunk(kind, data):
                        return struct.pack(">I", len(data)) + kind + data + struct.pack(
                            ">I", zlib.crc32(kind + data))
                    Path(provider.image_path).write_bytes(b"\x89PNG\r\n\x1a\n" + png_chunk(
                        b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)) + png_chunk(
                        b"IDAT", zlib.compress(b"\x00\xff\x00\x00\xff")) + png_chunk(b"IEND", b""))
                agent_dir = root / "agent"
                agent_dir.mkdir()
                (agent_dir / "models.json").write_text(json.dumps({"providers": {"synthetic": {
                    "baseUrl": f"http://127.0.0.1:{provider.server.server_port}/v1",
                    "api": "openai-completions", "apiKey": "synthetic-not-a-real-key",
                    "models": [{"id": "synthetic", "reasoning": False, "input": ["text", "image"],
                                "contextWindow": 4096, "maxTokens": 256,
                                "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}}]}}}))
                (agent_dir / "settings.json").write_text(json.dumps({"cacheWarming": "off",
                    "compaction": {"enabled": True, "reserveTokens": 1024, "keepRecentTokens": 0},
                    "retry": {"enabled": False}, "enableInstallTelemetry": False,
                    "enableAnalytics": False}))
                # Allowlist, not inherited environment: no real credentials or proxy routing.
                env = {"PATH": os.defpath, "HOME": directory, "PI_CODING_AGENT_DIR": str(agent_dir),
                       "PI_OFFLINE": "1", "PI_SKIP_VERSION_CHECK": "1", "PI_TELEMETRY": "0",
                       "NO_PROXY": "127.0.0.1,localhost"}
                compact_started = asyncio.Event()
                kinds = []
                reasons = []
                clients = []

                class ObservedPi(PiRpcClient):
                    async def prompt(self, message, on_event, images=None, *, on_accepted=None):
                        async def event(value):
                            await on_event(value)
                            kinds.append(value.get("type"))
                            if value.get("type") == "compaction_start":
                                reasons.append(value.get("reason"))
                                compact_started.set()
                        return await super().prompt(message, event, images, on_accepted=on_accepted)

                def factory(key, session_file):
                    client = ObservedPi([shutil.which("node"), PI_CLI, "--mode", "rpc",
                        "--session", str(session_file), "--provider", "synthetic", "--model", "synthetic",
                        "--no-extensions", "--no-skills", "--no-prompt-templates", "--no-context-files",
                        *(["--tools", "read"] if mode == "image" else ["--no-tools"]),
                        "--no-approve", "--offline", "--system-prompt", "Synthetic test only."],
                        root, env, timeout_seconds=15)
                    clients.append(client)
                    return client

                async def output(key, kind, text, request_id):
                    pass

                runtime = BridgeRuntime(root / "bridge", factory, output)
                key = SessionKey("agent_one", "matrix", "synthetic-real-pi")
                tasks = []
                try:
                    await runtime.submit(key, Incoming("Owner", "seed", "Seed " + "x" * 16000))
                    if mode == "manual":
                        await asyncio.wait_for(runtime.wait_idle(key), 15)
                        compact = asyncio.create_task(clients[0].compact())
                        tasks.append(compact)
                        self.assertTrue(await asyncio.to_thread(provider.summary_started.wait, 10))
                        provider.release_summary.set()
                        summary = await asyncio.wait_for(compact, 15)
                        request_count = len(provider.requests)
                        self.assertEqual(await clients[0].compact(), summary)
                        self.assertEqual(len(provider.requests), request_count)
                        self.assertEqual(await runtime.sweep_idle(force=True), 1)
                        self.assertEqual(len(provider.requests), request_count)
                        self.assertFalse(clients[0].running)
                        continue
                    await asyncio.wait_for(compact_started.wait(), 15)
                    self.assertEqual(reasons, ["threshold" if mode == "image" else mode])
                    if mode == "image":
                        self.assertLess(kinds.index("tool_execution_start"), kinds.index("tool_execution_end"))
                        self.assertLess(kinds.index("tool_execution_end"), kinds.index("compaction_start"))
                    self.assertEqual((await runtime.session_summary())["state_breakdown"], {"compacting": 1})
                    pid = clients[0]._process.pid
                    tasks.extend(asyncio.create_task(runtime.submit(
                        key, Incoming("Owner", f"queued-{i}", f"QUEUED-MARKER-{i}"))) for i in range(8))
                    await asyncio.sleep(0.05)
                    self.assertFalse(any(task.done() for task in tasks[-8:]))
                    self.assertEqual(provider.regular_calls, 1)
                    provider.release_summary.set()
                    await asyncio.wait_for(asyncio.gather(*tasks), 15)
                    await asyncio.wait_for(runtime.wait_idle(key), 15)
                    self.assertEqual(len(clients), 1)
                    self.assertEqual(clients[0]._process.pid, pid)
                    self.assertIn("compaction_end", kinds)
                    self.assertEqual(kinds[-1], "agent_settled")
                    messages = (await clients[0]._send_command("get_messages"))["data"]["messages"]
                    serialized = json.dumps(messages)
                    positions = [serialized.index(f"QUEUED-MARKER-{i}") for i in range(8)]
                    self.assertEqual(positions, sorted(positions))
                    self.assertEqual((await runtime.session_summary())["state_breakdown"], {"idle": 1})
                finally:
                    provider.release_summary.set()
                    for task in tasks:
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    await runtime.close()
