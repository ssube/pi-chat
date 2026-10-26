import assert from 'node:assert/strict';
import { test } from 'node:test';
import { fileURLToPath, pathToFileURL } from 'node:url';
const root = process.env.PI_PACKAGE_ROOT ?? fileURLToPath(new URL('../../tests/pi-sdk/node_modules/@earendil-works/pi-coding-agent', import.meta.url));
const { loadExtensions } = await import(new URL('dist/core/extensions/loader.js', pathToFileURL(`${root}/`)));

async function setup(options = {}) {
  if (options.configured === false) {
    delete process.env.PI_SUGGEST_PROVIDER;
    delete process.env.PI_SUGGEST_MODEL;
  } else {
    process.env.PI_SUGGEST_PROVIDER = 'test-provider';
    process.env.PI_SUGGEST_MODEL = 'test-model';
  }
  const loaded = await loadExtensions([fileURLToPath(new URL('./index.ts', import.meta.url))], '/tmp');
  assert.deepEqual(loaded.errors, []);
  const ext = loaded.extensions[0];
  let session = 'one', editor = '', resolve, reject;
  const notices = [], statuses = [], requests = [];
  const ctx = {
    mode: 'tui', sessionManager: { getSessionId: () => session, getBranch: () => [{ type: 'message', message: { role: 'user', content: 'What next?' } }] },
    modelRegistry: { find: () => ({}), hasConfiguredAuth: () => true, streamSimple: (_model, _context, options) => {
      requests.push(options);
      return { result: () => new Promise((yes, no) => { resolve = yes; reject = no; }) };
    } },
    ui: { getEditorText: () => editor, setEditorText: text => { editor = text; },
      notify: (...args) => notices.push(args), setStatus: (...args) => statuses.push(args) },
  };
  loaded.runtime.getSessionName = () => 'test';
  const command = ext.commands.get('suggest-next');
  return { ctx, notices, statuses, requests, run: args => command.handler(args ?? '', ctx),
    resolve: text => resolve({ stopReason: 'stop', content: [{ type: 'text', text }] }),
    fail: () => reject(new Error('offline failure')), editor: value => { editor = value; },
    getEditor: () => editor, switchSession: () => { session = 'two'; },
    event: async name => { for (const handler of ext.handlers.get(name) ?? []) await handler({}, ctx); } };
}

test('draft inserts once without submitting and status advertises cancellation', async () => {
  const f = await setup(); const run = f.run(); f.resolve('Try a small test.'); await run;
  assert.equal(f.getEditor(), 'Try a small test.');
  assert.equal(Object.hasOwn(f.requests[0], 'reasoning'), false);
  assert.equal(f.requests[0].maxTokens, 300);
  assert.match(f.statuses[0][1], /suggest-next cancel/);
  assert.equal(f.statuses.at(-1)[1], undefined);
});

test('duplicates do not submit a second request; cancellation settles even if provider ignores abort', async () => {
  const f = await setup(); const run = f.run(); await f.run();
  assert.equal(f.requests.length, 1); await f.run('cancel'); await run;
  assert.equal(f.requests[0].signal.aborted, true);
  assert.equal(f.getEditor(), ''); assert.match(f.notices.at(-1)[0], /cancelled/);
  f.resolve('Late result'); await Promise.resolve(); assert.equal(f.getEditor(), '');
});

test('session lifecycle transitions cancel and discard late results without stale notices', async () => {
  for (const name of ['session_before_switch', 'session_before_fork', 'session_before_tree', 'session_shutdown']) {
    const f = await setup(); const run = f.run(); await f.event(name); await run;
    f.switchSession(); f.resolve('Wrong session'); await Promise.resolve();
    assert.equal(f.getEditor(), ''); assert.equal(f.notices.length, 0);
    assert.equal(f.requests[0].signal.aborted, true);
  }
});

test('session identity and nonempty editor are checked before insertion', async () => {
  const f = await setup(); const run = f.run(); f.switchSession(); f.resolve('Wrong session'); await run;
  assert.equal(f.getEditor(), '');
  const g = await setup(); const draft = g.run(); g.editor('My draft'); g.resolve('Suggestion'); await draft;
  assert.equal(g.getEditor(), 'My draft'); assert.match(g.notices.at(-1)[0], /Editor changed/);
});

test('provider failure and deadline have distinct feedback; empty cancellation is harmless', async () => {
  const f = await setup(); const run = f.run(); f.fail(); await run;
  assert.equal(f.notices.at(-1)[1], 'error');
  await f.run('cancel'); assert.match(f.notices.at(-1)[0], /No prompt/);
  const old = globalThis.setTimeout;
  try {
    globalThis.setTimeout = (fn, ms, ...args) => old(fn, ms === 30000 ? 1 : ms, ...args);
    const g = await setup(); await g.run();
    assert.match(g.notices.at(-1)[0], /timed out/); assert.equal(g.notices.at(-1)[1], 'warning');
  } finally { globalThis.setTimeout = old; }
});


test('missing explicit model configuration refuses inference; headless calls do nothing', async () => {
  const f = await setup({ configured: false }); await f.run();
  assert.equal(f.requests.length, 0); assert.equal(f.getEditor(), '');
  assert.match(f.notices.at(-1)[0], /PI_SUGGEST_PROVIDER.*PI_SUGGEST_MODEL/);
  const g = await setup(); g.ctx.mode = 'rpc'; await g.run();
  assert.equal(g.requests.length, 0); assert.equal(g.notices.length, 0);
});
