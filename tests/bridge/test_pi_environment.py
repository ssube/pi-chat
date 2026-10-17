"""Owner-selected tool environment files must not weaken session boundaries."""
import json
import os
from pathlib import Path
from dataclasses import replace
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from bridge.config import Profile
from bridge.runtime.state import SessionKey


class PiEnvironmentTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.env_file = self.root / 'tools.env'
        self.path = self.root / 'profile.json'
        self.raw = {
            'agent': 'agent_three', 'work_dir': str(self.root),
            'state_dir': str(self.root), "provider": "test", "model": "test-model", 'pi_executable': '/usr/bin/pi',
            'pi_agent_dir': str(self.root), 'pi_env_file': str(self.env_file),
            'pi_path_prefix': ['/owner/bin'],
            'matrix': {'homeserver': 'https://example.test',
                       'user_id': '@agent_three:example.test',
                       'token_env': 'TEST_MATRIX_TOKEN', 'rooms': ['!room:example.test']},
        }
        environment = patch.dict(os.environ, {

            'TEST_MATRIX_TOKEN': 'transport-secret',
            'UNRELATED_SECRET': 'ambient-secret',
            'PATH': '/parent/bin',
        }, clear=True)
        environment.start()
        self.addCleanup(environment.stop)

    def profile(self):
        self.path.write_text(json.dumps(self.raw))
        return Profile.load(self.path)

    def client(self, profile=None, **mode):
        return (profile or self.profile()).pi_client(
            SessionKey(self.raw['agent'], 'matrix', 'room'), self.root / 'session.jsonl', **mode)

    def test_generic_names_quoted_values_comments_and_exports(self):
        self.env_file.write_text(
            '# owner tool settings\nexport NEW_TOOL_CACHE="/image/tool cache"\n'
            "TOOL_LABEL='a # literal comment'\nEMPTY=\nPLAIN=value # comment\n")
        env = self.client()._env
        self.assertEqual(env['NEW_TOOL_CACHE'], '/image/tool cache')
        self.assertEqual(env['TOOL_LABEL'], 'a # literal comment')
        self.assertEqual(env['EMPTY'], '')
        self.assertEqual(env['PLAIN'], 'value')
        self.assertNotIn('TEST_MATRIX_TOKEN', env)
        self.assertNotIn('UNRELATED_SECRET', env)

    def test_no_ambient_interpolation_or_shell_execution(self):
        self.env_file.write_text(
            'LITERAL="${UNRELATED_SECRET}/$HOME/$(touch never)/`echo never`"\n')
        self.assertEqual(self.client()._env['LITERAL'],
                         '${UNRELATED_SECRET}/$HOME/$(touch never)/`echo never`')
        self.assertFalse((self.root / 'never').exists())

    def test_file_is_reloaded_for_each_new_session(self):
        self.env_file.write_text('TOOL_SETTING=first\n')
        profile = self.profile()
        first = self.client(profile)
        self.env_file.write_text('TOOL_SETTING=second\n')
        self.assertEqual(first._env['TOOL_SETTING'], 'first')
        self.assertEqual(self.client(profile)._env['TOOL_SETTING'], 'second')

    def test_missing_file_fails_normal_launch(self):
        with self.assertRaises(FileNotFoundError):
            self.client()

    def test_invalid_content_fails_without_disclosing_values(self):
        for content in ['BROKEN="private-unclosed-value\n',
                        'BAD-NAME=private-value\n', 'BARE_NAME\n',
                        'VALUE=private\0value\n']:
            with self.subTest(content_type=content.split('=')[0]):
                self.env_file.write_text(content)
                with self.assertRaisesRegex(ValueError, 'Invalid pi_env_file') as error:
                    self.client()
                self.assertNotIn('private', str(error.exception))

    def test_configured_adapter_tokens_and_review_token_are_rejected(self):
        for name in ['TEST_MATRIX_TOKEN', 'TEST_REVIEW_TOKEN']:
            with self.subTest(name=name):
                self.env_file.write_text(f'{name}=private-value\n')
                profile = self.profile()
                if name == 'TEST_REVIEW_TOKEN':
                    profile = replace(profile, reviews=SimpleNamespace(token_env=name))
                with self.assertRaisesRegex(ValueError, 'credentials|review token') as error:
                    self.client(profile)
                self.assertNotIn('private-value', str(error.exception))

    def test_configured_discord_token_is_rejected(self):
        self.raw['agent'] = 'agent_two'
        self.raw['discord'] = {'token_env': 'TEST_DISCORD_TOKEN', 'guild_id': 1,
                               'channel_ids': [2]}
        os.environ['TEST_DISCORD_TOKEN'] = 'discord-fixture'
        self.env_file.write_text('TEST_DISCORD_TOKEN=private-value\n')
        with self.assertRaisesRegex(ValueError, 'Transport credentials'):
            self.client()

    def test_tool_file_cannot_override_configured_transport_alias(self):
        self.raw['matrix']['token_env'] = 'MATRIX_TOKEN'
        os.environ['MATRIX_TOKEN'] = 'adapter-fixture'
        self.env_file.write_text('MATRIX_TOKEN=owner-coordination-fixture\n')
        with self.assertRaisesRegex(ValueError, 'Transport credentials'):
            self.client()

    def test_safe_vision_and_status_sessions_do_not_read_the_file(self):
        # Missing owner tool files must not affect tool-free session isolation.
        for mode in ['safe_mode', 'vision_mode', 'status_mode']:
            with self.subTest(mode=mode):
                env = self.client(**{mode: True})._env
                self.assertNotIn('NEW_TOOL_CACHE', env)
        self.env_file.write_text('NEW_TOOL_CACHE=/image/cache\nMATRIX_TOKEN=fixture\n')
        for mode in ['safe_mode', 'vision_mode', 'status_mode']:
            with self.subTest(mode=mode):
                env = self.client(**{mode: True})._env
                self.assertNotIn('NEW_TOOL_CACHE', env)
                self.assertNotIn('MATRIX_TOKEN', env)

    def test_session_home_agent_directory_and_path_prefix_take_precedence(self):
        self.env_file.write_text('HOME=/wrong/home\nPI_CODING_AGENT_DIR=/wrong/agent\nPATH=/tool/bin\n')
        env = self.client()._env
        self.assertEqual(env['HOME'], str(self.root))
        self.assertEqual(env['PI_CODING_AGENT_DIR'], str(self.root))
        self.assertEqual(env['PATH'], '/owner/bin' + os.pathsep + '/tool/bin')
        self.assertEqual(os.environ['PATH'], '/parent/bin')
        self.assertNotIn('PI_CODING_AGENT_DIR', os.environ)

    def test_optional_profile_works_without_a_file(self):
        del self.raw['pi_env_file']
        self.assertEqual(self.client()._env['PATH'], '/owner/bin' + os.pathsep + '/parent/bin')

    def test_progress_summary_session_has_no_tools_identity_or_coordination(self):
        self.raw['identity_files'] = [str(self.root / 'missing-private-identity.md')]
        self.raw['pi_pass_env'] = ['UNRELATED_SECRET']
        client = self.client(safe_mode=True, progress_mode=True)
        self.assertIn('--no-tools', client._command)
        self.assertEqual(client._command[client._command.index('--thinking') + 1], 'off')
        self.assertNotIn('UNRELATED_SECRET', client._env)
        self.assertIn('--no-extensions', client._command)
        self.assertNotIn('MATRIX_ROOM_ID', client._env)
        self.assertNotIn('AGENT_BRIDGE_HISTORY_SOCKET', client._env)
        self.assertNotIn('TEST_MATRIX_TOKEN', client._env)
        prompt = (self.root / 'bridge-system.txt').read_text()
        self.assertIn('Summarize the ongoing work', prompt)
        self.assertIn('untrusted context', prompt)
        self.assertNotIn('Local agent identity', prompt)

    def test_health_socket_path_is_exported_only_to_normal_sessions(self):
        del self.raw['pi_env_file']
        self.assertEqual(self.client()._env['AGENT_BRIDGE_HEALTH_SOCKET'],
                         str(self.root / 'bridge-health.sock'))
        self.assertNotIn('AGENT_BRIDGE_HEALTH_SOCKET', self.client(safe_mode=True)._env)

    def test_env_file_path_validation_matches_other_profile_paths(self):
        for value in ['', 123, []]:
            with self.subTest(value=value):
                self.raw['pi_env_file'] = value
                with self.assertRaisesRegex(ValueError, 'pi_env_file'):
                    self.profile()
        self.raw['pi_env_file'] = 'owner-tool.env'
        self.assertEqual(self.profile().pi_env_file, Path('owner-tool.env').resolve())


if __name__ == '__main__':
    unittest.main()
