const { test } = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const { createJiti } = require(require.resolve('jiti', { paths: [path.resolve(__dirname, '../extensions')] }));
const jiti = createJiti(__filename);
const { attentionBell } = jiti(path.resolve(__dirname, '../extensions/lib/attention-bell.ts'));
const extension = jiti(path.resolve(__dirname, '../extensions/attention-bell.ts')).default;

test('BEL is a single control byte, TUI/TTY only, and write failure is harmless', () => {
  for (const mode of ['tui', 'rpc', 'json', 'print']) {
    for (const isTTY of [true, false, undefined]) {
      const writes = [];
      attentionBell({ mode }, { isTTY, write: value => writes.push(value) });
      assert.deepEqual(writes, mode === 'tui' && isTTY ? ['\x07'] : []);
    }
  }
  assert.doesNotThrow(() => attentionBell({ mode: 'tui' }, { isTTY: true, write() { throw Error('closed'); } }));
});

test('lifecycle hooks ring only at full settlement or successful compaction, without side effects at load', () => {
  const handlers = {}, writes = [];
  const oldWrite = process.stdout.write;
  const tty = Object.getOwnPropertyDescriptor(process.stdout, 'isTTY');
  Object.defineProperty(process.stdout, 'isTTY', { configurable: true, value: true });
  process.stdout.write = function (value, ...args) {
    if (value === '\x07') { writes.push(value); return true; }
    return oldWrite.call(this, value, ...args);
  };
  try {
    extension({ on: (name, handler) => handlers[name] = handler });
    assert.deepEqual(Object.keys(handlers).sort(), ['agent_settled', 'session_compact']);
    assert.equal(writes.length, 0);
    handlers.agent_settled({ aborted: false }, { mode: 'tui' });
    handlers.agent_settled({}, { mode: 'tui' }); // minimum supported SDK
    handlers.agent_settled({ aborted: true }, { mode: 'tui' });
    handlers.session_compact({}, { mode: 'tui' });
    assert.equal(writes.length, 3);
    for (const mode of ['rpc', 'json', 'print']) {
      handlers.agent_settled({}, { mode });
      handlers.session_compact({}, { mode });
    }
    Object.defineProperty(process.stdout, 'isTTY', { configurable: true, value: false });
    handlers.agent_settled({}, { mode: 'tui' });
    handlers.session_compact({}, { mode: 'tui' });
    assert.equal(writes.length, 3);
  } finally {
    process.stdout.write = oldWrite;
    if (tty) Object.defineProperty(process.stdout, 'isTTY', tty);
    else delete process.stdout.isTTY;
  }
});
