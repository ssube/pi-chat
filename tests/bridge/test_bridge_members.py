"""Member enumeration uses the existing conversation capability and safe socket boundary."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bridge.history_service import (HistoryRejected, HistoryService, members_page,
                                    validate_members_page, validate_request)
from bridge.members_cli import fetch, main
from bridge.mentions import Member, Roster


class MembersTests(unittest.IsolatedAsyncioTestCase):
    async def test_paged_cli_lookup_and_capability_rejection_over_socket(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "members.sock"
            roster = Roster([Member(f"@user{i}:example.test", f"user{i}", (f"User {i}",))
                             for i in range(7)], complete=False)

            async def callback(request):
                if request["capability"] != "x" * 32 or request["scope"] != "approved":
                    raise HistoryRejected("capability")
                self.assertEqual(request["operation"], "members")
                return members_page(roster, request["before"], request["limit"], request.get("targets"))

            service = HistoryService(path, callback)
            await service.start()
            try:
                cursor, collected = None, []
                while True:
                    page = await asyncio.to_thread(fetch, str(path), "matrix", "approved", "x" * 32,
                                                   before=cursor, limit=3)
                    self.assertTrue(page["available"])
                    self.assertFalse(page["complete"])
                    collected.extend(member["id"] for member in page["members"])
                    cursor = page["next_before"]
                    if not page["has_more"]:
                        break
                self.assertEqual(collected, [member.id for member in roster.members])
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                for scope, token in (("other", "x" * 32), ("approved", "y" * 32)):
                    with self.assertRaisesRegex(RuntimeError, "history capability is not valid"):
                        await asyncio.to_thread(fetch, str(path), "discord", scope, token)
                with self.assertRaisesRegex(RuntimeError, "unknown or ambiguous target; enumerate canonical members"):
                    await asyncio.to_thread(fetch, str(path), "matrix", "approved", "x" * 32,
                                            targets=["mis-typed"])
            finally:
                await service.close()

    async def test_callback_failure_and_malformed_member_page_are_private(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "members.sock"

            async def callback(request):
                raise RuntimeError("token=synthetic-private")

            service = HistoryService(path, callback)
            await service.start()
            try:
                with self.assertLogs("agent_bridge.history", level="WARNING") as logs:
                    with self.assertRaises(RuntimeError) as failure:
                        await asyncio.to_thread(fetch, str(path), "matrix", "approved", "x" * 32)
                self.assertNotIn("synthetic-private", str(failure.exception))
                self.assertNotIn("synthetic-private", str(logs.output))
            finally:
                await service.close()

    def test_validation_missing_unavailable_and_invalid_fields(self):
        roster = Roster([], available=False, complete=False)
        self.assertEqual(validate_members_page(members_page(roster, None, 50))["members"], [])
        for cursor in ("-1", "abc", "١", ""):
            with self.assertRaises(ValueError):
                members_page(roster, cursor, 50)
        request = {"transport": "matrix", "scope": "approved", "capability": "x" * 32}
        for operation in ([], {}, None, True, "unknown"):
            with self.assertRaises(ValueError):
                validate_request({**request, "operation": operation})
        for value in (None, {}, {"members": ["bad"]}, {"members": [{"id": "x", "name": "x", "kind": [], "aliases": []}]}):
            with self.assertRaises(ValueError):
                validate_members_page(value)

    def test_structured_targets_accept_aliases_without_at_and_emit_canonical_ids(self):
        roster = Roster([Member("@alice:example.test", "alice", ("alice", "Alice Smith"))])
        page = members_page(roster, None, 10, ["Alice Smith", "@alice", "@alice:example.test"])
        self.assertEqual(page["resolved"], ["@alice:example.test"])
        self.assertEqual(validate_members_page(page), page)
        with self.assertRaisesRegex(ValueError, "unknown or ambiguous target"):
            members_page(roster, None, 10, ["unknown"])
        request = validate_request({"operation": "members", "transport": "matrix", "scope": "approved",
                                    "capability": "x" * 32, "targets": ["Alice Smith"]})
        self.assertEqual(request["targets"], ["Alice Smith"])

    def test_cli_uses_context_and_hides_errors(self):
        environment = {"AGENT_BRIDGE_HISTORY_" + name: value for name, value in zip(
            ("SOCKET", "TRANSPORT", "SCOPE", "CAPABILITY"), ("/tmp/synthetic.sock", "matrix", "approved", "x" * 32))}
        result = members_page(Roster([], available=False, complete=False), None, 50)
        with patch.dict("os.environ", environment, clear=True), patch("sys.argv", ["bridge-members"]):
            with patch("bridge.members_cli.fetch", return_value=result), patch("builtins.print") as output:
                self.assertEqual(main(), 0)
                self.assertEqual(json.loads(output.call_args.args[0]), result)
            with patch("bridge.members_cli.fetch", side_effect=OSError("private-value")), patch("builtins.print") as output:
                self.assertEqual(main(), 1)
                self.assertNotIn("private-value", str(output.call_args))
            with patch("bridge.members_cli.fetch", side_effect=RuntimeError(
                    "unknown or ambiguous target; enumerate canonical members")), patch("builtins.print") as output:
                self.assertEqual(main(), 1)
                self.assertIn("enumerate canonical members", str(output.call_args))
