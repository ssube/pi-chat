"""Runtime contracts; Docker build/lifecycle runs on CI or when Docker is available."""
import asyncio
from contextlib import contextmanager
import http.server
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import uuid

from bridge.health_socket import HealthSocket
from runtime import entrypoint, healthcheck


ROOT = Path(__file__).resolve().parents[1]


class HealthTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.profile = self.root / "profile.json"
        self.profile.write_text(json.dumps({"state_dir": str(self.root / "bridge")}))
        self.socket = HealthSocket(self.root / "bridge/bridge-health.sock")
        await self.socket.start()
        self.addAsyncCleanup(self.socket.close)

    async def test_ready_and_pending_are_distinct(self):
        self.assertFalse(await asyncio.to_thread(healthcheck.probe, self.profile))
        self.socket.ready = True
        self.assertTrue(await asyncio.to_thread(healthcheck.probe, self.profile))

    async def test_public_socket_is_unhealthy(self):
        self.socket.ready = True
        self.socket.path.chmod(0o666)
        self.assertFalse(await asyncio.to_thread(healthcheck.probe, self.profile))

    async def test_malformed_and_missing_profiles_fail_closed(self):
        for content in ("not JSON", "{}", '{"state_dir": null}', '{"state_dir": "relative"}'):
            self.profile.write_text(content)
            with patch.dict(os.environ, {"AGENT_BRIDGE_PROFILE": str(self.profile)}):
                self.assertEqual(healthcheck.main(), 1)
        with patch.dict(os.environ, {"AGENT_BRIDGE_PROFILE": str(self.root / "missing")}):
            self.assertEqual(healthcheck.main(), 1)


