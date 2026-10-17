const { test } = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const { createJiti } = require(require.resolve('jiti', { paths: [path.resolve(__dirname, '../extensions')] }));
const extension = createJiti(__filename)(path.resolve(__dirname, '../extensions/pi-time.ts')).default;
const tools = {};
extension({ registerTool: (tool) => { tools[tool.name] = tool; } });
const sleep = (time, signal, onUpdate, ctx) => tools.sleep_until.execute('id', { time }, signal, onUpdate, ctx);

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

test('UI notice is emitted before waiting even when no progress callback is provided', async () => {
  const controller = new AbortController();
  const notices = [];
  const pending = sleep('1 hour', controller.signal, undefined, {
    hasUI: true, ui: { notify: (text, level) => notices.push({ text, level }) },
  });
  try {
    // execute has reached its first await: this must already be visible.
    assert.equal(notices.length, 1);
    assert.match(notices[0].text, /^Waiting 1 hour; expected wake: .*Z\.$/);
    assert.equal(notices[0].level, 'info');
  } finally {
    controller.abort();
    await assert.rejects(pending, { name: 'AbortError' });
  }
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
  for (const time of ['toagent-one', '3 months', '0 hours', '-2 hours', '0.0001 seconds',
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
