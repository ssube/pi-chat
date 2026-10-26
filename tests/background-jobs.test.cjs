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
const { default: extension, retentionConfig, expiredJobs, exactUnitMatches } = createJiti(__filename)(path.resolve(__dirname, '../extensions/background-jobs.ts'));

nodeTest('stable backend registers its tools without a draft label', () => {
  const tools = {};
  extension({ events: { on() {}, emit() {} }, on() {}, registerTool: tool => tools[tool.name] = tool, registerCommand() {} });
  assert.deepEqual(Object.keys(tools).sort(), [
    'background_cancel', 'background_list', 'background_start', 'background_status',
  ]);
  assert.match(tools.background_start.description, /^Systemd backend:/);
  assert.doesNotMatch(tools.background_start.description, /draft|experimental/i);
});

function setup(t, mode = 'tui', env = {}) {
  const handlers = {}, tools = {}, commands = {}, sent = [], statuses = [], selections = [], titles = [], choicesShown = [], branch = [];
  let confirmed = true;
  const ctx = { mode, cwd: process.cwd(), sessionManager: { getSessionId: () => 'test-session', getBranch: () => branch },
    ui: { setStatus: (...args) => statuses.push(args), setWidget() {}, notify() {}, confirm: async () => confirmed,
      custom: async factory => {
        const view = factory({ terminal: { rows: 24 }, requestRender() {} }, {}, {}, () => {});
        titles.push(view.body || view.title); choicesShown.push(view.choices);
        const choice = selections.shift(); return typeof choice === 'number' ? view.choices[choice] : choice;
      } } };
  const listeners = new Map();
  const events = {
    on: (name, fn) => { const list = listeners.get(name) ?? []; list.push(fn); listeners.set(name, list); },
    emit: (name, payload) => { for (const fn of listeners.get(name) ?? []) fn(payload); },
  };
  const original = Object.fromEntries(Object.keys(env).map(key => [key, process.env[key]]));
  Object.assign(process.env, env);
  try { extension({ events, on: (name, fn) => handlers[name] = fn, registerTool: tool => tools[tool.name] = tool,
    registerCommand: (name, cmd) => commands[name] = cmd, sendMessage: (...args) => sent.push(args),
    appendEntry: (customType, data) => branch.push({ type: 'custom', customType, data }) });
  } finally { for (const [key, value] of Object.entries(original)) {
    if (value === undefined) delete process.env[key]; else process.env[key] = value;
  } }
  const ready = handlers.session_start({}, ctx);
  t.after(() => handlers.session_shutdown({ reason: 'quit' }, ctx));
  const call = async (name, args = {}, signal, context = ctx) => (await tools[name].execute('call', args, signal, undefined, context)).structuredContent;
  const start = (command, options = {}) => call('background_start', { command, timeout_seconds: 5, ...options });
  return { events, ready, branch, ctx, handlers, tools, commands, sent, statuses, selections, titles, choicesShown, call, start,
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

// Refusal must also execute in CI without a user manager, unlike live lifecycle tests.
nodeTest('a launch refuses without creating artifacts when the user manager is unreachable', async t => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'pi-background-refusal-test-'));
  const original = { XDG_RUNTIME_DIR: process.env.XDG_RUNTIME_DIR,
    DBUS_SESSION_BUS_ADDRESS: process.env.DBUS_SESSION_BUS_ADDRESS, TMPDIR: process.env.TMPDIR };
  process.env.XDG_RUNTIME_DIR = path.join(directory, 'missing-runtime');
  // An exported bus address is an independent connection route on desktops.
  process.env.DBUS_SESSION_BUS_ADDRESS = `unix:path=${directory}/missing-runtime/bus`;
  process.env.TMPDIR = directory;
  t.after(() => { for (const [key, value] of Object.entries(original)) { if (value === undefined) delete process.env[key]; else process.env[key] = value; }
    fs.rmSync(directory, { recursive: true, force: true }); });
  const s = setup(t);
  await s.ready;
  await assert.rejects(s.start('echo must-not-run'), /No reachable systemd user manager/);
  // Only this test owns the temporary root; unrelated jobs cannot change it.
  assert.deepEqual(fs.readdirSync(directory), []);
  assert.equal((await s.call('background_list')).total, 0);
  assert.deepEqual(s.branch, []);
  assert.deepEqual(s.sent, []);
});

