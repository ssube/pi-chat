import assert from 'node:assert/strict';
import { test } from 'node:test';
import { fileURLToPath, pathToFileURL } from 'node:url';

// Exercise the real, pinned public SDK loader with offline model/UI fixtures.
const packageRoot = process.env.PI_PACKAGE_ROOT ?? fileURLToPath(new URL('./pi-sdk/node_modules/@earendil-works/pi-coding-agent', import.meta.url));
const { loadExtensions } = await import(new URL('dist/core/extensions/loader.js', pathToFileURL(`${packageRoot}/`)));

const path = process.env.PI_MODEL_EXTENSION ?? fileURLToPath(new URL('../extensions/model-control.ts', import.meta.url));
const base = { provider: 'openai-codex', id: 'model-a', name: 'Model A', api: 'openai-codex-responses', contextWindow: 524288, maxTokens: 128000, input: ['text', 'image'], reasoning: true, cost: { input: 2, output: 10, cacheRead: .2, cacheWrite: 0 } };
const short = { ...base, id: 'model-a-short', contextWindow: 262144 };
const other = { ...base, provider: 'ollama-cloud', id: 'model-b-short', name: 'Model B Short', api: 'openai-completions', input: ['text'], contextWindow: 262144 };
const unscoped = { ...base, id: 'unscoped' };

async function fixture(options = {}) {
  const loaded = await loadExtensions([path], '/tmp');
  assert.deepEqual(loaded.errors, []);
  const tool = loaded.extensions[0].tools.get('model_control').definition;
  let selected = options.source ?? short;
  const calls = { menu: [], judge: [], switches: [], statuses: [], notices: [] };
  const catalog = [base, short, other, unscoped];
  const ctx = {
    hasUI: options.hasUI ?? true, mode: options.mode ?? 'tui',
    get model() { return selected; },
    scopedModels: [base, short, other].map(model => ({ model })),
    sessionManager: { getSessionId: () => 'test-session', getBranch: () => [{ type: 'message', message: { role: 'user', content: 'Please try Model B Short.' } }] },
    getContextUsage: () => ({ tokens: 8000, contextWindow: selected.contextWindow, percent: 3 }),
    modelRegistry: {
      getAvailable: () => options.available ?? catalog,
      find: (provider, id) => catalog.find(model => model.provider === provider && model.id === id),
      async complete(model, context, requestOptions) {
        calls.judge.push({ model, context, requestOptions });
        if (options.judge) return options.judge({ model, context, requestOptions, ctx });
        return { stopReason: 'stop', content: [{ type: 'text', text: JSON.stringify({ verdict: 'accepted', reason: 'Suitable scoped model for this request.' }) }], usage: { input: 20, output: 10 } };
      },
    },
    ui: { setStatus: (...args) => calls.statuses.push(args), notify: (...args) => calls.notices.push(args), async custom(factory) {
      let selection;
      const view = factory({ terminal: { rows: 24 }, requestRender() {} }, {}, {}, value => { selection = value; });
      const title = view.body || view.title, choices = view.choices;
      calls.menu.push({ title, choices, view });
      if (options.key) { view.render(80); view.handleInput(options.key); view.dispose?.(); return selection; }
      if (options.menu) return options.menu({ ctx, setSelected: model => { selected = model; } });
      return options.choice ?? 'Deny';
    } },
  };
  loaded.runtime.getThinkingLevel = () => 'medium';
  loaded.runtime.setModel = async model => { calls.switches.push(model); if (options.setFails) return false; selected = model; return true; };
  const run = (args, signal) => tool.execute('test', args, signal ?? new AbortController().signal, undefined, ctx);
  return { run, calls, ctx, loaded };
}
const select = { action: 'select', provider: other.provider, model: other.id, reason: 'Try the requested favorite for this task.' };

test('current reports live selection; list includes exact IDs, limits and favorite markers without inference', async () => {
  const f = await fixture();
  assert.equal((await f.run({ action: 'current' })).details.selectedModel.id, short.id);
  const listed = (await f.run({ action: 'list', filter: 'model-b' })).details.models;
  assert.equal(listed.length, 1); assert.equal(listed[0].favorite, true); assert.equal(listed[0].contextWindow, 262144);
  const all = (await f.run({ action: 'list' })).details.models;
  assert.equal(all.find(model => model.id === unscoped.id).favorite, false);
  assert.equal(all.find(model => model.id === short.id).selected, true);
  assert.equal(f.calls.judge.length + f.calls.menu.length + f.calls.switches.length, 0);
});

