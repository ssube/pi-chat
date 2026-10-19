const { test } = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const { createJiti } = require(require.resolve('jiti', { paths: [path.resolve(__dirname, '../extensions')] }));
const extension = createJiti(__filename)(path.resolve(__dirname, '../extensions/pi-time.ts')).default;
const tools = {};
extension({ registerTool: (tool) => { tools[tool.name] = tool; } });
const sleep = (time, signal, onUpdate, ctx) => tools.sleep_until.execute('id', { time }, signal, onUpdate, ctx);
const freshSleep = () => {
  // Load after MockTimers installs its promise-timer function. A module loaded
  // earlier retains the native timer; advancing Date alone cannot wake it.
  const tools = {};
  createJiti(__filename, { moduleCache: false })(path.resolve(__dirname, '../extensions/pi-time.ts')).default({ registerTool: tool => tools[tool.name] = tool });
  return (time, signal, onUpdate, ctx) => tools.sleep_until.execute('id', { time }, signal, onUpdate, ctx);
};

test('clock returns the selected epoch, ISO UTC, or human display', async () => {
  const before = Date.now();
  const epoch = await tools.clock.execute('id', { mode: 'epoch_ms' });
  const iso = await tools.clock.execute('id', { mode: 'iso_utc' });
  const utc = await tools.clock.execute('id', { mode: 'human_utc' });
  const local = await tools.clock.execute('id', { mode: 'human_local' });
  const after = Date.now();
  assert.ok(epoch.details.value >= before && epoch.details.value <= after);
  assert.equal(epoch.content[0].text, String(epoch.details.value));
  assert.match(iso.details.value, /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$/);
  assert.ok(Date.parse(iso.details.value) >= before && Date.parse(iso.details.value) <= after);
  assert.match(utc.details.value, /UTC/);
  assert.match(local.details.value, /\([^()]+, UTC[+-]\d\d:\d\d\)$/);
  assert.equal(local.content[0].text, local.details.value);
});

test('sleep accepts a short interval and ISO time with offset; past target returns immediately', async () => {
  const start = Date.now();
  const interval = await sleep('0.02 seconds');
  assert.ok(Date.parse(interval.details.wokeAt) >= Date.parse(interval.details.target));
  assert.ok(Date.now() - start >= 15);
  const future = new Date(Date.now() + 30).toISOString();
  const iso = await sleep(future);
  assert.equal(iso.details.target, future);
  assert.ok(Date.parse(iso.details.wokeAt) >= Date.parse(future));
  const past = await sleep('2020-01-01T01:00:00+01:00');
  assert.equal(past.details.target, '2020-01-01T00:00:00.000Z');
  const negative = await sleep('2020-01-01T00:00:00-05:30');
  assert.equal(negative.details.target, '2020-01-01T05:30:00.000Z');
});

test('relative waits report the expected wake time and human interval before waiting', async (t) => {
  const now = Date.parse('2026-10-08T10:00:00Z');
  t.mock.method(Date, 'now', () => now);
  const controller = new AbortController();
  const updates = [];
  await assert.rejects(sleep('65 minutes', controller.signal, (update) => {
    updates.push(update); controller.abort();
  }), { name: 'AbortError' });
  assert.equal(updates.length, 1);
  assert.equal(updates[0].content[0].text, 'Waiting 1 hour, 5 minutes; expected wake: 2026-10-08T11:05:00.000Z.');
  assert.equal(updates[0].details.durationMs, 3_900_000);
});

test('absolute waits report human duration and normalize timezone offsets', async (t) => {
  t.mock.method(Date, 'now', () => Date.parse('2026-10-08T10:00:00Z'));
  const controller = new AbortController();
  const updates = [];
  await assert.rejects(sleep('2026-10-08T12:05:00+01:00', controller.signal, (update) => {
    updates.push(update); controller.abort();
  }), { name: 'AbortError' });
  assert.equal(updates[0].content[0].text, 'Waiting 1 hour, 5 minutes; expected wake: 2026-10-08T11:05:00.000Z.');
  const pastUpdates = [];
  const past = await sleep('2020-01-01T00:00:00Z', undefined, (update) => pastUpdates.push(update));
  assert.equal(past.details.durationMs, 0);
  assert.match(pastUpdates[0].content[0].text, /Waiting 0 seconds/);
  assert.match(past.content[0].text, /expected wake: 2020-01-01T00:00:00\.000Z; interval: 0 seconds/);
});

