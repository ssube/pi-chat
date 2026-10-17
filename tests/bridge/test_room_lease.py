"""Atomic handoff and expiry for the interactive Pi Matrix room lease."""

import asyncio
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from bridge.adapters.matrix import MatrixAdapter
from bridge.config import MatrixSettings
from bridge.room_lease import RoomLeases


ROOM = "!private:example.test"
USER = "@agent_one-agent:example.test"
SECRET = "a" * 64


class RoomLeaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.prepare = AsyncMock(return_value=True)
        self.leases = RoomLeases(self.root / "bridge.sock", (ROOM,), USER, self.prepare)
        await self.leases.start()
        self.addAsyncCleanup(self.leases.close)

    async def request(self, op="acquire", **changes):
        data = {"op": op, "room": ROOM, "sender": USER, "secret": SECRET,
                "inbox": str(self.root / "inbox.sock")}
        data.update(changes)
        reader, writer = await asyncio.open_unix_connection(str(self.leases.path))
        writer.write(json.dumps(data).encode() + b"\n")
        await writer.drain()
        result = json.loads(await reader.readline())
        writer.close()
        await writer.wait_closed()
        return result

    async def test_acquire_before_incoming_reply_and_release(self):
        delivered = []

        async def inbox(reader, writer):
            delivered.append(json.loads(await reader.readline()))
            writer.write(b'{"ok":true}\n')
            await writer.drain()
            writer.close()

        server = await asyncio.start_unix_server(inbox, str(self.root / "inbox.sock"))
        try:
            self.assertTrue((await self.request())["ok"])
            status = await self.request(op="status", room="")
            self.assertEqual(status["leases"][0]["room"], ROOM)
            self.assertGreater(status["leases"][0]["seconds"], 0)
            self.assertEqual((await self.request(op="status", room="", secret="b" * 64))["leases"], [])
            event = {"event_id": "$fast", "type": "m.room.message",
                     "sender": "@agent_two:example.test", "content": {"body": "reply"}}
            self.assertTrue(await self.leases.forward(ROOM, event))
            self.assertFalse((self.root / "matrix-lease-pending.json").exists())
            self.assertEqual(delivered[0]["event"]["event_id"], "$fast")
            self.assertEqual(delivered[0]["recipient"], USER)
            self.assertIs(delivered[0]["ping"], False)
            self.assertFalse((await self.request(secret="b" * 64))["ok"])
            self.assertEqual(self.prepare.await_count, 1)
            self.assertEqual((await self.request(op="release_all", room=""))["released"], 1)
            self.assertEqual((await self.request(op="status", room=""))["leases"], [])
            self.assertFalse(await self.leases.forward(ROOM, event))
        finally:
            server.close()
            await server.wait_closed()

    async def test_non_object_inbox_ack_restores_bridge_routing(self):
        async def inbox(reader, writer):
            await reader.readline()
            writer.write(b'[]\n')
            await writer.drain()
            writer.close()

        server = await asyncio.start_unix_server(inbox, str(self.root / "inbox.sock"))
        try:
            self.assertTrue((await self.request())["ok"])
            self.assertFalse(await self.leases.forward(ROOM, {"event_id": "$invalid"}))
            self.assertNotIn(ROOM, self.leases.leases)
        finally:
            server.close()
            await server.wait_closed()

    async def test_expiry_or_broken_inbox_reverts_to_bridge(self):
        self.assertTrue((await self.request())["ok"])
        self.leases.leases[ROOM].expires = time.monotonic() - 1
        self.assertFalse(await self.leases.forward(ROOM, {"event_id": "$late"}))
        self.assertTrue((await self.request())["ok"])
        self.assertFalse(await self.leases.forward(ROOM, {"event_id": "$undelivered"}))
        self.assertNotIn(ROOM, self.leases.leases)

    async def test_matrix_forward_marks_direct_ping_distinct_from_mention(self):
        delivered = []

        async def inbox(reader, writer):
            delivered.append(json.loads(await reader.readline()))
            writer.write(b'{"ok":true}\n')
            await writer.drain()
            writer.close()

        server = await asyncio.start_unix_server(inbox, str(self.root / "inbox.sock"))
        try:
            settings = MatrixSettings("https://example.test", USER, "TEST_MATRIX_TOKEN",
                                      (ROOM,), display_name="Agent_one")
            with patch.dict(os.environ, {"TEST_MATRIX_TOKEN": "test"}):
                adapter = MatrixAdapter("agent_one", settings, AsyncMock(), self.root,
                                        room_leases=self.leases)
            self.assertTrue((await self.request())["ok"])
            for event_id, body in (("$direct", "Agent_one: interactive test"),
                                   ("$mention", "Please ask Agent_one to check this")):
                event = {"type": "m.room.message", "event_id": event_id,
                         "sender": "@agent_two:example.test", "content": {
                             "msgtype": "m.text", "body": body,
                             "m.mentions": {"user_ids": [USER]}}}
                await adapter._handle_event(ROOM, event)
            adapter._request = AsyncMock(return_value={
                "room_id": ROOM, "event_id": "$sent", "type": "m.room.message",
                "sender": USER, "content": {"msgtype": "m.text", "body": "Original message"}})
            reply = {"m.relates_to": {"m.in_reply_to": {"event_id": "$sent"}},
                     "m.mentions": {"user_ids": [USER]}}
            await adapter._handle_event(ROOM, {
                "type": "m.room.message", "event_id": "$reply", "sender": "@agent_two:example.test",
                "content": {"msgtype": "m.text", "body": "this is a reply", **reply}})
            await adapter._handle_event(ROOM, {
                "type": "m.room.message", "event_id": "$both", "sender": "@agent_two:example.test",
                "content": {"msgtype": "m.text", "body": "Agent_one: reply and ping", **reply,
                            "formatted_body": f'<a href="https://matrix.to/#/{USER}">Agent_one</a>: reply and ping'}})
            self.assertEqual([item["ping"] for item in delivered], [True, True, False, True])
            self.assertEqual([item["reply"] is not None for item in delivered],
                             [False, False, True, True])
            self.assertEqual([item["recipient"] for item in delivered], [USER] * 4)
        finally:
            server.close()
            await server.wait_closed()

    async def test_progress_edits_skip_both_routes_and_user_edits_only_reach_active_inbox(self):
        delivered = []

        async def inbox(reader, writer):
            delivered.append(json.loads(await reader.readline()))
            writer.write(b'{"ok":true}\n')
            await writer.drain()
            writer.close()

        runtime = AsyncMock()
        settings = MatrixSettings("https://example.test", USER, "TEST_MATRIX_TOKEN", (ROOM,))
        with patch.dict(os.environ, {"TEST_MATRIX_TOKEN": "test"}):
            adapter = MatrixAdapter("agent_one", settings, runtime, self.root,
                                    room_leases=self.leases)
        server = await asyncio.start_unix_server(inbox, str(self.root / "inbox.sock"))
        try:
            self.assertTrue((await self.request())["ok"])
            for event_id, kind, relation in (
                    ("$ack", "ack", {}),
                    ("$edit", "progress", {"rel_type": "m.replace", "event_id": "$ack"}),
                    ("$edit-without-meta", None, {"rel_type": "m.replace", "event_id": "$ack"}),
                    ("$steered", "steered", {})):
                content = {"msgtype": "m.text", "body": "Still working…",
                           "m.mentions": {"user_ids": [USER]}}
                if kind:
                    content["org.example.pi_bridge"] = {"kind": kind}
                if relation:
                    content["m.relates_to"] = relation
                await adapter._handle_event(ROOM, {
                    "type": "m.room.message", "event_id": event_id,
                    "sender": "@agent_two:example.test", "content": content})
                self.assertIn(event_id, adapter._seen)
            self.assertEqual([item["event"]["event_id"] for item in delivered], ["$edit-without-meta"])
            runtime.submit.assert_not_awaited()
            await adapter._handle_event(ROOM, {
                "type": "m.room.message", "event_id": "$final",
                "sender": "@agent_two:example.test", "content": {
                    "msgtype": "m.text", "body": "Review complete",
                    "org.example.pi_bridge": {"kind": "final", "hop": 0},
                    "m.mentions": {"user_ids": [USER]}}})
            self.assertEqual([item["event"]["event_id"] for item in delivered],
                             ["$edit-without-meta", "$final"])
            self.assertEqual((await self.request(op="release_all", room=""))["released"], 1)
            await adapter._handle_event(ROOM, {
                "type": "m.room.message", "event_id": "$after-release",
                "sender": "@agent_two:example.test", "content": {
                    "msgtype": "m.text", "body": "Still working…",
                    "m.relates_to": {"rel_type": "m.replace", "event_id": "$ack"}}})
            self.assertEqual([item["event"]["event_id"] for item in delivered],
                             ["$edit-without-meta", "$final"])
            runtime.submit.assert_not_awaited()
        finally:
            server.close()
            await server.wait_closed()

    async def test_matrix_does_not_start_bridge_pi_after_forward_ack(self):
        runtime = AsyncMock()
        settings = MatrixSettings("https://example.test", USER, "TEST_MATRIX_TOKEN",
                                  (ROOM,))
        with patch.dict(os.environ, {"TEST_MATRIX_TOKEN": "test"}):
            adapter = MatrixAdapter("agent_one", settings, runtime, self.root,
                                    room_leases=self.leases)
        await self.request()  # Lease exists before the Matrix event arrives.
        self.leases.forward = AsyncMock(return_value=True)
        event = {"type": "m.room.message", "event_id": "$fast",
                 "sender": "@agent_two:example.test",
                 "content": {"msgtype": "m.text", "body": "reply"}}
        await adapter._handle_event(ROOM, event)
        self.leases.forward.assert_awaited_once_with(ROOM, event, None, None, False)
        runtime.submit.assert_not_awaited()
        self.assertIn("$fast", adapter._seen)

    async def test_matrix_image_handoff_includes_image_bytes(self):
        from bridge.runtime.pi_rpc import RpcImage

        runtime = AsyncMock()
        settings = MatrixSettings("https://example.test", USER, "TEST_MATRIX_TOKEN", (ROOM,))
        with patch.dict(os.environ, {"TEST_MATRIX_TOKEN": "test"}):
            adapter = MatrixAdapter("agent_one", settings, runtime, self.root,
                                    room_leases=self.leases)
        await self.request()
        adapter._image = AsyncMock(return_value=RpcImage("image/png", b"pngbytes"))
        self.leases.forward = AsyncMock(return_value=True)
        event = {"type": "m.room.message", "event_id": "$picture",
                 "sender": "@agent_two:example.test",
                 "content": {"msgtype": "m.image", "body": "diagram.png",
                             "url": "mxc://example.test/media"}}
        await adapter._handle_event(ROOM, event)
        self.leases.forward.assert_awaited_once_with(ROOM, event,
                                                      RpcImage("image/png", b"pngbytes").to_json(), None, False)
        runtime.submit.assert_not_awaited()

    async def test_reply_context_quotes_only_direct_parent_in_same_room(self):
        runtime = AsyncMock()
        settings = MatrixSettings("https://example.test", USER, "TEST_MATRIX_TOKEN", (ROOM,))
        with patch.dict(os.environ, {"TEST_MATRIX_TOKEN": "test"}):
            adapter = MatrixAdapter("agent_one", settings, runtime, self.root,
                                    room_leases=self.leases)
        await self.request()
        adapter._request = AsyncMock(return_value={
            "type": "m.room.message", "room_id": ROOM, "event_id": "$parent",
            "sender": "@agent_one:example.test",
            "content": {"msgtype": "m.text", "body": "> <@someone> grandparent\n\nParent text",
                        "m.relates_to": {"m.in_reply_to": {"event_id": "$grandparent"}}}})
        self.leases.forward = AsyncMock(return_value=True)
        event = {"type": "m.room.message", "event_id": "$child",
                 "sender": "@agent_two:example.test",
                 "content": {"msgtype": "m.text", "body": "Child text",
                             "m.relates_to": {"m.in_reply_to": {"event_id": "$parent"}}}}
        await adapter._handle_event(ROOM, event)
        self.leases.forward.assert_awaited_once_with(ROOM, event, None,
            {"event_id": "$parent", "sender": "@agent_one:example.test", "body": "Parent text"}, False)
        self.assertEqual(adapter._request.await_count, 1)
        adapter._request.reset_mock()
        adapter._request.return_value["room_id"] = "!other:example.test"
        self.assertEqual(await adapter._reply_context(ROOM, event), {"event_id": "$parent"})

    async def test_claim_during_history_read_forwards_instead_of_waking_bridge(self):
        runtime = AsyncMock()
        runtime.admit.return_value = True
        runtime.is_awake.return_value = False
        runtime.last_wake.return_value = ("", 0)
        settings = MatrixSettings("https://example.test", USER, "TEST_MATRIX_TOKEN", (ROOM,),
                                  direct_rooms=(ROOM,))
        with patch.dict(os.environ, {"TEST_MATRIX_TOKEN": "test"}):
            adapter = MatrixAdapter("agent_one", settings, runtime, self.root,
                                    room_leases=self.leases)
        reading = asyncio.Event()
        proceed = asyncio.Event()

        async def history(*args, **kwargs):
            reading.set()
            await proceed.wait()
            return "room context"

        adapter._history = AsyncMock(side_effect=history)
        delivered = []

        async def inbox(reader, writer):
            delivered.append(json.loads(await reader.readline()))
            writer.write(b'{"ok":true}\n')
            await writer.drain()
            writer.close()

        server = await asyncio.start_unix_server(inbox, str(self.root / "inbox.sock"))
        try:
            event = {"type": "m.room.message", "event_id": "$raced",
                     "sender": "@agent_two:example.test",
                     "content": {"msgtype": "m.text", "body": "reply"}}
            handler = asyncio.create_task(adapter._handle_event(ROOM, event))
            await asyncio.wait_for(reading.wait(), 2)
            self.assertTrue((await self.request())["ok"])
            proceed.set()
            await asyncio.wait_for(handler, 2)
            self.assertEqual(delivered[0]["event"]["event_id"], "$raced")
            runtime.submit.assert_not_awaited()
            self.assertIn("$raced", adapter._seen)
        finally:
            proceed.set()
            server.close()
            await server.wait_closed()

    async def test_claim_during_ambient_admission_forwards_room_message(self):
        runtime = AsyncMock()
        runtime.is_awake.return_value = False
        waiting = asyncio.Event()
        proceed = asyncio.Event()

        async def admit(*args, **kwargs):
            waiting.set()
            await proceed.wait()
            return False

        runtime.admit.side_effect = admit
        settings = MatrixSettings("https://example.test", USER, "TEST_MATRIX_TOKEN", (ROOM,))
        with patch.dict(os.environ, {"TEST_MATRIX_TOKEN": "test"}):
            adapter = MatrixAdapter("agent_one", settings, runtime, self.root,
                                    room_leases=self.leases)
        delivered = []

        async def inbox(reader, writer):
            delivered.append(json.loads(await reader.readline()))
            writer.write(b'{"ok":true}\n')
            await writer.drain()
            writer.close()

        server = await asyncio.start_unix_server(inbox, str(self.root / "inbox.sock"))
        try:
            event = {"type": "m.room.message", "event_id": "$ambient",
                     "sender": "@agent_two:example.test",
                     "content": {"msgtype": "m.text", "body": "room note"}}
            handler = asyncio.create_task(adapter._handle_event(ROOM, event))
            await asyncio.wait_for(waiting.wait(), 2)
            self.assertTrue((await self.request())["ok"])
            proceed.set()
            await asyncio.wait_for(handler, 2)
            self.assertEqual(delivered[0]["event"]["event_id"], "$ambient")
            runtime.submit.assert_not_awaited()
        finally:
            proceed.set()
            server.close()
            await server.wait_closed()

    async def test_lease_media_errors_keep_text_and_parent_reference(self):
        import aiohttp

        runtime = AsyncMock()
        settings = MatrixSettings("https://example.test", USER, "TEST_MATRIX_TOKEN", (ROOM,))
        with patch.dict(os.environ, {"TEST_MATRIX_TOKEN": "test"}):
            adapter = MatrixAdapter("agent_one", settings, runtime, self.root,
                                    room_leases=self.leases)
        await self.request()
        adapter._image = AsyncMock(side_effect=aiohttp.ClientError("offline"))
        adapter._request = AsyncMock(side_effect=aiohttp.ClientError("offline"))
        self.leases.forward = AsyncMock(return_value=True)
        event = {"type": "m.room.message", "event_id": "$picture",
                 "sender": "@agent_two:example.test",
                 "content": {"msgtype": "m.image", "body": "diagram.png",
                             "m.relates_to": {"m.in_reply_to": {"event_id": "$parent"}}}}
        await adapter._handle_event(ROOM, event)
        self.leases.forward.assert_awaited_once_with(
            ROOM, event, None, {"event_id": "$parent"}, False)
        runtime.submit.assert_not_awaited()
        self.assertIn("$picture", adapter._seen)

    async def test_competing_sessions_get_reason_and_remaining_time_without_owner_secrets(self):
        self.assertTrue((await self.request())["ok"])
        owner = self.leases.leases[ROOM]
        with patch("bridge.room_lease.time.monotonic", return_value=1000):
            owner.expires = 1090.2
            for changes in ({"secret": "b" * 64}, {"inbox": str(self.root / "other.sock")}):
                denied = await self.leases._command({
                    "op": "acquire", "room": ROOM, "sender": USER, "secret": SECRET,
                    "inbox": str(self.root / "inbox.sock"), **changes})
                self.assertEqual(denied, {"ok": False, "reason": "interactive_session_active",
                                          "lease_seconds_remaining": 91})
                self.assertIs(self.leases.leases[ROOM], owner)
                self.assertEqual(owner.expires, 1090.2)
        self.assertEqual(self.prepare.await_count, 1)
        self.assertNotIn(SECRET, json.dumps(denied))
        self.assertNotIn(str(self.root), json.dumps(denied))

    async def test_failed_handoff_reports_reason_without_claim_or_expiry_promise(self):
        self.prepare.return_value = False
        self.assertEqual(await self.request(), {"ok": False, "reason": "bridge_handoff_unavailable"})
        self.assertNotIn(ROOM, self.leases.leases)

    async def test_wrong_sender_and_failed_preparation_never_claim(self):
        self.assertFalse((await self.request(sender="@other:example.test"))["ok"])
        self.prepare.return_value = False
        self.assertFalse((await self.request())["ok"])
        self.assertNotIn(ROOM, self.leases.leases)
