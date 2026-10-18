const { test } = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const { createJiti } = require(require.resolve('jiti', { paths: [path.resolve(__dirname, '../extensions')] }));
const { default: extension, parseVerdict, taskListMarkdown, recentTaskMessages } = createJiti(__filename)(path.resolve(__dirname, '../extensions/tasks.ts'));

function setup(branch = []) {
  const handlers = {}, tools = {}, commands = {}, sent = [], notices = [], widgets = [], requests = [], titles = [];
  let selections = [], inputs = [], confirms = [], idle = true, queued = false;
  let judge = async () => ({ verdict: 'accepted', reason: 'The supplied test output satisfies the criterion.', missing: [] });
  const ctx = {
    mode: 'tui', model: { provider: 'test', id: 'offline-judge' },
    sessionManager: { getBranch: () => branch }, isIdle: () => idle, hasPendingMessages: () => queued,
    modelRegistry: { complete: async (...args) => {
      requests.push(args);
      const value = await judge(...args);
      return value?.content ? value : { content: [{ type: 'text', text: JSON.stringify(value) }], stopReason: 'stop', usage: { totalTokens: 42 } };
    } },
    ui: {
      theme: { fg: (_color, value) => value },
      setWidget: (key, value) => widgets.push(value), notify: (...args) => notices.push(args),
      select: async (title, choices) => {
        titles.push(title);
        const value = selections.shift();
        return typeof value === 'function' ? value(choices) : value;
      },
      input: async () => inputs.shift(), confirm: async () => confirms.shift() ?? true,
    },
  };
  extension({
    on: (name, handler) => { handlers[name] = handler; }, registerTool: tool => { tools[tool.name] = tool; },
    registerCommand: (name, command) => { commands[name] = command; },
    appendEntry: (customType, data) => branch.push({ type: 'custom', customType, data: structuredClone(data) }),
    sendMessage: (...args) => sent.push(args),
  });
  handlers.session_start({}, ctx);
  return { handlers, tools, commands, ctx, branch, sent, notices, widgets, requests, titles,
    select: (...values) => { selections = values; }, inputs: (...values) => { inputs = values; },
    confirms: (...values) => { confirms = values; }, judge: fn => { judge = fn; },
    idle: value => { idle = value; }, queued: value => { queued = value; },
  };
}
const call = (s, name, args = {}, signal) => s.tools[name].execute('tool-id', args, signal, undefined, s.ctx);
const list = async s => (await call(s, 'tasks_list')).details;
const add = (s, summary = 'Pass the tests', extra = {}) => call(s, 'tasks_add', { summary, ...extra });
const complete = (s, id = 1, evidence = 'node --test tests/example.test.cjs: 8 passed, 0 failed', signal) => call(s, 'tasks_complete', { task_id: id, evidence }, signal);
const input = s => s.handlers.input({ source: 'interactive', text: 'Continue' }, s.ctx);
const settle = (s, outcome = 'completed', canContinue = true) => s.handlers.agent_before_settle({ outcome, context: { canContinue } }, s.ctx);
const pickTask = id => choices => choices.find(choice => choice.startsWith(`#${id} [`));

test('ordered, stable IDs; limits/invalid input; no agent removal or override', async () => {
  const s = setup(); await add(s, 'A'); await add(s, 'B', { order: 1, acceptance: 'Observed B' });
  assert.deepEqual((await list(s)).tasks.map(task => task.id), [2, 1]);
  assert.equal((await list(s)).tasks[0].acceptance, 'Observed B');
  for (const args of [{ summary: ' ' }, { summary: 'X', order: 0 }, { summary: 'X', order: 4 }, { summary: 'X', order: 1.2 }, { summary: 'X', acceptance: ' ' }]) {
    await assert.rejects(call(s, 'tasks_add', args));
  }
  assert.equal(s.tools.tasks_remove, undefined); assert.equal(s.tools.tasks_override, undefined);
  for (let i = 2; i < 20; i++) await add(s, `Task ${i}`);
  await assert.rejects(add(s), /limit/);
});

test('agent unblocks only blocked tasks, preserves assessment, and restores pending state', async () => {
  const s = setup(); await add(s, 'Resolved prerequisite');
  s.judge(async () => ({ verdict: 'needs_work', reason: 'Missing result.', missing: ['Provide result.'] }));
  await complete(s);
  await call(s, 'tasks_block', { task_id: 1, reason: 'Waiting for result' });
  const before = (await list(s)).tasks[0];
  const requestsBefore = s.requests.length;
  const response = await call(s, 'tasks_unblock', { task_id: 1 });
  assert.equal(s.requests.length, requestsBefore, 'unblock does not call a model');
  assert.equal(response.details.task.status, 'pending');
  assert.equal(response.details.task.blocker, undefined);
  assert.equal(response.details.task.evidence, before.evidence);
  assert.deepEqual(response.details.task.assessment, before.assessment);
  assert.equal(s.sent.length, 0, 'unblock does not start inference');
  const restored = setup(s.branch);
  assert.equal((await list(restored)).tasks[0].status, 'pending');
  assert.equal((await list(restored)).tasks[0].blocker, undefined);
  for (const id of [0, 99, 1]) await assert.rejects(call(s, 'tasks_unblock', { task_id: id }), /not blocked/);
  s.judge(async () => ({ verdict: 'accepted', reason: 'Result supplied.', missing: [] }));
  await complete(s);
  await assert.rejects(call(s, 'tasks_unblock', { task_id: 1 }), /not blocked/);
});