test('completed waits retain readable milliseconds, target and actual wake details', async () => {
  const result = await sleep('0.001 seconds');
  assert.equal(result.details.durationMs, 1);
  assert.match(result.content[0].text, /interval: 1 millisecond\./);
  assert.ok(result.content[0].text.includes(result.details.target));
  assert.ok(result.content[0].text.includes(result.details.wokeAt));
});

test('already cancelled waits do not publish progress', async () => {
  const controller = new AbortController(); controller.abort();
  const updates = [];
  await assert.rejects(sleep('1 hour', controller.signal, (update) => updates.push(update)));
  assert.equal(updates.length, 0);
});

test('tool progress is the only waiting display when a callback and UI are both available', async () => {
  const controller = new AbortController(), updates = [], notices = [], statuses = [];
  await assert.rejects(sleep('1 hour', controller.signal, update => {
    updates.push(update); controller.abort();
  }, { hasUI: true, ui: { notify: text => notices.push(text), setStatus: (...args) => statuses.push(args) } }), { name: 'AbortError' });
  assert.equal(updates.length, 1); assert.deepEqual(notices, []); assert.deepEqual(statuses, []);
});

test('fallback uses one replaceable UI status and clears it on cancellation', async () => {
  const controller = new AbortController(), statuses = [], notices = [];
  const pending = sleep('1 hour', controller.signal, undefined, {
    hasUI: true, ui: { setStatus: (...args) => statuses.push(args), notify: text => notices.push(text) },
  });
  try {
    assert.equal(statuses.length, 1); assert.match(statuses[0][1], /^Waiting 1 hour; expected wake: .*Z\.$/);
    assert.deepEqual(notices, []);
  } finally { controller.abort(); await assert.rejects(pending, { name: 'AbortError' }); }
  assert.deepEqual(statuses.at(-1), [statuses[0][0], undefined]);
});

test('countdown publishes at a cadence appropriate to remaining duration', async t => {
  for (const [time, cadence] of [['2 hours', 60000], ['30 minutes', 15000], ['3 minutes', 5000], ['30 seconds', 1000]]) {
    t.mock.timers.enable({ apis: ['Date', 'setTimeout'], now: Date.parse('2026-10-18T20:00:00Z') });
    const controller = new AbortController(), updates = [];
    const pending = freshSleep()(time, controller.signal, update => updates.push(update));
    try {
      assert.equal(updates.length, 1);
      t.mock.timers.tick(cadence - 1); await Promise.resolve(); assert.equal(updates.length, 1);
      t.mock.timers.tick(1); await Promise.resolve(); await Promise.resolve();
      assert.equal(updates.length, 2, time);
      assert.equal(updates[1].details.remainingMs, updates[0].details.durationMs - cadence);
      assert.equal(updates[1].details.target, updates[0].details.target);
      assert.match(updates[1].content[0].text, /remaining/);
    } finally { controller.abort(); await assert.rejects(pending, { name: 'AbortError' }); t.mock.timers.reset(); }
  }
});

test('countdown accelerates in the final minute and completes at the original target', async t => {
  t.mock.timers.enable({ apis: ['Date', 'setTimeout'], now: Date.parse('2026-10-18T20:00:00Z') });
  const controller = new AbortController(), updates = [];
  const pending = freshSleep()('65 seconds', controller.signal, update => updates.push(update));
  try {
    t.mock.timers.tick(5000); await Promise.resolve(); await Promise.resolve();
    assert.equal(updates.at(-1).details.remainingMs, 60000);
    t.mock.timers.tick(1000); await Promise.resolve(); await Promise.resolve();
    assert.equal(updates.at(-1).details.remainingMs, 59000);
    t.mock.timers.tick(59000); const result = await pending;
    assert.equal(result.details.target, '2026-10-18T20:01:05.000Z');
    assert.equal(result.details.wokeAt, result.details.target); assert.equal(result.details.durationMs, 65000);
  } finally { controller.abort(); await pending.catch(() => {}); }
});

test('fallback countdown replaces one status and clears it after success', async t => {
  t.mock.timers.enable({ apis: ['Date', 'setTimeout'], now: Date.parse('2026-10-18T20:00:00Z') });
  const statuses = [], notices = [], controller = new AbortController();
  const pending = freshSleep()('3 seconds', controller.signal, undefined, { hasUI: true,
    ui: { setStatus: (...args) => statuses.push(args), notify: text => notices.push(text) } });
  try {
    t.mock.timers.tick(1000); await Promise.resolve(); await Promise.resolve();
    assert.match(statuses.at(-1)[1], /Waiting 2 seconds remaining/);
    t.mock.timers.tick(2000); await pending;
    assert.deepEqual(statuses.at(-1), [statuses[0][0], undefined]);
    assert.equal(new Set(statuses.map(([key]) => key)).size, 1); assert.deepEqual(notices, []);
  } finally { controller.abort(); await pending.catch(() => {}); }
});

