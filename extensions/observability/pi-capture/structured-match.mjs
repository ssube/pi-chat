import { spawn } from 'node:child_process';

const MAX_REPLY = 16 * 1024;
const TIMEOUT_MS = 1200;

/** Query the local ledger matcher over stdin; no command text enters process arguments. */
export function structuredMatch(input, env = process.env) {
  const repo = env.PI_CAPTURE_MATCH_REPO;
  if (!repo || !env.PI_LEDGER_DSN) return Promise.resolve(null);
  return new Promise((resolve) => {
    const childEnv = { ...env };
    delete childEnv.PI_LEDGER_WRITER_DSN;
    const child = spawn(env.PI_CAPTURE_MATCH_PYTHON || 'python3',
      ['-m', 'tools.ledger.error_cli', 'match', '--stdin'],
      { cwd: repo, env: childEnv, stdio: ['pipe', 'pipe', 'ignore'] });
    let output = '';
    let settled = false;
    const finish = (value) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve(value);
    };
    const timer = setTimeout(() => { child.kill(); finish(null); }, TIMEOUT_MS);
    child.stdout.on('data', (chunk) => {
      output += chunk.toString('utf8');
      if (output.length > MAX_REPLY) { child.kill(); finish(null); }
    });
    child.on('error', () => finish(null));
    child.on('close', (code) => {
      if (code !== 0) return finish(null);
      try {
        const result = JSON.parse(output);
        if (!Array.isArray(result.candidates) || result.candidates.length > 10) return finish(null);
        finish(result);
      } catch { finish(null); }
    });
    child.stdin.on('error', () => finish(null));
    child.stdin.end(JSON.stringify(input));
  });
}
