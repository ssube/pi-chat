"""Portable Matrix routing, identity, write guards and bounded media contracts."""
import importlib.util
import io
import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tools import message_agent
from tools.matrix_client import MatrixClient, MatrixError


def module(name):
    path = Path(__file__).resolve().parents[1] / 'tools' / (name + '.py')
    spec = importlib.util.spec_from_file_location('public_' + name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


cli = module('matrix')
operations = module('matrix_operations')


class MatrixTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.room = '!enabled:example.invalid'
        self.user = '@owner:example.invalid'
        self.peer = '@peer:example.invalid'
        self.profile = self.private('profile.json', {'matrix': {
            'homeserver': 'https://matrix.example.invalid', 'user_id': self.user,
            'token_env': 'TEST_MATRIX_TOKEN', 'rooms': [self.room]}})
        self.contacts = self.private('contacts.json', {'peer': {
            'room_id': self.room, 'user_id': self.peer}})
        self.config = self.private('config.json', {'profile_file': str(self.profile),
                                                 'contacts_file': str(self.contacts)})
        self.message = self.root / 'message.txt'
        self.message.write_text('Please reply.')
        environment = patch.dict(os.environ, {'TEST_MATRIX_TOKEN': 'synthetic'}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)

    def private(self, name, value):
        path = self.root / name
        path.write_text(json.dumps(value))
        path.chmod(0o600)
        return path

    def invoke(self, *args):
        with patch.object(cli.subprocess, 'run', return_value=SimpleNamespace(returncode=0)) as writer:
            code = cli.main(['--config', str(self.config), *args])
        return code, writer

    def test_contact_selects_enabled_room_and_exact_mention(self):
        code, writer = self.invoke('send', '--contact', 'peer', '--message-file', str(self.message))
        self.assertEqual(code, 0)
        command = writer.call_args.args[0]
        for flag, value in [('--profile-file', str(self.profile)), ('--room-id', self.room),
                            ('--to', self.peer)]:
            self.assertEqual(command[command.index(flag) + 1], value)
        self.assertEqual(Path(command[1]).name, 'message_agent.py')

    def test_room_update_does_not_invent_a_recipient(self):
        code, writer = self.invoke('send', self.room, '--message-file', str(self.message))
        self.assertEqual(code, 0)
        self.assertIn('--general', writer.call_args.args[0])
        self.assertNotIn('--to', writer.call_args.args[0])

    def test_unconfigured_or_disabled_contact_never_reaches_writer(self):
        self.private('contacts.json', {'peer': {'room_id': '!disabled:example.invalid',
                                              'user_id': self.peer}})
        with patch('sys.stderr', io.StringIO()):
            for name in ['peer', 'missing']:
                code, writer = self.invoke('send', '--contact', name, '--message-file', str(self.message))
                self.assertEqual(code, 1)
                writer.assert_not_called()

    def test_contact_cannot_override_room_or_mention(self):
        with patch('sys.stderr', io.StringIO()):
            for args in [(self.room,), (self.room, self.peer)]:
                code, writer = self.invoke('send', *args, '--contact', 'peer',
                                           '--message-file', str(self.message))
                self.assertEqual(code, 1)
                writer.assert_not_called()

    def test_alias_must_resolve_to_an_enabled_room(self):
        with patch.object(cli.reader, 'get', side_effect=[{'user_id': self.user},
                                                         {'room_id': '!disabled:example.invalid'}]), \
                patch('sys.stderr', io.StringIO()):
            code, writer = self.invoke('check', '#room:example.invalid')
        self.assertEqual(code, 1)
        writer.assert_not_called()

    def test_wrong_token_owner_blocks_reads(self):
        with patch.object(cli.reader, 'get', return_value={'user_id': self.peer}) as get, \
                patch('sys.stderr', io.StringIO()):
            code, writer = self.invoke('rooms')
        self.assertEqual(code, 1)
        self.assertEqual(get.call_count, 1)
        writer.assert_not_called()

    def test_incomplete_inherited_credentials_refuse(self):
        with patch.dict(os.environ, {'MATRIX_USER_ID': self.user}), \
                patch('sys.stderr', io.StringIO()):
            code, writer = self.invoke('check', self.room)
        self.assertEqual(code, 1)
        writer.assert_not_called()

    def test_wait_for_contact_uses_exact_sender_and_keeps_progress_filter(self):
        with patch.object(cli.reader, 'main', return_value=2) as reader:
            code, writer = self.invoke('wait', '--contact', 'peer', '--marker', 'DONE',
                                       '--skip-progress', '--timeout', '60')
        self.assertEqual(code, 2)
        args = reader.call_args.args[0]
        self.assertEqual(args[args.index('--sender') + 1], self.peer)
        self.assertIn('--skip-progress', args)
        writer.assert_not_called()

    def test_check_never_claims_attention_or_sends(self):
        client = Mock(user_id=self.user, room_id=self.room)
        client.whoami.return_value = self.user
        client.joined_members.return_value = {self.user: {}, self.peer: {}}
        with patch.object(message_agent, 'MatrixClient', return_value=client), \
                patch.object(message_agent, 'room_lease') as lease, patch('sys.stdout', io.StringIO()):
            self.assertEqual(message_agent.main(['--profile-file', str(self.profile),
                '--room-id', self.room, '--to', self.peer, '--check']), 0)
        lease.assert_not_called()
        client.send.assert_not_called()

    def test_reaction_pages_fall_back_only_on_unsupported_endpoint(self):
        target = {'event_id': '$event', 'room_id': self.room}
        with patch.object(cli.reader, 'get', side_effect=[target, RuntimeError('Matrix request failed (HTTP 404)')]), \
                patch.object(cli, 'reaction_history', return_value={'source': 'history'}) as fallback:
            self.assertEqual(cli.reactions('https://example.invalid', 'synthetic', self.room,
                                            '$event', 2, 20), {'source': 'history'})
        fallback.assert_called_once()
        with patch.object(cli.reader, 'get', side_effect=[target, RuntimeError('Matrix request failed (HTTP 403)')]), \
                patch.object(cli, 'reaction_history') as fallback:
            with self.assertRaises(RuntimeError):
                cli.reactions('https://example.invalid', 'synthetic', self.room, '$event', 2, 20)
        fallback.assert_not_called()


class MediaTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.room = Mock(room_id='!enabled:example.invalid')
        self.args = SimpleNamespace(operation='batch', profile_file=self.root / 'profile.json',
            room_id=self.room.room_id, to=None, general=True, caption='', interval=0,
            files=[], caption_before_file=False)

    def test_all_files_validate_before_any_upload(self):
        first = self.root / 'first.txt'
        first.write_text('first')
        self.args.files = [first, self.root / 'missing.txt']
        with patch.object(operations.sender, 'verified_room', return_value=self.room), \
                patch.object(operations, 'unencrypted'):
            with self.assertRaises(FileNotFoundError):
                operations.run(self.args)
        self.room.upload.assert_not_called()

    def test_uncertain_item_stops_batch_and_preserves_completed_receipts(self):
        for name in ['one.txt', 'two.txt', 'three.txt']:
            path = self.root / name
            path.write_text(name)
            self.args.files.append(path)
        self.room.upload.return_value = 'mxc://example.invalid/file'
        self.room.send_event.side_effect = ['$first', RuntimeError('uncertain')]
        output = io.StringIO()
        with patch.object(operations.sender, 'verified_room', return_value=self.room), \
                patch.object(operations, 'unencrypted'), \
                patch.object(operations.sender, 'guarded_write', side_effect=lambda room, write: write()), \
                patch('sys.stdout', output):
            self.assertEqual(operations.run(self.args), 1)
        receipts = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(receipts[0]['event_id'], '$first')
        self.assertEqual(receipts[1]['completed'], 1)
        self.assertEqual(receipts[1]['status'], 'failed_or_uncertain')
        self.assertEqual(self.room.upload.call_count, 2)
        self.assertEqual(self.room.send_event.call_count, 2)

    def test_wrong_reaction_target_never_writes(self):
        self.args.operation, self.args.event_id, self.args.key = 'react', '$target', '👍'
        self.room.event.return_value = {'event_id': '$target', 'room_id': '!other:example.invalid'}
        with patch.object(operations.sender, 'verified_room', return_value=self.room), \
                patch.object(operations, 'unencrypted'), \
                patch.object(operations.sender, 'guarded_write') as write:
            with self.assertRaisesRegex(ValueError, 'target'):
                operations.run(self.args)
        write.assert_not_called()

    def test_empty_or_oversized_files_refuse(self):
        path = self.root / 'file'
        path.touch()
        with self.assertRaisesRegex(ValueError, 'regular file'):
            operations.media(path)
        with path.open('wb') as stream:
            stream.truncate(operations.MAX_BYTES + 1)
        with self.assertRaisesRegex(ValueError, 'regular file'):
            operations.media(path)


class ClientRetryTests(unittest.TestCase):
    def test_put_retries_the_same_transaction_but_post_does_not_replay_5xx(self):
        client = MatrixClient('https://matrix.example.invalid', '@owner:example.invalid',
                              'synthetic', '!room:example.invalid')
        error = urllib.error.HTTPError('https://example.invalid', 503, 'unavailable', {}, io.BytesIO())
        success = Mock()
        success.__enter__ = Mock(return_value=io.StringIO('{"event_id":"$sent"}'))
        success.__exit__ = Mock(return_value=False)
        with patch('urllib.request.urlopen', side_effect=[error, success]) as request, \
                patch('time.sleep'):
            self.assertEqual(client.send_event('m.room.message', {'body': 'hello'}), '$sent')
        self.assertIs(request.call_args_list[0].args[0], request.call_args_list[1].args[0])
        with patch('urllib.request.urlopen', side_effect=error) as request:
            with self.assertRaisesRegex(MatrixError, 'HTTP 503'):
                client.upload(b'image', 'image.png', 'image/png')
        self.assertEqual(request.call_count, 1)
