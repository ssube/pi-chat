import json
import os
import tempfile
import unittest
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from tools.ledger.error_cli import daily_brief, error_suggestions, main as error_main
from tools.ledger.error_ledger import (_parse_since, approved_fixes, duration_stats,
                                  fix_content_sha256, ingest_one, ingest_outbox,
                                  slowest_calls, validate_occurrence,
                                  validate_skill_event)


class Cursor:
    rowcount = 1

    def __init__(self):
        self.statements = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, sql, params=None):
        self.statements.append((sql, params))


class Connection:
    def __init__(self):
        self.last_cursor = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def transaction(self):
        return nullcontext()

    def cursor(self):
        self.last_cursor = Cursor()
        return self.last_cursor


class FixCursor(Cursor):
    def __init__(self, rows):
        super().__init__()
        self.rows = rows

    def fetchall(self):
        return self.rows


class FixConnection(Connection):
    def __init__(self, rows):
        super().__init__()
        self.rows = rows

    def cursor(self):
        return FixCursor(self.rows)


class RecordingConnection(Connection):
    """Connection whose cursor returns no rows but records statements."""

    def cursor(self):
        self.last_cursor = FixCursor([])
        return self.last_cursor


def sample():
    return {
        "id": "pierr-123e4567-e89b-12d3-a456-426614174000",
        "agent": "agent_one", "origin": "bridge_matrix",
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "kind": "tool", "outcome": "error", "tool_name": "bash",
        "signature": "bash: command failed", "detail_ref": "/tmp/detail.json",
        "detail_sha256": "a" * 64, "redacted_summary": "Command failed [REDACTED]",
    }


