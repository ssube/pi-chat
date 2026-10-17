"""Compatibility and type validation at persistent and local-socket boundaries."""

import json
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bridge.config import Profile
from bridge.history_cli import fetch
from bridge.react_cli import send
from bridge.runtime.state import Checkpoint, read_checkpoint, write_checkpoint


class ProfileBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "profile.json"
        self.profile = {
            "provider": "test", "model": "test-model",
            "agent": "agent_three", "work_dir": self.directory.name,
            "state_dir": self.directory.name, "pi_executable": "/usr/bin/pi",
            "pi_agent_dir": self.directory.name,
            "matrix": {"homeserver": "https://example.test",
                       "user_id": "@agent_three:example.test",
                       "token_env": "TEST_MATRIX_TOKEN", "rooms": ["!room:example.test"]},
        }

    def load(self):
        self.path.write_text(json.dumps(self.profile))
        with patch.dict(os.environ, {"UNUSED_ROOM_ID": "!shared:example.test",
                                  "TEST_MATRIX_TOKEN": "test-token"}):
            return Profile.load(self.path)

    def test_rejects_wrong_profile_and_matrix_field_types(self):
        cases = [
            ("agent", [], "unknown agent"),
            ("work_dir", 42, "work_dir"),
            ("pi_path_prefix", "/usr/bin", "pi_path_prefix"),
            ("pi_extra_args", ["--yes", 1], "pi_extra_args"),
            ("model", False, "model"),
            ("image_model", 17, "image_model"),
            ("matrix", [], "Matrix settings"),
        ]
        for field, value, message in cases:
            with self.subTest(field=field):
                original = self.profile.get(field)
                self.profile[field] = value
                with self.assertRaisesRegex(ValueError, message):
                    self.load()
                if original is None:
                    self.profile.pop(field)
                else:
                    self.profile[field] = original
        rooms = self.profile["matrix"]["rooms"]
        self.profile["matrix"]["rooms"] = [123]
        with self.assertRaisesRegex(ValueError, "Matrix rooms"):
            self.load()
        self.profile["matrix"]["rooms"] = rooms

    def test_rejects_boolean_discord_id_and_retains_optional_defaults(self):
        profile = self.load()
        self.assertEqual(profile.pi_extra_args, ())
        self.assertEqual(profile.matrix.direct_rooms, ())
        self.profile["image_model"] = ""
        self.assertIsNone(self.load().image_model)
        self.profile["agent"] = "agent_two"
        self.profile["discord"] = {"token_env": "TEST_DISCORD_TOKEN",
                                   "guild_id": True, "channel_ids": [456]}
        with patch.dict(os.environ, {"UNUSED_ROOM_ID": "!shared:example.test",
                                  "TEST_MATRIX_TOKEN": "test-token",
                                  "TEST_DISCORD_TOKEN": "test-token"}):
            with self.assertRaisesRegex(ValueError, "Discord guild_id"):
                self.path.write_text(json.dumps(self.profile))
                Profile.load(self.path)
        self.profile["discord"]["guild_id"] = "123"
        self.profile["discord"]["channel_ids"] = ["456"]
        with patch.dict(os.environ, {"UNUSED_ROOM_ID": "!shared:example.test",
                                  "TEST_MATRIX_TOKEN": "test-token",
                                  "TEST_DISCORD_TOKEN": "test-token"}):
            self.path.write_text(json.dumps(self.profile))
            self.assertEqual(Profile.load(self.path).discord.channel_ids, (456,))

    def test_agent_four_accepts_each_transport_and_both_during_provisioning(self):
        self.profile["agent"] = "agent_four"
        self.profile["matrix"]["user_id"] = "@agent_four:example.test"
        self.profile["discord"] = {"token_env": "TEST_DISCORD_TOKEN",
                                   "guild_id": 123, "channel_ids": [456]}
        with patch.dict(os.environ, {"TEST_DISCORD_TOKEN": "test-token"}):
            profile = self.load()
            self.assertEqual(profile.agent, "agent_four")
            self.assertEqual(profile.matrix.waking_rooms, frozenset())
            self.assertEqual(profile.discord.wake_on_any_channel_ids, ())
            matrix = self.profile.pop("matrix")
            self.assertIsNone(self.load().matrix)
            self.profile["matrix"] = matrix
            self.profile.pop("discord")
            self.assertIsNone(self.load().discord)



class CheckpointBoundaryTests(unittest.TestCase):
    def test_old_checkpoint_defaults_and_unknown_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "state.json").write_text(json.dumps({
                "epoch": 2, "summary": "prior chat", "unused_future_field": "retained"}))
            checkpoint = read_checkpoint(root)
            self.assertEqual(checkpoint.epoch, 2)
            self.assertEqual(checkpoint.summary, "prior chat")
            self.assertEqual(checkpoint.last_wake_id, "")
            write_checkpoint(root, checkpoint)
            self.assertEqual(read_checkpoint(root), checkpoint)

    def test_rejects_corrupt_but_valid_json_field_types(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for field, value in (("epoch", True), ("last_activity_ms", -1),
                                 ("status", []), ("final_sent", "false"),
                                 ("summary_saved_ms", None), ("status", "unknown")):
                with self.subTest(field=field):
                    (root / "state.json").write_text(json.dumps({field: value}))
                    with self.assertRaisesRegex(ValueError, field):
                        read_checkpoint(root)
            self.assertEqual(read_checkpoint(Path(directory) / "missing"), Checkpoint())


class SocketResponseBoundaryTests(unittest.TestCase):
    class FakeSocket:
        def __init__(self, payload):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def settimeout(self, *_):
            pass

        def connect(self, *_):
            pass

        def sendall(self, *_):
            pass

        def makefile(self, *_):
            return io.BytesIO(self.payload)

    def test_history_cli_rejects_malformed_success_envelope(self):
        for payload in (b'{"ok":"true","page":{}}\n',
                        b'{"ok":true,"page":[]}\n',
                        b'{"ok":true}\n'):
            with self.subTest(payload=payload), patch(
                    "bridge.history_cli.socket.socket",
                    return_value=self.FakeSocket(payload)):
                with self.assertRaisesRegex(RuntimeError, "invalid history response"):
                    fetch("unused", "matrix", "room", "x" * 32)

    def test_reaction_cli_rejects_malformed_success_envelope(self):
        for payload in ('{"ok":1,"emoji":"👍"}\n'.encode(),
                        b'{"ok":true}\n', b'{"ok":true,"emoji":12}\n'):
            with self.subTest(payload=payload), patch(
                    "bridge.react_cli.socket.socket",
                    return_value=self.FakeSocket(payload)):
                with self.assertRaisesRegex(RuntimeError, "invalid reaction response"):
                    send("unused", "matrix", "room", "x" * 32, "$event", "👍")
