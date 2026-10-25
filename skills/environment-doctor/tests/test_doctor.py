"""Observable classification, bounded execution and credential boundaries."""

import contextlib
import importlib.util
import io
import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "doctor", Path(__file__).parents[1] / "scripts/doctor.py"
)
doctor = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(doctor)


def make_repo(base):
    """Create a committed git checkout with one tracked skill file."""
    repo = Path(base) / "repo"
    env = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    (repo / "skills").mkdir(parents=True)
    (repo / "skills" / "SKILL.md").write_text("---\nname: fixture\n---\n")
    subprocess.run(["git", "init", "-q", str(repo)], check=True, env=env)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True, env=env)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=fixture",
            "-c",
            "user.email=fixture@example.com",
            "commit",
            "-qm",
            "init",
        ],
        check=True,
        env=env,
    )
    return repo


class DoctorTests(unittest.TestCase):
    def test_timeout_kills_descendant_pipe_holder(self):
        before = time.monotonic()
        state, _ = doctor.run(
            [
                sys.executable,
                "-c",
                'import subprocess; subprocess.Popen(["sleep", "20"])',
            ],
            0.2,
        )
        self.assertEqual(state, "timeout")
        self.assertLess(time.monotonic() - before, 2)

    def test_output_is_capped_while_process_running(self):
        state, output = doctor.run(
            [sys.executable, "-c", 'import os;\nwhile True: os.write(1,b"x"*8192)'], 2
        )
        self.assertEqual(state, "output-limit")
        self.assertLessEqual(len(output), doctor.MAX_BYTES)

    def test_tool_errors_are_not_dumped(self):
        with (
            patch.object(doctor.shutil, "which", return_value="/tool"),
            patch.object(
                doctor, "run", return_value=("nonzero", b"access_token=PRIVATE")
            ),
        ):
            result = doctor.tool_check("git", "--version", 1)
        self.assertEqual(result["state"], "unknown")
        self.assertNotIn("PRIVATE", json.dumps(result))

    def test_actual_interpreter_and_distribution_ownership(self):
        result = doctor.python_check(sys.executable, 5)
        self.assertEqual(result["executable"], sys.executable)
        self.assertEqual(
            set(result["import_coverage"]),
            {item["state"] for item in result["imports"]},
        )
        self.assertTrue(all("distributions" in item for item in result["imports"]))

    def test_recursive_links_duplicates_and_alias_dedupe(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            skill = root / "original"
            skill.mkdir()
            (skill / "SKILL.md").write_text("---\nname: example\n---\n# Example")
            (root / "alias").symlink_to(skill, target_is_directory=True)
            self.assertEqual(doctor.skill_check(root)["skill_count"], 1)
            scripts = skill / "scripts"
            scripts.mkdir()
            (scripts / "send.py").symlink_to("removed.py")
            second = root / "other"
            second.mkdir()
            (second / "SKILL.md").write_text("---\nname: example\n---\n# Other")
            result = doctor.skill_check(root)
            self.assertEqual(result["state"], "failed")
            self.assertTrue(
                any(
                    path.endswith("/scripts/send.py") for path in result["broken_links"]
                )
            )
            self.assertEqual(result["duplicate_names"][0]["name"], "example")

    def test_nested_skill_roots_are_not_discovered_but_script_links_are_checked(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outer = root / "cron"
            outer.mkdir()
            (outer / "SKILL.md").write_text("---\nname: cron\n---\n# Owner cron")
            nested = outer / "managed"
            nested.mkdir()
            (nested / "SKILL.md").write_text("---\nname: cron\n---\n# Managed source")
            result = doctor.skill_check(root)
            self.assertEqual(result["state"], "ok")
            self.assertEqual(result["skill_count"], 1)
            self.assertEqual(result["duplicate_names"], [])
            (nested / "script.py").symlink_to("removed.py")
            result = doctor.skill_check(root)
            self.assertEqual(result["state"], "failed")
            self.assertTrue(
                any(
                    path.endswith("/managed/script.py")
                    for path in result["broken_links"]
                )
            )

    def test_readable_invalid_name_is_a_warning_not_unreadable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "db_postgres"
            package.mkdir()
            (package / "SKILL.md").write_text(
                "---\nname: db_postgres\n---\n# Compatibility"
            )
            result = doctor.skill_check(root)
            self.assertEqual(result["state"], "unknown")
            self.assertEqual(result["evidence"], "skill-name-warning")
            self.assertEqual(result["skill_count"], 1)
            self.assertEqual(
                result["name_warnings"][0]["codes"], ["invalid-name-characters"]
            )

    def test_stale_nested_collection_remains_a_real_collision(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for path in [root / "cron", root / "shared" / "cron"]:
                path.mkdir(parents=True)
                (path / "SKILL.md").write_text("---\nname: cron\n---\n# Cron")
            result = doctor.skill_check(root)
            self.assertEqual(result["state"], "failed")
            self.assertEqual(result["duplicate_names"][0]["name"], "cron")
            self.assertEqual(len(result["duplicate_names"][0]["paths"]), 2)

    def test_name_fallback_and_actual_validation_warnings(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fallback = root / "valid"
            fallback.mkdir()
            (fallback / "SKILL.md").write_text(
                "---\ndescription: Example\n---\n# Example"
            )
            self.assertEqual(doctor.skill_check(root)["state"], "ok")
            (fallback / "SKILL.md").write_text(
                '---\nname: "-bad--name-"\n---\n# Example'
            )
            result = doctor.skill_check(root)
            self.assertEqual(
                set(result["name_warnings"][0]["codes"]),
                {"name-edge-hyphen", "name-consecutive-hyphens"},
            )
            (fallback / "SKILL.md").write_text(
                "---\nname: [not, a, scalar]\n---\n# Example"
            )
            self.assertEqual(
                doctor.skill_check(root)["evidence"], "unreadable-skill-name"
            )

    def test_skill_cycle_and_scan_limit_are_not_clean(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "loop").symlink_to(root, target_is_directory=True)
            self.assertEqual(doctor.skill_check(root)["state"], "failed")
            (root / "loop").unlink()
            for number in range(5):
                (root / str(number)).write_text("x")
            with patch.object(doctor, "MAX_ENTRIES", 3):
                result = doctor.skill_check(root)
            self.assertEqual(result["state"], "unknown")
            self.assertEqual(result["evidence"], "scan-limit")

    def test_mcp_configured_not_connected_and_private_fields_filtered(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mcp.json"
            path.write_text(
                json.dumps(
                    {
                        "mcpServers": {
                            "active": {"env": {"TOKEN": "PRIVATE"}},
                            "off": {"disabled": True},
                        }
                    }
                )
            )
            result = doctor.mcp_check([path], None)
            self.assertEqual(result["state"], "unknown")
            self.assertEqual(
                {item["name"]: item["state"] for item in result["servers"]},
                {"active": "unknown", "off": "dormant"},
            )
            snapshot = Path(directory) / "snapshot.json"
            snapshot.write_text(
                json.dumps(
                    {
                        "servers": [
                            {
                                "name": "active",
                                "state": "connected",
                                "tools": ["a", "b"],
                                "transport": "PRIVATE",
                            }
                        ],
                        "errors": ["PRIVATE"],
                    }
                )
            )
            result = doctor.mcp_check([path], snapshot)
            self.assertEqual(result["servers"][0]["state"], "ok")
            self.assertEqual(result["state"], "unknown")
            self.assertNotIn("PRIVATE", json.dumps(result))
        self.assertEqual(doctor.mcp_check([], None)["state"], "unconfigured")

    def test_mcp_failure_takes_precedence_without_hiding_unknown_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "mcp.json"
            config.write_text(
                json.dumps({"mcpServers": {"observed": {}, "unobserved": {}}})
            )
            snapshot = root / "status.json"
            for reported, errors, expected_state, expected_exit in (
                ("failed", [], "failed", 1),
                ("error", ["PRIVATE"], "failed", 1),
                ("connected", ["PRIVATE"], "unknown", 0),
            ):
                with self.subTest(reported=reported, errors=bool(errors)):
                    snapshot.write_text(
                        json.dumps(
                            {
                                "servers": [{"name": "observed", "state": reported}],
                                "errors": errors,
                            }
                        )
                    )
                    output = io.StringIO()
                    with (
                        patch.object(
                            doctor,
                            "tool_check",
                            return_value={"check": "tool", "state": "ok"},
                        ),
                        patch.object(
                            doctor,
                            "python_check",
                            return_value={"check": "python", "state": "ok"},
                        ),
                        patch.object(
                            doctor,
                            "skill_check",
                            return_value={"check": "skills", "state": "ok"},
                        ),
                        contextlib.redirect_stdout(output),
                    ):
                        exit_code = doctor.main(
                            ["--mcp-config", str(config), "--mcp-status", str(snapshot)]
                        )
                    report = json.loads(output.getvalue())
                    result = next(
                        item for item in report["checks"] if item["check"] == "mcp"
                    )
                    self.assertEqual(result["state"], expected_state)
                    self.assertEqual(exit_code, expected_exit)
                    self.assertEqual(report["coverage"][expected_state], 1)
                    states = {item["name"]: item["state"] for item in result["servers"]}
                    self.assertEqual(states["unobserved"], "unknown")
                    self.assertEqual(
                        states["observed"],
                        "ok" if reported == "connected" else "failed",
                    )
                    self.assertEqual(
                        result["issues"], ["status-reports-errors"] if errors else []
                    )
                    self.assertEqual(result["snapshot_freshness"], "not-verified")
                    self.assertNotIn("PRIVATE", output.getvalue())

    def test_malformed_mcp_evidence_is_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "status.json"
            path.write_text('{"servers":[null]}')
            self.assertEqual(doctor.mcp_check([], path)["state"], "unknown")

    def test_service_rejects_credentials_and_query_without_request(self):
        with patch.object(doctor, "run") as run:
            for url in (
                "https://user:PRIVATE@example.test/",
                "https://@example.test/",
                "https://example.test/?token=PRIVATE",
                "https://example.test/?",
                "http://[invalid",
            ):
                result = doctor.service_check("example=" + url, 0.1)
                self.assertEqual(result["state"], "unknown")
                self.assertNotIn("PRIVATE", json.dumps(result))
            run.assert_not_called()

    def test_service_redirect_and_connection_errors_are_not_success(self):
        with patch.object(doctor, "run", return_value=("ok", b"302\n")):
            self.assertEqual(
                doctor.service_check("test=http://localhost/health", 0.1)["state"],
                "failed",
            )
        with patch.object(doctor, "run", return_value=("timeout", b"PRIVATE")):
            result = doctor.service_check("test=http://localhost/health", 0.1)
            self.assertEqual(result["state"], "failed")
            self.assertNotIn("PRIVATE", json.dumps(result))

    def test_real_readiness_get_does_not_follow_redirect_or_emit_body(self):
        calls = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                calls.append(self.path)
                self.send_response(302 if self.path == "/redirect" else 200)
                self.send_header("Location", "/must-not-follow")
                self.end_headers()
                self.wfile.write(b"PRIVATE")

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = "test=http://127.0.0.1:" + str(server.server_port)
            good = doctor.service_check(base + "/ready", 2)
            redirect = doctor.service_check(base + "/redirect", 2)
            self.assertEqual(good["state"], "ok")
            self.assertEqual(redirect["http_status"], 302)
            self.assertEqual(calls, ["/ready", "/redirect"])
            self.assertNotIn("PRIVATE", json.dumps([good, redirect]))
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_optional_cron_absence_and_output_allowlist(self):
        self.assertEqual(doctor.cron_check(None, None, 1)["state"], "unconfigured")
        with patch.object(
            doctor,
            "run",
            return_value=(
                "ok",
                b'{"owner":"test","matches":true,"source_jobs":7,"commands":"PRIVATE"}',
            ),
        ):
            result = doctor.cron_check(Path(__file__), None, 1)
        self.assertEqual(result["state"], "ok")
        self.assertEqual(result["source_jobs"], 7)
        self.assertNotIn("PRIVATE", json.dumps(result))

    def test_link_into_clean_checkout_is_tracked_and_unchanged(self):
        with tempfile.TemporaryDirectory() as base:
            repo = make_repo(base)
            link = Path(base) / "skill-link"
            link.symlink_to(Path("repo/skills/SKILL.md"))
            result = doctor.link_check([str(link)], repo, 5)
        entry = result["links"][0]
        self.assertEqual((result["state"], entry["state"]), ("ok", "ok"))
        self.assertEqual(entry["kind"], "relative")
        self.assertEqual(Path(result["checkout"]), repo)
        self.assertEqual(entry["repo_path"], "skills/SKILL.md")
        self.assertEqual((entry["tracked"], entry["modified"]), (True, False))
        self.assertEqual(entry["findings"], [])

    def test_targets_outside_the_one_checkout_are_confirmed_failures(self):
        with tempfile.TemporaryDirectory() as base:
            repo = make_repo(base)
            staging = Path(base) / "staging" / "x"
            staging.mkdir(parents=True)
            absolute = Path(base) / "staging-link"
            absolute.symlink_to(staging)
            release = Path(base) / "agent-skills-releases" / "v0.10.0" / "extensions"
            release.mkdir(parents=True)
            relative = Path(base) / "release-link"
            relative.symlink_to(Path("agent-skills-releases/v0.10.0/extensions"))
            result = doctor.link_check([str(absolute), str(relative)], repo, 5)
        self.assertEqual(result["state"], "failed")
        first, second = result["links"]
        self.assertEqual(first["findings"], ["absolute-target", "outside-checkout"])
        self.assertEqual(second["findings"], ["outside-checkout"])
        for entry in (first, second):
            self.assertEqual(
                (entry["state"], entry["evidence"]), ("failed", "outside-checkout")
            )
            self.assertEqual((entry["tracked"], entry["modified"]), (None, None))

    def test_dotdot_escape_is_not_contained(self):
        with tempfile.TemporaryDirectory() as base:
            repo = make_repo(base)
            (Path(base) / "outside.md").write_text("x\n")
            link = Path(base) / "escape"
            link.symlink_to(Path("repo/../outside.md"))
            result = doctor.link_check([str(link)], repo, 5)
        self.assertEqual(result["links"][0]["findings"], ["outside-checkout"])

    def test_modified_and_untracked_targets_are_reported(self):
        with tempfile.TemporaryDirectory() as base:
            repo = make_repo(base)
            (repo / "skills" / "SKILL.md").write_text("changed\n")
            (repo / "skills" / "extra.md").write_text("new\n")
            modified = Path(base) / "modified-link"
            modified.symlink_to(Path("repo/skills/SKILL.md"))
            untracked = Path(base) / "untracked-link"
            untracked.symlink_to(Path("repo/skills/extra.md"))
            result = doctor.link_check([str(modified), str(untracked)], repo, 5)
        findings = [entry["findings"] for entry in result["links"]]
        self.assertIn("modified-target", findings[0])
        self.assertIn("untracked-target", findings[1])
        self.assertEqual(result["state"], "ok")

    def test_broken_link_fails_and_non_link_is_unknown(self):
        with tempfile.TemporaryDirectory() as base:
            repo = make_repo(base)
            dangling = Path(base) / "dangling"
            dangling.symlink_to(Path(base) / "missing")
            plain = Path(base) / "plain"
            plain.write_text("not a link\n")
            result = doctor.link_check([str(dangling), str(plain)], repo, 5)
        self.assertEqual(
            (result["state"], result["links"][0]["state"]), ("failed", "failed")
        )
        self.assertEqual(result["links"][0]["evidence"], "broken-link")
        self.assertEqual(result["links"][1]["state"], "unknown")
        self.assertEqual(result["links"][1]["evidence"], "not-a-symlink")

    def test_missing_checkout_is_unknown_not_clean(self):
        with tempfile.TemporaryDirectory() as base:
            link = Path(base) / "skill-link"
            link.symlink_to(Path("repo/skills/SKILL.md"))
            absent = doctor.link_check([str(link)], Path(base) / "absent", 1)
            undeclared = doctor.link_check([str(link)], None, 1)
        self.assertEqual(
            (absent["state"], absent["evidence"]), ("unknown", "checkout-unavailable")
        )
        self.assertEqual(
            (undeclared["state"], undeclared["evidence"]),
            ("unknown", "no-checkout-declared"),
        )

    def test_git_failure_is_unknown_and_never_clean(self):
        with tempfile.TemporaryDirectory() as base:
            repo = make_repo(base)
            link = Path(base) / "skill-link"
            link.symlink_to(Path("repo/skills/SKILL.md"))
            with patch.object(doctor, "run", return_value=("nonzero", b"")):
                result = doctor.link_check([str(link)], repo, 1)
        entry = result["links"][0]
        self.assertEqual((result["state"], entry["state"]), ("unknown", "unknown"))
        self.assertIsNone(entry["tracked"])
        self.assertEqual(entry["evidence"], "git-ls-files-nonzero")
        self.assertEqual(entry["findings"], [])


if __name__ == "__main__":
    unittest.main()
