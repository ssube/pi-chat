import json
import os
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

from tools.ledger.error_cli import main
from tools.ledger.error_ledger import fix_content_sha256, publish_fix
from tools.ledger.fix_authoring import (approved_fix, check_fix, corroborate_fix, draft_fix,
                                  fix_evidence, read_private_document, resolve_evidence,
                                  write_private_document)

ERROR = "pierr-4b8ecad5-7df0-4119-8c7b-704b00594a17"
SUCCESS = "pitool-123e4567-e89b-12d3-a456-426614174000"


def proposal():
    return dict(id="bash-glob-no-match-exit-2", title="Optional path is absent",
                signature="a" * 64, pattern="ls: cannot access.*No such file",
                fix="Verify whether that specific path is optional before continuing.",
                owners=["agent_one"], applies_to_agents=["agent_one"], required_skills=[],
                source="owner-reviewed portable example", occurrence_ids=[ERROR])


def digest(value):
    return fix_content_sha256(value["pattern"], value["fix"], value["owners"],
                             value["applies_to_agents"], value["required_skills"])


def catalog_row(value):
    return (value["title"], value["signature"], value["pattern"], value["fix"],
            value["source"], digest(value), value["owners"], value["applies_to_agents"],
            value["required_skills"])


class Cursor:
    def __init__(self, connection):
        self.connection = connection
        self.rows = []
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def execute(self, sql, params=None):
        self.connection.statements.append((sql, params))
        if sql.lstrip().startswith("SELECT"):
            self.rows = next(self.connection.results)
        elif "INSERT INTO pi_tool_call.pi_tool_call_occurrence_fix" in sql:
            self.rowcount = next(self.connection.inserts)

    def fetchall(self):
        return self.rows


class Connection:
    def __init__(self, results=(), inserts=(1,)):
        self.results = iter(results)
        self.inserts = iter(inserts)
        self.statements = []
        self.transactions = 0

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def cursor(self):
        return Cursor(self)

    def transaction(self):
        self.transactions += 1
        return nullcontext()


