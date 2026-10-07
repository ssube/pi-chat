import { randomUUID } from 'node:crypto';
import { closeSync, lstatSync, mkdirSync, openSync, renameSync, unlinkSync, writeSync, chmodSync } from 'node:fs';
import { basename, dirname, join, resolve } from 'node:path';

const SKILL_NAME = /^[a-z0-9]+(?:-[a-z0-9]+)*$/;

export function skillNameFromRead(path) {
  if (typeof path !== 'string' || basename(path) !== 'SKILL.md') return null;
  const name = basename(dirname(path));
  return name.length <= 64 && SKILL_NAME.test(name) ? name : null;
}

export function expandedSkillName(text) {
  if (typeof text !== 'string') return null;
  // Pi expands /skill:name into a skill block; never persist the path or body.
  const match = /^<skill name="([a-z0-9-]{1,64})" location="[^"\n]+\/SKILL\.md">(?:\n|$)/.exec(text);
  if (!match) return null;
  return SKILL_NAME.test(match[1]) ? match[1] : null;
}

function privateDir(path) {
  mkdirSync(path, { recursive: true, mode: 0o700 });
  const stat = lstatSync(path);
  if (!stat.isDirectory() || stat.isSymbolicLink()) throw new Error('skill capture directory is not private');
  chmodSync(path, 0o700);
}

export function recordSkillEvent({ root, agent, origin, skillName, eventKind, source }) {
  if (!(/^[a-z][a-z0-9_-]{0,79}$/.test(agent)) || !/^[a-z][a-z0-9_-]{0,79}$/.test(origin)) throw new Error('invalid skill capture identity');
  if (typeof skillName !== 'string' || skillName.length > 64 || !SKILL_NAME.test(skillName)) {
    throw new Error('invalid skill name');
  }
  if (!((eventKind === 'loaded' && source === 'read') ||
        (eventKind === 'invoked' && source === 'skill_command'))) {
    throw new Error('invalid skill event');
  }
  const outbox = join(resolve(root), 'outbox');
  privateDir(resolve(root));
  privateDir(outbox);
  const row = { id: `piskill-${randomUUID()}`, agent, origin,
    occurred_at: new Date().toISOString(), skill_name: skillName,
    event_kind: eventKind, source };
  const dest = join(outbox, `${row.id}.json`);
  const tmp = `${dest}.${process.pid}.${randomUUID()}.tmp`;
  let fd;
  try {
    fd = openSync(tmp, 'wx', 0o600);
    writeSync(fd, JSON.stringify(row) + '\n');
    closeSync(fd); fd = undefined;
    renameSync(tmp, dest);
  } finally {
    if (fd !== undefined) closeSync(fd);
    try { unlinkSync(tmp); } catch {}
  }
  return row;
}
