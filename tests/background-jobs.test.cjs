const { test: nodeTest } = require('node:test');
const managerAvailable = require('node:child_process').spawnSync('systemctl', ['--user', 'show', '--property=Version'],
  { stdio: 'ignore', timeout: 2000 }).status === 0;
const test = (name, fn) => nodeTest(name, { skip: !managerAvailable && 'Requires a real systemd user manager' }, fn);
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const { setTimeout: delay } = require('node:timers/promises');
const { createJiti } = require(require.resolve('jiti', { paths: [path.resolve(__dirname, '../extensions')] }));
const extension = createJiti(__filename)(path.resolve(__dirname, '../extensions/background-jobs.ts')).default;

function setup(t, mode = 'tui') {
  const handlers = {}, tools = {}, commands = {}, sent = [], statuses = [], selections = [], titles = [], choicesShown = [], branch = [];
  let confirmed = true;
  const ctx = { mode, cwd: process.cwd(), sessionManager: { getSessionId: () => 'test-session', getBranch: () => branch },
    ui: { setStatus: (...args) => statuses.push(args), setWidget() {}, notify() {}, confirm: async () => confirmed,
      select: async (title, choices) => { titles.push(title); choicesShown.push(choices); const choice = selections.shift(); return typeof choice === 'number' ? choices[choice] : choice; } } };
  extension({ on: (name, fn) => handlers[name] = fn, registerTool: tool => tools[tool.name] = tool,
    registerCommand: (name, cmd) => commands[name] = cmd, sendMessage: (...args) => sent.push(args),
    appendEntry: (customType, data) => branch.push({ type: 'custom', customType, data }) });
  handlers.session_start({}, ctx);
  t.after(() => handlers.session_shutdown({ reason: 'quit' }, ctx));
  const call = async (name, args = {}, signal, context = ctx) => (await tools[name].execute('call', args, signal, undefined, context)).structuredContent;
  const start = (command, options = {}) => call('background_start', { command, timeout_seconds: 5, ...options });
  return { ctx, handlers, tools, commands, sent, statuses, selections, titles, choicesShown, call, start,
    confirm: value => { confirmed = value; } };
}
async function until(fn, timeout = 6000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) { const value = await fn(); if (value) return value; await delay(10); }
  throw new Error('Condition did not become true');
}
const ended = async (s, id) => until(async () => {
  const job = await s.call('background_status', { id });
  return ['succeeded', 'failed', 'cancelled', 'timed_out'].includes(job.status) && job;
});
const node = source => `${JSON.stringify(process.execPath)} -e ${"'" + source.replace(/'/g, "'\\''") + "'"}`;

test('success and failure steer structured results with provenance and both streams', async t => {
  const s = setup(t);
  const launched = await s.start('printf out; printf err >&2; exit 0');
  assert.equal(launched.session_id, 'test-session');
  assert.equal(launched.cwd, process.cwd());
  const success = await ended(s, launched.id);
  assert.equal(success.exit_code, 0);
  assert.equal(success.output, 'outerr');
  assert.equal(success.status, 'succeeded');
  const failure = await s.start('echo bad; echo worse >&2; exit 7');
  const failed = await ended(s, failure.id);
  assert.equal(failed.exit_code, 7);
  assert.equal(failed.status, 'failed');
  assert.equal(s.sent.length, 2);
  for (const [message, options] of s.sent) {
    assert.deepEqual(options, { deliverAs: 'steer', triggerTurn: true });
    assert.equal(message.customType, 'background-job');
    assert.match(message.content, /untrusted data/);
    assert.equal(message.details.event, 'completed');
    assert.equal(message.details.output_source, 'combined stdout + stderr');
    assert.ok(fs.existsSync(message.details.log_path));
  }
  assert.match(failed.output, /bad\nworse/);
  assert.equal(JSON.parse(fs.readFileSync(failed.metadata_path)).exit_code, 7);
  assert.equal(fs.statSync(path.dirname(failed.log_path)).mode & 0o777, 0o700);
  assert.equal(fs.statSync(failed.log_path).mode & 0o777, 0o600);
  assert.equal(fs.statSync(failed.metadata_path).mode & 0o777, 0o600);
});

