import hashlib
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bridge.config import MatrixSettings, Profile
from bridge.observability import record_bridge_failure
from bridge.runtime.pi_rpc import PiProviderError, PiRpcClient, PiRpcError, PiRpcTimeout, PiRunResult
from bridge.runtime.session import BridgeRuntime, Incoming, ReplyRejected
from bridge.runtime.state import SessionKey, read_checkpoint


class BridgeFailureWriterTests(unittest.TestCase):
    def test_pi_receives_origin_and_outbox_but_not_transport_token(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile = Profile(
                agent="agent_one", work_dir=root, state_dir=root / "state",
                pi_executable=Path("/usr/bin/pi"), pi_agent_dir=root / "agent",
                pi_path_prefix=(), model="test", image_model=None,
                pi_extra_args=(), pi_pass_env=(), identity_files=(),
                matrix=MatrixSettings("https://example.test", "@agent_one:example.test",
                                      "BRIDGE_TEST_SECRET", ("!room:example.test",)),
                discord=None,
            )
            key = SessionKey("agent_one", "matrix", "private-room")
            (root / "session").mkdir()
            outbox = root / "observability" / "outbox"
            with patch.dict(os.environ, {"BRIDGE_TEST_SECRET": "transport-secret",
                                      "PI_OBSERVABILITY_OUTBOX": str(outbox)},
                            clear=True):
                client = profile.pi_client(key, root / "session" / "conversation.jsonl")
            self.assertEqual(client._env["PI_OBSERVABILITY_OUTBOX"], str(outbox))
            self.assertEqual(client._env["PI_CAPTURE_AGENT"], "agent_one")
            self.assertEqual(client._env["PI_CAPTURE_ORIGIN"], "bridge_matrix")
            self.assertEqual(client._env["PI_CAPTURE_ROOT"], str(outbox.parent))
            self.assertEqual(client._env["AGENT_BRIDGE_AGENT"], "agent_one")
            self.assertEqual(client._env["AGENT_BRIDGE_TRANSPORT"], "matrix")
            self.assertNotIn("BRIDGE_TEST_SECRET", client._env)

    def test_provider_record_is_private_hashable_and_omits_raw_context(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key = SessionKey("agent_two", "discord", "private-channel-secret")
            occurrence = record_bridge_failure(root / "outbox", key,
                                               PiProviderError(500), epoch=4,
                                               phase="prompt",
                                               request_id="req-123",
                                               matrix_event_id="$event:example.test")
            self.assertTrue(occurrence.startswith("pierr-"))
            entry_path = root / "outbox" / f"{occurrence}.json"
            entry = json.loads(entry_path.read_text())
            detail_path = Path(entry["detail_ref"])
            detail = json.loads(detail_path.read_text())
            self.assertEqual(entry["origin"], "bridge_discord")
            self.assertEqual(entry["signature"], "provider:prompt:PiProviderError:http-500")
            self.assertEqual(detail["provider_http_status"], 500)
            self.assertEqual(detail["request_id"], "req-123")
            self.assertEqual(detail["matrix_event_id"], "$event:example.test")
            self.assertNotIn("request_id", entry)
            self.assertNotIn("matrix_event_id", entry)
            self.assertEqual(entry["detail_sha256"],
                             hashlib.sha256(detail_path.read_bytes()).hexdigest())
            self.assertNotIn("private-channel-secret", entry_path.read_text())
            self.assertNotIn("private-channel-secret", detail_path.read_text())
            for path in (entry_path, detail_path):
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)

    def test_untrusted_rpc_error_text_is_not_written(self):
        with tempfile.TemporaryDirectory() as directory:
            outbox = Path(directory) / "outbox"
            occurrence = record_bridge_failure(
                outbox, SessionKey("agent_three", "matrix", "room"),
                PiRpcError("token=secret-value in stderr"), epoch=2,
                phase="compact")
            entry = json.loads((outbox / f"{occurrence}.json").read_text())
            self.assertEqual(entry["redacted_summary"], "PiRpcError")
            self.assertNotIn("secret-value", (outbox / f"{occurrence}.json").read_text())
            self.assertNotIn("secret-value", Path(entry["detail_ref"]).read_text())

    def test_idle_timeout_record_is_distinct_and_reports_interval(self):
        with tempfile.TemporaryDirectory() as directory:
            outbox = Path(directory) / "outbox"
            occurrence = record_bridge_failure(
                outbox, SessionKey("agent_two", "matrix", "private-room"),
                PiRpcTimeout(900), epoch=86, phase="prompt")
            entry = json.loads((outbox / f"{occurrence}.json").read_text())
            detail = json.loads(Path(entry["detail_ref"]).read_text())
            self.assertEqual(entry["signature"], "session:prompt:PiRpcTimeout")
            self.assertEqual(detail["idle_timeout_seconds"], 900)
            self.assertNotIn("private-room", Path(entry["detail_ref"]).read_text())


class BridgeFailureRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_revision_rpc_failure_is_recorded_without_public_refusal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outbox = root / "outbox"
            outputs = []

            class RevisionFailureClient:
                active = False

                def __init__(self):
                    self.prompts = 0

                async def prompt(self, message, on_event, images=None, *, on_accepted=None):
                    self.prompts += 1
                    if on_accepted:
                        await on_accepted()
                    if self.prompts == 1:
                        return PiRunResult("Ask @absent-agent about it.")
                    raise PiRpcError("private provider context")

            async def send(key, kind, text, request_id):
                if kind == "final":
                    raise ReplyRejected("@absent-agent is not in this room. People here: Operator.")
                outputs.append(kind)

            runtime = BridgeRuntime(root / "state", lambda key, path: RevisionFailureClient(),
                                    send, failure_outbox=outbox)
            key = SessionKey("agent_one", "matrix", "private-room")
            await runtime.submit(key, Incoming("Operator", "$event", "Ask about it"))
            await runtime.wait_idle(key)
            entries = list(outbox.glob("*.json"))
            self.assertEqual(len(entries), 1)
            entry = json.loads(entries[0].read_text())
            detail = json.loads(Path(entry["detail_ref"]).read_text())
            self.assertEqual(entry["signature"], "session:output_revision:PiRpcError")
            self.assertEqual(detail["request_id"], "$event")
            self.assertNotIn("private provider context", entries[0].read_text())
            self.assertEqual(outputs, ["ack", "cleanup"])
            self.assertEqual(read_checkpoint(key.directory(root / "state")).status,
                             "delivery_error")

    async def test_process_start_failure_becomes_recordable_rpc_error(self):
        with tempfile.TemporaryDirectory() as directory:
            client = PiRpcClient(["/missing/pi-binary"], Path(directory), {})

            async def on_event(event):
                pass

            with self.assertRaisesRegex(PiRpcError, "could not start"):
                await client.prompt("hello", on_event)

    async def test_pi_failure_recorded_but_delivery_failure_is_not(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outbox = root / "outbox"

            class FailingClient:
                active = False

                async def prompt(self, message, on_event, images=None, *, on_accepted=None):
                    if on_accepted:
                        await on_accepted()
                    raise PiProviderError(503)

                async def stop(self):
                    pass

            async def send(*args):
                pass

            async def no_sleep(delay):
                pass

            runtime = BridgeRuntime(root / "state", lambda key, path: FailingClient(),
                                    send, failure_outbox=outbox, sleep=no_sleep)
            key = SessionKey("agent_one", "matrix", "private-room")
            await runtime.submit(key, Incoming("person", "event", "secret prompt"))
            await runtime.wait_idle(key)
            entries = list(outbox.glob("*.json"))
            self.assertEqual(len(entries), 3)  # Each failed provider attempt is an occurrence.
            for entry in entries:
                payload = entry.read_text()
                self.assertNotIn("secret prompt", payload)
                self.assertEqual(json.loads(payload)["origin"], "bridge_matrix")

            class SuccessfulClient(FailingClient):
                async def prompt(self, message, on_event, images=None, *, on_accepted=None):
                    if on_accepted:
                        await on_accepted()
                    return PiRunResult("done")

            async def failed_delivery(key, kind, text, request_id):
                if kind == "final":
                    raise RuntimeError("transport delivery failed")

            runtime = BridgeRuntime(root / "other", lambda key, path: SuccessfulClient(),
                                    failed_delivery, failure_outbox=outbox)
            await runtime.submit(key, Incoming("person", "event2", "hello"))
            await runtime.wait_idle(key)
            self.assertEqual(len(list(outbox.glob("*.json"))), 3)

    async def test_session_failure_records_support_id_and_next_request_succeeds(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outbox = root / "outbox"
            outputs: list[tuple[str, str, str]] = []

            class RecoveringClient:
                active = False

                def __init__(self):
                    self.prompts = 0

                async def prompt(self, message, on_event, images=None, *, on_accepted=None):
                    self.prompts += 1
                    if on_accepted:
                        await on_accepted()
                    if self.prompts == 1:
                        raise PiRpcError("process exited unexpectedly")
                    return PiRunResult("recovered without restart")

                async def stop(self):
                    pass

            async def send(key, kind, text, request_id):
                outputs.append((kind, text, request_id))

            runtime = BridgeRuntime(root / "state", lambda key, path: RecoveringClient(),
                                    send, failure_outbox=outbox)
            key = SessionKey("agent_two", "matrix", "private-room")
            await runtime.submit(key, Incoming("person", "event1", "first prompt"))
            await runtime.wait_idle(key)
            await runtime.submit(key, Incoming("person", "event2", "second prompt"))
            await runtime.wait_idle(key)

            entries = list(outbox.glob("*.json"))
            self.assertEqual(len(entries), 1)
            entry = json.loads(entries[0].read_text())
            detail = json.loads(Path(entry["detail_ref"]).read_text())
            self.assertEqual(entry["signature"], "session:prompt:PiRpcError")
            self.assertEqual(entry["origin"], "bridge_matrix")
            self.assertEqual(detail["request_id"], "event1")
            self.assertEqual(detail["matrix_event_id"], "event1")
            self.assertNotIn("first prompt", entries[0].read_text())
            self.assertNotIn("first prompt", detail)

            error_text = next(text for kind, text, _ in outputs if kind == "error")
            self.assertIn("PiRpcError", error_text)
            self.assertIn("Support ID:", error_text)
            final_text = next(text for kind, text, _ in outputs if kind == "final")
            self.assertEqual(final_text, "recovered without restart")

    async def test_provider_500_record_includes_status_and_support_id(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outbox = root / "outbox"
            outputs: list[tuple[str, str, str]] = []

            class Provider500Client:
                active = False

                async def prompt(self, message, on_event, images=None, *, on_accepted=None):
                    if on_accepted:
                        await on_accepted()
                    raise PiProviderError(500)

                async def stop(self):
                    pass

            async def send(key, kind, text, request_id):
                outputs.append((kind, text, request_id))

            async def no_sleep(delay):
                pass

            runtime = BridgeRuntime(root / "state", lambda key, path: Provider500Client(),
                                    send, failure_outbox=outbox, sleep=no_sleep)
            key = SessionKey("agent_two", "matrix", "private-room")
            await runtime.submit(key, Incoming("person", "event1", "prompt text"))
            await runtime.wait_idle(key)

            entries = list(outbox.glob("*.json"))
            self.assertEqual(len(entries), 3)
            for entry in entries:
                payload = json.loads(entry.read_text())
                self.assertEqual(payload["signature"], "provider:prompt:PiProviderError:http-500")
                self.assertNotIn("prompt text", entry.read_text())

            error_text = next(text for kind, text, _ in outputs if kind == "error")
            self.assertIn("HTTP 500", error_text)
            self.assertNotIn("prompt text", error_text)
