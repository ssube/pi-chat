#!/usr/bin/env node
import { homedir } from 'node:os';
import { join } from 'node:path';
import { redactedExcerpt } from './storage.mjs';
import { loadSecrets } from './redact.mjs';

const id = process.argv[2];
if (!id) { process.stderr.write('usage: node excerpt.mjs pierr-<uuid>\n'); process.exit(2); }
let secrets;
try { secrets = loadSecrets(); } catch { process.stderr.write('redaction configuration unavailable; excerpt withheld\n'); process.exit(1); }
try {
  process.stdout.write(redactedExcerpt(process.env.PI_CAPTURE_ROOT || join(homedir(), '.local', 'state', 'pi-capture'), id, secrets) + '\n');
} catch (error) {
  process.stderr.write(`cannot produce excerpt: ${error instanceof Error ? error.message : 'unknown'}\n`);
  process.exit(1);
}