test('concurrent fallback waits use separate status keys and cancellation leaves no updates', async () => {
  const a = new AbortController(), b = new AbortController(), visible = new Map();
  const ctx = { hasUI: true, ui: { setStatus: (key, value) => value === undefined ? visible.delete(key) : visible.set(key, value) } };
  const first = tools.sleep_until.execute('first', { time: '1 hour' }, a.signal, undefined, ctx);
  const second = tools.sleep_until.execute('second', { time: '1 hour' }, b.signal, undefined, ctx);
  assert.equal(visible.size, 2);
  a.abort(); await assert.rejects(first, { name: 'AbortError' }); assert.equal(visible.size, 1);
  b.abort(); await assert.rejects(second, { name: 'AbortError' }); assert.equal(visible.size, 0);
});

test('cancelled countdown has no later progress callbacks', async t => {
  t.mock.timers.enable({ apis: ['Date', 'setTimeout'], now: Date.parse('2026-10-18T20:00:00Z') });
  const controller = new AbortController(), updates = [];
  const pending = freshSleep()('30 seconds', controller.signal, update => updates.push(update));
  controller.abort(); await assert.rejects(pending, { name: 'AbortError' });
  t.mock.timers.tick(300000); await Promise.resolve(); assert.equal(updates.length, 1);
});

test('a real short wait emits a decreasing countdown without a duplicate UI notice', async () => {
  const updates = [], notices = [];
  const result = await sleep('1.05 seconds', undefined, update => updates.push(update), {
    hasUI: true, ui: { notify: text => notices.push(text) },
  });
  assert.ok(updates.length >= 2); assert.deepEqual(notices, []);
  assert.ok(updates[1].details.remainingMs < updates[0].details.remainingMs);
  assert.equal(updates[1].details.target, result.details.target);
  assert.ok(Date.parse(result.details.wokeAt) >= Date.parse(result.details.target));
});

test('callback-equipped headless clients receive countdown events without UI calls', async t => {
  t.mock.timers.enable({ apis: ['Date', 'setTimeout'], now: Date.parse('2026-10-18T20:00:00Z') });
  const controller = new AbortController(), updates = [];
  const pending = freshSleep()('30 seconds', controller.signal, update => updates.push(update), { hasUI: false,
    ui: { setStatus: () => assert.fail('No headless UI status'), notify: () => assert.fail('No headless notification') } });
  try {
    t.mock.timers.tick(1000); await Promise.resolve(); await Promise.resolve();
    assert.equal(updates.length, 2); assert.equal(updates[1].details.remainingMs, 29000);
  } finally { controller.abort(); await assert.rejects(pending, { name: 'AbortError' }); }
});

test('a disposed fallback UI cannot mask cancellation or a successful result', async () => {
  const ui = { setStatus: (_key, value) => { if (value === undefined) throw new Error('UI disposed'); } };
  const controller = new AbortController();
  const pending = sleep('1 hour', controller.signal, undefined, { hasUI: true, ui });
  controller.abort(); await assert.rejects(pending, { name: 'AbortError' });
  const result = await sleep('0.001 seconds', undefined, undefined, { hasUI: true, ui });
  assert.ok(Date.parse(result.details.wokeAt) >= Date.parse(result.details.target));
});

test('headless and already-aborted waits do not send UI notices', async () => {
  const notices = [];
  const ui = { notify: (text) => notices.push(text) };
  await sleep('0.001 seconds', undefined, undefined, { hasUI: false, ui });
  const controller = new AbortController(); controller.abort();
  await assert.rejects(sleep('1 hour', controller.signal, undefined, { hasUI: true, ui }));
  assert.deepEqual(notices, []);
});

test('rejects ambiguous, invalid, and sub-millisecond inputs', async () => {
  for (const time of ['tomorrow', '3 months', '0 hours', '-2 hours', '0.0001 seconds',
    '2026-10-08T12:00:00', '2026-02-30T12:00:00Z', '2026-10-08T12:00:00+25:00',
    '2026-10-08T12:00:00+01:60',
    '999999999999999999 hours']) {
    await assert.rejects(sleep(time), Error, time);
  }
});

test('abort interrupts a pending wait', async () => {
  const controller = new AbortController();
  const pending = sleep('3 hours', controller.signal);
  controller.abort();
  await assert.rejects(pending, { name: 'AbortError' });
  await assert.rejects(sleep('1 second', controller.signal));
});
