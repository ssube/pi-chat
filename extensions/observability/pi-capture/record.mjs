#!/usr/bin/env node
import { homedir } from 'node:os';
import { join } from 'node:path';
import { recordOccurrence } from './storage.mjs';
import { loadSecrets } from './redact.mjs';

const MAX_INPUT_BYTES = 64 * 1024;
const short = (value, max = 2000) => typeof value === 'string' ? value.slice(0, max) : undefined;
const context = (value) => {
  if (value === undefined || value === null) return undefined;
  try { return JSON.stringify(value).slice(0, 4000); } catch { return '[unserializable]'; }
};
let chunks = [];
let size = 0;
try {
  for await (const chunk of process.stdin) {
    size += chunk.length;
    if (size > MAX_INPUT_BYTES) throw new Error('input too large');
    chunks.push(Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk));
  }
  const data = JSON.parse(Buffer.concat(chunks).toString('utf8'));
  if (!data || !['provider', 'session'].includes(data.kind) || data.outcome !== 'error' ||
      !process.env.PI_CAPTURE_AGENT || !process.env.PI_CAPTURE_ORIGIN) throw new Error('invalid record');
  if ((data.agent && data.agent !== process.env.PI_CAPTURE_AGENT) ||
      (data.origin && data.origin !== process.env.PI_CAPTURE_ORIGIN)) throw new Error('identity mismatch');
  const secrets = loadSecrets();
  const input = data.input && typeof data.input === 'object' ? data.input : {};
  const output = data.output && typeof data.output === 'object' ? data.output : {};
  const safeInput = { meeting_id: short(input.meeting_id, 200), stage: short(input.stage, 100), attempt: Number.isSafeInteger(input.attempt) ? input.attempt : undefined, last_event: context(input.last_event) };
  const safeOutput = { error_class: short(output.error_class, 100), category: short(output.category, 100), exit_code: Number.isSafeInteger(output.exit_code) ? output.exit_code : undefined, non_json_lines: Array.isArray(output.non_json_lines) ? output.non_json_lines.slice(-10).map((line) => short(line, 1000)).filter(Boolean) : undefined };
  const row = recordOccurrence({
    root: process.env.PI_CAPTURE_ROOT || join(homedir(), '.local', 'state', 'pi-capture'),
    agent: process.env.PI_CAPTURE_AGENT,
    origin: process.env.PI_CAPTURE_ORIGIN,
    kind: data.kind,
    outcome: 'error',
    toolName: null,
    input: safeInput,
    output: safeOutput,
    messages: Array.isArray(data.messages) ? data.messages.slice(-5).map((message) => short(message, 2000)).filter(Boolean) : [],
    sessionId: typeof data.session_id === 'string' ? data.session_id : undefined,
    extraSecrets: secrets,
  });
  process.stdout.write(`${row.id}\n`);
} catch {
  // No supplied command, log, context, or secret is safe to echo here.
  process.stderr.write('pi-capture: record rejected\n');
  process.exitCode = 1;
}