test('human can approve cross-provider favorite, sees warning and all three choices; no judge call', async () => {
  const f = await fixture({ choice: 'Approve' });
  const result = await f.run(select);
  assert.equal(result.details.approval, 'human'); assert.equal(result.details.changed, true);
  assert.equal(f.ctx.model, other); assert.equal(f.calls.judge.length, 0);
  assert.deepEqual(f.calls.menu[0].choices, ['Approve', 'Deny', 'Send to judge']);
  assert.match(f.calls.menu[0].title, /PROVIDER CHANGE/); assert.match(f.calls.menu[0].title, /pricing/);
});

test('deny or dismiss never switches or invokes judge', async () => {
  for (const choice of ['Deny', 'Dismiss']) {
    const f = await fixture({ choice }); assert.equal((await f.run(select)).details.changed, false);
    assert.equal(f.calls.switches.length + f.calls.judge.length, 0);
  }
});

test('judge accepts cross-provider favorite only after human delegation, uses original current model and bounded tool-free evidence', async () => {
  const f = await fixture({ choice: 'Send to judge' }); const result = await f.run(select);
  assert.equal(result.details.changed, true); assert.equal(result.details.approval, 'judge');
  assert.equal(f.calls.menu.length, 1); assert.equal(f.calls.judge.length, 1); assert.equal(f.calls.switches.length, 1);
  const request = f.calls.judge[0]; assert.equal(request.model.id, base.id);
  assert.equal(request.context.tools, undefined); assert.equal(request.context.messages.length, 1);
  assert.equal(request.requestOptions.maxTokens, 1024); assert.equal(request.requestOptions.cacheRetention, 'none');
  assert.equal(result.usage.input, 20);
});

test('judge denial, uncertainty, malformed response, error and incomplete output fail closed', async () => {
  for (const judge of [
    async () => ({ stopReason: 'stop', content: [{ type: 'text', text: '{"verdict":"denied","reason":"Wrong capabilities."}' }] }),
    async () => ({ stopReason: 'stop', content: [{ type: 'text', text: '{"verdict":"unable_to_assess","reason":"Insufficient evidence."}' }] }),
    async () => ({ stopReason: 'stop', content: [{ type: 'text', text: '{"verdict":"accepted","reason":""}' }] }),
    async () => ({ stopReason: 'stop', content: [{ type: 'text', text: 'Approve!' }] }),
    async () => { throw new Error('PRIVATE_PROVIDER_ERROR'); },
    async () => ({ stopReason: 'length', content: [{ type: 'text', text: '{"verdict":"accepted","reason":"yes"}' }] }),
  ]) {
    const f = await fixture({ choice: 'Send to judge', judge }); const result = await f.run(select);
    assert.equal(result.details.changed, false); assert.equal(f.calls.switches.length, 0);
    assert.doesNotMatch(JSON.stringify(result), /PRIVATE_PROVIDER_ERROR/);
  }
});

test('unscoped, unknown, unauthenticated and invalid requests rejected before approval', async () => {
  for (const [options, params, pattern] of [
    [{}, { ...select, provider: base.provider, model: unscoped.id }, /scoped/],
    [{}, { ...select, model: 'nonexistent' }, /unavailable/],
    [{ available: [short] }, select, /authentication/],
    [{}, { ...select, reason: undefined }, /reason/],
  ]) {
    const f = await fixture(options); await assert.rejects(f.run(params), pattern);
    assert.equal(f.calls.menu.length + f.calls.judge.length + f.calls.switches.length, 0);
  }
});

test('noninteractive contexts cannot approve, while read-only inspection still works', async () => {
  for (const options of [{ hasUI: false }, { mode: 'rpc' }, { mode: 'print' }]) {
    const f = await fixture(options); await assert.rejects(f.run(select), /approval menu/);
    assert.ok((await f.run({ action: 'current' })).details.selectedModel);
    assert.equal(f.calls.switches.length, 0);
  }
});

