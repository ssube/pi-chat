import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bridge.adapters.discord import DiscordAdapter
from bridge.adapters.matrix import MatrixAdapter
from bridge.config import DiscordSettings, MatrixSettings, Profile
from bridge.mentions import BROADCAST_REJECTION, find_mentions, has_broadcast_ping
from bridge.runtime.session import BridgeRuntime, Incoming, ReplyRejected
from bridge.runtime.state import SessionKey


class BroadcastTokenTests(unittest.TestCase):
    def test_tokens_boundaries_and_escapes(self):
        for text in ('@here', 'hello @everyone!', '(@channel)', '@room.', '@HERE',
                     '`@room`', '```@here```', '@here: please read', r'\\@here',
                     ' '.join('@person' for _ in range(40)) + ' @room'):
            with self.subTest(text=text):
                self.assertTrue(has_broadcast_ping(text))
        for text in (r'\@here', r'\@everyone', r'\@channel', r'\@room', '@ room',
                     '@ everyone', 'user@here.example', '@room:example.test',
                     '@roommate', '@here-user', '@room.example', '@channel_name'):
            with self.subTest(text=text):
                self.assertFalse(has_broadcast_ping(text))
        self.assertEqual(find_mentions(r'\@room'), [])
        self.assertEqual(find_mentions(r'\@person'), [])

    def test_profile_defaults_allowlists_and_enabled_target_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'profile.json'
            raw = {'agent': 'agent_two', 'work_dir': directory, 'state_dir': directory,
                   "provider": "test", "model": "test-model", 'pi_executable': '/usr/bin/pi', 'pi_agent_dir': directory,
                   'matrix': {'homeserver': 'https://example.test', 'user_id': '@agent_two:example.test',
                              'token_env': 'TEST_MATRIX_TOKEN', 'rooms': ['!alerts:example.test']},
                   'discord': {'token_env': 'TEST_DISCORD_TOKEN', 'guild_id': 1, 'channel_ids': [2]}}
            with patch.dict(os.environ, {'TEST_MATRIX_TOKEN': 'fixture', 'TEST_DISCORD_TOKEN': 'fixture',
                                         'COUNCIL_MATRIX_ROOM_ID': '!council:example.test'}):
                path.write_text(json.dumps(raw))
                profile = Profile.load(path)
                self.assertEqual(profile.matrix.broadcast_allowed_room_ids, ())
                self.assertEqual(profile.discord.broadcast_allowed_channel_ids, ())
                raw['matrix']['broadcast_allowed_room_ids'] = ['!alerts:example.test']
                raw['discord']['broadcast_allowed_channel_ids'] = [2]
                path.write_text(json.dumps(raw))
                profile = Profile.load(path)
                self.assertEqual(profile.matrix.broadcast_allowed_room_ids, ('!alerts:example.test',))
                self.assertEqual(profile.discord.broadcast_allowed_channel_ids, (2,))
                raw['matrix']['broadcast_allowed_room_ids'] = ['!other:example.test']
                path.write_text(json.dumps(raw))
                with self.assertRaisesRegex(ValueError, 'broadcast-allowed rooms'):
                    Profile.load(path)
                raw['matrix']['broadcast_allowed_room_ids'] = []
                raw['discord']['broadcast_allowed_channel_ids'] = [3]
                path.write_text(json.dumps(raw))
                with self.assertRaisesRegex(ValueError, 'broadcast-allowed channels'):
                    Profile.load(path)