class FixAuthoringTests(unittest.TestCase):
    def test_new_draft_is_private_unapproved_and_does_not_guess_from_exit_code(self):
        connection = Connection([[ (ERROR, "agent_one", "a" * 64, "error") ], []])
        value = draft_fix(connection, "new-fix", ["err-4b8ecad5"])
        self.assertEqual(value["signature"], "a" * 64)
        self.assertEqual(value["occurrence_ids"], [ERROR])
        self.assertEqual(value["pattern"], "")
        self.assertEqual(value["fix"], "")
        self.assertEqual(value["applies_to_agents"], [])
        self.assertNotIn("approved_by", value)
        self.assertTrue(all(sql.lstrip().startswith("SELECT") for sql, _ in connection.statements))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "proposal.json"
            write_private_document(path, value)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(read_private_document(path), value)
            with self.assertRaises(FileExistsError):
                write_private_document(path, {"overwrite": True})

    def test_private_reader_rejects_public_files_symlinks_nonobjects_and_oversize(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "proposal.json"
            path.write_text("{}")
            path.chmod(0o644)
            with self.assertRaises(ValueError):
                read_private_document(path)
            path.chmod(0o600)
            link = Path(directory) / "link"
            link.symlink_to(path)
            with self.assertRaises(OSError):
                read_private_document(link)
            for contents in ("[]", " " * 65537):
                path.write_text(contents)
                with self.assertRaises(ValueError):
                    read_private_document(path)
            fifo = Path(directory) / "fifo"
            os.mkfifo(fifo, 0o600)
            with self.assertRaises(ValueError):
                read_private_document(fifo)

    def test_reference_resolution_rejects_missing_ambiguous_or_success_draft(self):
        cases = [([], "await ingestion"),
                 ([(ERROR, "agent_one", "sig", "error")] * 2, "Ambiguous"),
                 ([(SUCCESS, "agent_one", "sig", "success")], "error occurrences")]
        for rows, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                resolve_evidence(Connection([rows]), ["err-4b8ecad5"], errors_only=True)
        for references in ([], ["err-%"], ["err-4b8eca"] * 17):
            with self.assertRaises(ValueError):
                resolve_evidence(Connection(), references)

    def test_existing_draft_keeps_scope_and_pins_reviewed_content(self):
        original = proposal()
        connection = Connection([[(ERROR, "agent_three", "other-signature", "error")],
                                 [catalog_row(original)]])
        value = draft_fix(connection, original["id"], ["err-4b8ecad5"])
        self.assertEqual(value["applies_to_agents"], ["agent_one"])
        self.assertEqual(value["owners"], ["agent_one"])
        self.assertEqual(value["base_content_sha256"], digest(original))
        self.assertNotIn("approved_by", value)

    def test_proposal_check_is_read_only_and_approval_is_still_required_to_publish(self):
        value = proposal()
        connection = Connection([[(ERROR, "agent_one", value["signature"], "error")]])
        self.assertEqual(check_fix(connection, value)["id"], value["id"])
        self.assertTrue(all(sql.lstrip().startswith("SELECT") for sql, _ in connection.statements))
        with self.assertRaisesRegex(ValueError, "missing required"):
            publish_fix(Connection(), value)

    def test_validation_fails_before_writes_on_malformed_or_private_documents(self):
        for change in ({"id": 7}, {"owners": [{}]}, {"required_skills": [7]},
                       {"approved_by": []}, {"pattern": "["}, {"fix": "password=abcdefghijk"},
                       {"occurrence_ids": ["err-4b8ecad5"]}):
            connection = Connection()
            with self.subTest(change=change), self.assertRaises(ValueError):
                publish_fix(connection, {**proposal(), "approved_by": "agent_one", **change})
            self.assertEqual(connection.statements, [])

    def test_corroboration_only_inserts_evidence_and_retries_are_idempotent(self):
        value = proposal()
        value.update(base_content_sha256=digest(value), occurrence_ids=[ERROR, SUCCESS])
        for inserts, expected in [((1, 1), 2), ((0, 0), 0)]:
            connection = Connection([[catalog_row(value)],
                                     [(ERROR, "agent_one", value["signature"], "error")],
                                     [(SUCCESS, "agent_one", "success-signature", "success")]],
                                    inserts=inserts)
            self.assertEqual(corroborate_fix(connection, value), expected)
            self.assertEqual(connection.transactions, 1)
            writes = [sql for sql, _ in connection.statements if not sql.lstrip().startswith("SELECT")]
            self.assertEqual(len(writes), 2)
            self.assertTrue(all("ON CONFLICT DO NOTHING" in sql for sql in writes))
            self.assertTrue(all("pi_tool_call_occurrence_fix" in sql for sql in writes))
            self.assertIn("FOR UPDATE OF f", connection.statements[0][0])

    def test_corroboration_rejects_missing_stale_or_edited_fixes_before_inserting(self):
        original = proposal()
        original["base_content_sha256"] = digest(original)
        for change in ({"fix": "Ignore the error"}, {"applies_to_agents": ["agent_one", "agent_three"]},
                       {"base_content_sha256": "b" * 64}, {"source": "changed"}):
            connection = Connection([[catalog_row(original)]])
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, "review and publish"):
                corroborate_fix(connection, dict(original, **change))
            self.assertFalse(any("INSERT" in sql for sql, _ in connection.statements))
        with self.assertRaisesRegex(ValueError, "not active"):
            corroborate_fix(Connection([[]]), original)

    def test_tampered_catalog_digest_fails_closed(self):
        row = list(catalog_row(proposal()))
        row[5] = "f" * 64
        with self.assertRaisesRegex(ValueError, "fresh review"):
            approved_fix(Connection([[row]]), proposal()["id"])

    def test_new_draft_rejects_mixed_error_signatures(self):
        connection = Connection([[(ERROR, "agent_one", "a", "error")],
                                 [("pierr-123456", "agent_three", "b", "error")], []])
        with self.assertRaisesRegex(ValueError, "different signatures"):
            draft_fix(connection, "new-fix", ["err-4b8ecad5", "err-123456"])

    def test_fix_evidence_readback_is_bound_to_the_requested_fix(self):
        connection = Connection([[(ERROR, "agent_one", "error", "timestamp")]])
        self.assertEqual(fix_evidence(connection, "bash-glob-no-match-exit-2"),
                         [(ERROR, "agent_one", "error", "timestamp")])
        sql, params = connection.statements[0]
        self.assertEqual(params, ("bash-glob-no-match-exit-2",))
        self.assertTrue(sql.lstrip().startswith("SELECT"))
        self.assertIn("pi_tool_call_occurrence_fix", sql)
        with self.assertRaises(ValueError):
            fix_evidence(Connection(), "unsafe' OR true")

    def test_corroborate_cli_uses_publisher_and_evidence_readback_uses_reader(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "evidence.json"
            write_private_document(path, proposal())
            with patch("tools.ledger.error_cli.connect", return_value=Connection()) as connect, \
                 patch("tools.ledger.error_cli.corroborate_fix", return_value=0):
                self.assertEqual(main(["corroborate-fix", "--file", str(path)]), 0)
            connect.assert_called_once_with(read_only=False)
            with patch("tools.ledger.error_cli.connect", return_value=Connection()) as connect, \
                 patch("tools.ledger.error_cli.fix_evidence", return_value=[]):
                self.assertEqual(main(["fix-evidence", "--id", proposal()["id"]]), 0)
            connect.assert_called_once_with(read_only=True)

    def test_draft_and_check_cli_use_reader_not_publisher(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "draft.json"
            connection = Connection([[(ERROR, "agent_one", "a" * 64, "error")], []])
            with patch("tools.ledger.error_cli.connect", return_value=connection) as connect:
                self.assertEqual(main(["draft-fix", "--id", "new-fix", "--error",
                                       "err-4b8ecad5", "--output", str(path)]), 0)
            connect.assert_called_once_with(read_only=True)
            path.write_text(json.dumps(proposal()))
            with patch("tools.ledger.error_cli.connect",
                       return_value=Connection([[(ERROR, "agent_one", "a" * 64, "error")]])) as connect:
                self.assertEqual(main(["check-fix", "--file", str(path)]), 0)
            connect.assert_called_once_with(read_only=True)


if __name__ == "__main__":
    unittest.main()
