const { test } = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const { createJiti } = require(require.resolve('jiti', { paths: [path.resolve(__dirname, '../extensions')] }));
const { default: extension } = createJiti(__filename)(path.resolve(__dirname, '../extensions/bridge-workload-metrics.ts'));
const KEY = 'agent-bridge/workload/counts/v1';
const counts = { pending: 1, progress: 1, blocked: 1, question: 1, complete: 2 };
const flush = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); };
function setup(mode = 'rpc', tasks = counts, fleet = { version: 1, totalActive: 3, topLevelAsyncCapacity: { used: 1, limit: 4 }, entries: [{ goal: 'PRIVATE', key: 'PRIVATE' }] }) {
  const handlers = {}, listeners = new Map(), frames = [], requests = [];
  const events = {
    on(name, fn) { const set = listeners.get(name) ?? new Set(); set.add(fn); listeners.set(name, set); return () => set.delete(fn); },
    emit(name, data) { for (const fn of listeners.get(name) ?? []) fn(data); },
  };
  events.on('tasks:metrics-v1', query => { if (tasks) query.counts = tasks; });
  events.on('subagents:rpc:v1:request', request => {
    requests.push(request);
    if (fleet) events.emit(`subagents:rpc:v1:reply:${request.requestId}`, { version: 1, requestId: request.requestId, success: true, data: { fleet } });
  });
  const ctx = { mode, ui: { setStatus: (key, text) => frames.push({ key, text }) } };
  extension({ events, on: (name, handler) => { handlers[name] = handler; } });
  return { ctx, frames, requests, listeners, events, handlers, start: () => handlers.session_start({}, ctx), stop: () => handlers.session_shutdown({}, ctx) };
}
test('RPC observer sends only bounded aggregate counts, with no inference or tool execution API', async () => {
  const s = setup(); s.start(); await flush();
  assert.equal(s.frames.length, 1); assert.equal(s.frames[0].key, KEY);
  assert.deepEqual(JSON.parse(s.frames[0].text), { version: 1, tasks: counts, subagents: { active: 3, capacity_used: 1, capacity_limit: 4 } });
  assert.ok(s.frames[0].text.length < 512); assert.doesNotMatch(s.frames[0].text, /PRIVATE|requestId|entries|goal/);
  assert.equal(s.requests[0].method, 'status'); assert.deepEqual(s.requests[0].params, {});
  assert.equal([...s.listeners.keys()].filter(key => key.startsWith('subagents:rpc:v1:reply:')).reduce((n, key) => n + s.listeners.get(key).size, 0), 0);
  s.stop(); assert.equal(s.frames.at(-1).text, undefined);
});
test('TUI/headless modes never query sources or publish status', async () => {
  for (const mode of ['tui', 'print']) { const s = setup(mode); s.start(); await flush(); assert.equal(s.frames.length, 0); assert.equal(s.requests.length, 0); s.stop(); }
});
test('valid native unlimited capacity preserves outstanding work, including zero', async () => {
  for (const active of [0, 3]) {
    const s = setup('rpc', counts, { version: 1, totalActive: active, topLevelAsyncCapacity: { used: active, limit: 0 } });
    s.start(); await flush();
    assert.deepEqual(JSON.parse(s.frames[0].text).subagents, { active, capacity_used: active, capacity_limit: 0 });
    s.stop();
  }
});
test('missing or invalid sources are null, not zero, and independent', async () => {
  const s = setup('rpc', null, { version: 1, totalActive: -1, topLevelAsyncCapacity: { used: 0, limit: 4 } });
  s.start(); await flush(); assert.deepEqual(JSON.parse(s.frames[0].text), { version: 1, tasks: null, subagents: null }); s.stop();
  const partial = setup('rpc', { ...counts, secret: 1 }); partial.start(); await flush();
  assert.equal(JSON.parse(partial.frames[0].text).tasks, null); assert.equal(JSON.parse(partial.frames[0].text).subagents.active, 3); partial.stop();
});
test('absent subagent source times out; stop cancels late replies and cleans listeners', async t => {
  t.mock.timers.enable({ apis: ['setTimeout', 'setInterval'] });
  const s = setup('rpc', counts, null); s.start(); await flush(); assert.equal(s.frames.length, 0);
  t.mock.timers.tick(1_001); await flush(); assert.equal(JSON.parse(s.frames[0].text).subagents, null);
  t.mock.timers.tick(15_000); await flush(); s.stop(); const before = s.frames.length;
  await flush(); assert.equal(s.frames.length, before, 'cancelled update cannot republish');
  assert.ok([...s.listeners.entries()].filter(([key]) => key.startsWith('subagents:rpc:v1:reply:')).every(([, handlers]) => handlers.size === 0));
});
test('changes coalesce, heartbeat refreshes, and switching clears the previous session', async t => {
  t.mock.timers.enable({ apis: ['setTimeout', 'setInterval'] });
  const s = setup(); s.start(); await flush();
  for (let i = 0; i < 100; i++) s.events.emit('tasks:changed-v1', {});
  t.mock.timers.tick(999); await flush(); assert.equal(s.frames.length, 1);
  t.mock.timers.tick(1); await flush(); assert.equal(s.frames.length, 2);
  t.mock.timers.tick(14_000); await flush(); assert.equal(s.frames.length, 3);
  s.handlers.session_before_switch(); assert.equal(s.frames.at(-1).text, undefined);
  const before = s.frames.length; t.mock.timers.tick(60_000); await flush(); assert.equal(s.frames.length, before);
  s.start(); await flush(); assert.equal(JSON.parse(s.frames.at(-1).text).version, 1); s.stop();
});