test('unblock respects stops, paused reminders and the no-progress budget', async () => {
  const s = setup(); await add(s); input(s);
  settle(s); settle(s);
  await call(s, 'tasks_block', { task_id: 1, reason: 'Waiting' });
  await call(s, 'tasks_unblock', { task_id: 1 });
  assert.equal(settle(s), undefined, 'unblock must not replenish the reminder budget');
  s.handlers.input({ source: 'interactive', text: 'Stop' }, s.ctx);
  await call(s, 'tasks_block', { task_id: 1, reason: 'Waiting again' });
  await call(s, 'tasks_unblock', { task_id: 1 });
  assert.equal(settle(s), undefined, 'unblock must not override a stop');
  s.select('Pause reminders', 'Close'); await s.commands.tasks.handler('', s.ctx);
  input(s);
  await call(s, 'tasks_block', { task_id: 1, reason: 'Paused' });
  await call(s, 'tasks_unblock', { task_id: 1 });
  assert.equal((await list(s)).paused, true); assert.equal(settle(s), undefined);
});

test('unblocking aborts an assessment and prevents a stale verdict applying', async () => {
  const s = setup(); await add(s);
  await call(s, 'tasks_block', { task_id: 1, reason: 'Wait' });
  let resolve;
  s.judge(() => new Promise(r => { resolve = r; }));
  const assessment = complete(s);
  const signal = s.requests[0][2].signal;
  await call(s, 'tasks_unblock', { task_id: 1 });
  assert.equal(signal.aborted, true);
  resolve({ verdict: 'accepted', reason: 'Late result.', missing: [] });
  assert.match((await assessment).details.reason, /changed|cancelled/);
  assert.equal((await list(s)).tasks[0].status, 'pending');
});

test('progress is explicit, persisted, reversible, and never completion or inference', async () => {
  const s = setup(); await add(s); input(s);
  const response = await call(s, 'tasks_progress', { task_id: 1 });
  assert.equal(response.details.task.status, 'progress'); assert.equal(s.requests.length, 0); assert.equal(s.sent.length, 0);
  const count = s.branch.length;
  await call(s, 'tasks_progress', { task_id: 1 }); assert.equal(s.branch.length, count, 'same state is a no-op');
  assert.equal((await list(setup(s.branch))).tasks[0].status, 'progress');
  const reminder = settle(s); assert.match(reminder.entries[0].content, /"status":"progress"/);
  await s.commands['tasks-send'].handler('', s.ctx); assert.match(s.sent.at(-1)[0].content, /in progress/);
  s.judge(async () => ({ verdict: 'needs_work', reason: 'Missing result.', missing: ['Provide result.'] }));
  await complete(s); const before = (await list(s)).tasks[0]; assert.equal(before.status, 'progress');
  await call(s, 'tasks_progress', { task_id: 1, active: false });
  const pending = (await list(s)).tasks[0]; assert.equal(pending.status, 'pending'); assert.equal(pending.evidence, before.evidence);
  assert.deepEqual(pending.assessment, before.assessment);
  await call(s, 'tasks_block', { task_id: 1, reason: 'Waiting' });
  await assert.rejects(call(s, 'tasks_progress', { task_id: 1 }), /blocked/);
  await call(s, 'tasks_unblock', { task_id: 1 }); await call(s, 'tasks_progress', { task_id: 1 });
  s.judge(async () => ({ verdict: 'accepted', reason: 'Result provided.', missing: [] })); await complete(s);
  for (const id of [0, 1, 99]) await assert.rejects(call(s, 'tasks_progress', { task_id: id }), /missing|complete/);
});

test('progress cannot replenish reminders or override stop/pause and cancels stale assessments', async () => {
  const s = setup(); await add(s); input(s); settle(s); settle(s);
  await call(s, 'tasks_progress', { task_id: 1 }); assert.equal(settle(s), undefined);
  s.handlers.input({ source: 'interactive', text: 'Stop' }, s.ctx);
  await call(s, 'tasks_progress', { task_id: 1, active: false }); assert.equal(settle(s), undefined);
  s.select('Pause reminders', 'Close'); await s.commands.tasks.handler('', s.ctx); input(s);
  await call(s, 'tasks_progress', { task_id: 1 }); assert.equal((await list(s)).paused, true); assert.equal(settle(s), undefined);
  let resolve; s.judge(() => new Promise(done => { resolve = done; })); const assessment = complete(s);
  await call(s, 'tasks_progress', { task_id: 1, active: false }); assert.equal(s.requests[0][2].signal.aborted, true);
  resolve({ verdict: 'accepted', reason: 'Late result.', missing: [] }); await assessment;
  assert.equal((await list(s)).tasks[0].status, 'pending');
});

