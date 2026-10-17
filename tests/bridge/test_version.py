import subprocess
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from bridge import _version_from_git


class BridgeVersionTests(unittest.TestCase):
    def test_exact_tag_uses_semver(self):
        with patch("bridge.subprocess.run", return_value=Mock(stdout="v0.2.9-0-g9bab606e\n")) as run:
            self.assertEqual(_version_from_git(Path("/release")), "0.2.9")
        self.assertEqual(run.call_args.args[0],
                         ["git", "-C", "/release", "describe", "--tags", "--long",
                          "--match", "v[0-9]*", "--abbrev=8", "HEAD"])

    def test_dev_checkout_includes_distance_and_commit(self):
        with patch("bridge.subprocess.run", return_value=Mock(stdout="v0.2.9-3-g7d3304f6\n")):
            self.assertEqual(_version_from_git(Path("/checkout")), "0.2.9-3-7d3304f6")

    def test_missing_or_unrecognized_git_metadata_is_honest(self):
        with patch("bridge.subprocess.run", side_effect=subprocess.CalledProcessError(128, "git")):
            self.assertEqual(_version_from_git(Path("/source")), "unknown")
        with patch("bridge.subprocess.run", return_value=Mock(stdout="unrecognized-tag\n")):
            self.assertEqual(_version_from_git(Path("/source")), "unknown")