class BroadcastOutputTests(unittest.IsolatedAsyncioTestCase):
    async def test_matrix_rejects_original_text_before_any_translation_or_send(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'TEST_MATRIX_TOKEN': 'fixture'}):
            settings = MatrixSettings('https://example.test', '@agent_one:example.test',
                                      'TEST_MATRIX_TOKEN', ('!room:example.test',))
            adapter = MatrixAdapter('agent_one', settings, SimpleNamespace(record_activity=AsyncMock()), Path(directory))
            adapter._render_mentions = AsyncMock(side_effect=AssertionError('must not translate'))
            adapter._request = AsyncMock()
            key = SessionKey('agent_one', 'matrix', '!room:example.test:main')
            for kind in ('final', 'ack', 'progress', 'error', 'steered'):
                for token in ('@here', '@everyone', '@channel', '@room'):
                    with self.subTest(kind=kind, token=token), self.assertRaises(ReplyRejected) as raised:
                        await adapter.output(key, kind, 'Please @person check ' + token, '$input')
                    self.assertEqual(str(raised.exception), BROADCAST_REJECTION)
            adapter._render_mentions.assert_not_awaited()
            adapter._request.assert_not_awaited()

    async def test_matrix_allowlist_and_literal_words_with_native_notifications(self):
        cases = [('Alert @room and @person', True, True),
                 ('Alert @here @everyone @channel', True, False),
                 (r'Literal \@room', False, False), ('Literal @ room', False, False),
                 ('Example `@room`', True, False)]
        for text, allowed, room_ping in cases:
            with self.subTest(text=text), tempfile.TemporaryDirectory() as directory, \
                 patch.dict(os.environ, {'TEST_MATRIX_TOKEN': 'fixture'}):
                settings = MatrixSettings('https://example.test', '@agent_one:example.test',
                                          'TEST_MATRIX_TOKEN', ('!room:example.test',),
                                          broadcast_allowed_room_ids=('!room:example.test',) if allowed else ())
                adapter = MatrixAdapter('agent_one', settings, SimpleNamespace(record_activity=AsyncMock()), Path(directory))
                adapter._room_members = AsyncMock(return_value={'@person:example.test': {'display_name': 'Person'}})
                adapter._request = AsyncMock(return_value={'event_id': '$posted'})
                key = SessionKey('agent_one', 'matrix', '!room:example.test:thread:$root')
                await adapter.output(key, 'final', text, '$input')
                content = adapter._request.call_args.kwargs['data']
                self.assertEqual(content['m.mentions']['room'], room_ping)
                self.assertEqual(content['body'], text.replace('@person', '@person:example.test'))
                if '@person' in text:
                    self.assertEqual(content['m.mentions']['user_ids'], ['@person:example.test'])

    def discord_adapter(self, allowed=(), parent=None):
        adapter = DiscordAdapter('agent_two', DiscordSettings('TOKEN', 1, (2,),
                                 broadcast_allowed_channel_ids=allowed),
                                 SimpleNamespace(record_activity=AsyncMock()))
        request = SimpleNamespace(
            guild=SimpleNamespace(id=1, members=[SimpleNamespace(id=3, name='person', display_name='Person',
                                                               global_name=None)], roles=[]),
            channel=SimpleNamespace(id=4 if parent else 2, parent_id=parent, members=[], send=AsyncMock(),
                                    typing=AsyncMock()), reply=AsyncMock())
        key = SessionKey('agent_two', 'discord', str(request.channel.id))
        adapter._requests[(key, '10')] = request
        return adapter, request, key

    async def test_discord_rejects_original_text_before_any_translation_or_send(self):
        adapter, request, key = self.discord_adapter()
        adapter._render_mentions = AsyncMock(side_effect=AssertionError('must not translate'))
        for kind in ('final', 'ack', 'progress', 'error', 'steered'):
            for token in ('@here', '@everyone', '@channel', '@room'):
                with self.subTest(kind=kind, token=token), self.assertRaises(ReplyRejected) as raised:
                    await adapter.output(key, kind, 'Please @person check ' + token, '10')
                self.assertEqual(str(raised.exception), BROADCAST_REJECTION)
                self.assertIs(adapter._requests[(key, '10')], request)
        adapter._render_mentions.assert_not_awaited()
        request.reply.assert_not_awaited()
        request.channel.send.assert_not_awaited()
        request.channel.typing.assert_not_awaited()

    async def test_discord_allowlist_threads_and_literal_words_with_native_notifications(self):
        for text, allowed, parent, everyone in [('Alert @everyone and @person', (2,), None, True),
                                                ('Alert @here', (2,), 2, True),
                                                ('Literal @room @channel', (2,), None, False),
                                                (r'Literal \@everyone', (), None, False),
                                                ('Literal @ everyone', (), None, False),
                                                ('Example `@everyone`', (2,), None, False)]:
            with self.subTest(text=text):
                adapter, request, key = self.discord_adapter(allowed, parent)
                await adapter.output(key, 'final', text, '10')
                content, = request.reply.call_args.args
                mentions = request.reply.call_args.kwargs['allowed_mentions']
                self.assertEqual(mentions.everyone, everyone)
                self.assertEqual(content, text.replace('@person', '<@3>'))
                self.assertEqual([user.id for user in mentions.users], [3] if '@person' in text else [])

    async def test_discord_code_spanning_chunks_does_not_authorize_broadcasts(self):
        adapter, request, key = self.discord_adapter((2,))
        text = '```\n' + ('sample ' * 400) + '@everyone\n```'
        await adapter.output(key, 'final', text, '10')
        calls = request.reply.call_args_list + request.channel.send.call_args_list
        self.assertGreater(len(calls), 1)
        self.assertTrue(all(not call.kwargs['allowed_mentions'].everyone for call in calls))

    async def test_private_feedback_reaches_resident_agent_before_safe_rewrite(self):
        from test_bridge_runtime import FakeRpc
        from bridge.runtime.pi_rpc import PiRunResult

        for transport in ('matrix', 'discord'):
            with self.subTest(transport=transport), tempfile.TemporaryDirectory() as directory, \
                 patch.dict(os.environ, {'TEST_MATRIX_TOKEN': 'fixture'}):
                clients = []

                class RevisionRpc(FakeRpc):
                    async def prompt(self, message, on_event, images=None, *, on_accepted=None):
                        self.prompts.append(message)
                        if on_accepted:
                            await on_accepted()
                        return PiRunResult('Alert @everyone and @person' if len(self.prompts) == 1
                                           else 'Alert for everyone.')

                def factory(key, session_file):
                    client = RevisionRpc(session_file)
                    clients.append(client)
                    return client

                if transport == 'matrix':
                    settings = MatrixSettings('https://example.test', '@agent_one:example.test',
                                              'TEST_MATRIX_TOKEN', ('!room:example.test',))
                    adapter = MatrixAdapter('agent_one', settings, None, Path(directory))
                    adapter._request = AsyncMock(return_value={'event_id': '$posted'})
                    key = SessionKey('agent_one', 'matrix', '!room:example.test:main')
                else:
                    adapter, request, key = self.discord_adapter()
                rt = BridgeRuntime(Path(directory), factory, adapter.output)
                adapter.runtime = rt
                await rt.submit(key, Incoming('sender', '10', 'Send an alert'))
                import asyncio
                await asyncio.wait_for(rt.wait_idle(key), timeout=3)
                self.assertEqual(len(clients), 1)
                self.assertEqual(len(clients[0].prompts), 2)
                self.assertIn(BROADCAST_REJECTION, clients[0].prompts[1])
                posted = ([call.kwargs['data']['body'] for call in adapter._request.call_args_list
                           if 'body' in call.kwargs['data']]
                          if transport == 'matrix' else [call.args[0] for call in request.reply.call_args_list])
                self.assertIn('Alert for everyone.', posted)
                self.assertTrue(all('@everyone' not in text and BROADCAST_REJECTION not in text for text in posted))

    async def test_matrix_broadcast_input_is_not_filtered(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'TEST_MATRIX_TOKEN': 'fixture'}):
            rt = SimpleNamespace(is_awake=AsyncMock(return_value=True), admit=AsyncMock(return_value=True),
                                 submit=AsyncMock(), record_wake=AsyncMock())
            settings = MatrixSettings('https://example.test', '@agent_one:example.test',
                                      'TEST_MATRIX_TOKEN', ('!room:example.test',))
            adapter = MatrixAdapter('agent_one', settings, rt, Path(directory))
            adapter._request = AsyncMock()
            event = {'type': 'm.room.message', 'event_id': '$input', 'sender': '@sender:example.test',
                     'content': {'msgtype': 'm.text', 'body': '@room incoming', 'm.mentions': {'room': True}}}
            await adapter._handle_event('!room:example.test', event)
            rt.submit.assert_awaited_once()
            adapter._request.assert_not_awaited()

    async def test_discord_broadcast_input_is_not_filtered(self):
        adapter, request, key = self.discord_adapter()
        user = SimpleNamespace(id=9)
        adapter.client = SimpleNamespace(user=user)
        request.author = SimpleNamespace(id=11, bot=True, display_name='Agent')
        request.id = 10
        request.content = '@everyone incoming'
        request.mentions = [user]
        request.attachments = []
        adapter.runtime.admit = AsyncMock(return_value=True)
        adapter.runtime.is_awake = AsyncMock(return_value=True)
        adapter.runtime.submit = AsyncMock()
        adapter.runtime.record_wake = AsyncMock()
        await adapter._on_message(request)
        adapter.runtime.submit.assert_awaited_once()
        request.reply.assert_not_awaited()

    async def test_discord_requester_fallback_resolves_mention_when_cache_empty(self):
        """Regression: when guild members are not cached, resolve the author."""
        adapter = DiscordAdapter('agent_two', DiscordSettings('TOKEN', 1, (2,)),
                                 SimpleNamespace(record_activity=AsyncMock()))
        author = SimpleNamespace(id=300779287412473867, name='user',
                                 display_name='user', global_name=None)
        request = SimpleNamespace(
            guild=SimpleNamespace(id=1, members=[], roles=[]),
            channel=SimpleNamespace(id=2, parent_id=None, members=[],
                                    send=AsyncMock(), typing=AsyncMock()),
            author=author, reply=AsyncMock())
        key = SessionKey('agent_two', 'discord', '2')
        adapter._requests[(key, '10')] = request
        await adapter.output(key, 'final', 'Hey @user, please review this', '10')
        content, = request.reply.call_args.args
        self.assertEqual(content, 'Hey <@300779287412473867>, please review this')
        mentions = request.reply.call_args.kwargs['allowed_mentions']
        self.assertEqual([user.id for user in mentions.users], [300779287412473867])

    async def test_discord_unknown_mention_returns_private_roster_feedback(self):
        """An unresolved handle is never posted; Pi gets the roster and rewrites."""
        adapter = DiscordAdapter('agent_two', DiscordSettings('TOKEN', 1, (2,)),
                                 SimpleNamespace(record_activity=AsyncMock()))
        author = SimpleNamespace(id=415673811858620417, name='beezelbee',
                                 display_name='beezelbee', global_name=None)
        request = SimpleNamespace(
            guild=SimpleNamespace(id=1, members=[], roles=[]),
            channel=SimpleNamespace(id=2, parent_id=None, members=[],
                                    send=AsyncMock(), typing=AsyncMock()),
            author=author, reply=AsyncMock())
        key = SessionKey('agent_two', 'discord', '2')
        adapter._requests[(key, '10')] = request
        with self.assertRaisesRegex(ReplyRejected, 'Canonical targets: beezelbee'):
            await adapter.output(key, 'final', 'Hey @notinthechannel, look here', '10')
        request.reply.assert_not_awaited()