class EntrypointTests(unittest.TestCase):
    def test_root_is_refused_even_for_diagnostic_commands(self):
        with patch.object(os, "geteuid", return_value=0):
            with self.assertRaisesRegex(SystemExit, "refuses root"):
                entrypoint.main()

    def test_private_directories_do_not_repair_public_existing_data(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "public"
            path.mkdir(mode=0o755)
            with self.assertRaises(ValueError):
                entrypoint.private_directory(path)
            self.assertEqual(path.stat().st_mode & 0o777, 0o755)

    def test_profile_is_structurally_valid_without_real_credentials(self):
        from bridge.config import Profile
        profile = Profile.load(ROOT / "runtime/profile.example.json",
                               require_transport_credentials=False)
        self.assertEqual(str(profile.pi_executable), "/usr/local/bin/pi")
        self.assertEqual(str(profile.state_dir), "/state/bridge")
        self.assertIn("--no-extensions", profile.pi_extra_args)


@contextmanager
def matrix_fixture():
    """Synthetic messages and an offline provider only: no real account or model."""
    final = threading.Event()

    class Handler(http.server.BaseHTTPRequestHandler):
        syncs = 0

        def log_message(self, *_args):
            pass

        def send_json(self, value):
            body = json.dumps(value).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if "/sync?" in self.path:
                time.sleep(0.2)
                Handler.syncs += 1
                events = []
                if Handler.syncs == 2:
                    events = [{"type": "m.room.message", "event_id": "$fixture",
                               "sender": "@requester:example.invalid", "origin_server_ts": 1,
                               "content": {"msgtype": "m.text", "body": "hello",
                                           "m.mentions": {"user_ids": ["@example_agent:example.invalid"]}}}]
                self.send_json({"next_batch": str(Handler.syncs), "rooms": {"join": {
                    "!fixture:example.invalid": {"timeline": {"events": events}}}}})
            else:
                self.send_json({"joined": {"@example_agent:example.invalid": {
                    "display_name": "example_agent"}}})

        def do_PUT(self):
            payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}")
            if "bridge-fast-offline: 1 requests" in payload.get("body", ""):
                final.set()
            self.send_json({"event_id": "$fixture-response"})

    server = http.server.ThreadingHTTPServer(("0.0.0.0", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port, final
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class DockerRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which("docker"):
            if os.environ.get("GITHUB_ACTIONS") == "true":
                raise RuntimeError("Docker is required for the CI runtime build/lifecycle test")
            raise unittest.SkipTest("Docker is not installed")
        result = subprocess.run(["docker", "info", "--format", "{{.OSType}}"],
                                capture_output=True, text=True, timeout=30)
        if result.returncode or result.stdout.strip() != "linux":
            if os.environ.get("GITHUB_ACTIONS") == "true":
                raise RuntimeError("A working Linux Docker daemon is required on CI")
            raise unittest.SkipTest("A working Linux Docker daemon is required")
        if os.getuid() == 0:
            raise unittest.SkipTest("Lifecycle mount tests require a non-root host UID")
        cls.image = "pi-chat-runtime-test:" + uuid.uuid4().hex[:12]
        result = subprocess.run(["docker", "build", "--progress=plain", "-t", cls.image, "."],
                                cwd=ROOT, capture_output=True, text=True, timeout=900)
        if result.returncode:
            raise AssertionError("Runtime image build failed:\n" + result.stdout[-12000:] + result.stderr[-12000:])
        cls.addClassCleanup(subprocess.run, ["docker", "image", "rm", cls.image],
                            capture_output=True, timeout=30)

    def docker(self, *args, check=True, timeout=60):
        result = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)
        if check and result.returncode:
            self.fail(f"Docker {args[0]} failed:\n{result.stdout[-6000:]}{result.stderr[-6000:]}")
        return result

    def test_preinstalled_components_without_browser_or_privilege(self):
        result = self.docker("run", "--rm", "--network", "none", self.image, "pi", "--version")
        self.assertEqual(result.stdout.strip(), "0.99.1")
        result = self.docker("run", "--rm", "--network", "none", self.image, "python", "-c",
                             "import os, shutil; from pathlib import Path; "
                             "from playwright.sync_api import sync_playwright; "
                             "assert os.geteuid() == 1000; "
                             "assert not any(shutil.which(x) for x in ['sudo','sshd','chromium','redis-server','chroma']); "
                             "p = sync_playwright().start(); "
                             "assert not Path(p.chromium.executable_path).exists(); p.stop(); "
                             "print('non-root; Playwright client only')")
        self.assertIn("Playwright client only", result.stdout)
        result = self.docker("run", "--rm", "--user", "0:0", self.image, "pi", "--version", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("refuses root", result.stderr)

    def test_bridge_pi_health_and_graceful_shutdown(self):
        name = "pi-chat-runtime-test-" + uuid.uuid4().hex[:12]
        self.addCleanup(self.docker, "rm", "-f", name, check=False)
        with tempfile.TemporaryDirectory() as directory, matrix_fixture() as (port, final):
            root = Path(directory)
            for path in (root / "state", root / "work"):
                path.mkdir(mode=0o700)
            profile = json.loads((ROOT / "runtime/profile.example.json").read_text())
            profile.update(provider="bridge-fast-offline", model="test")
            profile["pi_extra_args"] = ["--offline", "--no-skills", "--no-context-files", "--extension",
                                        "/fixtures/bridge/fixtures/offline-modes.ts"]
            profile["matrix"].update(homeserver=f"http://host.docker.internal:{port}",
                                     rooms=["!fixture:example.invalid"])
            profile_path = root / "profile.json"
            profile_path.write_text(json.dumps(profile))
            profile_path.chmod(0o600)
            args = ["--user", f"{os.getuid()}:{os.getgid()}", "--read-only", "--cap-drop=ALL",
                    "--security-opt=no-new-privileges", "--tmpfs", "/tmp:rw,nosuid,nodev,mode=1777",
                    "--add-host=host.docker.internal:host-gateway", "-e", "MATRIX_TOKEN=synthetic-test-only",
                    "--mount", f"type=bind,src={root / 'state'},dst=/state",
                    "--mount", f"type=bind,src={root / 'work'},dst=/workspace",
                    "--mount", f"type=bind,src={profile_path},dst=/config/profile.json,readonly",
                    "--mount", f"type=bind,src={ROOT / 'tests'},dst=/fixtures,readonly"]
            # Fail before starting Supervisor for an overly readable profile.
            profile_path.chmod(0o644)
            refused = self.docker("run", "--rm", *args, self.image, check=False)
            self.assertNotEqual(refused.returncode, 0)
            self.assertIn("startup refused", refused.stderr)
            profile_path.chmod(0o600)
            self.docker("run", "-d", "--name", name, *args, self.image)
            if not final.wait(60):
                self.fail("Offline bridge/Pi turn did not complete:\n" + self.docker("logs", name).stdout)
            result = self.docker("exec", name, "supervisorctl", "-c",
                                 "/opt/pi-chat/runtime/supervisord.conf", "status")
            self.assertIn("bridge", result.stdout)
            self.assertIn("RUNNING", result.stdout)
            self.docker("exec", name, "python", "/opt/pi-chat/runtime/healthcheck.py")
            # Real Pi RPC contracts inside the built image; no provider calls.
            self.docker("exec", "-e", "BRIDGE_TEST_PI_CLI=/opt/pi-runtime/node_modules/@earendil-works/pi-coding-agent/dist/cli.js",
                        name, "python", "-m", "unittest", "discover", "-s", "/fixtures/bridge",
                        "-p", "test_bridge_real_pi.py", timeout=120)
            # Pi is alive under bridge ownership, not an independent Supervisor program.
            result = self.docker("exec", name, "python", "-c",
                                 "from pathlib import Path; "
                                 "cmds=[p.read_bytes() for p in Path('/proc').glob('[0-9]*/cmdline') if p.exists()]; "
                                 "assert any(b'--mode\\x00rpc\\x00' in c for c in cmds)")
            self.docker("stop", "--time", "60", name, timeout=75)
            status = json.loads(self.docker("inspect", name).stdout)[0]["State"]
            self.assertFalse(status["Running"])
            self.assertFalse(status["OOMKilled"])
            self.assertEqual(status["ExitCode"], 0)
            self.assertFalse((root / "state/bridge/bridge-health.sock").exists())
            sessions = list((root / "state/bridge/sessions").rglob("*.jsonl"))
            self.assertTrue(sessions)
            self.assertTrue(any("bridge-fast-offline" in p.read_text() for p in sessions))
