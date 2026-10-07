"""Isolated tests for the read-only Matrix history and wait helper."""

import importlib.util
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "room_history.py"
spec = importlib.util.spec_from_file_location("room_history", SCRIPT)
assert spec and spec.loader
reader = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reader)


def event(id_, sender="@peer:example.test", body="READY", timestamp=1000):
    return {"event_id": id_, "sender": sender, "type": "m.room.message",
            "origin_server_ts": timestamp, "content": {"msgtype": "m.text", "body": body}}


class MatrixReadTests(unittest.TestCase):
    def test_history_filters_sender_keyword_progress_and_returns_chronological(self):
        pages = [
            {"chunk": [event("$new", body="ready to ship", timestamp=3000),
                       event("$other", sender="@other:example.test", timestamp=2500),
                       event("$progress", body="Still working…", timestamp=2400)],
             "end": "next"},
            {"chunk": [event("$old", body="READY for gate", timestamp=2000),
                       event("$too-old", timestamp=1000)], "end": "older"},
        ]
        with patch.object(reader, "get", side_effect=pages) as get:
            result = reader.history("https://example.test", "private-token", "!room:example.test",
                                    limit=3, pages=2, sender="@peer:example.test",
                                    after_ms=1500, skip_progress=True, contains="ready")
        self.assertEqual([e["event_id"] for e in result], ["$old", "$new"])
        self.assertEqual(get.call_count, 2)
        self.assertEqual(get.call_args_list[1].args[-1]["from"], "next")

    def test_wait_for_skips_old_matching_message_and_wrong_sender(self):
        replies = [
            {"start": "s0", "chunk": [event("$old", body="Deploy DONE")]},
            {"end": "s1", "chunk": [event("$wrong", sender="@other:example.test",
                                         body="Deploy DONE"), event("$notyet", body="Pending")]},
            {"end": "s2", "chunk": [event("$fresh", body="Deploy done now")]},
        ]
        with patch.object(reader, "get", side_effect=replies) as get, \
                patch.object(reader.time, "sleep", return_value=None), \
                patch.object(reader.time, "monotonic", return_value=0):
            found = reader.wait_for_message("https://example.test", "private-token",
                                            "!room:example.test", sender="@peer:example.test",
                                            keyword="deploy DONE", timeout=10, interval=2)
        self.assertEqual(found["event_id"], "$fresh")
        self.assertEqual(get.call_args_list[0].args[-1]["dir"], "b")
        self.assertEqual(get.call_args_list[1].args[-1]["dir"], "f")
        self.assertEqual(get.call_args_list[2].args[-1]["from"], "s1")

    def test_wait_for_timeout_is_distinct_from_failure(self):
        with patch.object(reader, "get", side_effect=[{"start": "s0", "chunk": []},
                                                      {"chunk": []}]), \
                patch.object(reader.time, "sleep", return_value=None), \
                patch.object(reader.time, "monotonic", side_effect=[0, 0, 1, 1]):
            self.assertIsNone(reader.wait_for_message("https://example.test", "token",
                                                       "!room:example.test",
                                                       sender="@peer:example.test",
                                                       keyword="ready", timeout=1, interval=2))

    def test_private_files_and_identity_are_required(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory) / "profile.json"
            identity = Path(directory) / "identity.json"
            profile.write_text(json.dumps({"matrix": {"homeserver": "https://example.test",
                                                     "user_id": "@self:example.test",
                                                     "rooms": ["!allowed:example.test"]}}))
            identity.write_text(json.dumps({"user_id": "@self:example.test", "token": "secret"}))
            profile.chmod(0o600)
            identity.chmod(0o600)
            self.assertEqual(reader.credentials(profile, identity)[3], {"!allowed:example.test"})
            identity.chmod(0o644)
            with self.assertRaisesRegex(ValueError, "owner-only"):
                reader.credentials(profile, identity)
            identity.chmod(0o600)
            identity.write_text(json.dumps({"user_id": "@other:example.test", "token": "secret"}))
            with self.assertRaisesRegex(ValueError, "same usable Matrix account"):
                reader.credentials(profile, identity)

    def test_env_file_scope_and_quoted_token(self):
        with tempfile.TemporaryDirectory() as directory:
            env = Path(directory) / "chair.env"
            env.write_text("MATRIX_URL=https://example.test\n"
                           "MATRIX_USER_ID=@chair:example.test\n"
                           "MATRIX_ROOM_ID=!shared:example.test\n"
                           "MATRIX_TOKEN='secret#part'\n")
            env.chmod(0o600)
            self.assertEqual(reader.env_credentials(env),
                             ("https://example.test", "@chair:example.test",
                              "secret#part", {"!shared:example.test"}))
            with patch.object(reader, "get") as get, redirect_stderr(io.StringIO()) as errors:
                code = reader.main(["--env-file", str(env), "--room-id", "!other:example.test"])
            self.assertEqual(code, 1)
            get.assert_not_called()
            self.assertNotIn("secret", errors.getvalue())

    def test_main_rejects_room_outside_profile_before_room_fetch(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory) / "profile.json"
            identity = Path(directory) / "identity.json"
            profile.write_text(json.dumps({"matrix": {"homeserver": "https://example.test",
                                                     "user_id": "@self:example.test",
                                                     "rooms": ["!allowed:example.test"]}}))
            identity.write_text(json.dumps({"user_id": "@self:example.test", "token": "secret"}))
            profile.chmod(0o600)
            identity.chmod(0o600)
            out, err = io.StringIO(), io.StringIO()
            with patch.object(reader, "get") as get, redirect_stdout(out), redirect_stderr(err):
                code = reader.main(["--profile-file", str(profile), "--identity-file", str(identity),
                                    "--room-id", "!other:example.test"])
            self.assertEqual(code, 1)
            get.assert_not_called()
            self.assertNotIn("secret", err.getvalue())

    def test_wait_for_cli_requires_sender(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            reader.main(["--profile-file", "/private/profile", "--identity-file", "/private/identity",
                         "--room-id", "!room:example.test", "--wait-for", "READY"])


if __name__ == "__main__":
    unittest.main()