test('already selected is a no-op, and pre-cancelled requests do not open UI', async () => {
  const f = await fixture(); assert.equal((await f.run({ action: 'select', provider: short.provider, model: short.id, reason: 'same' })).details.changed, false);
  const abort = new AbortController(); abort.abort(); await assert.rejects(f.run(select, abort.signal), /cancelled/);
  assert.equal(f.calls.menu.length, 0);
});

test('selection changes during approval invalidate it', async () => {
  const f = await fixture({ menu: ({ setSelected }) => { setSelected(base); return 'Approve'; } });
  await assert.rejects(f.run(select), /changed/); assert.equal(f.calls.switches.length, 0);
});

test('judge cancellation settles even if provider ignores abort; no switch', async () => {
  const abort = new AbortController();
  const f = await fixture({ choice: 'Send to judge', judge: async () => { abort.abort(); return new Promise(() => {}); } });
  const result = await f.run(select, abort.signal);
  assert.equal(result.details.changed, false); assert.equal(f.calls.switches.length, 0);
});

test('session shutdown cancels pending judge; overlapping selection refused', async () => {
  let begin; const started = new Promise(resolve => { begin = resolve; });
  const f = await fixture({ choice: 'Send to judge', judge: async () => { begin(); return new Promise(() => {}); } });
  const running = f.run(select); await started;
  await assert.rejects(f.run(select), /already awaiting/);
  for (const handler of f.loaded.extensions[0].handlers.get('session_shutdown')) await handler({}, f.ctx);
  assert.equal((await running).details.changed, false); assert.equal(f.calls.switches.length, 0);
});

test('failed runtime selection is not reported as success', async () => {
  const f = await fixture({ choice: 'Approve', setFails: true }); await assert.rejects(f.run(select), /failed/);
});


test('approval prices are readable, and judgment status/cancellation preserve the selected model', async () => {
  const f = await fixture({ choice: 'Send to judge', judge: () => new Promise(() => {}) });
  const run = f.run(select);
  await new Promise(resolve => setImmediate(resolve));
  assert.match(f.calls.menu[0].title, /input \$2 · output \$10/);
  assert.doesNotMatch(f.calls.menu[0].title, /\{"input"/);
  assert.match(f.calls.statuses.at(-1)[1], /model-judge-cancel/);
  await f.loaded.extensions[0].commands.get('model-judge-cancel').handler('', f.ctx);
  const result = await run;
  assert.equal(result.details.changed, false);
  assert.match(result.details.assessment.reason, /cancelled/);
  assert.equal(f.calls.switches.length, 0);
  assert.deepEqual(f.calls.statuses.at(-1), ['model-judge', undefined]);
});

test('judgment feedback clears after approval/failure and direct approval has no judge status', async () => {
  for (const options of [{ choice: 'Send to judge' }, { choice: 'Send to judge', judge: () => { throw Error('failure'); } }]) {
    const f = await fixture(options); await f.run(select);
    assert.deepEqual(f.calls.statuses.at(-1), ['model-judge', undefined]);
  }
  const f = await fixture({ choice: 'Approve' }); await f.run(select);
  assert.equal(f.calls.statuses.length, 0);
  await f.loaded.extensions[0].commands.get('model-judge-cancel').handler('', f.ctx);
  assert.match(f.calls.notices.at(-1)[0], /No model-change/);
});


test('approval uses consistent a/d/j mnemonics and q/Escape deny without changing models', async () => {
  for (const key of ['q', '\x1b', 'd']) {
    const f = await fixture({ key }); const outcome = await f.run(select);
    assert.equal(outcome.details.changed, false); assert.equal(f.calls.switches.length, 0);
  }
  const human = await fixture({ key: 'a' }); assert.equal((await human.run(select)).details.approval, 'human');
  const judge = await fixture({ key: 'j' }); assert.equal((await judge.run(select)).details.approval, 'judge');
  assert.equal(judge.calls.judge.length, 1);
  const view = human.calls.menu[0].view;
  assert.ok(view.render(40).length <= 20);
  view.handleInput('?'); assert.match(view.render(80).join('\n'), /keys/);
});
