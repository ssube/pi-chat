import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';

// Certainty scoring is not implemented; matching is advisory.
export const CERTAINTY_THRESHOLD = null;

const SKILL_ID = /^[a-z0-9]+(?:-[a-z0-9]+)*$/;
const AGENT_ID = /^[a-z][a-z0-9_-]*$/;

// Must match the database exporter's canonical JSON exactly: these five keys in
// this order, sorted string arrays, compact separators, and unescaped Unicode.
export function canonicalFixContent(fix) {
  const sorted = (value, pattern) => {
    if (!Array.isArray(value) || value.some((item) => typeof item !== 'string' || !pattern.test(item))) throw new Error('invalid fix scope');
    return [...value].sort();
  };
  if (typeof fix.pattern !== 'string' || fix.pattern.length > 500 ||
      typeof fix.fix !== 'string' || fix.fix.length > 4000) throw new Error('invalid fix content');
  const owners = sorted(fix.owners, AGENT_ID);
  const applies = sorted(fix.applies_to_agents, AGENT_ID);
  const skills = sorted(fix.required_skills, SKILL_ID);
  if (owners.length === 0 || applies.length === 0) throw new Error('empty fix scope');
  return JSON.stringify({
    applies_to_agents: applies,
    fix: fix.fix,
    owners,
    pattern: fix.pattern,
    required_skills: skills,
  });
}

export function contentDigest(fix) {
  return createHash('sha256').update(canonicalFixContent(fix), 'utf8').digest('hex');
}

export function isVerifiedFix(fix) {
  try {
    return typeof fix.id === 'string' && fix.id.length > 0 && fix.reviewed === true &&
      typeof fix.content_sha256 === 'string' && /^[0-9a-f]{64}$/.test(fix.content_sha256) &&
      contentDigest(fix) === fix.content_sha256;
  } catch { return false; }
}

export function loadKnownFixes(path) {
  try {
    const data = JSON.parse(readFileSync(path, 'utf8'));
    if (data.version !== 1 || !Array.isArray(data.fixes)) return [];
    return data.fixes.filter(isVerifiedFix);
  } catch { return []; }
}

export function knownFixNudge(matches, occurrenceId) {
  // Histories can overlap across fixes, even with different catalog labels.
  // Without occurrence sets, the maximum is a truthful lower bound; summing
  // would double-count. This metadata is not a certainty or efficacy score.
  const complete = matches.length > 0 && matches.every((fix) =>
    Number.isSafeInteger(fix.seen_count) && fix.seen_count >= 0);
  const count = complete ? Math.max(...matches.map((fix) => fix.seen_count)) : null;
  const history = complete
    ? `Similar errors have been seen at least ${count} times as of the last sync.`
    : 'Reviewed suggestions may apply; historical count unavailable in this snapshot.';
  const reference = occurrenceId.replace(/^pierr-/, 'err-').slice(0, 12);
  return `Error ${occurrenceId}: ${history} If you're unsure what caused this error, run \`python3 -m tools.ledger.error_cli ${reference}\` from the public repository to get suggested fixes.`;
}

export function matchingFixes(fixes, { agent, enabledSkills, text }) {
  const found = [];
  for (const fix of fixes) {
    if (!isVerifiedFix(fix)) continue;
    if (!fix.applies_to_agents.includes(agent)) continue;
    if (!fix.required_skills.every((skill) => enabledSkills.has(skill))) continue;
    let matched = false;
    try { matched = new RegExp(fix.pattern, 'i').test(String(text).slice(0, 4096)); } catch { continue; }
    if (matched) found.push(fix);
    // Keep the capture payload within the ingester's 16-ID contract.
    if (found.length === 16) break;
  }
  return found;
}

export function notifiableFixes(matches, cooldowns, now = Date.now()) {
  const due = matches.filter((fix) => !cooldowns.has(fix.id) ||
    now - cooldowns.get(fix.id) >= 300_000);
  for (const fix of due) cooldowns.set(fix.id, now);
  return due;
}