nodeTest('exact unit identity requires the recorded description, slice and accepted submission', () => {
  const job = { id: 'job-1', label: 'Background job', slice: 'pi-jobs-session.slice', submission_state: 'confirmed',
    systemd: { LoadState: 'loaded', Slice: 'pi-jobs-session.slice', Description: 'Pi job-1: Background job' } };
  assert.equal(exactUnitMatches(job), true);
  assert.equal(exactUnitMatches({ ...job, systemd: { ...job.systemd, Description: 'Other job' } }), false);
  assert.equal(exactUnitMatches({ ...job, systemd: { ...job.systemd, Slice: 'another-session.slice' } }), false);
  assert.equal(exactUnitMatches({ ...job, systemd: { ...job.systemd, LoadState: 'not-found' } }), false);
  assert.equal(exactUnitMatches({ ...job, submission_state: 'rejected' }), false);
});

nodeTest('uncertain submission retains exact identity and private environment without resubmission', async t => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'pi-background-uncertain-test-'));
  const bin = path.join(root, 'bin'); fs.mkdirSync(bin, { mode: 0o700 });
  const marker = path.join(root, 'attempts');
  fs.writeFileSync(path.join(bin, 'systemctl'), String.raw`#!/bin/sh
if [ "$3" = "--property=Version" ]; then printf 'Version=256\n'; exit 0; fi
if [ "$FAKE_UNIT_STATE" = "loaded" ]; then
  printf 'LoadState=loaded\nActiveState=activating\nSubState=start\nSlice=%s\nDescription=Pi job-1: Background job\n' "$FAKE_SLICE"
elif [ "$FAKE_UNIT_STATE" = "loaded-wrong-description" ]; then
  printf 'LoadState=loaded\nActiveState=activating\nSubState=start\nSlice=%s\nDescription=Another job\n' "$FAKE_SLICE"
elif [ "$FAKE_UNIT_STATE" = "loaded-inactive" ]; then
  printf 'LoadState=loaded\nActiveState=failed\nSubState=failed\nSlice=%s\nDescription=Pi job-1: Background job\n' "$FAKE_SLICE"
else
  printf 'LoadState=not-found\nActiveState=inactive\nSubState=dead\n'
fi
`, { mode: 0o700 });
  fs.writeFileSync(path.join(bin, 'systemd-run'), String.raw`#!/bin/sh
printf 'attempt\n' >> "$FAKE_SUBMISSION_MARKER"
printf 'simulated manager loss\n' >&2
exit 1
`, { mode: 0o700 });
  const original = { PATH: process.env.PATH, TMPDIR: process.env.TMPDIR,
    FAKE_SUBMISSION_MARKER: process.env.FAKE_SUBMISSION_MARKER,
    FAKE_UNIT_STATE: process.env.FAKE_UNIT_STATE, FAKE_SLICE: process.env.FAKE_SLICE };
  const s = setup(t);
  try {
    process.env.PATH = `${bin}:${original.PATH}`;
    process.env.TMPDIR = root;
    process.env.FAKE_SUBMISSION_MARKER = marker;
    assert.equal(os.tmpdir(), root);
    await s.ready;
    const job = await s.start('echo must-not-run', { unsafe_to_terminate: true });
    assert.equal(job.status, 'unknown');
    assert.equal(job.submission_state, 'uncertain');
    assert.match(job.error, /Submission not confirmed/);
    assert.ok(job.directory.startsWith(root + path.sep));
    assert.match(job.unit, /^pi-bg-[0-9a-f-]+[.]service$/);
    assert.equal(fs.readFileSync(marker, 'utf8'), 'attempt\n');
    assert.equal(fs.existsSync(job.completion_path), false);
    assert.equal(fs.statSync(job.directory).mode & 0o777, 0o700);
    assert.equal(fs.statSync(path.join(job.directory, 'environment')).mode & 0o777, 0o600);
    const request = JSON.parse(fs.readFileSync(path.join(job.directory, 'request.json'), 'utf8'));
    assert.equal(request.unit, job.unit);
    const status = await s.call('background_status', { id: job.id });
    assert.equal(status.status, 'unknown');
    assert.equal(status.submission_state, 'uncertain', 'unit absence is not proof of rejection or non-execution');
    process.env.FAKE_UNIT_STATE = 'loaded'; process.env.FAKE_SLICE = job.slice;
    const matched = await s.call('background_status', { id: job.id });
    assert.equal(matched.submission_state, 'confirmed');
    assert.equal(matched.status, 'running', 'a live exact-identity unit can be reattached');
    process.env.FAKE_UNIT_STATE = 'loaded-wrong-description';
    const wrongDescription = await s.call('background_status', { id: job.id });
    assert.equal(wrongDescription.status, 'unknown');
    assert.match(wrongDescription.error, /identity could not be confirmed/);
    process.env.FAKE_UNIT_STATE = 'loaded-inactive';
    const inactive = await s.call('background_status', { id: job.id });
    assert.equal(inactive.status, 'unknown', 'a non-live unit without hook data has no known outcome');
    assert.match(inactive.error, /outcome unknown/);
    process.env.FAKE_UNIT_STATE = 'loaded'; process.env.FAKE_SLICE = 'another-session.slice';
    const mismatch = await s.call('background_status', { id: job.id });
    assert.equal(mismatch.status, 'unknown');
    assert.match(mismatch.error, /identity could not be confirmed/);
    process.env.FAKE_UNIT_STATE = 'not-found';
    const absent = await s.call('background_status', { id: job.id });
    assert.equal(absent.status, 'unknown');
    assert.match(absent.error, /outcome unknown/);
    await s.call('background_list');
    assert.equal(fs.readFileSync(marker, 'utf8'), 'attempt\n', 'observation must not resubmit');
    assert.equal(fs.existsSync(path.join(job.directory, 'environment')), true);
    assert.equal(s.sent.length, 0);
  } finally {
    try { await s.handlers.session_shutdown({ reason: 'test' }, s.ctx); }
    finally {
      for (const [key, value] of Object.entries(original)) {
        if (value === undefined) delete process.env[key]; else process.env[key] = value;
      }
      // The fake runner cannot create a live unit; remove only this test-owned root.
      fs.rmSync(root, { recursive: true, force: true });
    }
  }
});

