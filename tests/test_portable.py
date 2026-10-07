"""Portable identity, sender, and lease behavior without a live transport."""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from bridge.config import Profile
from bridge.room_lease import RoomLeases
from tools import message_agent
from tools.matrix_client import MatrixError


class PortableTests(unittest.TestCase):
    def test_generic_profiles_and_sender_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            room, user, peer = '!room:example.invalid', '@agent:example.invalid', '@peer:example.invalid'
            raw = dict(agent='custom_assistant', work_dir=directory, state_dir=directory,
                       pi_executable='/bin/false', pi_agent_dir=directory, provider='test', model='test-model',
                       matrix=dict(homeserver='https://matrix.example.invalid', user_id=user,
                                   token_env='TEST_TOKEN', rooms=[room]))
            path = root / 'profile.json'
            path.write_text(json.dumps(raw))
            path.chmod(0o600)
            text = root / 'message.txt'
            text.write_text('Please reply.')
            env = dict(TEST_TOKEN='synthetic', AGENT_BRIDGE_ROOM_LEASE_SOCKET='/tmp/example.sock',
                       PI_ROOM_LEASE_BRIDGE_SOCKET='/tmp/example.sock', PI_ROOM_LEASE_INBOX='/tmp/inbox.sock',
                       PI_ROOM_LEASE_SECRET='x' * 64)
            args = ['message_agent', '--profile-file', str(path), '--room-id', room, '--to', peer,
                    '--message-file', str(text)]
            client = Mock(user_id=user)
            client.whoami.return_value = user
            client.joined_members.return_value = {user: {}, peer: {}}
            lease = Mock(return_value=True)
            with patch.dict(os.environ, env, clear=True), patch.object(message_agent, 'MatrixClient', return_value=client), \
                 patch.object(message_agent, 'room_lease', lease):
                self.assertEqual(Profile.load(path).agent, 'custom_assistant')
                with patch('sys.argv', args + ['--check']):
                    self.assertEqual(message_agent.main(), 0)
                lease.assert_not_called()
                client.send.assert_not_called()

                def send(body, recipient):
                    lease.assert_called_once_with('acquire', room, user)
                    self.assertTrue(body.startswith(peer))
                    return '$sent'
                client.send.side_effect = send
                with patch('sys.argv', args):
                    self.assertEqual(message_agent.main(), 0)
                    for error, calls in [('Matrix HTTP 403', 2), ('Matrix network/timeout failure', 1)]:
                        lease.reset_mock()
                        client.send.side_effect = MatrixError(error)
                        with self.assertRaises(MatrixError):
                            message_agent.main()
                        self.assertEqual(lease.call_count, calls)
                    client.send.reset_mock()
                    lease.side_effect = RuntimeError('refused')
                    with self.assertRaisesRegex(RuntimeError, 'refused'):
                        message_agent.main()
                    client.send.assert_not_called()
                    del os.environ['PI_ROOM_LEASE_SECRET']
                    with self.assertRaisesRegex(RuntimeError, 'extension unavailable'):
                        message_agent.main()
                    client.send.assert_not_called()
                raw.pop('matrix')
                raw['discord'] = dict(token_env='TEST_TOKEN', guild_id=123, channel_ids=[456])
                path.write_text(json.dumps(raw))
                self.assertEqual(Profile.load(path).agent, 'custom_assistant')


class LeaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_claim_status_mobile_delivery_expiry_and_release(self):
        room = '!room:example.invalid'
        service = RoomLeases(Path('/tmp/unused.sock'), (room,), '@agent:example.invalid', AsyncMock(return_value=True))
        request = dict(op='acquire', room=room, sender='@agent:example.invalid', inbox='/tmp/inbox.sock', secret='x' * 64)
        self.assertEqual(await service._command(request), {'ok': True, 'created': True})
        expires = service.leases[room].expires
        await service._command(dict(op='status', secret=request['secret']))
        self.assertEqual(service.leases[room].expires, expires)
        self.assertFalse((await service._command({**request, 'secret': 'z' * 64}))['ok'])
        reader = Mock(readline=AsyncMock(return_value=b'{"ok":true}\n'))
        writer = Mock(drain=AsyncMock(), wait_closed=AsyncMock())
        with patch('bridge.room_lease.asyncio.open_unix_connection', AsyncMock(return_value=(reader, writer))):
            event = dict(event_id='$mobile', sender='@human:example.invalid')
            self.assertTrue(await service.forward(room, event))
            self.assertGreaterEqual(service.leases[room].expires, expires)
            self.assertEqual(json.loads(writer.write.call_args.args[0])['event'], event)
            reader.readline.return_value = b'{"ok":false}\n'
            self.assertFalse(await service.forward(room, event))
            self.assertFalse(service.active(room))
        await service._command(request)
        service.leases[room].expires = time.monotonic() - 1
        self.assertFalse(await service.forward(room, {}))
        await service._command(request)
        self.assertEqual(await service._command(dict(op='release_all', secret=request['secret'])), {'ok': True, 'released': 1})
        service.prepare.return_value = False
        self.assertFalse((await service._command(request))['ok'])
        with self.assertRaisesRegex(ValueError, 'invalid room'):
            await service._command({**request, 'sender': '@other:example.invalid'})