test('progress menu actions preserve selection, stop state and assessment-only completion', async () => {
  const s = setup(); await add(s); input(s);
  s.select(pickTask(1), 'Mark in progress', 'Close'); await s.commands.tasks.handler('', s.ctx);
  assert.equal((await list(s)).tasks[0].status, 'progress'); assert.equal(s.sent.length, 0);
  s.select(pickTask(1), 'Return to pending', 'Close'); await s.commands.tasks.handler('', s.ctx);
  assert.equal((await list(s)).tasks[0].status, 'pending'); assert.equal(s.sent.length, 0);
  s.handlers.session_start({}, s.ctx); await call(s, 'tasks_progress', { task_id: 1 });
  assert.equal(settle(s), undefined, 'loading a session stays suspended');
});

test('completion returns remaining progress IDs and menu changes cancel assessments without resetting budget', async () => {
  const s = setup(); await add(s, 'First'); await add(s, 'Second'); input(s);
  await call(s, 'tasks_progress', { task_id: 2 });
  assert.deepEqual((await complete(s, 1)).details.remaining, [2]);
  settle(s); settle(s);
  for (const [action, status] of [['Return to pending', 'pending'], ['Mark in progress', 'progress']]) {
    let resolve; s.judge(() => new Promise(done => { resolve = done; })); const assessment = complete(s, 2);
    const signal = s.requests.at(-1)[2].signal;
    s.select(pickTask(2), action, 'Close'); await s.commands.tasks.handler('', s.ctx);
    assert.equal(signal.aborted, true);
    resolve({ verdict: 'accepted', reason: 'Late result.', missing: [] }); await assessment;
    assert.equal((await list(s)).tasks[1].status, status);
    assert.equal(settle(s), undefined, 'menu status changes do not replenish reminders');
  }
});