nodeTest('local spawn rejection is distinct from uncertain creation and retains the private snapshot', async t => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'pi-background-spawn-rejection-test-'));
  const bin = path.join(root, 'bin'); fs.mkdirSync(bin, { mode: 0o700 });
  fs.writeFileSync(path.join(bin, 'systemctl'), String.raw`#!/bin/sh
if [ "$3" = "--property=Version" ]; then printf 'Version=256\n'; exit 0; fi
printf 'LoadState=not-found\nActiveState=inactive\nSubState=dead\n'
`, { mode: 0o700 });
  const original = { PATH: process.env.PATH, TMPDIR: process.env.TMPDIR };
  const s = setup(t);
  try {
    process.env.PATH = bin; process.env.TMPDIR = root;
    await s.ready;
    const job = await s.start('echo must-not-run', { unsafe_to_terminate: true });
    assert.equal(job.status, 'unknown');
    assert.equal(job.submission_state, 'rejected');
    assert.match(job.error, /rejected before systemd-run started/);
    assert.equal(fs.existsSync(job.completion_path), false);
    assert.equal(fs.statSync(path.join(job.directory, 'environment')).mode & 0o777, 0o600);
    const observed = await s.call('background_status', { id: job.id });
    assert.equal(observed.submission_state, 'rejected');
    assert.equal(observed.status, 'unknown');
    assert.equal((await s.call('background_list')).total, 1);
    assert.equal(s.sent.length, 0);
    assert.equal(fs.existsSync(path.join(job.directory, 'environment')), true);
  } finally {
    try { await s.handlers.session_shutdown({ reason: 'test' }, s.ctx); }
    finally {
      for (const [key, value] of Object.entries(original)) {
        if (value === undefined) delete process.env[key]; else process.env[key] = value;
      }
      fs.rmSync(root, { recursive: true, force: true });
    }
  }
});

