"""Offline exporter checks. Every transcript here is synthetic."""
from __future__ import annotations

import base64
import hashlib
from html.parser import HTMLParser
import json
import io
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from tools.export_session import (JS, chapter_request, chapter_turns, inline, load_chapters,
                                  load_session, main, markdown, redact, render_session,
                                  select_entries, validate_chapters)


HEADER = {'type': 'session', 'version': 3, 'id': 'synthetic-session', 'timestamp': '2026-01-01T10:00:00Z', 'cwd': '/PRIVATE_CWD'}


def message(entry_id, parent, role, content, **fields):
    return {'type': 'message', 'id': entry_id, 'parentId': parent,
            'timestamp': '2026-01-01T10:01:00Z',
            'message': {'role': role, 'content': content, **fields}}


class Document(HTMLParser):
    def __init__(self, source):
        super().__init__()
        self.elements = []
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'session.jsonl'
        self.output = self.root / 'session.html'

    def write(self, entries, header=HEADER):
        self.source.write_text('\n'.join(json.dumps(e) for e in [header, *entries]) + '\n', encoding='utf-8')

    def test_bom_blank_lines_and_legacy(self):
        self.source.write_text('\ufeff' + json.dumps({'type': 'session', 'version': 1}) + '\n\n' + json.dumps({'type': 'message', 'message': {'role': 'user', 'content': 'Hello'}}), encoding='utf-8')
        _, entries = load_session(self.source)
        self.assertEqual(entries[0]['id'], 'legacy-1')
        self.assertIsNone(entries[0]['parentId'])
        self.assertEqual(select_entries(entries, None, False), entries)

    def test_branches_explicit_leaf_and_multiple_roots(self):
        entries = [message('a', None, 'user', 'root'), message('b', 'a', 'assistant', 'old branch'),
                   message('c', 'a', 'assistant', 'new branch'), message('d', None, 'user', 'new root')]
        self.assertEqual([e['id'] for e in select_entries(entries, None, False)], ['d'])
        self.assertEqual([e['id'] for e in select_entries(entries, 'c', False)], ['a', 'c'])
        self.assertEqual(select_entries(entries, None, True), entries)
        with self.assertRaisesRegex(ValueError, 'leaf'):
            select_entries(entries, 'absent', False)

    def test_rejects_bad_json_without_echoing_content(self):
        self.source.write_text(json.dumps(HEADER) + '\nPRIVATE_SECRET invalid', encoding='utf-8')
        result = subprocess.run([sys.executable, str(Path(__file__).parents[1] / 'tools/export_session.py'), str(self.source)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn('line 2', result.stderr)
        self.assertNotIn('PRIVATE_SECRET', result.stderr)
        self.assertFalse(self.output.exists())

    def test_invalid_records_and_graphs(self):
        cases = [
            [42], [{}], [{'type': 'session'}],
            [message('a', 'missing', 'user', 'x')],
            [message('a', 'a', 'user', 'x')],
            [message('a', None, 'user', 'x'), message('a', None, 'user', 'y')],
            [{'type': 'message', 'id': 'a', 'parentId': None, 'message': None}],
            [{'type': 'custom', 'id': [], 'parentId': None}],
        ]
        for records in cases:
            with self.subTest(records=records):
                self.write(records)
                with self.assertRaises(ValueError):
                    load_session(self.source)
        self.write([], {**HEADER, 'version': 999})
        with self.assertRaises(ValueError):
            load_session(self.source)

    def test_default_omissions_are_not_merely_hidden(self):
        entries = [message('s', None, 'system', 'PRIVATE_SYSTEM', sections={'skills': 'PRIVATE_SKILLS'}),
                   message('u', 's', 'user', [{'type': 'image', 'mimeType': 'image/png', 'data': 'PRIVATE_IMAGE'}, {'type': 'text', 'text': 'Hello'}]),
                   message('a', 'u', 'assistant', [{'type': 'thinking', 'thinking': 'PRIVATE_THINKING', 'thinkingSignature': 'PRIVATE_SIGNATURE'}, {'type': 'text', 'text': 'Hi'}]),
                   {'type': 'custom', 'id': 'c', 'parentId': 'a', 'data': 'PRIVATE_STATE'},
                   {'type': 'custom_message', 'id': 'h', 'parentId': 'c', 'display': False, 'content': 'PRIVATE_HIDDEN'}]
        output = render_session(HEADER, entries)
        self.assertNotIn('PRIVATE_', output)
        self.assertIn('Image attachment', output)
        included = render_session(HEADER, entries, thinking=True, system=True)
        self.assertIn('PRIVATE_SYSTEM', included)
        self.assertIn('PRIVATE_SKILLS', included)
        self.assertIn('PRIVATE_THINKING', included)
        self.assertNotIn('PRIVATE_SIGNATURE', included)

    def test_tools_errors_shell_and_summary(self):
        entries = [message('a', None, 'assistant', [{'type': 'toolCall', 'name': 'read', 'arguments': {'path': 'tool-argument-sentinel'}}]),
                   message('t', 'a', 'toolResult', [{'type': 'text', 'text': '<b>tool-output-sentinel</b>'}], toolName='read', isError=True),
                   message('b', 't', 'bashExecution', [], command='echo test', output='test', exitCode=0),
                   {'type': 'compaction', 'id': 'c', 'parentId': 'b', 'summary': 'Summary text', 'systemMessage': {'content': 'PRIVATE_CHECKPOINT'}},
                   {'type': 'context_edit', 'id': 'e', 'parentId': 'c', 'replacement': 'PRIVATE_REPLACEMENT'}]
        output = render_session(HEADER, entries)
        self.assertIn('Show tool output · error', output)
        self.assertIn('&lt;b&gt;tool-output-sentinel&lt;/b&gt;', output)
        self.assertIn('$ echo test', output)
        self.assertIn('Summary text', output)
        self.assertNotIn('PRIVATE_', output)
        without = render_session(HEADER, entries, tools=False)
        self.assertNotIn('tool-argument-sentinel', without)
        self.assertNotIn('tool-output-sentinel', without)
        self.assertIn('$ echo test', without)

    def test_html_injection_and_content_security_policy(self):
        attack = '</script><script>alert(1)</script><img src="https://bad.invalid/x" onerror="alert(2)">'
        output = render_session(HEADER, [message('u', None, 'user', attack)], title=attack)
        document = Document(output)
        self.assertEqual(sum(tag == 'script' for tag, _ in document.elements), 1)
        self.assertFalse(any(tag in {'img', 'iframe', 'object', 'link'} for tag, _ in document.elements))
        self.assertNotIn(attack, output)
        self.assertFalse(any(key.startswith('on') for _, attrs in document.elements for key in attrs))
        digest = base64.b64encode(hashlib.sha256(JS.encode()).digest()).decode()
        csp = next(attrs['content'] for tag, attrs in document.elements if tag == 'meta' and attrs.get('http-equiv') == 'Content-Security-Policy')
        self.assertIn("default-src 'none'", csp)
        self.assertIn(f"'sha256-{digest}'", csp)
        self.assertNotIn("script-src 'unsafe-inline'", csp)

    def test_markdown_and_safe_links(self):
        output = markdown('# Heading\n\n**Bold** and `code`\n\n- one\n- two\n\n> quote\n\n```python\nprint("<safe>")\n```')
        for expected in ('<h3>Heading</h3>', '<strong>Bold</strong>', '<code>code</code>', '<ul>', '<li>two</li>', '<blockquote>quote</blockquote>', '&lt;safe&gt;'):
            self.assertIn(expected, output)
        for url in ('javascript:alert', 'data:text/html,pwn', '//remote.invalid', 'file:///etc/passwd'):
            self.assertNotIn('<a ', inline(f'[x]({url})'))
        self.assertIn('href="https://example.org"', inline('[safe](https://example.org)'))
        self.assertNotIn('<a ', inline('![image](https://example.org/pixel.png)'))

    def test_private_output_no_clobber_and_force(self):
        self.write([message('u', None, 'user', 'Hello')])
        self.assertEqual(main([str(self.source)]), 0)
        self.assertEqual(stat.S_IMODE(self.output.stat().st_mode), 0o600)
        self.output.write_text('keep-me', encoding='utf-8')
        self.assertEqual(main([str(self.source)]), 1)
        self.assertEqual(self.output.read_text(), 'keep-me')
        self.assertEqual(main([str(self.source), '--force']), 0)
        self.assertIn('Hello', self.output.read_text())
        original = self.source.read_bytes()
        self.assertEqual(main([str(self.source), '-o', str(self.source), '--force']), 1)
        self.assertEqual(self.source.read_bytes(), original)
        link = self.root / 'hardlink.html'
        link.hardlink_to(self.source)
        self.assertEqual(main([str(self.source), '-o', str(link), '--force']), 1)
        self.assertEqual(self.source.read_bytes(), original)

    def test_empty_session_and_cli_branch_selection(self):
        self.write([])
        self.assertEqual(main([str(self.source)]), 0)
        self.assertIn('No displayable entries', self.output.read_text())
        self.write([message('a', None, 'user', 'question'), message('b', 'a', 'assistant', 'abandoned-sentinel'), message('c', 'a', 'assistant', 'selected-sentinel')])
        self.assertEqual(main([str(self.source), '--force']), 0)
        self.assertNotIn('abandoned-sentinel', self.output.read_text())
        self.assertIn('selected-sentinel', self.output.read_text())
        self.assertEqual(main([str(self.source), '--force', '--leaf', 'b']), 0)
        self.assertIn('abandoned-sentinel', self.output.read_text())
        self.assertNotIn('selected-sentinel', self.output.read_text())
        self.assertEqual(main([str(self.source), '--force', '--all-branches']), 0)
        self.assertIn('abandoned-sentinel', self.output.read_text())
        self.assertIn('Parent: a', self.output.read_text())

    def test_copied_script_runs_without_repository_or_pi(self):
        self.write([message('u', None, 'user', 'Standalone check')])
        standalone = self.root / 'export.py'
        standalone.write_bytes((Path(__file__).parents[1] / 'tools/export_session.py').read_bytes())
        result = subprocess.run([sys.executable, '-I', str(standalone), str(self.source)],
                                cwd=self.root, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Standalone check', self.output.read_text())

    def test_frontend_filter_expand_and_hash_with_node(self):
        # Exercise the exact embedded JS against a tiny DOM; no browser download.
        stub = r'''
const assert = require('node:assert/strict');
const controls = Object.fromEntries(['search','role','count','empty','expand'].map(id => [id, {value: id === 'role' ? 'all' : '', addEventListener(type, fn) {this[type] = fn;}}]));
const detail = {open:false};
const fakeChapters = [{dataset:{chapter:'1'}}, {dataset:{chapter:'2'}, classList:{contains:name=>name==='chapter-marker'}, scrollIntoView(){}}];
const fakeCards = [{textContent:'Hello user', dataset:{role:'user',chapter:'1'}, querySelectorAll:()=>[], classList:{contains:()=>true}, scrollIntoView(){}}, {textContent:'read output secret needle', dataset:{role:'tool',chapter:'2'}, querySelectorAll:()=>[detail], classList:{contains:()=>true}, scrollIntoView(){}}];
global.document = {documentElement:{classList:{add(){}}}, querySelectorAll:selector=>selector==='.message'?fakeCards:fakeChapters, getElementById:id => id === 'entry-2' ? fakeCards[1] : id === 'chapter-2' ? fakeChapters[1] : controls[id]};
global.location = {hash:''};
global.window = {addEventListener(type, fn){this[type] = fn;}};
'''
        checks = r'''
assert.equal(controls.count.textContent, '2 of 2 entries shown');
controls.search.value = 'NEEDLE'; controls.search.input();
assert.equal(cards[0].hidden, true); assert.equal(cards[1].hidden, false);
assert.equal(fakeChapters[0].hidden, true); assert.equal(fakeChapters[1].hidden, false);
controls.expand.click(); assert.equal(detail.open, true);
controls.expand.click(); assert.equal(detail.open, false);
controls.role.value = 'user'; controls.role.change(); assert.equal(controls.empty.hidden, false);
location.hash = '#entry-2'; window.hashchange();
assert.equal(cards[1].hidden, false); assert.equal(controls.search.value, '');
controls.search.value = 'missing'; controls.search.input();
location.hash = '#chapter-2'; window.hashchange();
assert.equal(fakeChapters[1].hidden, false); assert.equal(controls.search.value, '');
'''
        result = subprocess.run(['node', '-e', stub + JS + checks], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


class ChapterTests(unittest.TestCase):
    def test_light_redaction(self):
        sensitive = '''Contact person@example.invalid via https://private.invalid/a?token=hidden.
Bearer synthetic-bearer-value
OLLAMA_API_KEY="synthetic-key-value"
{"password": "synthetic pass phrase"}
secret=synthetic-secret
sk-syntheticlongtokenvalue
host.internal git.home.example.invalid 192.168.1.22
/home/alice/private/file.txt C:\\Users\\alice\\private.txt
-----BEGIN PRIVATE KEY-----\nsynthetic material\n-----END PRIVATE KEY-----'''
        clean = redact(sensitive)
        for value in ('person@', 'private.invalid', 'synthetic-', 'synthetic pass', 'synthetic material', 'host.internal', 'example.invalid', '192.168', '/home/alice', 'Users\\alice'):
            self.assertNotIn(value, clean)
        self.assertIn('[EMAIL]', clean)
        self.assertIn('[CREDENTIAL]', clean)
        self.assertIn('[PRIVATE KEY]', clean)

    def test_only_user_and_assistant_text_are_submitted(self):
        entries = [message('s', None, 'system', 'OMIT_SYSTEM'),
                   message('u', 's', 'user', [{'type': 'text', 'text': 'Please inspect /home/alice/file'}, {'type': 'image', 'data': 'OMIT_IMAGE'}]),
                   message('a', 'u', 'assistant', [{'type': 'text', 'text': 'Investigating'}, {'type': 'thinking', 'thinking': 'OMIT_THINKING'}, {'type': 'toolCall', 'name': 'read', 'arguments': {'path': 'OMIT_ARGUMENT'}}]),
                   message('t', 'a', 'toolResult', 'OMIT_RESULT'),
                   message('b', 't', 'bashExecution', '', command='OMIT_SHELL', output='OMIT_SHELL'),
                   {'type': 'compaction', 'id': 'c', 'parentId': 'b', 'summary': 'OMIT_COMPACTION'},
                   {'type': 'custom_message', 'id': 'x', 'parentId': 'c', 'content': 'OMIT_EXTENSION'}]
        turns = chapter_turns(entries)
        request = chapter_request(turns)
        text = json.dumps(request)
        self.assertNotIn('OMIT_', text)
        self.assertNotIn('/home/alice', text)
        self.assertNotIn('entry-2', text)  # Anchors stay local.
        self.assertEqual(turns[0]['anchor'], 'entry-2')
        self.assertEqual(request['model'], 'deepseek-v4.1-flash')
        self.assertFalse(request['think'])
        self.assertIn('Investigating', text)

    def test_request_is_bounded_without_losing_tail_turns(self):
        turns = [{'turn': i, 'anchor': f'entry-{i}', 'user': 'question ' * 1500, 'assistant': 'answer ' * 1500} for i in range(1, 401)]
        request = chapter_request(turns)
        self.assertLessEqual(len(json.dumps(request, ensure_ascii=False).encode()), 80000)
        digest = json.loads(request['messages'][1]['content'])
        self.assertEqual(len(digest), 400)
        self.assertEqual(digest[-1]['turn'], 400)
        with self.assertRaises(ValueError):
            chapter_request([])

    def test_invalid_model_chapters_are_rejected(self):
        for chapters in ([], [{'start_turn': 2, 'title': 'x', 'summary': 'y'}],
                         [{'start_turn': True, 'title': 'x', 'summary': 'y'}],
                         [{'start_turn': 1, 'title': '', 'summary': 'y'}],
                         [{'start_turn': 1, 'title': 'x', 'summary': 'y'}, {'start_turn': 1, 'title': 'x', 'summary': 'y'}],
                         [{'start_turn': 1, 'title': 'x', 'summary': 'y'}, {'start_turn': 99, 'title': 'x', 'summary': 'y'}]):
            with self.subTest(chapters=chapters), self.assertRaises(ValueError):
                validate_chapters({'chapters': chapters}, 3)

    def test_live_request_mock_and_cache_reuse_without_key(self):
        entries = [message('u', None, 'user', 'A question'), message('a', 'u', 'assistant', 'An answer')]
        chapters = [{'start_turn': 1, 'title': 'A topic', 'summary': 'A concise summary.'}]
        result = {'message': {'content': json.dumps({'chapters': chapters})}, 'prompt_eval_count': 100, 'eval_count': 30}
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / 'chapters.json'
            with patch.dict('os.environ', {'OLLAMA_API_KEY': 'test-value'}), patch('tools.export_session.urlopen', return_value=io.BytesIO(json.dumps(result).encode())) as network:
                self.assertEqual(load_chapters(entries, cache), chapters)
                network.assert_called_once()
                request = network.call_args.args[0]
                self.assertEqual(request.full_url, 'https://ollama.com/api/chat')
                self.assertNotIn('test-value', request.data.decode())
            self.assertEqual(stat.S_IMODE(cache.stat().st_mode), 0o600)
            sent = cache.with_name(cache.name + '.request.json')
            self.assertEqual(stat.S_IMODE(sent.stat().st_mode), 0o600)
            with patch.dict('os.environ', {}, clear=True), patch('tools.export_session.urlopen') as network:
                self.assertEqual(load_chapters(entries, cache), chapters)
                network.assert_not_called()
                with self.assertRaisesRegex(ValueError, 'does not match'):
                    load_chapters([message('u', None, 'user', 'Changed question')], cache)
                network.assert_not_called()

    def test_failed_call_does_not_retry_or_overwrite_request(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / 'chapters.json'
            entries = [message('u', None, 'user', 'Question')]
            with patch.dict('os.environ', {'OLLAMA_API_KEY': 'test-value'}), patch('tools.export_session.urlopen', side_effect=OSError('private details')) as network:
                with self.assertRaisesRegex(ValueError, 'no automatic retry'):
                    load_chapters(entries, cache)
                self.assertFalse(cache.exists())
                with self.assertRaises(FileExistsError):
                    load_chapters(entries, cache)
                network.assert_called_once()

    def test_chapter_links_replace_turn_links_and_escape_model_text(self):
        entries = [message('u', None, 'user', ''), message('v', 'u', 'user', 'Next question')]
        chapters = [{'start_turn': 1, 'title': '<script>bad</script>', 'summary': 'A summary.'}]
        output = render_session(HEADER, entries, chapters=chapters)
        document = Document(output)
        nav = output.split('<nav ', 1)[1].split('</nav>', 1)[0]
        self.assertEqual(nav.count('<a '), 1)
        self.assertIn('href="#chapter-1"', nav)
        self.assertIn('<section id="chapter-1"', output)
        self.assertLess(output.index('<section id="chapter-1"'), output.index('<article id="entry-1"'))
        self.assertIn('2 entries</p>', output)
        self.assertIn('&lt;script&gt;bad&lt;/script&gt;', nav)
        self.assertIn('AI-generated', output)
        self.assertEqual(sum(tag == 'script' for tag, _ in document.elements), 1)
        self.assertIn('Empty user message', output)


if __name__ == '__main__':
    unittest.main()
