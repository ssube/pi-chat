"""Live isolated-server tests; never connect to the user's tmux server."""
import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "tmux_control.py"
spec = importlib.util.spec_from_file_location("tmux_control", SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class ValidationTests(unittest.TestCase):
    def test_subcommand_help_expands_literal_percent(self):
        import contextlib
        import io
        for action in ["list", "read", "send"]:
            output = io.StringIO()
            with self.subTest(action=action), contextlib.redirect_stdout(output):
                with self.assertRaises(SystemExit) as exc:
                    module.main([action, "--help"])
                self.assertEqual(exc.exception.code, 0)
                if action != "list":
                    self.assertIn("%3", output.getvalue())
                    self.assertNotIn("%%3", output.getvalue())

    def test_rejects_ambiguous_target_before_tmux(self):
        for target in ["work:0", "0", "", "%1;", "%-1"]:
            with self.subTest(target=target), patch.object(module.subprocess, "run") as run:
                with self.assertRaises(SystemExit) as exc:
                    module.main(["send", "--target", target, "--text", "hello"])
                self.assertEqual(exc.exception.code, 2)
                run.assert_not_called()

    def test_rejects_control_characters_even_without_enter(self):
        for text in ["one\ntwo", "one\r", "\x1b", "\x00", "\x7f", "\x85", "\u2028", "\t"]:
            with self.subTest(text=repr(text)), patch.object(module.subprocess, "run") as run:
                with self.assertRaises(SystemExit):
                    module.main(["send", "--target", "%0", "--text", text, "--no-enter"])
                run.assert_not_called()

    def test_missing_tmux(self):
        with patch.object(module.subprocess, "run", side_effect=FileNotFoundError):
            self.assertEqual(module.main(["list"]), 1)

    def test_failed_paste_cleans_buffer_without_enter_or_input_echo(self):
        import contextlib
        import io
        calls = []

        def run(command, **kwargs):
            calls.append(command)
            if "display-message" in command:
                return subprocess.CompletedProcess(command, 0, "0:0\n", "")
            if "paste-buffer" in command:
                raise subprocess.CalledProcessError(1, command, stderr="fixture failure")
            if "list-buffers" in command:
                return subprocess.CompletedProcess(command, 0, "agent-tmux-fixed\n", "")
            return subprocess.CompletedProcess(command, 0, "", "")

        stderr = io.StringIO()
        with patch.object(module.subprocess, "run", side_effect=run), \
                patch.object(module.uuid, "uuid4") as identifier, contextlib.redirect_stderr(stderr):
            identifier.return_value.hex = "fixed"
            self.assertEqual(module.main(["send", "--target", "%0", "--text", "private-fixture"]), 1)
        self.assertTrue(any("delete-buffer" in c for c in calls))
        self.assertFalse(any("send-keys" in c for c in calls))
        self.assertNotIn("private-fixture", stderr.getvalue())

    def test_invalid_history(self):
        for history in ["-1", "10001"]:
            with patch.object(module.subprocess, "run") as run:
                with self.assertRaises(SystemExit):
                    module.main(["read", "--target", "%0", "--history", history])
                run.assert_not_called()


@unittest.skipUnless(shutil.which("tmux"), "tmux not installed")
class LiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="tmux-skill-test-")
        self.addCleanup(self.temp.cleanup)
        self.env = {"PATH": os.defpath, "HOME": self.temp.name, "TERM": "xterm-256color"}
        # -S creates a server unrelated to the user's desktop or default socket.
        self.socket = str(Path(self.temp.name) / "server.sock")
        self.base = ["tmux", "-S", self.socket]
        self.addCleanup(lambda: subprocess.run(self.base + ["kill-server"],
                                              capture_output=True, env=self.env))
        receiver = Path(self.temp.name) / "receiver.py"
        receiver.write_text(
            "import sys,json\nprint('READY', flush=True)\n"
            "for line in sys.stdin:\n print('GOT:'+json.dumps(line.rstrip('\\n'),ensure_ascii=False),flush=True)\n",
            encoding="utf-8",
        )
        command = shlex.join([sys.executable, "-u", str(receiver)])
        self.tmux("-f", "/dev/null", "new-session", "-d", "-s", "fixture",
                  "-x", "160", "-y", "24", command)
        self.pane = self.tmux("list-panes", "-F", "#{pane_id}").strip()
        self.wait_for("READY")

    def tmux(self, *args):
        return subprocess.run(self.base + list(args), capture_output=True, text=True,
                              env=self.env, check=True, timeout=10).stdout

    def cli(self, *args, input=None, ok=True):
        result = subprocess.run([sys.executable, str(SCRIPT), "--socket-path", self.socket, *args],
                                input=input, capture_output=True, text=True, env=self.env, timeout=10)
        if ok:
            self.assertEqual(result.returncode, 0, result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0)
        return result

    def read(self):
        return self.cli("read", "--target", self.pane).stdout

    def wait_for(self, text):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            screen = self.read()
            if text in screen:
                return screen
            time.sleep(.02)
        self.fail(f"Expected fixture output {text!r}; screen={screen!r}")

    def test_list_and_read(self):
        listing = self.cli("list").stdout
        self.assertIn(self.pane + "\tfixture:0.0\t", listing)
        self.assertIn("READY", self.read())

    def test_literal_text_and_default_enter(self):
        text = "C-c Enter quotes ' \" $HOME $(not-a-command);"
        self.cli("send", "--target", self.pane, "--text", text)
        self.wait_for("GOT:" + json.dumps(text))

    def test_stdin_unicode_and_no_enter_then_enter(self):
        text = "café 世界;"
        self.cli("send", "--target", self.pane, "--stdin", "--no-enter", input=text)
        self.wait_for(text)
        self.assertNotIn("GOT:", self.read())
        self.cli("send", "--target", self.pane, "--text", "")
        self.wait_for("GOT:" + json.dumps(text, ensure_ascii=False))

    def test_semicolon_alone_and_existing_buffer_preserved(self):
        self.tmux("set-buffer", "-b", "existing", "keep-me")
        self.cli("send", "--target", self.pane, "--text", ";")
        self.wait_for('GOT:";"')
        self.assertEqual(self.tmux("show-buffer", "-b", "existing"), "keep-me")
        self.assertEqual(self.tmux("list-buffers", "-F", "#{buffer_name}").strip(), "existing")

    def test_target_only_one_pane(self):
        self.tmux("split-window", "-d", "-t", self.pane, "cat")
        panes = self.tmux("list-panes", "-F", "#{pane_id}").splitlines()
        other = next(p for p in panes if p != self.pane)
        self.cli("send", "--target", self.pane, "--text", "only-this-pane")
        self.wait_for('GOT:"only-this-pane"')
        self.assertNotIn("only-this-pane", self.cli("read", "--target", other).stdout)

    def test_copy_mode_refused(self):
        self.tmux("copy-mode", "-t", self.pane)
        self.cli("send", "--target", self.pane, "--text", "no-send", ok=False)
        self.assertNotIn("no-send", self.read())

    def test_missing_pane_and_server(self):
        self.cli("read", "--target", "%999999", ok=False)
        self.cli("send", "--target", "%999999", "--text", "private-input", ok=False)
        self.tmux("kill-server")
        result = self.cli("list", ok=False)
        self.assertIn("tmux operation failed", result.stderr)

    def test_alternate_screen_and_history(self):
        # Send a fixture instruction consumed by a new process, not a user's shell.
        alt = "import sys,time; sys.stdout.write('\\x1b[?1049hALT-SCREEN'); sys.stdout.flush(); time.sleep(30)"
        self.tmux("respawn-pane", "-k", "-t", self.pane,
                  shlex.join([sys.executable, "-u", "-c", alt]))
        self.wait_for("ALT-SCREEN")
        self.cli("read", "--target", self.pane, "--history", "10", ok=False)

    def test_history(self):
        for i in range(35):
            self.cli("send", "--target", self.pane, "--text", f"row-{i}")
        self.wait_for('GOT:"row-34"')
        self.assertNotIn('GOT:"row-0"', self.read())
        self.assertIn('GOT:"row-0"', self.cli("read", "--target", self.pane, "--history", "100").stdout)

    def test_newline_stdin_rejected(self):
        self.cli("send", "--target", self.pane, "--stdin", input="do-not-send\n", ok=False)
        self.assertNotIn("do-not-send", self.read())


if __name__ == "__main__":
    unittest.main()
