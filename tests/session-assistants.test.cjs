// Keep optional assistant suites on the existing public CI entrypoint.
const { test } = require('node:test');
const { execFileSync } = require('node:child_process');
const path = require('node:path');
const assert = require('node:assert/strict');

test('session assistant SDK and conversation suites', () => {
  const env = { ...process.env };
  delete env.NODE_TEST_CONTEXT;
  const output = execFileSync(process.execPath, ['--test', '--test-reporter=tap',
    path.join(__dirname, 'model-control.test.mjs'),
    path.join(__dirname, '../extensions/next-prompt/command.test.mjs'),
    path.join(__dirname, '../extensions/next-prompt/context.test.mjs'),
  ], { env, encoding: 'utf8', timeout: 30_000 });
  assert.match(output, /# tests 24\b/);
  assert.match(output, /# pass 24\b/);
});
