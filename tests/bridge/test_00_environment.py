"""Fail during full-suite discovery before restricted sockets can hang asyncio."""

import os
import socket
import unittest
from unittest.mock import MagicMock, patch


def check_local_sockets():
    try:
        sender, receiver = socket.socketpair()
        with sender, receiver:
            sender.setblocking(False)
            sender.send(b"\0")
    except OSError as error:
        environment = ("the OpenAI Codex sandbox" if
                       os.environ.get("CODEX_SANDBOX_NETWORK_DISABLED") == "1"
                       else "this restricted environment")
        raise SystemExit(
            f"Cannot run the bridge test suite in {environment}: local socket-pair "
            f"wakeup is unavailable ({error.strerror or type(error).__name__}). "
            "Asyncio needs it to wake the event loop after worker threads finish; "
            "tests can pass and then hang during executor shutdown. "
            "Run the suite outside the restricted sandbox, using Codex sandbox "
            "escalation when applicable. No external network access is needed "
            "for this check."
        ) from None


def load_tests(loader, tests, pattern):
    # Discovery sorts filenames, so this runs before loading the bridge tests.
    # SystemExit deliberately stops discovery; a failed TestCase would allow
    # later tests to continue into the same shutdown hang.
    check_local_sockets()
    return tests


class EnvironmentPreflightTests(unittest.TestCase):
    def test_codex_marker_alone_does_not_reject_working_sockets(self):
        sender, receiver = MagicMock(), MagicMock()
        with patch.dict(os.environ, {"CODEX_SANDBOX_NETWORK_DISABLED": "1"}), \
                patch("socket.socketpair", return_value=(sender, receiver)):
            check_local_sockets()
        sender.send.assert_called_once_with(b"\0")
        sender.__exit__.assert_called_once()
        receiver.__exit__.assert_called_once()

    def test_codex_socket_write_rejection_explains_shutdown_hang(self):
        sender, receiver = MagicMock(), MagicMock()
        sender.send.side_effect = PermissionError(1, "Operation not permitted")
        with patch.dict(os.environ, {"CODEX_SANDBOX_NETWORK_DISABLED": "1"}), \
                patch("socket.socketpair", return_value=(sender, receiver)), \
                self.assertRaises(SystemExit) as raised:
            check_local_sockets()
        self.assertIn("OpenAI Codex sandbox", str(raised.exception))
        self.assertIn("executor shutdown", str(raised.exception))
        self.assertIn("sandbox escalation", str(raised.exception))
        sender.__exit__.assert_called_once()
        receiver.__exit__.assert_called_once()

    def test_socket_creation_rejection_also_stops_discovery(self):
        with patch("socket.socketpair", side_effect=PermissionError(1, "Operation not permitted")), \
                self.assertRaises(SystemExit):
            load_tests(None, unittest.TestSuite(), None)

    def test_restriction_without_codex_marker_is_explained(self):
        with patch.dict(os.environ, {}, clear=True), \
                patch("socket.socketpair", side_effect=PermissionError(1, "Operation not permitted")), \
                self.assertRaises(SystemExit) as raised:
            check_local_sockets()
        self.assertIn("this restricted environment", str(raised.exception))
