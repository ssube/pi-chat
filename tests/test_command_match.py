import unittest
from unittest.mock import patch

from tools.ledger.command_match import (command_forms, exit_code_from_output, parse_or_abstain,
                                   publish_example, rank_examples, stderr_suppression_check,
                                   store_parse)
from tools.ledger.error_cli import structured_match


class Cursor:
    def __init__(self, rows=()):
        self.rows = rows
        self.statements = []
        self.rowcount = 1

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, sql, params=None):
        self.statements.append((sql, params))

    def fetchall(self):
        return self.rows


class Connection:
    def __init__(self, rows=()):
        self.current = Cursor(rows)

    def cursor(self):
        return self.current

    def transaction(self):
        return self.current


class CommandMatchTests(unittest.TestCase):
    def test_chain_and_steps_keep_structure_without_literal_paths(self):
        parsed = parse_or_abstain('git add /private/file && git commit -m "done"',
                                  'Command exited with code 1', 1)
        self.assertEqual(parsed['status'], 'parsed')
        self.assertEqual(parsed['whole_form'], 'git add $PATH && git commit -m $MESSAGE')
        self.assertEqual([step['command_name'] for step in parsed['steps']],
                         ['git add', 'git commit'])
        self.assertTrue(parsed['execution_uncertain'])
        self.assertEqual(parsed['error_class'], 'none')
        cursor = Cursor()
        store_parse(cursor, 'pierr-123e4567-e89b-12d3-a456-426614174000', parsed)
        self.assertEqual(len(cursor.statements), 3)
        self.assertNotIn('/private/file', str(cursor.statements))

    def test_semicolon_and_glob_roles_disambiguate_ls(self):
        chain = parse_or_abstain('echo ok; grep -c failure log.txt', '0', 1)
        self.assertEqual(chain['step_count'], 2)
        self.assertEqual(chain['operators'], [';'])
        self.assertEqual(chain['command_name'], 'grep')
        glob = parse_or_abstain('ls *.toml *.yaml', 'No such file or directory', 2)
        partial = parse_or_abstain('ls present.txt missing.txt', 'No such file or directory', 2)
        self.assertEqual(glob['arg_roles'], ['glob', 'glob'])
        self.assertEqual(partial['arg_roles'], ['path', 'path'])
        secret_option = parse_or_abstain('curl -psecret --token=example https://host.invalid', '', 22)
        self.assertNotIn('secret', secret_option['whole_form'])
        self.assertNotIn('example', secret_option['whole_form'])

    def test_exit_and_error_class_do_not_infer_hidden_stderr(self):
        self.assertEqual(exit_code_from_output('some lines\nCommand exited with code 2'), 2)
        self.assertIsNone(exit_code_from_output('some lines'))
        parsed = parse_or_abstain('ls missing 2>/dev/null', 'Command exited with code 2', 2)
        self.assertEqual(parsed['error_class'], 'none')
        self.assertTrue(parsed['stderr_suppressed'])
        self.assertFalse(parsed['output_present'])

    def test_suppression_check_excludes_intentional_conditions(self):
        self.assertIsNone(stderr_suppression_check(
            'if grep -q x f 2>/dev/null; then echo found; fi', True))
        self.assertIsNone(stderr_suppression_check(
            'while read -r line 2>/dev/null; do echo "$line"; done', True))
        for command in ('cp "$SOURCE" "$DEST" 2>/dev/null',
                        'tar -czf "$ARCHIVE" -C "$DIR" "$FILE" 2>/dev/null',
                        'curl -fsS "$URL" 2>/dev/null',
                        'set -e; VAR=$(false 2>/dev/null); echo "$VAR"'):
            self.assertIsNotNone(stderr_suppression_check(command, True), command)
        self.assertIsNone(stderr_suppression_check('ls x 2>/dev/null', False))

    def test_structured_ranker_filters_near_misses_and_limits_to_approved_fixes(self):
        rows = [
            ('ls-exit-code-two-partial-missing', 'ls $PATH $PATH',
             'ls $PATH $PATH', 1, None, False, 'missing_path', True,
             [], ['path', 'path'], True, 'a' * 64),
            ('bash-glob-no-match-exit-2', 'ls $GLOB $GLOB',
             'ls $GLOB $GLOB', 1, None, False, 'missing_path', True,
             [], ['glob', 'glob'], True, 'b' * 64),
        ]
        fixes = [
            {'id': 'ls-exit-code-two-partial-missing', 'content_sha256': 'a' * 64,
             'seen_count': 5},
            {'id': 'bash-glob-no-match-exit-2', 'content_sha256': 'b' * 64,
             'seen_count': 3},
        ]
        with patch('tools.ledger.error_ledger.approved_fixes', return_value=fixes):
            partial = parse_or_abstain('ls present.txt absent.txt',
                                       'ls: cannot access absent.txt: No such file or directory', 2)
            found = rank_examples(Connection(rows), partial, 'agent_one', set())
            self.assertEqual([item['fix_id'] for item in found],
                             ['ls-exit-code-two-partial-missing'])
            self.assertEqual(found[0]['disposition'], 'conditional_hint')
            glob = parse_or_abstain('ls *.toml *.yaml',
                                    'ls: cannot access *.toml: No such file or directory', 2)
            found = rank_examples(Connection(rows), glob, 'agent_one', set())
            self.assertEqual([item['fix_id'] for item in found],
                             ['bash-glob-no-match-exit-2'])
            self.assertEqual(rank_examples(Connection(rows),
                parse_or_abstain('if ls x; then echo yes; fi', '', 2), 'agent_one', set()), [])

    def test_form_counts_query_whole_and_individual_commands(self):
        whole = Connection()
        command_forms(whole, since='7 days', scope='whole', limit=20)
        self.assertIn('p.whole_form', whole.current.statements[0][0])
        self.assertNotIn('pi_command_step', whole.current.statements[0][0])
        steps = Connection()
        command_forms(steps, since='7 days', scope='step', limit=20)
        self.assertIn('pi_command_step', steps.current.statements[0][0])
        self.assertIn('s.command_form', steps.current.statements[0][0])

    def test_reference_match_uses_postgres_parse_and_returns_no_command_text(self):
        row = ('pierr-123456ab', 'agent_one', 'ls /private/path 2>/dev/null',
               'Command exited with code 2', 2, 'parsed',
               'ls $PATH $REDIRECT', 'ls', 'ls $PATH', 1, [], ['path'],
               None, False, False, True, 'none', False)
        connection = Connection([row])
        with patch('tools.ledger.command_match.rank_examples', return_value=[]) as rank:
            result = structured_match(connection, 'err-123456', None, set())
        self.assertEqual(result['id'], row[0])
        self.assertEqual(result['parse_status'], 'parsed')
        self.assertEqual(result['stderr_check']['check_id'],
                         'stderr-suppressed-on-failure')
        self.assertNotIn('/private/path', str(result))
        self.assertEqual(rank.call_args.args[1]['command_form'], 'ls $PATH')

    def test_example_publication_requires_current_owner_approval_and_stores_only_form(self):
        fix = {'base_content_sha256': 'a' * 64, 'owners': ['agent_three'],
               'applies_to_agents': ['agent_one', 'agent_three']}
        document = {'id': 'partial-ls-example', 'fix_id': 'ls-exit-code-two-partial-missing',
                    'approved_digest': 'b' * 64, 'approved_by': 'agent_three',
                    'agents': ['agent_one'], 'context_note': 'An optional path was missing.',
                    'command': 'ls /private/one /private/two',
                    'output': 'No such file or directory', 'exit_code': 2}
        connection = Connection()
        with patch('tools.ledger.fix_authoring.approved_fix', return_value=fix):
            with self.assertRaisesRegex(ValueError, 'changed'):
                publish_example(connection, document)
            self.assertEqual(connection.current.statements, [])
            document['approved_digest'] = 'a' * 64
            document['approved_by'] = 'agent_two'
            with self.assertRaisesRegex(ValueError, 'owner'):
                publish_example(connection, document)
            self.assertEqual(connection.current.statements, [])
            document['approved_by'] = 'agent_three'
            self.assertEqual(publish_example(connection, document), document['id'])
        self.assertEqual(len(connection.current.statements), 1)
        sql, params = connection.current.statements[0]
        self.assertIn('pi_fix_example', sql)
        self.assertNotIn('/private/one', str(params))


if __name__ == '__main__':
    unittest.main()
