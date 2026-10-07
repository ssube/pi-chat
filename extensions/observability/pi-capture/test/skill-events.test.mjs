import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync, statSync, readdirSync, symlinkSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { expandedSkillName, recordSkillEvent, skillNameFromRead } from '../skill-events.mjs';

test('only successful SKILL.md reads have an eligible skill name', () => {
  assert.equal(skillNameFromRead('/private/skills/matrix-read/SKILL.md'), 'matrix-read');
  assert.equal(skillNameFromRead('/private/skills/matrix-read/references/foo.md'), null);
  assert.equal(skillNameFromRead('/private/skills/Bad Name/SKILL.md'), null);
  assert.equal(skillNameFromRead('/SKILL.md'), null);
  assert.equal(skillNameFromRead(undefined), null);
});

test('recognizes only Pi expanded skill blocks, never prompt prose', () => {
  const expanded = '<skill name="cron" location="/private/skills/cron/SKILL.md">\nReferences are relative to /private/skills/cron.\n</skill>';
  assert.equal(expandedSkillName(expanded), 'cron');
  assert.equal(expandedSkillName(`Please consider ${expanded}`), null);
  assert.equal(expandedSkillName('/skill:cron run this'), null);
  assert.equal(expandedSkillName('<skill name="bad name" location="/x/SKILL.md">'), null);
  assert.equal(expandedSkillName('<skill name="cron" location="/x/not-a-skill.md">'), null);
});

test('writes bounded private metadata into existing capture outbox', () => {
  const root = mkdtempSync(join(tmpdir(), 'pi-skill-event-'));
  const row = recordSkillEvent({ root, agent: 'agent_one', origin: 'interactive',
    skillName: 'matrix-read', eventKind: 'loaded', source: 'read' });
  assert.match(row.id, /^piskill-[0-9a-f-]{36}$/);
  const file = join(root, 'outbox', `${row.id}.json`);
  assert.equal(statSync(file).mode & 0o777, 0o600);
  assert.equal(statSync(join(root, 'outbox')).mode & 0o777, 0o700);
  const saved = readFileSync(file, 'utf8');
  assert.deepEqual(JSON.parse(saved), row);
  assert.deepEqual(Object.keys(row), ['id', 'agent', 'origin', 'occurred_at',
    'skill_name', 'event_kind', 'source']);
  assert.equal(saved.includes('/private/'), false);
  assert.equal(readdirSync(join(root, 'outbox')).length, 1);
});

test('rejects invalid identity, skill, source, and symlinked storage', () => {
  const root = mkdtempSync(join(tmpdir(), 'pi-skill-event-'));
  const base = { root, agent: 'agent_two', origin: 'bridge_matrix', skillName: 'cron',
    eventKind: 'invoked', source: 'skill_command' };
  for (const change of [
    { agent: '../outsider' }, { origin: 'bridge matrix' }, { skillName: '../cron' },
    { skillName: 'x'.repeat(65) }, { eventKind: 'used' }, { source: 'read' },
  ]) assert.throws(() => recordSkillEvent({ ...base, ...change }));
  const link = `${root}-link`;
  symlinkSync(root, link);
  assert.throws(() => recordSkillEvent({ ...base, root: link }));
});