test('one progress slot is enforced by tools and menu without mutation on refusal', async () => {
  const s = setup(); await add(s, 'First'); await add(s, 'Second'); input(s);
  assert.equal((await list(s)).progress_limit, 1);
  await call(s, 'tasks_progress', { task_id: 1 });
  const before = await list(s); const entries = s.branch.length;
  await assert.rejects(call(s, 'tasks_progress', { task_id: 2 }), /At most 1 task.*#1/);
  assert.deepEqual(await list(s), before); assert.equal(s.branch.length, entries);
  await call(s, 'tasks_progress', { task_id: 1 }); assert.equal(s.branch.length, entries);
  s.select(pickTask(2), 'Mark in progress', 'Close'); await s.commands.tasks.handler('', s.ctx);
  assert.deepEqual(await list(s), before); assert.equal(s.branch.length, entries);
  assert.ok(s.notices.some(([message, kind]) => kind === 'warning' && /At most 1 task/.test(message)));
  assert.match(s.widgets.at(-1).join('\n'), /1\/1 in progress/);
  assert.match(JSON.stringify(s.handlers.context({ messages: [] }, s.ctx)), /progress_limit/);
  await call(s, 'tasks_progress', { task_id: 1, active: false });
  s.select(pickTask(2), 'Mark in progress', 'Close'); await s.commands.tasks.handler('', s.ctx);
  assert.equal((await list(s)).tasks[1].status, 'progress');
  await call(s, 'tasks_block', { task_id: 2, reason: 'Wait' }); await call(s, 'tasks_progress', { task_id: 1 });
  await complete(s, 1); await call(s, 'tasks_unblock', { task_id: 2 }); await call(s, 'tasks_progress', { task_id: 2 });
  assert.equal((await list(s)).tasks[1].status, 'progress');
});

test('restored excess progress is preserved and can drain without permitting new starts', async () => {
  const s = setup([{ type: 'custom', customType: 'tasks-state-v1', data: { version: 1, nextId: 4, paused: false,
    tasks: [1, 2, 3].map(id => ({ id, summary: `Task ${id}`, acceptance: 'Result', status: id < 3 ? 'progress' : 'pending' })) } }]);
  assert.deepEqual((await list(s)).tasks.map(task => task.status), ['progress', 'progress', 'pending']);
  await assert.rejects(call(s, 'tasks_progress', { task_id: 3 }), /#1, #2/);
  await call(s, 'tasks_progress', { task_id: 1 });
  await call(s, 'tasks_progress', { task_id: 1, active: false });
  await assert.rejects(call(s, 'tasks_progress', { task_id: 3 }), /#2/);
  await call(s, 'tasks_progress', { task_id: 2, active: false });
  await call(s, 'tasks_progress', { task_id: 3 });
  assert.deepEqual((await list(s)).tasks.map(task => task.status), ['pending', 'pending', 'progress']);
  assert.equal((await list(setup(s.branch))).tasks[2].status, 'progress');
});

test('judge isolation, exact criteria, evidence, usage, and accepted persistence', async () => {
  const s = setup(); await add(s, 'Run tests', { acceptance: 'Eight tests pass' });
  const result = await complete(s);
  assert.equal(result.details.task.status, 'complete'); assert.equal(result.details.task.completedBy, 'judge');
  assert.equal(result.usage.totalTokens, 42);
  const [model, context, options] = s.requests[0];
  assert.equal(model.id, 'offline-judge'); assert.equal(context.tools, undefined);
  assert.equal(context.messages.length, 1); assert.match(context.systemPrompt, /Do not invent requirements/);
  assert.match(context.messages[0].content, /Eight tests pass/); assert.match(context.messages[0].content, /8 passed/);
  assert.ok(options.sessionId); assert.equal(options.maxTokens, 2048); assert.ok(options.signal);
  const restored = setup(s.branch); assert.equal((await list(restored)).tasks[0].status, 'complete');
  await assert.rejects(complete(s), /already complete/);
});

test('rejection names missing evidence, remains pending, and is visible in subsequent context', async () => {
  const s = setup(); await add(s);
  s.judge(async () => ({ verdict: 'needs_work', reason: 'Failure-path tests are not shown.', missing: ['Provide output showing invalid inputs are rejected.'] }));
  const response = await complete(s);
  assert.equal(response.details.task.status, 'pending');
  assert.match(response.content[0].text, /invalid inputs/);
  const context = s.handlers.context({ messages: [{ role: 'user', content: 'Continue' }] }, s.ctx).messages;
  assert.match(context.at(-1).content, /invalid inputs/);
  assert.equal(context[0].content, 'Continue');
  s.judge(async () => ({ verdict: 'accepted', reason: 'Output now covers the missing failure path.', missing: [] }));
  await complete(s); assert.equal((await list(s)).tasks[0].status, 'complete');
});

test('malformed, vague, conflicting, truncated, unavailable judge responses fail open-work, never approve', async () => {
  const s = setup(); await add(s);
  const responses = [
    { verdict: 'needs_work', reason: 'No.', missing: [] },
    { verdict: 'accepted', reason: 'Yes.', missing: ['Actually missing tests'] },
    { verdict: 'accepted', reason: '', missing: [] },
    { content: [{ type: 'text', text: '<approved/>' }], stopReason: 'stop' },
    { content: [{ type: 'text', text: '{"verdict":"accepted"}' }], stopReason: 'length' },
  ];
  for (const response of responses) {
    s.judge(async () => response); const result = await complete(s);
    assert.equal(result.details.assessment.verdict, 'unable_to_assess'); assert.equal((await list(s)).tasks[0].status, 'pending');
    assert.ok(result.details.assessment.reason);
  }
  s.judge(async () => { throw new Error('SECRET_PROVIDER_TOKEN'); });
  assert.doesNotMatch((await complete(s)).content[0].text, /SECRET/);
  s.ctx.model = undefined; assert.equal((await complete(s)).details.assessment.verdict, 'unable_to_assess');
  await assert.rejects(complete(s, 1, ' '), /Evidence/);
  assert.throws(() => parseVerdict('{"verdict":"needs_work","reason":"no","missing":[]}'), /missing evidence/);
});

test('context survives compaction and reload without duplicate snapshots, branch navigation restores precisely', async () => {
  const s = setup(); await add(s, 'Still pending');
  const once = s.handlers.context({ messages: [] }, s.ctx);
  const twice = s.handlers.context(once, s.ctx);
  assert.equal(twice.messages.length, 1);
  const restored = setup(s.branch); assert.match(restored.handlers.context({ messages: [] }, restored.ctx).messages[0].content, /Still pending/);
  s.handlers.session_tree({}, { ...s.ctx, sessionManager: { getBranch: () => [] } });
  assert.equal((await list(s)).tasks.length, 0);
  s.handlers.session_tree({}, s.ctx); assert.equal((await list(s)).tasks[0].summary, 'Still pending');
  assert.equal(settle(s), undefined, 'restoring alone does not start model inference');
});

test('early termination gets at most two reminders; abort, errors, queues, and blocked tasks never auto-continue', async () => {
  const s = setup(); await add(s); input(s);
  for (let i = 1; i <= 2; i++) {
    const boundary = settle(s); assert.equal(boundary.continue, true);
    assert.match(boundary.entries[0].content, new RegExp(`reminder ${i}/2`));
    assert.equal(boundary.entries[0].customType, 'tasks-reminder-v1');
  }
  assert.equal(settle(s), undefined);
  input(s); s.queued(true); assert.equal(settle(s), undefined); s.queued(false);
  assert.equal(settle(s, 'aborted'), undefined); assert.equal(settle(s), undefined);
  input(s); assert.equal(settle(s, 'error'), undefined); assert.equal(settle(s), undefined);
  input(s); await call(s, 'tasks_block', { task_id: 1, reason: 'Need the user to authorize spending.' });
  assert.equal(settle(s), undefined); assert.equal((await list(s)).tasks[0].status, 'blocked');
  assert.match(s.handlers.context({ messages: [] }, s.ctx).messages[0].content, /authorize spending/);
});

test('three-minute idle fallback is deduplicated, bounded, and cleaned up on transitions', async t => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const s = setup(); await add(s); input(s);
  s.handlers.agent_settled({}, s.ctx); s.handlers.agent_settled({}, s.ctx);
  t.mock.timers.tick(179999); assert.equal(s.sent.length, 0);
  t.mock.timers.tick(1); assert.equal(s.sent.length, 1);
  assert.deepEqual(s.sent[0][1], { deliverAs: 'followUp', triggerTurn: true });
  s.handlers.agent_settled({}, s.ctx); t.mock.timers.tick(180000); assert.equal(s.sent.length, 2);
  s.handlers.agent_settled({}, s.ctx); t.mock.timers.tick(180000); assert.equal(s.sent.length, 2);
  input(s); s.handlers.agent_settled({}, s.ctx); s.handlers.session_before_switch({}, s.ctx);
  t.mock.timers.tick(180000); assert.equal(s.sent.length, 2);
  input(s); s.handlers.agent_settled({}, s.ctx); s.handlers.session_shutdown({}, s.ctx);
  t.mock.timers.tick(180000); assert.equal(s.sent.length, 2);
});

test('idle fallback does not interrupt a busy agent, queued messages, or an open menu', async t => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const s = setup(); await add(s); input(s);
  s.handlers.agent_settled({}, s.ctx); s.idle(false); t.mock.timers.tick(180000); assert.equal(s.sent.length, 0);
  s.idle(true); s.handlers.agent_settled({}, s.ctx); s.queued(true); t.mock.timers.tick(180000); assert.equal(s.sent.length, 0);
  s.queued(false); s.handlers.agent_settled({}, s.ctx); s.select('Close'); await s.commands.tasks.handler('', s.ctx);
  t.mock.timers.tick(180000); assert.equal(s.sent.length, 0);
});

test('/tasks is a menu: add, inspect, cancel removal, confirmed removal, IDs never reused', async () => {
  const s = setup(); s.select('Add task', 'Close'); s.inputs('User task', 'Tests pass', '1');
  await s.commands.tasks.handler('', s.ctx);
  assert.equal((await list(s)).tasks[0].summary, 'User task');
  s.select(pickTask(1), 'Back', 'Close'); await s.commands.tasks.handler('', s.ctx);
  assert.match(s.titles.at(-2), /Acceptance: Tests pass/);
  s.select(pickTask(1), 'Remove task', 'Close'); s.confirms(false); await s.commands.tasks.handler('', s.ctx);
  assert.equal((await list(s)).tasks.length, 1);
  s.select(pickTask(1), 'Remove task', 'Close'); await s.commands.tasks.handler('', s.ctx);
  assert.equal((await list(s)).tasks.length, 0);
  await add(s, 'Next'); assert.equal((await list(s)).tasks[0].id, 2);
});

test('menu-only override records user authorship, paused state persists, blocked tasks resume', async () => {
  const s = setup(); await add(s); input(s);
  s.select('Pause reminders', 'Close'); await s.commands.tasks.handler('', s.ctx);
  assert.equal(settle(s), undefined); assert.equal((await list(setup(s.branch))).paused, true);
  s.select('Resume reminders', 'Close'); await s.commands.tasks.handler('', s.ctx);
  assert.equal((await list(s)).paused, false);
  await call(s, 'tasks_block', { task_id: 1, reason: 'Waiting for approval' });
  s.select(pickTask(1), 'Resume task', 'Close'); await s.commands.tasks.handler('', s.ctx);
  assert.equal((await list(s)).tasks[0].status, 'pending');
  s.select(pickTask(1), 'Mark complete (user override)', 'Close'); await s.commands.tasks.handler('', s.ctx);
  const task = (await list(s)).tasks[0]; assert.equal(task.status, 'complete'); assert.equal(task.completedBy, 'user');
  assert.equal(s.requests.length, 0); assert.equal((await list(setup(s.branch))).tasks[0].completedBy, 'user');
});

test('judge cancellation and late results cannot cross session/branch or user edits', async () => {
  for (const action of ['switch', 'remove', 'override', 'abort']) {
    const s = setup(); await add(s); let resolve;
    s.judge(() => new Promise(done => { resolve = done; }));
    const controller = new AbortController(); const active = complete(s, 1, '8 tests passed', controller.signal);
    await assert.rejects(complete(s), /in progress/);
    if (action === 'switch') s.handlers.session_start({}, { ...s.ctx, sessionManager: { getBranch: () => [] } });
    if (action === 'abort') controller.abort();
    if (action === 'remove' || action === 'override') {
      s.select(pickTask(1), action === 'remove' ? 'Remove task' : 'Mark complete (user override)', 'Close');
      await s.commands.tasks.handler('', s.ctx);
    }
    resolve({ verdict: 'accepted', reason: 'Tests passed.', missing: [] });
    assert.equal((await active).details.verdict, 'unable_to_assess');
    const tasks = (await list(s)).tasks;
    if (action === 'switch' || action === 'remove') assert.equal(tasks.length, 0);
    else assert.equal(tasks[0].completedBy, action === 'override' ? 'user' : undefined);
  }
});

test('corrupted saved state disables mutations and reminders instead of dropping tasks', async () => {
  const s = setup([{ type: 'custom', customType: 'tasks-state-v1', data: { version: 999, tasks: [] } }]);
  await assert.rejects(add(s), /unavailable/); await assert.rejects(list(s), /unavailable/);
  input(s); assert.equal(settle(s), undefined); assert.match(s.notices[0][0], /invalid saved state/);
});

test('final assistant boundary can be continued by appending a reminder even when initial canContinue is false', async () => {
  const s = setup(); await add(s); input(s);
  assert.equal(settle(s, 'completed', false).continue, true);
});

test('explicit stop text suppresses reminders until a new user request, not extension input', async () => {
  const s = setup(); await add(s); input(s);
  s.handlers.input({ source: 'interactive', text: 'Please stop working for now.' }, s.ctx);
  assert.equal(settle(s), undefined);
  s.handlers.input({ source: 'extension', text: 'Continue' }, s.ctx); assert.equal(settle(s), undefined);
  input(s); assert.equal(settle(s).continue, true);
});

test('judge deadline bounds adapters that never settle and records an assessment error', async t => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const s = setup(); await add(s); s.judge(async () => new Promise(() => {}));
  const running = complete(s); t.mock.timers.tick(60000);
  const response = await running;
  assert.equal(response.details.assessment.verdict, 'unable_to_assess');
  assert.match(response.details.assessment.reason, /60-second deadline/);
  assert.equal((await list(s)).tasks[0].status, 'pending');
});