test('launch returns immediately; list/status observe live progress and bound large tails', async t => {
  const s = setup(t, 'rpc');
  const controller = new AbortController();
  const launch = await s.call('background_start', {
    command: node('console.log("ready"); setTimeout(() => console.log("done"), 500);'), timeout_seconds: 5,
  }, controller.signal);
  controller.abort(); // No ownership by a foreground tool signal after launch.
  await until(async () => (await s.call('background_status', { id: launch.id })).output.includes('ready'));
  const list = await s.call('background_list');
  assert.equal(list.total, 1); assert.equal(list.active, 1); assert.equal(list.completed, 0);
  assert.match(list.jobs[0].output, /ready/);
  assert.ok(list.jobs[0].output_bytes > 0);
  assert.ok(list.jobs[0].elapsed_ms > 0);
  assert.deepEqual(s.statuses, []);
  await ended(s, launch.id);
  const large = await s.start(node('process.stdout.write("x".repeat(200000) + "\\nlast\\nend\\n");'));
  const final = await ended(s, large.id);
  assert.ok(final.output_bytes > 200000);
  assert.ok(Buffer.byteLength(final.output) <= 16384);
  assert.equal(final.truncated, true);
  assert.ok(fs.statSync(final.log_path).size > 200000);
  const tail = await s.call('background_status', { id: large.id, tail_lines: 2 });
  assert.equal(tail.output, 'last\nend');
  const paged = await s.call('background_list', { limit: 1 });
  assert.equal(paged.jobs.length, 1); assert.equal(paged.next_offset, 1);
  assert.equal((await s.call('background_list', { offset: 1, limit: 1 })).jobs[0].id, large.id);
});

test('oneshot timeout kills the cgroup, including a TERM-ignoring child in a new process group', async t => {
  const s = setup(t);
  const source = 'process.on("SIGTERM", () => {}); console.log(process.pid); setInterval(() => {}, 1000);';
  const job = await s.start(`setsid ${node(source)} & wait`, { timeout_seconds: 0.4 });
  const ready = await until(async () => { const status = await s.call('background_status', { id: job.id }); return /^\d+/.test(status.output) && status; });
  const childPid = Number(ready.output.trim());
  const final = await ended(s, job.id);
  assert.equal(final.status, 'timed_out');
  assert.equal(final.deadline_exceeded, true);
  assert.equal(final.signal, 'SIGTERM');
  assert.equal(final.exit_code, null);
  // A killed child may remain a zombie until this container's PID 1 reaps it.
  try { assert.equal(fs.readFileSync(`/proc/${childPid}/stat`, 'utf8').split(' ')[2], 'Z'); }
  catch (error) { if (error.code !== 'ENOENT') throw error; }
  assert.equal(s.sent.length, 1);
});

test('explicit cancel is immediate, idempotent and reports signal/exit; unknown IDs fail', async t => {
  const s = setup(t);
  const job = await s.start('sleep 20');
  assert.equal((await s.call('background_cancel', { id: job.id })).status, 'stopping');
  await s.call('background_cancel', { id: job.id });
  assert.equal((await ended(s, job.id)).status, 'cancelled');
  assert.equal(s.sent.length, 1);
  assert.equal((await s.call('background_cancel', { id: job.id })).status, 'cancelled');
  await assert.rejects(s.call('background_status', { id: 'missing' }), /Unknown/);
});

test('unsafe jobs exceed deadline without termination, refuse cancel, then report real completion', async t => {
  const s = setup(t);
  const job = await s.start('sleep 1; echo survived', { timeout_seconds: 0.05, unsafe_to_terminate: true });
  await until(async () => (await s.call('background_status', { id: job.id })).status === 'deadline_exceeded');
  assert.equal(s.sent.length, 0); // Only completion steers automatically.
  await assert.rejects(s.call('background_cancel', { id: job.id }), /unsafe to terminate/);
  const final = await ended(s, job.id);
  assert.equal(final.status, 'succeeded'); assert.equal(final.exit_code, 0);
  assert.equal(final.deadline_exceeded, true); assert.equal(final.output, 'survived');
  assert.equal(s.sent.length, 1);
});