nodeTest('prepared submission after a manager-loss gap reattaches only a matching live unit', async t => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'pi-background-prepared-recovery-test-'));
  const bin = path.join(root, 'bin'); fs.mkdirSync(bin, { mode: 0o700 });
  const marker = path.join(root, 'attempts');
  fs.writeFileSync(path.join(bin, 'systemctl'), String.raw`#!/bin/sh
if [ "$3" = "--property=Version" ]; then printf 'Version=256\n'; exit 0; fi
if [ "$FAKE_UNIT_STATE" = "loaded" ]; then
  printf 'LoadState=loaded\nActiveState=activating\nSubState=start\nSlice=%s\nDescription=Pi job-1: Background job\n' "$FAKE_SLICE"
else
  printf 'LoadState=not-found\nActiveState=inactive\nSubState=dead\n'
fi
`, { mode: 0o700 });
  fs.writeFileSync(path.join(bin, 'systemd-run'), String.raw`#!/bin/sh
printf 'attempt\n' >> "$FAKE_SUBMISSION_MARKER"
exit 1
`, { mode: 0o700 });
  const original = { PATH: process.env.PATH, TMPDIR: process.env.TMPDIR,
    FAKE_SUBMISSION_MARKER: process.env.FAKE_SUBMISSION_MARKER,
    FAKE_UNIT_STATE: process.env.FAKE_UNIT_STATE, FAKE_SLICE: process.env.FAKE_SLICE };
  const s = setup(t);
  try {
    process.env.PATH = `${bin}:${original.PATH}`; process.env.TMPDIR = root;
    process.env.FAKE_SUBMISSION_MARKER = marker;
    await s.ready;
    const job = await s.start('echo must-not-run', { unsafe_to_terminate: true });
    await s.handlers.session_shutdown({ reason: 'simulate-reload' }, s.ctx);
    // Simulate a crash after request persistence but before systemd-run returned.
    fs.writeFileSync(job.metadata_path, fs.readFileSync(path.join(job.directory, 'request.json')), { mode: 0o600 });
    process.env.FAKE_UNIT_STATE = 'loaded'; process.env.FAKE_SLICE = job.slice;
    await s.handlers.session_start({}, s.ctx);
    const recovered = await s.call('background_status', { id: job.id });
    assert.equal(recovered.unit, job.unit);
    assert.equal(recovered.status, 'running');
    assert.equal(recovered.submission_state, 'confirmed');
    await s.handlers.session_shutdown({ reason: 'simulate-unconfirmed-stop' }, s.ctx);
    const unresolved = JSON.parse(fs.readFileSync(job.metadata_path, 'utf8'));
    Object.assign(unresolved, { status: 'unknown', error: 'Stop not confirmed: synthetic manager loss', submission_state: 'confirmed' });
    fs.writeFileSync(job.metadata_path, JSON.stringify(unresolved), { mode: 0o600 });
    await s.handlers.session_start({}, s.ctx);
    const stillUnknown = await s.call('background_status', { id: job.id });
    assert.equal(stillUnknown.status, 'unknown', 'a loaded unit does not resolve an unconfirmed stop');
    assert.match(stillUnknown.error, /Stop not confirmed/);
    assert.equal(fs.readFileSync(marker, 'utf8'), 'attempt\n', 'recovery never submits a second unit');
    assert.equal(fs.existsSync(path.join(job.directory, 'environment')), true);
    assert.equal(s.sent.length, 0);
  } finally {
    try { await s.handlers.session_shutdown({ reason: 'test' }, s.ctx); }
    finally {
      for (const [key, value] of Object.entries(original)) {
        if (value === undefined) delete process.env[key]; else process.env[key] = value;
      }
      fs.rmSync(root, { recursive: true, force: true });
    }
  }
});

nodeTest('cancel refuses a loaded same-slice unit with the wrong description', async t => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'pi-background-cancel-identity-test-'));
  const bin = path.join(root, 'bin'); fs.mkdirSync(bin, { mode: 0o700 });
  const stopMarker = path.join(root, 'stop-attempted');
  fs.writeFileSync(path.join(bin, 'systemctl'), String.raw`#!/bin/sh
if [ "$3" = "--property=Version" ]; then printf 'Version=256\n'; exit 0; fi
if [ "$2" = "stop" ]; then printf 'stop\n' >> "$FAKE_STOP_MARKER"; exit 0; fi
if [ "$4" = "--property=ActiveState" ] && [ "$5" = "--value" ]; then printf 'inactive\n'; exit 0; fi
printf 'LoadState=loaded\nActiveState=activating\nSubState=start\nSlice=%s\nDescription=Other job\n' "$FAKE_SLICE"
`, { mode: 0o700 });
  fs.writeFileSync(path.join(bin, 'systemd-run'), '#!/bin/sh\nexit 1\n', { mode: 0o700 });
  const original = { PATH: process.env.PATH, TMPDIR: process.env.TMPDIR,
    FAKE_SLICE: process.env.FAKE_SLICE, FAKE_STOP_MARKER: process.env.FAKE_STOP_MARKER };
  const s = setup(t);
  try {
    process.env.PATH = `${bin}:${original.PATH}`; process.env.TMPDIR = root;
    process.env.FAKE_STOP_MARKER = stopMarker;
    await s.ready;
    const job = await s.start('echo must-not-run');
    process.env.FAKE_SLICE = job.slice;
    await s.call('background_cancel', { id: job.id });
    const refused = await until(async () => {
      const metadata = JSON.parse(fs.readFileSync(job.metadata_path, 'utf8'));
      return /Stop request failed/.test(metadata.error ?? '') && metadata;
    });
    assert.equal(refused.status, 'unknown');
    assert.match(refused.error, /Cannot confirm exact unit identity/);
    assert.equal(fs.existsSync(stopMarker), false, 'no systemctl stop was sent');
    // Retire only this fake test job so the shared test harness can shut down cleanly.
    fs.writeFileSync(job.completion_path, JSON.stringify({ status: 'failed', finished_at: new Date().toISOString(),
      exit_code: null, signal: null, error: 'synthetic test result', systemd_result: 'failure', deadline_exceeded: false }), { mode: 0o600 });
    await s.call('background_status', { id: job.id });
    assert.equal(fs.existsSync(stopMarker), false);
  } finally {
    try { await s.handlers.session_shutdown({ reason: 'test' }, s.ctx); }
    finally {
      for (const [key, value] of Object.entries(original)) {
        if (value === undefined) delete process.env[key]; else process.env[key] = value;
      }
      fs.rmSync(root, { recursive: true, force: true });
    }
  }
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
  // A stale observer can retain an uncertain submission until the hook record is read.
  const metadata = JSON.parse(fs.readFileSync(job.metadata_path, 'utf8'));
  Object.assign(metadata, { submission_state: 'uncertain', status: 'detached', finished_at: null });
  fs.writeFileSync(job.metadata_path, JSON.stringify(metadata), { mode: 0o600 });
  await until(() => fs.existsSync(job.completion_path));
  const completion = JSON.parse(fs.readFileSync(job.completion_path, 'utf8'));
  assert.equal(completion.status, 'failed');
  assert.equal(completion.exit_code, 7);
  assert.equal(completion.submission_state, undefined, 'the standalone hook records outcome, not Pi classification');
  assert.equal(fs.existsSync(path.join(job.directory, 'environment')), false);
  assert.equal(fs.statSync(job.completion_path).mode & 0o777, 0o600);
  assert.equal(s.sent.length, 0);
  s.handlers.session_start({}, s.ctx);
  const restored = await ended(s, job.id);
  assert.equal(restored.exit_code, 7); assert.match(restored.output, /independent-hook/);
  assert.equal(restored.submission_state, 'confirmed', 'the observer confirms creation from the hook record');
  assert.equal(JSON.parse(fs.readFileSync(job.metadata_path, 'utf8')).submission_state, 'confirmed');
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