class ErrorLedgerTests(unittest.TestCase):
    def test_contract_rejects_unsafe_shapes(self):
        value = sample()
        self.assertEqual(validate_occurrence(value)["redacted_command"], None)
        self.assertEqual(validate_occurrence({**value, "redacted_command": "exit 1"})["redacted_command"], "exit 1")
        self.assertEqual(validate_occurrence(value)["duration_ms"], None)
        self.assertEqual(validate_occurrence({**value, "duration_ms": 0})["duration_ms"], 0)
        self.assertEqual(validate_occurrence({**value, "duration_ms": 1250})["duration_ms"], 1250)
        for field, bad in (("origin", "bridge Matrix"), ("occurred_at", "2026-10-01"),
                           ("detail_ref", "relative/file"), ("detail_sha256", "x"),
                           ("redacted_command", "x" * 2049),
                           ("duration_ms", -1), ("duration_ms", True),
                           ("duration_ms", 12.5), ("duration_ms", "1250"),
                           ("duration_ms", 2**31),
                           ("signature", "x" * 301)):
            with self.subTest(field=field):
                item = {**value, field: bad}
                with self.assertRaises(ValueError):
                    validate_occurrence(item)

    def test_ingest_commits_before_ack_and_only_inserts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            value = {**sample(), "duration_ms": 1250}
            queued = root / (value["id"] + ".json")
            queued.write_text(json.dumps(value))
            os.chmod(queued, 0o600)
            connection = Connection()
            self.assertTrue(ingest_one(connection, queued, root / "processed"))
            self.assertFalse(queued.exists())
            self.assertTrue((root / "processed" / queued.name).exists())
            self.assertEqual(len(connection.last_cursor.statements), 1)
            sql, params = connection.last_cursor.statements[0]
            self.assertIn("ON CONFLICT (id) DO NOTHING", sql)
            self.assertEqual(params["duration_ms"], 1250)

    def test_ingest_links_suggested_fix_to_occurrence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            value = {**sample(), "matched_fix_ids": ["bash-empty-exit-1"]}
            queued = root / (value["id"] + ".json")
            queued.write_text(json.dumps(value))
            os.chmod(queued, 0o600)
            connection = Connection()
            self.assertTrue(ingest_one(connection, queued, root / "processed"))
            self.assertEqual(len(connection.last_cursor.statements), 2)
            self.assertIsNone(connection.last_cursor.statements[0][1]["duration_ms"])
            sql, params = connection.last_cursor.statements[1]
            self.assertIn("pi_tool_call_suggestion", sql)
            self.assertEqual(params, (value["id"], "bash-empty-exit-1"))

    def test_ingest_indexes_redacted_command_and_audits_dry_run_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            value = {**sample(), "redacted_command": "ls ./present ./missing 2>/dev/null",
                     "redacted_summary": "Command exited with code 2", "exit_code": 2,
                     "candidate_matches": [{"fix_id": "ls-exit-code-two-partial-missing",
                                            "score": 82, "matcher_version": 1,
                                            "disposition": "conditional_hint",
                                            "shown_to_user": True}]}
            queued = root / (value["id"] + ".json")
            queued.write_text(json.dumps(value))
            os.chmod(queued, 0o600)
            connection = Connection()
            self.assertTrue(ingest_one(connection, queued, root / "processed"))
            statements = connection.last_cursor.statements
            self.assertTrue(any("pi_command_parse" in sql for sql, _ in statements))
            self.assertTrue(any("pi_command_step" in sql for sql, _ in statements))
            self.assertTrue(any("pi_match_trial" in sql for sql, _ in statements))
            self.assertNotIn("./present", str(statements[1:]))

    def test_candidate_audit_rejects_unbounded_or_malformed_data(self):
        value = sample()
        for candidate in ([{"fix_id": "x", "score": 101, "matcher_version": 1,
                            "disposition": "conditional_hint", "shown_to_user": True}],
                          [{}], "not a list"):
            with self.subTest(candidate=candidate), self.assertRaises(ValueError):
                validate_occurrence({**value, "candidate_matches": candidate})

    def test_skill_event_metadata_rejects_context_and_invalid_shapes(self):
        value = {"id": "piskill-123e4567-e89b-12d3-a456-426614174000",
                 "agent": "agent_one", "origin": "interactive",
                 "occurred_at": "2026-10-03T04:00:00Z", "skill_name": "matrix-read",
                 "event_kind": "loaded", "source": "read"}
        self.assertEqual(validate_skill_event(value)["skill_name"], "matrix-read")
        for change in ({"path": "/private/skill/SKILL.md"}, {"prompt": "private"},
                       {"session_id": "private"}, {"skill_name": "../private"},
                       {"skill_name": "x" * 65}, {"provider": "test", "model": "test-model", "agent": "other"},
                       {"origin": "bridge matrix"}, {"occurred_at": "2026-10-03"},
                       {"event_kind": "invoked"}, {"id": "piskill-not-a-uuid"}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_skill_event({**value, **change})
        self.assertEqual(validate_skill_event({**value, "event_kind": "invoked",
                                               "source": "skill_command"})["source"], "skill_command")

    def test_skill_event_ingests_in_distinct_table_before_ack(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            value = {"id": "piskill-123e4567-e89b-12d3-a456-426614174000",
                     "agent": "agent_three", "origin": "bridge_matrix",
                     "occurred_at": "2026-10-03T04:00:00Z", "skill_name": "cron",
                     "event_kind": "invoked", "source": "skill_command"}
            queued = root / (value["id"] + ".json")
            queued.write_text(json.dumps(value))
            os.chmod(queued, 0o600)
            connection = Connection()
            self.assertTrue(ingest_one(connection, queued, root / "processed"))
            self.assertFalse(queued.exists())
            self.assertTrue((root / "processed" / queued.name).exists())
            self.assertEqual(len(connection.last_cursor.statements), 1)
            sql, params = connection.last_cursor.statements[0]
            self.assertIn("INSERT INTO pi_tool_call.pi_skill_event", sql)
            self.assertNotIn("pi_tool_call_occurrence", sql)
            self.assertEqual(params["skill_name"], "cron")

    def test_skill_event_db_failure_preserves_outbox(self):
        class FailingCursor(Cursor):
            def execute(self, *_):
                raise RuntimeError("database unavailable")

        class FailingConnection(Connection):
            def cursor(self):
                return FailingCursor()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            value = {"id": "piskill-123e4567-e89b-12d3-a456-426614174000",
                     "agent": "agent_two", "origin": "bridge_matrix",
                     "occurred_at": "2026-10-03T04:00:00Z", "skill_name": "cron",
                     "event_kind": "loaded", "source": "read"}
            queued = root / (value["id"] + ".json")
            queued.write_text(json.dumps(value))
            os.chmod(queued, 0o600)
            with self.assertRaisesRegex(RuntimeError, "database unavailable"):
                ingest_one(FailingConnection(), queued, root / "processed")
            self.assertTrue(queued.exists())
            self.assertFalse((root / "processed").exists())

    def test_bad_skill_record_is_quarantined_without_losing_tool_telemetry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tool = {**sample(), "id": "pitool-123e4567-e89b-12d3-a456-426614174000"}
            tool_path = root / (tool["id"] + ".json")
            tool_path.write_text(json.dumps(tool))
            os.chmod(tool_path, 0o600)
            bad = root / "piskill-123e4567-e89b-12d3-a456-426614174000.json"
            bad.write_text('{"skill_name": "private/incomplete"}')
            os.chmod(bad, 0o600)
            with self.assertLogs("tools.ledger.error_ledger", "WARNING") as logs:
                inserted, acknowledged = ingest_outbox(Connection(), root, root / "processed")
            self.assertEqual((inserted, acknowledged), (1, 1))
            self.assertTrue((root / "processed" / tool_path.name).exists())
            rejected = root / "rejected" / bad.name
            self.assertEqual(rejected.read_text(), '{"skill_name": "private/incomplete"}')
            self.assertEqual((root / "rejected").stat().st_mode & 0o777, 0o700)
            self.assertEqual(rejected.stat().st_mode & 0o777, 0o600)
            self.assertNotIn("private/incomplete", " ".join(logs.output))

    def test_skill_grant_failure_does_not_hold_tool_occurrences(self):
        class FailingSkillCursor(Cursor):
            def execute(self, sql, params=None):
                if "INSERT INTO pi_tool_call.pi_skill_event" in sql:
                    raise RuntimeError("missing skill grant")
                super().execute(sql, params)

        class FailingSkillConnection(Connection):
            def cursor(self):
                return FailingSkillCursor()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tool = {**sample(), "id": "pitool-123e4567-e89b-12d3-a456-426614174000"}
            skill = {"id": "piskill-123e4567-e89b-12d3-a456-426614174000",
                     "agent": "agent_one", "origin": "interactive",
                     "occurred_at": "2026-10-03T04:00:00Z", "skill_name": "cron",
                     "event_kind": "loaded", "source": "read"}
            for value in (tool, skill):
                path = root / (value["id"] + ".json")
                path.write_text(json.dumps(value))
                os.chmod(path, 0o600)
            with self.assertRaisesRegex(RuntimeError, "missing skill grant"):
                ingest_outbox(FailingSkillConnection(), root, root / "processed")
            self.assertTrue((root / "processed" / (tool["id"] + ".json")).exists())
            self.assertTrue((root / (skill["id"] + ".json")).exists())


    def test_daily_brief_stays_within_size_limit_and_prioritizes_trends(self):
        top = [("a" * 64, None, None, None, None, None, 5)] * 3
        examples = [(f"pierr-{i:08x}-0000-0000-0000-000000000000", "agent_one", None)
                    for i in range(20)]
        brief = daily_brief(20, top, examples)
        self.assertLessEqual(len(brief), 300)
        self.assertIn("20 unreviewed Pi errors", brief)
        self.assertIn("Trend: 5 pending", brief)
        self.assertIn("Instance: agent_one:pierr-", brief)

    def test_slowest_calls_filters_window_and_agent_and_clamps_limit(self):
        connection = RecordingConnection()
        slowest_calls(connection, since="7 days", agent="agent_two", limit=5000)
        sql, params = connection.last_cursor.statements[0]
        self.assertIn("duration_ms IS NOT NULL", sql)
        self.assertIn("agent = %s", sql)
        self.assertIn("ORDER BY duration_ms DESC", sql)
        self.assertIsInstance(params[0], datetime)
        self.assertEqual(params[1:], ["agent_two", 500])

    def test_slowest_calls_without_agent_keeps_default_window_and_limit(self):
        connection = RecordingConnection()
        slowest_calls(connection)
        sql, params = connection.last_cursor.statements[0]
        self.assertNotIn("agent = %s", sql)
        self.assertEqual(params[1:], [15])

    def test_duration_stats_groups_by_agent_and_tool(self):
        connection = RecordingConnection()
        duration_stats(connection, since="24 hours", agent="agent_one")
        sql, params = connection.last_cursor.statements[0]
        self.assertIn("percentile_cont(0.95)", sql)
        self.assertIn("GROUP BY agent, tool_name", sql)
        self.assertIn("agent = %s", sql)
        self.assertEqual(len(params), 2)

    def test_parse_since_rejects_unknown_windows(self):
        for bad in ("soon", "7 moons", "-3 days", "0x8 hours", ""):
            with self.assertRaises(ValueError):
                _parse_since(bad)
        self.assertIsInstance(_parse_since("7 days"), datetime)

    def test_errors_command_passes_documented_filters(self):
        seen = {}

        def fake_errors(conn, since=None, agent_for_errors=None,
                        unresolved_only=False):
            seen.update(since=since, agent_for_errors=agent_for_errors,
                        unresolved_only=unresolved_only)
            return iter(())

        with patch("tools.ledger.error_cli.connect", return_value=Connection()), \
             patch("tools.ledger.error_cli.recent_errors", side_effect=fake_errors):
            self.assertEqual(error_main(["errors", "--since", "3 days",
                                         "--agent", "agent_three",
                                         "--unresolved-only"]), 0)
        self.assertEqual(seen, {"since": "3 days", "agent_for_errors": "agent_three",
                                "unresolved_only": True})

    def test_successes_and_slowest_commands_pass_windows(self):
        with patch("tools.ledger.error_cli.connect", return_value=Connection()), \
             patch("tools.ledger.error_cli.recent_successes", return_value=iter(())) as successes:
            self.assertEqual(error_main(["successes", "--since", "12 hours"]), 0)
            successes.assert_called_once()
            self.assertEqual(successes.call_args.kwargs["since"], "12 hours")
        with patch("tools.ledger.error_cli.connect", return_value=Connection()), \
             patch("tools.ledger.error_cli.slowest_calls", return_value=iter(())) as slowest:
            self.assertEqual(error_main(["slowest", "--agent", "agent_two",
                                         "--limit", "5"]), 0)
            self.assertEqual(slowest.call_args.kwargs,
                             {"since": None, "agent": "agent_two", "limit": 5})
        with patch("tools.ledger.error_cli.connect", return_value=Connection()), \
             patch("tools.ledger.error_cli.duration_stats", return_value=iter(())) as stats:
            self.assertEqual(error_main(["duration-stats", "--since", "2 days"]), 0)
            self.assertEqual(stats.call_args.kwargs,
                             {"since": "2 days", "agent": None})



    def test_error_lookup_rejects_missing_ambiguous_and_invalid_references(self):
        for rows, message in [([], "awaiting ingestion"),
                              ([("pierr-123456a", "agent_one"), ("pierr-123456b", "agent_one")],
                               "Ambiguous")]:
            with self.assertRaisesRegex(ValueError, message):
                error_suggestions(FixConnection(rows), "err-123456", set())
        with self.assertRaisesRegex(ValueError, "Invalid"):
            error_suggestions(FixConnection([]), "err-%", set())

    def test_error_lookup_returns_only_attached_applicable_verified_fixes(self):
        class LookupConnection(Connection):
            def __init__(self):
                self.results = iter([[("pierr-123456ab", "agent_one")], [("fix-1",)]])
                self.queries = []

            def cursor(self):
                cursor = FixCursor(next(self.results))
                self.queries.append(cursor)
                return cursor

        connection = LookupConnection()
        with patch("tools.ledger.error_cli.approved_fixes",
                   return_value=[{"id": "fix-1", "fix": "retry"},
                                 {"id": "unrelated", "fix": "do not return"}]) as fixes:
            result = error_suggestions(connection, "err-123456", {"cron"})
        self.assertEqual(result, {"id": "pierr-123456ab",
                                  "fixes": [{"id": "fix-1", "fix": "retry"}]})
        fixes.assert_called_once_with(connection, "agent_one", {"cron"})
        self.assertEqual(connection.queries[0].statements[0][1], ("pierr-123456%",))

    def test_error_reference_cli_is_read_only(self):
        with patch("tools.ledger.error_cli.connect", return_value=Connection()) as connect_mock, \
             patch("tools.ledger.error_cli.error_suggestions",
                   return_value={"id": "pierr-123456", "fixes": []}) as lookup, \
             patch.dict(os.environ, {"PI_CAPTURE_ENABLED_SKILLS": "cron"}):
            self.assertEqual(error_main(["err-123456"]), 0)
        connect_mock.assert_called_once_with(read_only=True)
        self.assertEqual(lookup.call_args.args[1:], ("err-123456", {"cron"}))

    def test_export_counts_error_rows_once_across_signature_matches_and_evidence(self):
        connection = RecordingConnection()
        self.assertEqual(approved_fixes(connection, "agent_one", set()), [])
        sql = connection.last_cursor.statements[0][0]
        self.assertIn("e.outcome = 'error'", sql)
        self.assertIn("e.signature = f.signature", sql)
        self.assertIn("s.fix_id = f.id AND s.occurrence_id = e.id", sql)
        self.assertIn("link.fix_id = f.id AND link.occurrence_id = e.id", sql)
        # EXISTS predicates count each occurrence row once, even if agent_twol
        # evidence sources link it; successes never contribute.
        self.assertEqual(sql.count("EXISTS"), 2)

    def test_fix_digest_is_order_independent_and_export_fails_on_drift(self):
        digest = fix_content_sha256("timeout", "retry after 30s", ["agent_three", "agent_one"],
                                    ["agent_one", "agent_three"], ["cron", "skill-creator"])
        self.assertEqual(digest, fix_content_sha256("timeout", "retry after 30s",
                         ["agent_one", "agent_three"], ["agent_three", "agent_one"],
                         ["skill-creator", "cron"]))
        row = ("fix-1", "timeout", "retry after 30s", digest,
               ["agent_one", "agent_three"], ["agent_one", "agent_three"], ["cron", "skill-creator"],
               "timeout-signature", 12)
        exported = approved_fixes(FixConnection([row]), "agent_one", {"cron", "skill-creator"})
        self.assertEqual(exported[0]["content_sha256"], digest)
        self.assertEqual(exported[0]["fix"], "retry after 30s")
        self.assertEqual(exported[0]["seen_count"], 12)
        self.assertEqual(exported[0]["signature"], "timeout-signature")
        with self.assertRaisesRegex(RuntimeError, "fresh approval required"):
            approved_fixes(FixConnection([row[:2] + ("injected text",) + row[3:]]),
                           "agent_one", {"cron", "skill-creator"})


if __name__ == "__main__":
    unittest.main()