test('shutdown stops safe jobs, detaches unsafe jobs, keeps logs, and never steers the next session', async t => {
  const s = setup(t);
  const safe = await s.start('sleep 20');
  const unsafe = await s.start('sleep 1.3; echo survived-cleanup', { unsafe_to_terminate: true });
  await s.handlers.session_shutdown({ reason: 'reload' }, s.ctx);
  assert.equal((await s.call('background_status', { id: safe.id })).status, 'cancelled');
  assert.equal((await s.call('background_status', { id: unsafe.id })).status, 'detached');
  await assert.rejects(s.start('echo no'), /No active/);
  s.handlers.session_start({}, { ...s.ctx, sessionManager: { getSessionId: () => 'new-session', getBranch: () => [] } });
  assert.equal((await s.call('background_list')).total, 0);
  await until(() => fs.readFileSync(unsafe.log_path, 'utf8').trim() === 'survived-cleanup');
  assert.equal(s.sent.length, 0);
  const next = await s.start('echo next');
  await ended(s, next.id);
  assert.notEqual(next.log_path, safe.log_path);
});

test('spawn errors are reported; invalid and aborted launches do not create jobs', async t => {
  const s = setup(t);
  for (const params of [{ command: ' ' }, { command: 'a\0b' }, { command: 'echo ok', timeout_seconds: 0 }, { command: 'echo ok', timeout_seconds: Infinity }]) {
    await assert.rejects(s.start(params.command, params));
  }
  const abort = new AbortController(); abort.abort();
  await assert.rejects(s.call('background_start', { command: 'echo no', timeout_seconds: 1 }, abort.signal), /cancelled/);
  assert.equal((await s.call('background_list')).total, 0);
  const failed = await s.call('background_start', { command: 'echo no', timeout_seconds: 1 }, undefined, { ...s.ctx, cwd: '/missing/pi-test-directory' });
  const final = await ended(s, failed.id);
  assert.equal(final.status, 'failed'); assert.equal(final.exit_code, 200);
  assert.match(final.output, /No such file or directory/);
  assert.equal(final.systemd_result, 'exit-code');
});

test('a launch reaches the user manager when XDG_RUNTIME_DIR is unset', async t => {
  if (typeof process.getuid !== 'function' || !fs.existsSync(`/run/user/${process.getuid()}`)) return t.skip('No /run/user/<uid> fallback directory');
  const original = { XDG_RUNTIME_DIR: process.env.XDG_RUNTIME_DIR, DBUS_SESSION_BUS_ADDRESS: process.env.DBUS_SESSION_BUS_ADDRESS };
  delete process.env.XDG_RUNTIME_DIR;
  delete process.env.DBUS_SESSION_BUS_ADDRESS;
  t.after(() => { for (const [key, value] of Object.entries(original)) { if (value === undefined) delete process.env[key]; else process.env[key] = value; } });
  const s = setup(t);
  const job = await s.start('echo runtime-fallback-ok');
  const final = await ended(s, job.id);
  assert.equal(final.status, 'succeeded');
  assert.match(final.output, /runtime-fallback-ok/);
});

test('a launch refuses without creating artifacts when the user manager is unreachable', async t => {
  const original = { XDG_RUNTIME_DIR: process.env.XDG_RUNTIME_DIR, DBUS_SESSION_BUS_ADDRESS: process.env.DBUS_SESSION_BUS_ADDRESS };
  process.env.XDG_RUNTIME_DIR = '/nonexistent-pi-runtime-dir';
  // An exported bus address is an independent connection route on desktops.
  process.env.DBUS_SESSION_BUS_ADDRESS = 'unix:path=/nonexistent-pi-runtime-dir/bus';
  t.after(() => { for (const [key, value] of Object.entries(original)) { if (value === undefined) delete process.env[key]; else process.env[key] = value; } });
  const s = setup(t);
  const count = () => fs.readdirSync(os.tmpdir()).filter(name => name.startsWith('pi-background-systemd-')).length;
  const before = count();
  await assert.rejects(s.start('echo must-not-run'), /No reachable systemd user manager/);
  assert.equal(count(), before);
  assert.equal((await s.call('background_list')).total, 0);
});