nodeTest('retention configuration validates integers and supplies bounded defaults', () => {
  assert.deepEqual(retentionConfig({}), { maxActive: 5, historyLimit: 100, maxAgeSeconds: 86400 });
  assert.deepEqual(retentionConfig({ PI_BACKGROUND_MAX_ACTIVE: '2', PI_BACKGROUND_HISTORY_LIMIT: '0',
    PI_BACKGROUND_HISTORY_TTL_SECONDS: '0' }), { maxActive: 2, historyLimit: 0, maxAgeSeconds: 0 });
  for (const key of ['PI_BACKGROUND_MAX_ACTIVE', 'PI_BACKGROUND_HISTORY_LIMIT', 'PI_BACKGROUND_HISTORY_TTL_SECONDS']) {
    for (const value of ['', ' ', '-1', '1.5', 'NaN', 'Infinity', '9007199254740992']) {
      assert.throws(() => retentionConfig({ [key]: value }), new RegExp(key));
    }
  }
  assert.throws(() => retentionConfig({ PI_BACKGROUND_MAX_ACTIVE: '0' }), /MAX_ACTIVE/);
});

nodeTest('retention uses completion time, newest-first count and TTL; unresolved work is never eligible', () => {
  const now = Date.now();
  const job = (id, age, status = 'succeeded') => ({ id: `job-${id}`, status,
    finished_at: new Date(now - age * 1000).toISOString() });
  const jobs = [job(1, 60), job(2, 20), job(3, 10), job(4, 10),
    ...['running', 'deadline_exceeded', 'stopping', 'unknown', 'detached'].map((s, i) => job(5 + i, 1000, s)),
    { ...job(10, 1000), finished_at: null }, { ...job(11, 1000), finished_at: 'invalid' }];
  const ids = config => expiredJobs(jobs, config, now).map(j => j.id);
  assert.deepEqual(ids({ historyLimit: 2, maxAgeSeconds: 0 }), ['job-2', 'job-1']);
  assert.deepEqual(ids({ historyLimit: 100, maxAgeSeconds: 60 }), ['job-1']);
  assert.deepEqual(ids({ historyLimit: 3, maxAgeSeconds: 20 }), ['job-2', 'job-1']);
  assert.deepEqual(ids({ historyLimit: 0, maxAgeSeconds: 0 }), ['job-4', 'job-3', 'job-2', 'job-1']);
});

