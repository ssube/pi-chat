import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, mkdirSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { structuredMatch } from '../structured-match.mjs';

test('structured lookup skips when reader configuration is absent', async () => {
  assert.equal(await structuredMatch({ agent: 'agent_one' }, {}), null);
});

test('structured lookup sends redacted input only through stdin', async () => {
  const repo = mkdtempSync(join(tmpdir(), 'pi-match-'));
  mkdirSync(join(repo, 'tools', 'ledger'), { recursive: true });
  writeFileSync(join(repo, 'tools', '__init__.py'), '');
  writeFileSync(join(repo, 'tools', 'ledger', '__init__.py'), '');
  writeFileSync(join(repo, 'tools', 'ledger', 'error_cli.py'), `
import json, sys
data = json.load(sys.stdin)
assert len(sys.argv) == 3 and sys.argv[1:] == ['match', '--stdin']
assert data['command'] == 'ls $PATH 2>/dev/null'
print(json.dumps({'candidates': [{'fix_id': 'fix-one', 'score': 90,
  'matcher_version': 1, 'disposition': 'conditional_hint'}], 'stderr_check': None}))
`);
  const result = await structuredMatch(
    { agent: 'agent_one', command: 'ls $PATH 2>/dev/null', output: '', exit_code: 2 },
    { PI_CAPTURE_MATCH_REPO: repo, PI_LEDGER_DSN: 'postgresql://unused' });
  assert.equal(result.candidates[0].fix_id, 'fix-one');
});