test('/jobs shows elapsed status and live output, and offers confirmed cancellation', async t => {
  const s = setup(t);
  const job = await s.start('echo visible; sleep 20', { label: 'CI wait' });
  await until(async () => (await s.call('background_status', { id: job.id })).output.includes('visible'));
  s.selections.push(0, 'Cancel job', undefined);
  await s.commands.jobs.handler('', s.ctx);
  assert.match(s.titles[0], /Background jobs/);
  assert.match(s.titles[1], /visible/);
  assert.match(s.titles[1], /combined stdout\/stderr/);
  assert.equal((await ended(s, job.id)).status, 'cancelled');
});

test('/jobs refuses automatic unsafe cancellation and respects a declined cancel confirmation', async t => {
  const s = setup(t);
  const safe = await s.start('sleep 20');
  s.confirm(false); s.selections.push(0, 'Cancel job', undefined);
  await s.commands.jobs.handler('', s.ctx);
  assert.equal((await s.call('background_status', { id: safe.id })).status, 'running');
  const unsafe = await s.start('sleep 0.3', { unsafe_to_terminate: true });
  s.selections.push(1, 'Back', undefined);
  await s.commands.jobs.handler('', s.ctx);
  assert.deepEqual(s.choicesShown.at(-2), ['Back', 'Send log tail as steering']);
  assert.equal((await ended(s, unsafe.id)).exit_code, 0);
});

test('completion hook retains results after unsafe detach and unit collection, with no Pi observer', async t => {
  const s = setup(t);
  const job = await s.start('sleep 0.2; echo independent-hook; exit 7', { unsafe_to_terminate: true });
  await s.handlers.session_shutdown({ reason: 'reload' }, s.ctx);
  await until(() => fs.existsSync(job.completion_path));
  const completion = JSON.parse(fs.readFileSync(job.completion_path, 'utf8'));
  assert.equal(completion.status, 'failed');
  assert.equal(completion.exit_code, 7);
  assert.equal(fs.existsSync(path.join(job.directory, 'environment')), false);
  assert.equal(fs.statSync(job.completion_path).mode & 0o777, 0o600);
  assert.equal(s.sent.length, 0);
  s.handlers.session_start({}, s.ctx);
  const restored = await ended(s, job.id);
  assert.equal(restored.exit_code, 7); assert.match(restored.output, /independent-hook/);
  assert.equal(s.sent.length, 1);
  await s.call('background_status', { id: job.id });
  assert.equal(s.sent.length, 1);
});

test('environment and cwd survive quoting without becoming systemd or shell expansions', async t => {
  const s = setup(t);
  const cwd = fs.mkdtempSync(path.join(require('node:os').tmpdir(), "pi cwd % $ ' "));
  const value = "literal $PATH %n \\\\ \"quoted\" `code`\nsecond line";
  process.env.PI_BG_TEST_VALUE = value;
  t.after(() => { delete process.env.PI_BG_TEST_VALUE; });
  const launched = await s.call('background_start', {
    command: node('console.log(JSON.stringify({cwd:process.cwd(),value:process.env.PI_BG_TEST_VALUE}));'), timeout_seconds: 5,
  }, undefined, { ...s.ctx, cwd });
  const job = await ended(s, launched.id);
  assert.equal(job.status, 'succeeded');
  assert.deepEqual(JSON.parse(job.output), { cwd, value });
  assert.equal(fs.existsSync(path.join(job.directory, 'environment')), false);
});