test('active limit rejects without artifacts, counts unsafe jobs, and releases capacity after completion', async t => {
  const s = setup(t, 'rpc', { PI_BACKGROUND_MAX_ACTIVE: '1' });
  await s.ready;
  const first = await s.start('sleep 0.4', { unsafe_to_terminate: true });
  const before = fs.readdirSync(os.tmpdir()).filter(n => n.startsWith('pi-background-systemd-')).length;
  await assert.rejects(s.start('echo rejected'), /Active background job limit reached \(1\)/);
  assert.equal(fs.readdirSync(os.tmpdir()).filter(n => n.startsWith('pi-background-systemd-')).length, before);
  await ended(s, first.id);
  const next = await s.start('echo accepted');
  await ended(s, next.id);
  assert.equal((await s.call('background_list')).total, 2);
});

test('concurrent launch attempts reserve active capacity before contacting systemd', async t => {
  const s = setup(t, 'rpc', { PI_BACKGROUND_MAX_ACTIVE: '1' });
  await s.ready;
  const results = await Promise.allSettled([s.start('sleep 2'), s.start('sleep 2')]);
  assert.equal(results.filter(r => r.status === 'fulfilled').length, 1);
  assert.match(results.find(r => r.status === 'rejected').reason.message, /Active background job limit/);
});

test('history pruning persists across resume, preserves logs, and never reuses IDs or removes active work', async t => {
  const s = setup(t, 'rpc', { PI_BACKGROUND_MAX_ACTIVE: '2', PI_BACKGROUND_HISTORY_LIMIT: '1' });
  await s.ready;
  const live = await s.start('sleep 20');
  const done = [];
  for (let i = 0; i < 4; i++) {
    const launched = await s.start('echo retained');
    done.push(await ended(s, launched.id));
    await s.call('background_list');
  }
  const list = await s.call('background_list');
  assert.deepEqual(list.jobs.map(j => j.id), [live.id, done.at(-1).id]);
  assert.equal(list.active, 1);
  for (const job of done.slice(0, -1)) {
    await assert.rejects(s.call('background_status', { id: job.id }), /Unknown background job/);
    assert.ok(s.branch.some(entry => entry.customType === 'background-systemd-job' && entry.data.id === job.id));
    assert.ok(s.branch.some(entry => entry.customType === 'background-systemd-jobs-pruned' && entry.data.ids.includes(job.id)));
    assert.equal(fs.readFileSync(job.log_path, 'utf8'), 'retained\n');
    assert.equal(JSON.parse(fs.readFileSync(path.join(job.directory, 'request.json'), 'utf8')).unit, job.unit);
    assert.equal(JSON.parse(fs.readFileSync(job.metadata_path, 'utf8')).id, job.id);
    const completion = JSON.parse(fs.readFileSync(job.completion_path, 'utf8'));
    assert.equal(completion.status, 'succeeded');
    assert.equal(completion.exit_code, 0);
    assert.equal(fs.existsSync(path.join(job.directory, 'environment')), false, 'only the normal hook removed its temporary snapshot');
  }
  await s.handlers.session_shutdown({}, s.ctx);
  s.branch.push({ type: 'custom', customType: 'background-systemd-jobs-pruned', data: { ids: ['job-NaN'] } });
  await s.handlers.session_start({}, s.ctx);
  const restored = await s.call('background_list');
  assert.equal(restored.completed, 1);
  assert.ok(!restored.jobs.some(j => done.slice(0, -1).some(old => old.id === j.id)));
  const next = await s.start('echo unique');
  assert.equal(next.id, 'job-6');
  await ended(s, next.id);
});

test('TTL prunes restored completed jobs while preserving logs and ID high-water mark', async t => {
  const s = setup(t, 'rpc', { PI_BACKGROUND_HISTORY_TTL_SECONDS: '1' });
  await s.ready;
  const job = await s.start('echo old');
  await ended(s, job.id);
  await s.handlers.session_shutdown({}, s.ctx);
  const metadata = JSON.parse(fs.readFileSync(job.metadata_path, 'utf8'));
  metadata.finished_at = new Date(Date.now() - 2000).toISOString();
  fs.writeFileSync(job.metadata_path, JSON.stringify(metadata));
  await s.handlers.session_start({}, s.ctx);
  assert.equal((await s.call('background_list')).total, 0);
  assert.equal(fs.readFileSync(job.log_path, 'utf8'), 'old\n');
  await s.handlers.session_shutdown({}, s.ctx);
  await s.handlers.session_start({}, s.ctx);
  const next = await s.start('echo new');
  assert.equal(next.id, 'job-2');
  await ended(s, next.id);
});

