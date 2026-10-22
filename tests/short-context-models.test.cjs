const { test } = require('node:test');
const assert = require('node:assert/strict');
const { mkdtempSync, writeFileSync, rmSync } = require('node:fs');
const { tmpdir } = require('node:os');
const path = require('node:path');
const { createJiti } = require(require.resolve('jiti', { paths: [path.resolve(__dirname, '../extensions')] }));
const stubPath = path.resolve(__dirname, 'fixtures/short-context-sdk.cjs');
const { state } = require(stubPath);
const { default: extension, parseSelections } = createJiti(__filename, {
  alias: { '@earendil-works/pi-coding-agent': stubPath },
})(path.resolve(__dirname, '../extensions/short-context-models.ts'));

const source = { provider: 'example', id: 'chat', name: 'Example Chat', api: 'example-api',
  contextWindow: 1001, maxTokens: 800, cost: { input: 1 }, input: ['text'], reasoning: true };
function setup(t, config, extra = {}) {
  state.dir = mkdtempSync(path.join(tmpdir(), 'short-context-test-'));
  t.after(() => rmSync(state.dir, { recursive: true, force: true }));
  state.calls = [];
  if (config !== undefined) writeFileSync(path.join(state.dir, 'short-context-models.json'), JSON.stringify(config));
  const requests = [], registered = [];
  const provider = { id: 'example', getModels: () => [source], getAllModels: () => [source, { id: 'image', type: 'image' }],
    stream(model, context, options) { requests.push({ method: 'stream', model, context, options, receiver: this }); return 'stream'; },
    streamSimple(model, context, options) { requests.push({ method: 'streamSimple', model, context, options, receiver: this }); return 'simple'; },
    ...extra.provider };
  state.runtime = { getProvider: id => id === 'example' ? provider : undefined,
    getModel: (providerId, id) => providerId === 'example' && id === 'chat' ? source : undefined,
    ...extra.runtime };
  return { provider, requests, registered, run: () => extension({ registerProvider: p => registered.push(p) }) };
}
const selection = [{ provider: 'example', id: 'chat' }];

test('missing or empty config is inert without creating a runtime', async t => {
  for (const config of [undefined, []]) {
    const s = setup(t, config); await s.run();
    assert.deepEqual(state.calls, []); assert.deepEqual(s.registered, []);
  }
});
test('configuration rejects invalid entries and duplicates', () => {
  for (const input of [null, {}, 'x', [null], [[]], [{}], [{ provider: ' ', id: 'chat' }],
    [{ provider: 'example', id: 1 }], [{ ...selection[0], endpoint: 'https://example.invalid' }], [...selection, ...selection]]) {
    assert.throws(() => parseSelections(input), /Short-context|short-context/);
  }
  assert.deepEqual(parseSelections(selection), selection);
});
test('malformed JSON fails without reading runtime/auth configuration', async t => {
  const s = setup(t, []);
  writeFileSync(path.join(state.dir, 'short-context-models.json'), '{');
  await assert.rejects(s.run(), SyntaxError);
  assert.deepEqual(state.calls, []);
});
test('adds half-context copies, clamps output limit, preserves originals and disables network refresh', async t => {
  const s = setup(t, selection); await s.run();
  assert.deepEqual(state.calls, [{ modelsPath: path.join(state.dir, 'models.json'), authPath: path.join(state.dir, 'auth.json'),
    refreshOnCreate: false, allowModelNetwork: false }]);
  const [p] = s.registered, models = p.getModels();
  assert.equal(models[0], source); assert.equal(source.contextWindow, 1001); assert.equal(source.maxTokens, 800);
  assert.deepEqual(models[1], { ...source, id: 'chat-short', name: 'Example Chat (Short Context)', contextWindow: 500, maxTokens: 500 });
  assert.equal(models[1].cost, source.cost);
  assert.equal(p.getAllModels()[1].type, 'image'); assert.equal(p.getAllModels()[2].id, 'chat-short');
});
test('both provider entrypoints route aliases without losing options or caller payload changes', async t => {
  const s = setup(t, selection); await s.run();
  const [p] = s.registered, alias = p.getModels()[1], context = { messages: [] };
  for (const method of ['stream', 'streamSimple']) {
    const signal = new AbortController().signal;
    let callbackTarget;
    const options = { signal, temperature: 0.2, customField: 'retained', onPayload: async (payload, target) => {
      callbackTarget = target; return { ...payload, callerField: true, model: 'wrong-alias' };
    } };
    assert.equal(p[method](alias, context, options), method === 'stream' ? 'stream' : 'simple');
    const request = s.requests.at(-1);
    assert.equal(request.receiver, s.provider); assert.equal(request.model.id, 'chat');
    assert.equal(request.model.contextWindow, 500); assert.equal(alias.id, 'chat-short');
    assert.equal(request.context, context); assert.equal(request.options.signal, signal);
    assert.equal(request.options.customField, 'retained'); assert.equal(request.options.temperature, 0.2);
    const payload = { model: 'chat-short', messages: [] };
    assert.deepEqual(await request.options.onPayload(payload, request.model), { ...payload, model: 'chat', callerField: true });
    assert.equal(callbackTarget, request.model); assert.equal(payload.model, 'chat-short');
    assert.equal(options.onPayload === request.options.onPayload, false);
  }
});
test('original requests pass through unchanged; absent callbacks and in-place callbacks work', async t => {
  const s = setup(t, selection); await s.run(); const [p] = s.registered;
  for (const method of ['stream', 'streamSimple']) {
    const options = { onPayload: payload => payload };
    p[method](source, {}, options); assert.equal(s.requests.at(-1).options, options);
    assert.equal(s.requests.at(-1).model, source);
    p[method](p.getModels()[1], {});
    const callback = s.requests.at(-1).options.onPayload;
    assert.deepEqual(await callback({ model: 'chat-short' }, source), { model: 'chat' });
    for (const payload of [null, 'text', ['array']]) assert.equal(await callback(payload, source), payload);
    p[method](p.getModels()[1], {}, { onPayload: payload => { payload.changed = true; } });
    assert.deepEqual(await s.requests.at(-1).options.onPayload({}, source), { changed: true, model: 'chat' });
    p[method](p.getModels()[1], {}, { onPayload: async () => { throw new Error('caller error'); } });
    await assert.rejects(s.requests.at(-1).options.onPayload({}, source), /caller error/);
  }
});
test('missing providers/models, collisions and invalid context fail before any registration', async t => {
  for (const config of [[...selection, { provider: 'missing', id: 'chat' }], [{ provider: 'example', id: 'missing' }]]) {
    const s = setup(t, config); await assert.rejects(s.run(), /not found/); assert.deepEqual(s.registered, []);
  }
  const collision = setup(t, selection, { runtime: { getModel: () => source } });
  await assert.rejects(collision.run(), /already defined/); assert.deepEqual(collision.registered, []);
  for (const contextWindow of [0, 1, 1.5, NaN, Infinity]) {
    const s = setup(t, selection, { runtime: { getModel: (_p, id) => id === 'chat' ? { ...source, contextWindow } : undefined } });
    await assert.rejects(s.run(), /valid context/); assert.deepEqual(s.registered, []);
  }
});
test('providers without getAllModels fall back to chat models', async t => {
  const s = setup(t, selection, { provider: { getAllModels: undefined } }); await s.run();
  assert.deepEqual(s.registered[0].getAllModels().map(m => m.id), ['chat', 'chat-short']);
});
