import { randomUUID, createHash } from 'node:crypto';
import { mkdirSync, openSync, writeSync, closeSync, renameSync, unlinkSync, readFileSync, lstatSync, chmodSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { redactText, redactValue, stableSignature } from './redact.mjs';

function privateDir(path) {
  mkdirSync(path, { recursive: true, mode: 0o700 });
  const stat = lstatSync(path);
  if (!stat.isDirectory() || stat.isSymbolicLink()) throw new Error('capture directory must be a real directory');
  chmodSync(path, 0o700);
}
function atomicPrivate(path, data) {
  const tmp = `${path}.${process.pid}.${randomUUID()}.tmp`;
  let fd;
  try {
    fd = openSync(tmp, 'wx', 0o600);
    writeSync(fd, data);
    closeSync(fd); fd = undefined;
    renameSync(tmp, path);
  } finally {
    if (fd !== undefined) closeSync(fd);
    try { unlinkSync(tmp); } catch {}
  }
}
function bounded(value, max) { return String(value ?? '').slice(0, max); }

export function recordOccurrence({ root, agent, origin, kind = 'tool', outcome, toolName = null, input, output, messages = [], sessionId, toolCallId, durationMs, matchedFixIds = [], candidateMatches = [], exitCode = null, extraSecrets = [] }) {
  if (!['error', 'success'].includes(outcome)) throw new Error('invalid outcome');
  if (!/^[a-z][a-z0-9_-]*$/.test(agent)) throw new Error('invalid agent');
  if (!/^[a-z][a-z0-9_-]*$/.test(origin)) throw new Error('invalid origin');
  const id = `${outcome === 'error' ? 'pierr' : 'pitool'}-${randomUUID()}`;
  const occurredAt = new Date().toISOString();
  const base = resolve(root);
  const outbox = join(base, 'outbox');
  const details = join(base, 'details');
  privateDir(base); privateDir(outbox); privateDir(details);
  const redactedInput = redactValue(input, extraSecrets);
  const redactedOutput = redactValue(output, extraSecrets);
  const redactedCommand = toolName === 'bash' && typeof redactedInput?.command === 'string'
    ? bounded(redactedInput.command, 2048) : null;
  const command = bounded(JSON.stringify(redactedInput), 4096);
  const log = bounded(JSON.stringify(redactedOutput), 8192);
  const signature = createHash('sha256').update(stableSignature(toolName ?? kind, command, outcome === 'error' ? log.slice(0, 512) : '')).digest('hex');
  let detailRef = null;
  let detailSha = null;
  if (outcome === 'error') {
    detailRef = join(details, `${id}.json`);
    const detail = {
      id, agent, origin, occurred_at: occurredAt, kind, tool_name: toolName,
      session_id: sessionId ?? null, tool_call_id: toolCallId ?? null,
      preceding_messages: redactValue(messages.slice(-5), extraSecrets),
      command: redactedInput, logs: redactedOutput,
    };
    const bytes = Buffer.from(JSON.stringify(detail, null, 2) + '\n');
    detailSha = createHash('sha256').update(bytes).digest('hex');
    atomicPrivate(detailRef, bytes);
  }
  const summary = outcome === 'error' ? bounded(redactText(typeof output === 'string' ? output : log, extraSecrets), 240) : 'success';
  const row = {
    id, agent, origin, occurred_at: occurredAt, kind, outcome,
    tool_name: toolName, signature, redacted_command: redactedCommand,
    detail_ref: detailRef, detail_sha256: detailSha,
    redacted_summary: summary,
    session_id: sessionId ?? null, tool_call_id: toolCallId ?? null,
    duration_ms: Number.isFinite(durationMs) ? durationMs : null,
    matched_fix_ids: matchedFixIds,
    candidate_matches: candidateMatches,
    exit_code: Number.isInteger(exitCode) && exitCode >= 0 && exitCode <= 255 ? exitCode : null,
  };
  atomicPrivate(join(outbox, `${id}.json`), Buffer.from(JSON.stringify(row) + '\n'));
  return row;
}

export function redactedExcerpt(root, id, extraSecrets = []) {
  if (!/^pierr-[0-9a-f-]{36}$/.test(id)) throw new Error('invalid occurrence id');
  const path = join(resolve(root), 'details', `${id}.json`);
  const bytes = readFileSync(path);
  const detail = JSON.parse(bytes.toString('utf8'));
  if (detail.id !== id) throw new Error('evidence id mismatch');
  const hash = createHash('sha256').update(bytes).digest('hex');
  const result = redactValue(detail, extraSecrets);
  return `Occurrence: ${id}\nStored evidence SHA-256: ${hash}\n` + bounded(JSON.stringify(result, null, 2), 6000);
}

/** Delivery audit contains only bounded references, versions, reasons and timing. */
export function recordDeliveryReceipt({ root, agent, occurrenceId, outcome, metadata }) {
  const id = `pidelivery-${randomUUID()}`;
  const row = { id, occurrence_id: occurrenceId, agent, occurred_at: new Date().toISOString(), outcome, metadata };
  const bytes = Buffer.from(JSON.stringify(row) + '\n');
  if (bytes.length > 8192) throw new Error('oversized delivery receipt');
  const outbox = join(resolve(root), 'outbox');
  privateDir(outbox);
  atomicPrivate(join(outbox, `${id}.json`), bytes);
  return row;
}