nodeTest('malformed prune markers cannot abort session restore', async t => {
  const s = setup(t, 'rpc');
  await s.ready;
  await s.handlers.session_shutdown({}, s.ctx);
  for (const data of [null, {}, { ids: null }, { ids: 'job-1' }, { ids: [null] },
    { ids: ['job-NaN'] }, { ids: ['job-9007199254740992'] }]) {
    s.branch.push({ type: 'custom', customType: 'background-systemd-jobs-pruned', data });
  }
  await s.handlers.session_start({}, s.ctx);
  assert.equal((await s.call('background_list')).total, 0);
});


const { default: tasksExtension } = createJiti(__filename)(path.resolve(__dirname, '../extensions/tasks.ts'));
function attachTasks(s) {
  const tools = {}, handlers = {}, messages = [];
  tasksExtension({ events: s.events, registerFlag() {}, getFlag() {},
    registerTool: tool => { tools[tool.name] = tool; }, registerCommand() {},
    on: (name, fn) => { handlers[name] = fn; },
    appendEntry: (customType, data) => s.branch.push({ type: 'custom', customType, data: structuredClone(data) }),
    sendMessage: (...args) => messages.push(args),
  });
  handlers.session_start({}, s.ctx);
  return { handlers, messages,
    call: async (name, args = {}) => (await tools[name].execute('task-call', args, undefined, undefined, s.ctx)).details,
  };
}
const lookupJob = (s, job_id, session_id = 'test-session') => {
  const query = { version: 1, session_id, job_id }; s.events.emit('background:lookup-v1', query); return query;
};

test('optional peer lookup exposes only session-scoped metadata and works without tasks', async t => {
  const s = setup(t, 'rpc'); await s.ready;
  const changes = []; s.events.on('background:changed-v1', event => changes.push(event));
  const job = await s.start('echo private-job-output; sleep 0.2');
  const query = lookupJob(s, job.id);
  assert.equal(query.available, true);
  assert.deepEqual(Object.keys(query.job).sort(), ['finished_at', 'id', 'session_id', 'status', 'unit']);
  assert.equal(query.job.unit, job.unit); assert.equal(query.job.finished_at, null);
  assert.equal(lookupJob(s, job.id, 'another-session').available, undefined);
  assert.equal(lookupJob(s, 'job-9999').available, true); assert.equal(lookupJob(s, 'job-9999').job, undefined);
  await until(() => s.sent.length === 1);
  assert.equal(lookupJob(s, job.id).job.status, 'succeeded');
  assert.ok(changes.length >= 1);
  assert.ok(changes.every(event => JSON.stringify(event) === JSON.stringify({ version: 1, session_id: 'test-session' })));
  await s.handlers.session_shutdown({}, s.ctx);
  assert.equal(lookupJob(s, job.id).available, undefined);
});

test('real background completion unblocks tasks before its sole completion wake, including failed jobs', async t => {
  const s = setup(t, 'rpc'); await s.ready; const tasks = attachTasks(s);
  for (const exitCode of [0, 7]) {
    const task = (await tasks.call('tasks_add', { summary: `Inspect job exit ${exitCode}` })).task;
    await tasks.call('tasks_progress', { task_id: task.id });
    const job = await s.start(`sleep 0.25; echo finished; exit ${exitCode}`);
    const blocked = await tasks.call('tasks_block', { task_id: task.id, reason: 'Waiting for result', background_job_id: job.id });
    assert.equal(blocked.task.status, 'blocked');
    const before = s.sent.length;
    await until(() => s.sent.length > before);
    const result = (await tasks.call('tasks_list')).tasks.find(item => item.id === task.id);
    assert.equal(result.status, 'progress'); assert.equal(result.background_job, undefined);
    assert.equal(result.completedBy, undefined); assert.equal(tasks.messages.length, 0);
    assert.equal(s.sent.at(-1)[0].details.exit_code, exitCode);
    assert.equal(s.sent.length, before + 1);
    await tasks.call('tasks_progress', { task_id: task.id, active: false });
  }
});

