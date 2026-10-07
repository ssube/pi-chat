"""Regression for issue #39: bridge.send_cli must not exist or be suggested."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bridge.config import Profile
from bridge.runtime.state import SessionKey


class SendCliRegressionTests(unittest.TestCase):
    """The bridge has reaction, history, activity, and sessions helpers, but no
    outbound-message helper.  The final reply is sent by the bridge itself."""

    def test_send_cli_module_does_not_exist(self):
        """Importing bridge.send_cli fails, matching the original error."""
        with self.assertRaises(ModuleNotFoundError):
            import bridge.send_cli  # type: ignore # noqa: F401

    def test_matrix_prompt_rejects_send_cli(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profile.json"
            base = {
                "provider": "test", "model": "test-model",
                "agent": "agent_three",
                "work_dir": directory,
                "state_dir": directory,
                "pi_executable": sys.executable,
                "pi_agent_dir": directory,
                "pi_path_prefix": [directory],
                "pi_extra_args": [],
                "matrix": {
                    "homeserver": "https://example.test",
                    "user_id": "@agent_three:example.test",
                    "token_env": "TEST_MATRIX_TOKEN",
                    "rooms": ["!room:example.test"],
                },
            }
            path.write_text(json.dumps(base))
            with patch.dict(os.environ, {
                "UNUSED_ROOM_ID": "!shared:example.test",
                "TEST_MATRIX_TOKEN": "transport-secret",
            }):
                profile = Profile.load(path)
                profile.pi_client(
                    SessionKey("agent_three", "matrix", "room"),
                    Path(directory) / "conversation.jsonl",
                )
                prompt = (Path(directory) / "bridge-system.txt").read_text()
                self.assertIn("do not invoke a `bridge.send_cli`", prompt)
                self.assertIn("there is no such module", prompt)
                self.assertIn("Return your reply as the final message", prompt)


if __name__ == "__main__":
    unittest.main()
