"""SIGHUP adopts a changed private profile without restarting live transports."""
import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from bridge.config import Profile
from bridge.reload import reload, restart_required, sighup


class ProfileReloadTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.path = self.root / 'profile.json'
        self.rooms = ['!first:example.test', '!second:example.test']
        self.raw = {
            'agent': 'agent_two', 'work_dir': str(self.root), 'state_dir': str(self.root),
            'pi_executable': '/usr/bin/pi', 'pi_agent_dir': str(self.root),
            'provider': 'test', 'model': 'test-model',
            'identity_files': [str(self.root / 'AGENTS.md')],
            'matrix': {
                'homeserver': 'https://matrix.example.test',
                'user_id': '@agent_two-agent:example.test',
                'display_name': 'Agent_two',
                'hookshot_user_id': '@hookshot:example.test',
                'token_env': 'TEST_MATRIX_TOKEN',
                'rooms': list(self.rooms),
                'startup_room_id': self.rooms[0],
                'wake_on_any_room_ids': [self.rooms[0]],
            },
            'discord': {
                'token_env': 'TEST_DISCORD_TOKEN', 'guild_id': 111,
                'channel_ids': [222, 333], 'wake_on_any_channel_ids': [222],
            },
        }
        environment = patch.dict(os.environ, {
            'COUNCIL_MATRIX_ROOM_ID': '!council:example.test',
            'TEST_MATRIX_TOKEN': 'transport-secret',
            'OTHER_TOKEN': 'transport-secret',
            'TEST_DISCORD_TOKEN': 'transport-secret',
            'OTHER': 'transport-secret',
        }, clear=True)
        environment.start()
        self.addCleanup(environment.stop)

    def profile(self, raw=None):
        self.path.write_text(json.dumps(self.raw if raw is None else raw))
        return Profile.load(self.path)

    def services(self, profile):
        return {
            'adapters': {'matrix': SimpleNamespace(settings=profile.matrix),
                         'discord': SimpleNamespace(settings=profile.discord)},
            'room_leases': SimpleNamespace(rooms=frozenset(profile.matrix.rooms)),
            'review_queue': SimpleNamespace(matrix=profile.matrix),
            'runtime': SimpleNamespace(image_captioner=None),
            'activity': SimpleNamespace(generate=profile.generate_activity),
        }

    def changed(self, change):
        raw = json.loads(json.dumps(self.raw))
        change(raw)
        return raw

    def test_unchanged_profile_needs_no_restart(self):
        profile = self.profile()
        self.assertEqual(restart_required(profile, profile), ())

    def test_live_routing_session_and_identity_changes_apply_in_place(self):
        current = self.profile()
        updated = self.profile(self.changed(lambda raw: raw.update({
            'model': 'other-model', 'image_model': 'vision-model',
            'pi_extra_args': ['--approve'],
            'matrix': dict(raw['matrix'], rooms=[*self.rooms, '!third:example.test'],
                           display_name='Agent_two R.'),
            'discord': dict(raw['discord'], channel_ids=[222, 333, 444]),
        })))
        self.assertEqual(restart_required(current, updated), ())

    def test_open_transports_and_private_paths_need_a_restart(self):
        current = self.profile()

        def matrix_field(name, value):
            return lambda raw: raw['matrix'].__setitem__(name, value)

        cases = {
            'work_dir': lambda raw: raw.update(work_dir=str(self.root / 'other')),
            'state_dir': lambda raw: raw.update(state_dir=str(self.root / 'other')),
            'pi_executable': lambda raw: raw.update(pi_executable='/opt/pi'),
            'pi_agent_dir': lambda raw: raw.update(pi_agent_dir=str(self.root / 'agent')),
            'pi_path_prefix': lambda raw: raw.update(pi_path_prefix=['/opt/bin']),
            'matrix.homeserver': matrix_field('homeserver', 'https://other.test'),
            'matrix.user_id': matrix_field('user_id', '@other:example.test'),
            'matrix.token_env': matrix_field('token_env', 'OTHER_TOKEN'),
            'discord.guild_id': lambda raw: raw['discord'].__setitem__('guild_id', 999),
            'discord.token_env': lambda raw: raw['discord'].__setitem__('token_env', 'OTHER'),
        }
        for name, change in cases.items():
            with self.subTest(setting=name):
                updated = self.profile(self.changed(change))
                self.assertIn(name, restart_required(current, updated))

    def test_changing_the_agent_or_disabling_a_transport_needs_a_restart(self):
        current = self.profile()
        other_agent = self.profile(self.changed(lambda raw: raw.update(agent='agent_four')))
        self.assertIn('agent', restart_required(current, other_agent))
        one_transport = self.profile(self.changed(
            lambda raw: (raw.update(agent='agent_four'), raw.pop('discord'))))
        self.assertIn('discord', restart_required(current, one_transport))

    def test_review_queue_topology_is_created_once(self):
        current = self.profile()
        enabled = replace(current, matrix=replace(current.matrix, pr_review_rooms=(self.rooms[0],)))
        self.assertIn('matrix.pr_review_rooms', restart_required(current, enabled))
        self.assertEqual(restart_required(enabled, enabled), ())
        widened = replace(enabled, matrix=replace(enabled.matrix, pr_review_rooms=(self.rooms[1],)))
        self.assertEqual(restart_required(enabled, widened), ())

    def test_reload_points_every_running_service_at_the_new_profile(self):
        current = self.profile()
        services = self.services(current)
        raw = self.changed(lambda value: (
            value.update(model='other-model', image_model='vision-model',
                         default_mode='smart',
                         modes={'fast': {'provider': 'test', 'model': 'other-model'},
                                'smart': {'provider': 'test', 'model': 'best-model'}},
                         identity_files=[str(self.root / 'AGENTS.md'),
                                         str(self.root / 'PROFILE.md')]),
            value['matrix'].update(rooms=[*self.rooms, '!third:example.test'],
                                   display_name='Agent_two R.'),
            value['discord'].update(channel_ids=[222]),
        ))
        self.profile(raw)
        updated = reload(self.path, current, **services)
        self.assertIsNotNone(updated)
        self.assertEqual(updated.model, 'other-model')
        self.assertEqual(services['adapters']['matrix'].settings, updated.matrix)
        self.assertEqual(services['adapters']['discord'].settings, updated.discord)
        self.assertEqual(services['room_leases'].rooms,
                         frozenset([*self.rooms, '!third:example.test']))
        self.assertEqual(services['review_queue'].matrix, updated.matrix)
        self.assertEqual(services['runtime'].modes, updated.conversation_modes)
        self.assertEqual(services['runtime'].default_mode, 'smart')
        self.assertIs(services['runtime'].image_captioner.__self__, updated)
        self.assertIs(services['runtime'].progress_summarizer.__self__, updated)
        self.assertIs(services['activity'].generate.__self__, updated)

    def test_damaged_profile_keeps_the_running_configuration(self):
        current = self.profile()
        services = self.services(current)
        self.path.write_text('{"agent": "agent_two",')
        with self.assertLogs('agent_bridge.reload', level='WARNING') as logged:
            self.assertIsNone(reload(self.path, current, **services))
        self.assertIn('keeping the running profile', '\n'.join(logged.output))
        self.assertEqual(services['adapters']['matrix'].settings, current.matrix)
        self.assertEqual(services['adapters']['discord'].settings, current.discord)

    def test_structurally_changed_profile_keeps_the_running_configuration(self):
        current = self.profile()
        services = self.services(current)
        services['runtime'].modes = dict(current.conversation_modes)
        services['runtime'].default_mode = current.default_mode
        self.profile(self.changed(lambda raw: raw.update(
            state_dir=str(self.root / 'new'), model='other-model')))
        with self.assertLogs('agent_bridge.reload', level='WARNING') as logged:
            self.assertIsNone(reload(self.path, current, **services))
        summary = '\n'.join(logged.output)
        self.assertIn('needs a restart', summary)
        self.assertIn('state_dir', summary)
        self.assertNotIn(str(self.root), summary)
        # One restart-required field refuses the whole file, including the
        # live-looking fields adopted alongside it.
        self.assertEqual(services['adapters']['matrix'].settings, current.matrix)
        self.assertEqual(services['adapters']['matrix'].settings.rooms,
                         tuple(current.matrix.rooms))
        self.assertEqual(services['room_leases'].rooms, frozenset(current.matrix.rooms))
        self.assertEqual(services['runtime'].modes, dict(current.conversation_modes))
        self.assertEqual(services['runtime'].default_mode, current.default_mode)

    def test_structurally_changed_profile_keeps_the_running_services_when_rooms_change(self):
        current = self.profile()
        services = self.services(current)
        services['runtime'].modes = dict(current.conversation_modes)
        self.profile(self.changed(lambda raw: (
            raw.update(work_dir=str(self.root / 'other')),
            raw['matrix'].update(rooms=[*self.rooms, '!third:example.test']))))
        with self.assertLogs('agent_bridge.reload', level='WARNING'):
            self.assertIsNone(reload(self.path, current, **services))
        self.assertEqual(services['adapters']['matrix'].settings, current.matrix)
        self.assertEqual(services['room_leases'].rooms, frozenset(current.matrix.rooms))
        self.assertEqual(services['runtime'].modes, dict(current.conversation_modes))

    def test_sighup_reloads_then_checkpoints_once(self):
        current = self.profile()
        services = self.services(current)
        calls: list[int] = []

        async def sweep() -> int:
            calls.append(1)
            return 2

        self.profile(self.changed(lambda raw: raw.update(model='other-model')))
        with self.assertLogs('agent_bridge.reload', level='INFO') as logged:
            updated = asyncio.run(sighup(self.path, current, sweep=sweep, **services))
        self.assertEqual(updated.model, 'other-model')
        self.assertEqual(len(calls), 1)
        summary = '\n'.join(logged.output)
        self.assertIn('SIGHUP reloaded the bridge profile', summary)
        self.assertIn('SIGHUP checkpointed 2 idle conversations', summary)

    def test_sighup_checkpoints_when_the_reload_is_rejected(self):
        current = self.profile()
        services = self.services(current)
        calls: list[int] = []

        async def sweep() -> int:
            calls.append(1)
            return 0

        self.path.write_text('{"agent": "agent_two",')
        with self.assertLogs('agent_bridge.reload', level='WARNING') as logged:
            kept = asyncio.run(sighup(self.path, current, sweep=sweep, **services))
        self.assertIs(kept, current)
        self.assertEqual(len(calls), 1)
        self.assertIn('keeping the running profile', '\n'.join(logged.output))
        self.assertEqual(services['adapters']['matrix'].settings, current.matrix)

    def test_sighup_keeps_an_adopted_profile_when_the_checkpoint_fails(self):
        current = self.profile()
        services = self.services(current)
        calls: list[int] = []

        async def sweep() -> int:
            calls.append(1)
            raise RuntimeError('checkpoint failed')

        self.profile(self.changed(lambda raw: raw.update(model='other-model')))
        with self.assertLogs('agent_bridge.reload', level='ERROR') as logged:
            updated = asyncio.run(sighup(self.path, current, sweep=sweep, **services))
        self.assertEqual(updated.model, 'other-model')
        self.assertEqual(len(calls), 1)
        self.assertIn('SIGHUP checkpoint sweep failed', '\n'.join(logged.output))


if __name__ == '__main__':
    unittest.main()