test('reconnecting background jobs reconciles dependencies in either extension startup order', async t => {
  const s = setup(t, 'rpc'); await s.ready; const tasks = attachTasks(s);
  const task = (await tasks.call('tasks_add', { summary: 'Await detached job' })).task;
  const job = await s.start('sleep 0.4; echo restored', { unsafe_to_terminate: true });
  await tasks.call('tasks_block', { task_id: task.id, reason: 'Waiting for result', background_job_id: job.id });
  await s.handlers.session_shutdown({}, s.ctx);
  assert.equal((await tasks.call('tasks_list')).tasks[0].background_job_availability, 'unavailable');
  await until(() => fs.existsSync(job.completion_path));
  // Tasks starts first, while background provider is unavailable.
  tasks.handlers.session_start({}, s.ctx);
  assert.equal((await tasks.call('tasks_list')).tasks[0].status, 'blocked');
  await s.handlers.session_start({}, s.ctx);
  assert.equal((await tasks.call('tasks_list')).tasks[0].status, 'pending');
  assert.equal(s.sent.length, 1); assert.equal(tasks.messages.length, 0);
  // Replaying the pre-unblock branch with background already ready exercises reverse order.
  const resumeIndex = s.branch.findLastIndex(entry => entry.customType === 'tasks-delta-v2' && entry.data.upsert.some(task => task.status === 'pending' && task.id === 1));
  assert.ok(resumeIndex >= 0);
  s.branch.splice(resumeIndex);
  tasks.handlers.session_start({}, s.ctx);
  assert.equal((await tasks.call('tasks_list')).tasks[0].status, 'pending');
  assert.equal(s.sent.length, 1);
});

test('completion rings once, while progress, queries, restored notified jobs and headless jobs stay silent', async t => {
  await require('./bell-capture.cjs')(async count => {
    const s = setup(t); await s.ready;
    const job = await s.start('sleep 0.15; printf done');
    assert.equal(count(), 0);
    await ended(s, job.id);
    await until(() => s.sent.length === 1);
    assert.equal(count(), 1);
    await s.call('background_status', { id: job.id });
    await s.call('background_list');
    assert.equal(count(), 1);
    await s.handlers.session_shutdown({ reason: 'reload' }, s.ctx);
    await s.handlers.session_start({}, s.ctx);
    assert.equal(count(), 1);
    s.ctx.mode = 'rpc';
    const other = await s.start('printf headless');
    await ended(s, other.id);
    await until(() => s.sent.length === 2);
    assert.equal(count(), 1);
  });
});

test('background footer stays hidden when idle and clears after completion', async t => {
  const s = setup(t); await s.ready;
  assert.ok(s.statuses.every(([key, value]) => key !== 'background-jobs' || value === undefined));
  const job = await s.start('sleep 0.1');
  assert.ok(s.statuses.some(([key, value]) => key === 'background-jobs' && /1 background/.test(value ?? '')));
  await ended(s, job.id);
  assert.deepEqual(s.statuses.at(-1), ['background-jobs', undefined]);
});


nodeTest('RPC job menu emits a protocol-visible notice without requiring systemd', async () => {
  const commands = {}; const notices = [];
  extension({ events: { on() {}, emit() {} }, on() {}, registerTool() {}, registerCommand: (name, command) => commands[name] = command });
  await commands.jobs.handler('', { mode: 'rpc', ui: { notify: (...args) => notices.push(args) } });
  assert.deepEqual(notices, [['/jobs requires the interactive terminal UI.', 'warning']]);
});

test('job list and log-detail actions cannot bypass the shared cap', async t => {
  const s = setup(t); await s.ready;
  const job = await s.start('printf "%s" "a long log line"', { label: 'Long job label '.repeat(7) }); await ended(s, job.id);
  s.ctx.ui.select = async () => { throw Error('Unbounded menu bypass'); };
  let frame = 0;
  s.ctx.ui.custom = async factory => {
    const view = factory({ terminal: { rows: 8 }, requestRender() {} }, {}, {}, () => {});
    for (const width of [20, 40, 80]) assert.ok(view.render(width).length <= 4);
    frame++; return frame === 1 ? view.choices[0] : frame === 2 ? 'Back' : undefined;
  };
  await s.commands.jobs.handler('', s.ctx); assert.equal(frame, 3);
});

test('actual jobs menu retains status colors and shows human CPU/memory labels without coloring tool data', async t => {
  const { stripVTControlCharacters } = require('node:util');
  const s = setup(t); await s.ready;
  s.ctx.ui.theme = { fg: (role, text) => `\x1b[${role === 'error' ? 31 : 36}m${text}\x1b[0m` };
  const job = await s.start('exit 7'); const observed = await ended(s, job.id);
  assert.equal(observed.status, 'failed');
  const render = [], custom = s.ctx.ui.custom;
  s.ctx.ui.custom = factory => custom((...args) => {
    const view = factory(...args); render.push(...view.render(160)); return view;
  });
  s.selections.push(0, 'Back', undefined); await s.commands.jobs.handler('', s.ctx);
  assert.ok(render.join('\n').includes('\x1b[31mfailed\x1b[0m'));
  const text = stripVTControlCharacters(render.join('\n'));
  assert.match(text, /CPU time:/); assert.match(text, /memory:/);
  assert.doesNotMatch(text, /CPU ns:|memory bytes:|\[31m/);
});
