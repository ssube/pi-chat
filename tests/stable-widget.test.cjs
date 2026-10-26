const { test } = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const { createJiti } = require(require.resolve('jiti', { paths: [path.resolve(__dirname, '../extensions')] }));
const jiti = createJiti(__filename);
const { stableWidget } = jiti('../extensions/lib/stable-widget.ts');
const { visibleWidth } = jiti(require.resolve('@earendil-works/pi-tui', { paths: [path.resolve(__dirname, '../extensions')] }));

test('updates mount once, preserve widget order, hide/remount deliberately', () => {
  const order = [], mounted = new Map(); let redraws = 0;
  const ui = { setWidget(key, value) {
    const index = order.indexOf(key); if (index >= 0) order.splice(index, 1);
    if (!value) { mounted.delete(key); return; }
    order.push(key); mounted.set(key, value({ terminal: { rows: 24 }, requestRender: () => redraws++ }, {}));
  } };
  const ctx = { mode: 'tui', ui };
  const first = stableWidget('tasks', '/tasks'), second = stableWidget('questions', '/questions');
  first(ctx, () => ['first']); second(ctx, () => ['second']);
  for (let i = 0; i < 100; i++) first(ctx, () => [`update ${i}`]);
  assert.deepEqual(order, ['tasks', 'questions']); assert.equal(redraws, 100);
  assert.deepEqual(mounted.get('tasks').render(80), [' update 99']);
  first(ctx); assert.deepEqual(order, ['questions']);
  first(ctx, () => ['returned']); assert.deepEqual(order, ['questions', 'tasks']);
});

test('Unicode previews are width-bounded, height-responsive and disclose overflow', () => {
  let component; const terminal = { rows: 24 };
  const ctx = { mode: 'tui', ui: { setWidget(_key, factory) { component = factory({ terminal, requestRender() {} }, {}); } } };
  const update = stableWidget('tasks', '/tasks');
  update(ctx, () => Array.from({ length: 20 }, (_, i) => `${i} ${'界'.repeat(100)}`));
  for (const rows of [8, 12, 24, 40]) {
    terminal.rows = rows;
    for (const width of [20, 40, 80]) {
      const rendered = component.render(width);
      assert.ok(rendered.length <= Math.min(8, Math.max(1, Math.floor(rows / 3))));
      assert.ok(rendered.every(line => visibleWidth(line) <= width));
      assert.match(rendered.at(-1), /more rows/);
    }
  }
  assert.deepEqual(component.render(0), []);
});

test('headless updates perform no UI calls and a different UI mounts independently', () => {
  let calls = 0; const update = stableWidget('questions', '/questions');
  update({ mode: 'rpc', ui: { setWidget() { throw Error('not terminal'); } } }, () => ['question']);
  for (let i = 0; i < 2; i++) update({ mode: 'tui', ui: { setWidget() { calls++; } } }, () => ['question']);
  assert.equal(calls, 2);
});


test('component widgets retain the array path one-column inset within the width budget', () => {
  let view;
  const update = stableWidget('tasks', '/tasks');
  update({ mode: 'tui', ui: { setWidget(_key, factory) { view = factory({ terminal: { rows: 24 }, requestRender() {} }, {}); } } }, () => ['\x1b[34m界 content\x1b[0m']);
  for (const width of [1, 2, 5, 20, 80]) {
    const [line] = view.render(width); assert.ok(line.startsWith(' ')); assert.ok(visibleWidth(line) <= width);
  }
});