test('filesystem completion arrives without status polling, retaining slice, description and systemd execution data', async t => {
  const s = setup(t, 'rpc');
  const label = 'event test %n';
  const job = await s.start('sleep 0.2; echo event-driven', { label });
  await until(() => s.sent.length === 1);
  const event = s.sent[0][0].details;
  assert.equal(event.event, 'completed');
  assert.equal(event.status, 'succeeded');
  assert.match(event.slice, /^pi-jobs-[a-f0-9]{20}\.slice$/);
  assert.equal(event.systemd_final.Slice, event.slice);
  assert.equal(event.systemd_final.Description, 'Pi ' + job.id + ': ' + label);
  assert.equal(event.systemd_final.ExecMainStatus, '0');
  assert.ok(BigInt(event.systemd_final.ExecMainStartTimestampMonotonic) > 0n);
  assert.ok(BigInt(event.systemd_final.ExecMainExitTimestampMonotonic) >= BigInt(event.systemd_final.ExecMainStartTimestampMonotonic));
  assert.equal(event.progress_interval_seconds, null);
  assert.equal(event.last_progress_at, null);
});

test('progress is opt-in and rate-limited; neither progress nor unsafe deadline steers', async t => {
  const s = setup(t);
  for (const progress_interval of ['0s', '1s', 'toString']) {
    await assert.rejects(s.start('echo no', { progress_interval }), /Invalid progress_interval/);
  }
  const job = await s.start('echo visible-progress; sleep 7', { timeout_seconds: 10, progress_interval: '5s' });
  const initial = JSON.parse(fs.readFileSync(job.metadata_path, 'utf8'));
  assert.equal(initial.last_progress_at, null);
  const sample = await until(() => {
    const metadata = JSON.parse(fs.readFileSync(job.metadata_path, 'utf8'));
    return metadata.last_progress_at && metadata;
  }, 6500);
  assert.ok(Date.parse(sample.last_progress_at) - Date.parse(job.started_at) >= 4900);
  assert.equal(sample.progress_interval_seconds, 5);
  assert.match(sample.progress_snapshot.latest_line, /visible-progress/);
  assert.equal(sample.systemd.Slice, job.slice);
  assert.equal(s.sent.length, 0);
  await s.call('background_cancel', { id: job.id });
  await ended(s, job.id);
});

test('/jobs manual steering carries a bounded tail and does not suppress automatic completion', async t => {
  const s = setup(t);
  const job = await s.start(node('process.stdout.write("x".repeat(200000)); setTimeout(() => {}, 1500);'));
  await until(() => fs.statSync(job.log_path).size > 16384);
  assert.equal(s.sent.length, 0);
  s.selections.push(0, 'Send log tail as steering');
  await s.commands.jobs.handler('', s.ctx);
  assert.equal(s.sent.length, 1);
  const [message, options] = s.sent[0];
  assert.equal(message.details.event, 'log_tail_requested');
  assert.equal(message.details.id, job.id);
  assert.equal(message.details.log_path, job.log_path);
  assert.equal(message.details.truncated, true);
  assert.ok(Buffer.byteLength(message.details.output) <= 16384);
  assert.deepEqual(options, { deliverAs: 'steer', triggerTurn: true });
  await ended(s, job.id);
  assert.equal(s.sent.length, 2);
  assert.equal(s.sent[1][0].details.event, 'completed');
});

test('restored observation metadata cannot change immutable unit identity or unsafe policy', async t => {
  const s = setup(t);
  const job = await s.start('sleep 1; echo immutable', { unsafe_to_terminate: true });
  await s.handlers.session_shutdown({ reason: 'reload' }, s.ctx);
  const metadata = JSON.parse(fs.readFileSync(job.metadata_path, 'utf8'));
  Object.assign(metadata, { unit: 'other.service', session_id: 'other-session', slice: 'user.slice',
    unsafe_to_terminate: false, log_path: '/not-the-job-log', command: 'unexpected' });
  fs.writeFileSync(job.metadata_path, JSON.stringify(metadata));
  await s.handlers.session_start({}, s.ctx);
  const restored = await s.call('background_status', { id: job.id });
  assert.equal(restored.unit, job.unit);
  assert.equal(restored.slice, job.slice);
  assert.equal(restored.session_id, job.session_id);
  assert.equal(restored.command, job.command);
  assert.equal(restored.log_path, job.log_path);
  assert.equal(restored.unsafe_to_terminate, true);
  await assert.rejects(s.call('background_cancel', { id: job.id }), /unsafe to terminate/);
  await ended(s, job.id);
});
