const { test } = require('node:test');
const assert = require('node:assert/strict');
const { mkdtempSync, writeFileSync, chmodSync, rmSync } = require('node:fs');
const { tmpdir } = require('node:os');
const path = require('node:path');
const { createJiti } = require(require.resolve('jiti', { paths: [path.resolve(__dirname, '../extensions')] }));
const extension = createJiti(__filename)(path.resolve(__dirname, '../extensions/matrix-room-attention.ts')).default;

test('labels are loaded only for interactive non-bridge Pi from an owner-only file', () => {
  const dir = mkdtempSync(path.join(tmpdir(), 'room-label-test-'));
  const file = path.join(dir, 'label.json');
  const names = ['PI_ROOM_LEASE_LABEL_FILE', 'PI_ROOM_LEASE_LABEL_ROOM', 'PI_ROOM_LEASE_LABEL_NAME', 'AGENT_BRIDGE_INVOCATION'];
  const previous = Object.fromEntries(names.map(name => [name, process.env[name]]));
  try {
    writeFileSync(file, JSON.stringify({ room_id: '!test:example.invalid', name: 'Test room' }), { mode: 0o600 });
    process.env.PI_ROOM_LEASE_LABEL_FILE = file;
    delete process.env.AGENT_BRIDGE_INVOCATION;
    delete process.env.PI_ROOM_LEASE_LABEL_ROOM;
    delete process.env.PI_ROOM_LEASE_LABEL_NAME;
    let start;
    extension({ on: (_name, handler) => { start = handler; } });
    start({}, { mode: 'rpc' });
    assert.equal(process.env.PI_ROOM_LEASE_LABEL_NAME, undefined);
    process.env.AGENT_BRIDGE_INVOCATION = '1';
    start({}, { mode: 'tui' });
    assert.equal(process.env.PI_ROOM_LEASE_LABEL_NAME, undefined);
    delete process.env.AGENT_BRIDGE_INVOCATION;
    start({}, { mode: 'tui' });
    assert.equal(process.env.PI_ROOM_LEASE_LABEL_ROOM, '!test:example.invalid');
    assert.equal(process.env.PI_ROOM_LEASE_LABEL_NAME, 'Test room');
    chmodSync(file, 0o644);
    assert.throws(() => start({}, { mode: 'tui' }), /owner-only/);
  } finally {
    for (const [name, value] of Object.entries(previous)) {
      if (value === undefined) delete process.env[name];
      else process.env[name] = value;
    }
    rmSync(dir, { recursive: true, force: true });
  }
});
