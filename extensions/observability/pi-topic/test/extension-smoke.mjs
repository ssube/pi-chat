// Run with Pi's bundled Node 22+ and the pi-coding-agent package resolvable.
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
const require = createRequire(import.meta.url);
const { createJiti } = require(require.resolve('jiti', { paths: [fileURLToPath(new URL('../../../', import.meta.url))] }));
const load = createJiti(fileURLToPath(import.meta.url), { interopDefault: true,
  alias: { '@earendil-works/pi-tui': fileURLToPath(new URL('../../../../tests/pi-tui-mock.cjs', import.meta.url)) } });
const extension = load(fileURLToPath(new URL('../index.ts', import.meta.url))).default;
process.env.PI_TOPIC_PROVIDER = 'test';
process.env.PI_TOPIC_MODEL = 'test-model';

// The hook fixture must never rename the developer's real tmux pane.
const tmux = process.env.TMUX;
const pane = process.env.TMUX_PANE;
delete process.env.TMUX;
delete process.env.TMUX_PANE;

const handlers = new Map();
const commands = new Map();
const entries = [];
const statuses = [];
let name;
let title;
let calls = 0;
let response = '{"name":"Deduplicate Events","topic":"Event deduplication implementation","label":"Event dedupe"}';
const pi = {
  on(event, handler) { handlers.set(event, handler); },
  registerCommand(command, definition) { commands.set(command, definition); },
  registerEntryRenderer(type, renderer) { assert.equal(type, 'pi-topic-status'); assert.equal(typeof renderer, 'function'); },
  appendEntry(type, data) { assert.equal(type, 'pi-topic-status'); entries.push(data.message); },
  getSessionName() { return name; },
  setSessionName(value) { name = value; },
};
const ctx = {
  mode: 'tui',
  isIdle: () => false, // settled and explicit commands may still report busy
  sessionManager: { getBranch: () => [
    { type: 'message', message: { role: 'user', content: 'Implement event deduplication.' } },
  ] },
  modelRegistry: {
    find: () => ({ id: 'test-model' }),
    hasConfiguredAuth: () => true,
    streamSimple: () => {
      calls++;
      return { result: async () => ({
        stopReason: 'stop',
        content: [{ type: 'text', text: response }],
      }) };
    },
  },
  ui: {
    setTitle(value) { title = value; },
    setStatus(key, value) { assert.equal(key, 'pi-topic'); statuses.push(value); },
  },
};
extension(pi);
assert.ok(commands.has('topic-refresh'));
await handlers.get('agent_settled')({}, ctx);
// agent_settled is notification-only, so its handler starts an async update.
await new Promise(resolve => setTimeout(resolve, 10));
assert.equal(calls, 1);
assert.equal(name, 'Deduplicate Events');
assert.equal(title, 'π - Event deduplication implementation');
assert.deepEqual(statuses, ['… Refreshing topic', undefined]);
assert.deepEqual(entries, ['Topic: Deduplicate Events']);
response = 'not json';
await commands.get('topic-refresh').handler('', ctx);
assert.equal(calls, 2);
assert.equal(entries.at(-1), 'Topic refresh returned no usable label; previous title kept');
assert.equal(title, 'π - Event deduplication implementation');
assert.deepEqual(statuses.slice(-2), ['… Refreshing topic', undefined]);
await handlers.get('session_shutdown')({}, ctx);
if (tmux !== undefined) process.env.TMUX = tmux;
if (pane !== undefined) process.env.TMUX_PANE = pane;
console.log('Pi hook smoke test passed');