test('unrelated task additions do not invalidate an in-flight task assessment', async () => {
  const s = setup(); await add(s); let resolve;
  s.judge(() => new Promise(done => { resolve = done; }));
  const running = complete(s); await add(s, 'Independent work');
  resolve({ verdict: 'accepted', reason: 'Relevant test output supplied.', missing: [] });
  assert.equal((await running).details.task.status, 'complete');
  assert.equal((await list(s)).tasks[1].status, 'pending');
});

test('user add/resume wakes via attributed follow-up; pausing and merely browsing do not', async () => {
  const s = setup();
  s.select('Add task', 'Close'); s.inputs('User requested work', '', '');
  await s.commands.tasks.handler('', s.ctx);
  assert.equal(s.sent.length, 1); assert.deepEqual(s.sent[0][1], { deliverAs: 'followUp', triggerTurn: true });
  s.select('Close'); await s.commands.tasks.handler('', s.ctx); assert.equal(s.sent.length, 1);
  s.select('Pause reminders', 'Close'); await s.commands.tasks.handler('', s.ctx); assert.equal(s.sent.length, 1);
  s.select('Resume reminders', 'Close'); await s.commands.tasks.handler('', s.ctx); assert.equal(s.sent.length, 2);
});

test('accepted completion resets no-progress budget; rejected submissions do not', async () => {
  const s = setup(); await add(s); await add(s, 'Second'); input(s);
  assert.equal(settle(s).continue, true); assert.equal(settle(s).continue, true); assert.equal(settle(s), undefined);
  s.judge(async () => ({ verdict: 'needs_work', reason: 'Missing observation.', missing: ['Show the test output.'] }));
  await complete(s); assert.equal(settle(s), undefined);
  s.judge(async () => ({ verdict: 'accepted', reason: 'Evidence is sufficient.', missing: [] }));
  await complete(s); assert.equal(settle(s).continue, true);
});

