"""Validate the opt-in bell snippet without touching a user's tmux server."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


@unittest.skipUnless(shutil.which("tmux"), "tmux is not installed")
class TmuxBellConfigTests(unittest.TestCase):
    def test_minimal_config_on_an_isolated_server(self):
        config = Path(__file__).resolve().parents[1] / "examples" / "tmux-bells.conf"
        with tempfile.TemporaryDirectory(prefix="pi-chat-tmux-") as directory:
            command = [shutil.which("tmux"), "-S", str(Path(directory) / "socket")]
            env = {"PATH": os.defpath, "HOME": directory, "TERM": "xterm-256color"}

            def run(*args):
                return subprocess.run(command + list(args), env=env, check=True,
                                      capture_output=True, text=True, timeout=10).stdout.strip()

            try:
                run("-f", str(config), "new-session", "-d", "-s", "bell-check", "/bin/sleep 60")
                self.assertEqual(run("show-window-options", "-gv", "monitor-activity"), "off")
                self.assertEqual(run("show-window-options", "-gv", "monitor-bell"), "on")
                self.assertEqual(run("show-options", "-gv", "bell-action"), "other")
            finally:
                subprocess.run(command + ["kill-server"], env=env, capture_output=True, timeout=10)