test('real pinned Pi RPC emits the observer status while idle, without inference', { skip: !process.env.PI_TASKS_SDK_MODULE, timeout: 20_000 }, async () => {
  const fs = require('node:fs'); const os = require('node:os'); const { spawn } = require('node:child_process');
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'bridge-observer-rpc-'));
  const fixture = path.join(root, 'sources.ts');
  fs.writeFileSync(fixture, `export default function(pi) {
    pi.events.on('tasks:metrics-v1', query => { query.counts = {pending:2,progress:1,blocked:0,question:0,complete:3}; });
    pi.events.on('subagents:rpc:v1:request', request => {
      if(request.method !== 'status' || Object.keys(request.params).length) throw new Error('not read-only');
      pi.events.emit('subagents:rpc:v1:reply:'+request.requestId, {version:1,requestId:request.requestId,success:true,data:{fleet:{version:1,totalActive:2,topLevelAsyncCapacity:{used:1,limit:4}}}});
    });
  }`);
  const cli = path.join(path.dirname(path.resolve(process.env.PI_TASKS_SDK_MODULE)), 'bundle/cli.js');
  const child = spawn(process.execPath, [cli, '--mode', 'rpc', '--no-extensions', '-e', fixture, '-e', path.resolve(__dirname, '../extensions/bridge-workload-metrics.ts'), '--no-skills', '--no-prompt-templates', '--no-themes', '--session', path.join(root, 'session.jsonl')], {
    cwd: root, env: { PATH: process.env.PATH, HOME: root, PI_CODING_AGENT_DIR: path.join(root, 'agent'), NODE_PATH: process.env.NODE_PATH }, stdio: ['pipe', 'pipe', 'pipe'],
  });
  let buffer = ''; const frames = []; let stderr = '';
  child.stderr.on('data', data => { stderr += data; });
  try {
    const snapshot = await new Promise((resolve, reject) => {
      const deadline = setTimeout(() => reject(new Error('No idle observer frame: '+stderr.slice(0, 500))), 15_000);
      child.on('exit', code => { clearTimeout(deadline); reject(new Error('Pi exited '+code+': '+stderr.slice(0, 500))); });
      child.stdout.on('data', chunk => {
        buffer += chunk;
        while (buffer.includes('\n')) {
          const end = buffer.indexOf('\n'), line = buffer.slice(0, end); buffer = buffer.slice(end+1);
          let event; try { event = JSON.parse(line); } catch { continue; }
          frames.push(event);
          if (event.type === 'extension_ui_request' && event.method === 'setStatus' && event.statusKey === KEY && event.statusText) {
            clearTimeout(deadline); resolve(JSON.parse(event.statusText));
          }
        }
      });
    });
    assert.deepEqual(snapshot, { version: 1, tasks: { pending: 2, progress: 1, blocked: 0, question: 0, complete: 3 }, subagents: { active: 2, capacity_used: 1, capacity_limit: 4 } });
    assert.ok(!frames.some(event => ['agent_start','message_start','message_update'].includes(event.type)), 'idle fixture must not start a model turn');
  } finally {
    child.kill('SIGTERM'); await new Promise(resolve => child.exitCode !== null ? resolve() : child.once('exit', resolve));
    fs.rmSync(root, { recursive: true, force: true });
  }
});