test('assessment provenance retains submitted model when selection changes mid-request', async () => {
  const s = setup(); await add(s); let resolve;
  s.judge(() => new Promise(done => { resolve = done; }));
  const running = complete(s);
  s.ctx.model = { provider: 'other', id: 'new-selection' };
  resolve({ verdict: 'accepted', reason: 'Evidence satisfies the task.', missing: [] });
  assert.deepEqual((await running).details.judge, { provider: 'test', model: 'offline-judge' });
});

test('/tasks-send steers Markdown incomplete first, stable within groups, without list-generation inference', async () => {
  const s = setup(); await add(s, 'Completed first'); await add(s, 'Pending second'); await add(s, 'Blocked third');
  await complete(s, 1); await call(s, 'tasks_block', { task_id: 3, reason: 'Needs approval' });
  const before = await list(s); s.requests.length = 0;
  await s.commands['tasks-send'].handler('', s.ctx);
  const [message, options] = s.sent.at(-1);
  assert.match(message.content, /- \[ \] #2 Pending second/); assert.match(message.content, /- \[ \] #3 Blocked third — \*\*blocked:\*\* Needs approval/);
  assert.match(message.content, /- \[x\] #1 Completed first/);
  assert.ok(message.content.indexOf('#2') < message.content.indexOf('#3'));
  assert.ok(message.content.indexOf('#3') < message.content.indexOf('#1'));
  assert.equal(message.display, true); assert.equal(message.customType, 'tasks-list');
  assert.deepEqual(options, { deliverAs: 'steer', triggerTurn: true });
  assert.equal(s.requests.length, 0); assert.deepEqual(await list(s), before);
});

test('Markdown snapshots handle empty lists and escape multiline/markup/control content', () => {
  assert.match(taskListMarkdown([]), /No tasks/);
  const output = taskListMarkdown([{ id: 1, summary: '*bold*\n- [x] <img>\x1b', status: 'pending' }]);
  assert.match(output, /\\\*bold\\\*/); assert.match(output, /&lt;img&gt;/);
  assert.equal(output.split('\n').filter(line => line.startsWith('- ')).length, 1);
  assert.doesNotMatch(output, /\x1b/);
});

test('recent context is six text messages only, bounded, with no tools, system, thinking, attachments, or recursion', () => {
  const entries = [{ type: 'custom', customType: 'secret', data: 'SECRET_SYSTEM' }];
  for (let i = 0; i < 8; i++) entries.push({ type: 'message', message: { role: i % 2 ? 'assistant' : 'user', content: [{ type: 'text', text: `visible ${i}` }, { type: 'thinking', thinking: 'SECRET_THINKING' }, { type: 'image', data: 'SECRET_IMAGE' }] } });
  entries.push({ type: 'message', message: { role: 'toolResult', content: [{ type: 'text', text: 'SECRET_TOOL' }] } });
  entries.push({ type: 'custom_message', customType: 'tasks-summary', content: 'OLD_SUMMARY' });
  const messages = recentTaskMessages(entries);
  assert.equal(messages.length, 6); assert.equal(messages[0].text, 'visible 2');
  assert.doesNotMatch(JSON.stringify(messages), /SECRET|OLD_SUMMARY/);
  const long = recentTaskMessages([{ type: 'message', message: { role: 'user', content: 'x'.repeat(3000) } }]);
  assert.equal(long[0].text.length, 2012); assert.match(long[0].text, /\[truncated\]/);
});

const summaryResponse = (text = 'Pending #1 needs review; ask the reviewer for the concrete blocker.') => ({
  content: [{ type: 'text', text }], stopReason: 'stop', usage: { totalTokens: 70 },
});

test('summary is an isolated current-model aside with task context, recent messages and a question, shared without mutations', async () => {
  const s = setup(); await add(s, 'Finish the implementation');
  s.branch.push({ type: 'message', message: { role: 'user', content: 'We are stuck waiting for code review.' } });
  s.branch.push({ type: 'message', message: { role: 'assistant', content: [{ type: 'text', text: 'The tests are green.' }, { type: 'thinking', thinking: 'SECRET_THINKING' }] } });
  const before = await list(s); s.judge(async () => summaryResponse());
  const response = await call(s, 'tasks_summary', { prompt: 'What can move independently?' });
  assert.equal(response.details.status, 'posted'); assert.equal(response.usage.totalTokens, 70);
  const [model, context, options] = s.requests[0];
  assert.equal(model.id, 'offline-judge'); assert.equal(context.tools, undefined); assert.equal(context.messages.length, 1);
  const data = JSON.parse(context.messages[0].content);
  assert.equal(data.question, 'What can move independently?'); assert.equal(data.tasks[0].summary, 'Finish the implementation');
  assert.equal(data.recent_messages.length, 2); assert.match(data.recent_messages[0].text, /waiting for code review/);
  assert.doesNotMatch(JSON.stringify(context), /SECRET_THINKING/); assert.equal(options.maxTokens, 1024); assert.ok(options.sessionId);
  assert.match(context.systemPrompt, /Separate recorded facts from suggestions/);
  assert.equal(s.sent[0][0].customType, 'tasks-summary'); assert.equal(s.sent[0][0].display, true);
  assert.match(s.sent[0][0].content, /What can move independently/);
  assert.deepEqual(s.sent[0][1], { deliverAs: 'steer', triggerTurn: true });
  assert.equal(s.sent[0][0].details.recent_message_count, 2);
  assert.deepEqual(await list(s), before);
});

test('summary slash arguments and menu question both reach the aside; menu list posts the same snapshot', async () => {
  const s = setup(); await add(s); s.judge(async () => summaryResponse());
  await s.commands['tasks-summary'].handler('Why are we stuck?', s.ctx);
  assert.equal(JSON.parse(s.requests[0][1].messages[0].content).question, 'Why are we stuck?');
  s.select('Summarize tasks with AI'); s.inputs('What help is needed?');
  await s.commands.tasks.handler('', s.ctx);
  assert.equal(JSON.parse(s.requests[1][1].messages[0].content).question, 'What help is needed?');
  assert.equal(s.sent.length, 2);
  s.select('Send task list to session'); await s.commands.tasks.handler('', s.ctx);
  assert.equal(s.sent.at(-1)[0].customType, 'tasks-list'); assert.equal(s.requests.length, 2);
  s.select('Summarize tasks with AI'); s.inputs(undefined); await s.commands.tasks.handler('', s.ctx);
  assert.equal(s.requests.length, 2, 'cancelled question does not call a model');
});

test('summary failures, empty or oversized input/output and no-model states never publish or alter tasks', async () => {
  const s = setup(); assert.equal((await call(s, 'tasks_summary')).details.status, 'empty'); await add(s);
  const before = await list(s);
  for (const reply of [summaryResponse(''), { ...summaryResponse(), stopReason: 'length' }, summaryResponse('x'.repeat(8001))]) {
    s.judge(async () => reply); assert.equal((await call(s, 'tasks_summary')).details.status, 'failed');
  }
  s.judge(async () => { throw new Error('SECRET_PROVIDER'); });
  assert.doesNotMatch(JSON.stringify(await call(s, 'tasks_summary')), /SECRET_PROVIDER/);
  s.ctx.model = undefined; assert.equal((await call(s, 'tasks_summary')).details.status, 'unavailable');
  assert.equal(s.sent.length, 0); assert.deepEqual(await list(s), before);
  s.ctx.model = { provider: 'test', id: 'offline' };
  await assert.rejects(call(s, 'tasks_summary', { prompt: 'x'.repeat(2001) }), /Summary question/);
});

test('aside timeout and duplicate invocation cancel without publishing; session transitions discard late results', async t => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  for (const action of ['deadline', 'duplicate', 'switch', 'abort']) {
    const s = setup(); await add(s); let resolve;
    s.judge(() => new Promise(done => { resolve = done; }));
    const controller = new AbortController(); const running = call(s, 'tasks_summary', {}, controller.signal);
    if (action === 'deadline') t.mock.timers.tick(60000);
    if (action === 'duplicate') assert.equal((await call(s, 'tasks_summary')).details.status, 'cancelling');
    if (action === 'switch') s.handlers.session_start({}, { ...s.ctx, sessionManager: { getBranch: () => [] } });
    if (action === 'abort') controller.abort();
    const outcome = await running;
    assert.equal(outcome.details.status, 'cancelled'); assert.equal(s.sent.length, 0);
    resolve(summaryResponse()); await Promise.resolve(); assert.equal(s.sent.length, 0);
  }
});

test('summary records actual model and labels its snapshot stale if tasks change mid-request', async () => {
  const s = setup(); await add(s); let resolve;
  s.judge(() => new Promise(done => { resolve = done; })); const running = call(s, 'tasks_summary');
  await add(s, 'New task'); s.ctx.model = { provider: 'other', id: 'later-model' };
  resolve(summaryResponse()); const response = await running;
  assert.equal(response.details.stale, true); assert.equal(response.details.model, 'offline-judge');
  assert.equal(s.sent[0][0].details.model, 'offline-judge'); assert.match(s.sent[0][0].content, /Tasks changed during generation/);
  assert.equal((await list(s)).tasks.length, 2);
});

test('oversized aside inputs refuse inference rather than silently dropping tasks', async () => {
  const tasks = Array.from({ length: 100 }, (_, i) => ({ id: i + 1, summary: 'x'.repeat(500), acceptance: 'ok', status: 'complete', completedBy: 'user' }));
  const s = setup([{ type: 'custom', customType: 'tasks-state-v1', data: { version: 1, nextId: 101, paused: false, tasks } }]);
  const response = await call(s, 'tasks_summary');
  assert.equal(response.details.status, 'too_large'); assert.equal(s.requests.length, 0); assert.equal(s.sent.length, 0);
});

test('slash summary and agent tool use the same visible steering publication, including during active execution', async () => {
  const s = setup(); await add(s); s.idle(false); s.judge(async () => summaryResponse());
  await s.commands['tasks-summary'].handler('User asks for help', s.ctx);
  await call(s, 'tasks_summary', { prompt: 'Agent asks for help' });
  assert.equal(s.sent.length, 2);
  for (const [message, options] of s.sent) {
    assert.equal(message.customType, 'tasks-summary'); assert.equal(message.display, true);
    assert.deepEqual(options, { deliverAs: 'steer', triggerTurn: true });
    assert.match(message.content, /Pending #1 needs review/);
  }
});

test('blocked assistance requests retain why/what context for user and LLM without completing the task', async () => {
  const s = setup(); await add(s); input(s);
  const reason = 'Need user assistance: deployment target is unspecified; please choose staging or production.';
  await call(s, 'tasks_block', { task_id: 1, reason });
  assert.equal((await list(s)).tasks[0].blocker, reason); assert.equal((await list(s)).tasks[0].status, 'blocked');
  assert.match(s.widgets.at(-1).join('\n'), /please choose staging or production/);
  assert.match(s.handlers.context({ messages: [] }, s.ctx).messages[0].content, /user assistance/);
  assert.equal(settle(s), undefined);
  const llmReason = 'Need LLM assistance: two explanations fit the test output; suggest a discriminating check.';
  await call(s, 'tasks_block', { task_id: 1, reason: llmReason });
  s.judge(async () => summaryResponse()); await call(s, 'tasks_summary', { prompt: 'Help resolve the blocked task.' });
  assert.equal(JSON.parse(s.requests[0][1].messages[0].content).tasks[0].blocker, llmReason);
  assert.equal((await list(s)).tasks[0].status, 'blocked', 'advice is not unblock/completion');
  assert.equal(settle(s), undefined);
});

test('TUI status labels use distinct theme colors with text retained; menu selection and stored data stay intact', async () => {
  const s = setup(); const colored = [];
  s.ctx.ui.theme.fg = (color, value) => { colored.push([color, value]); return `<${color}>${value}</${color}>`; };
  await add(s, 'Pending'); await add(s, 'Blocked'); await add(s, 'Complete'); await add(s, 'Progress');
  await call(s, 'tasks_progress', { task_id: 4 });
  await call(s, 'tasks_block', { task_id: 2, reason: 'Need user input' }); await complete(s, 3);
  assert.match(s.widgets.at(-1).join('\n'), /<accent>\[pending\]<\/accent>/);
  assert.match(s.widgets.at(-1).join('\n'), /<warning>\[blocked\]<\/warning>/);
  assert.match(s.widgets.at(-1).join('\n'), /<mdLink>\[progress\]<\/mdLink>/);
  s.select(choices => {
    assert.ok(choices.some(choice => /<success>\[complete\]<\/success>/.test(choice)));
    assert.ok(choices.some(choice => /<mdLink>\[progress\]<\/mdLink>/.test(choice)));
    return choices.find(choice => choice.startsWith('#2 '));
  }, 'Back', 'Close');
  await s.commands.tasks.handler('', s.ctx);
  assert.ok(s.titles.some(title => /#2 <warning>\[blocked\]<\/warning>/.test(title)));
  assert.ok(colored.some(([color, label]) => color === 'success' && label === '[complete]'));
  assert.doesNotMatch(JSON.stringify(await list(s)), /<accent>|<warning>|<success>|<mdLink>/);
  await s.commands['tasks-send'].handler('', s.ctx);
  assert.doesNotMatch(s.sent.at(-1)[0].content, /<accent>|<warning>|<success>|<mdLink>/);
});
