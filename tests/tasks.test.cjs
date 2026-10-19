const { test } = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const { createJiti } = require(require.resolve('jiti', { paths: [path.resolve(__dirname, '../extensions')] }));
const { default: extension, parseVerdict, taskListMarkdown, recentTaskMessages } = createJiti(__filename)(path.resolve(__dirname, '../extensions/tasks.ts'));

function setup(branch = [], flags = {}) {
  const handlers = {}, tools = {}, commands = {}, sent = [], notices = [], widgets = [], requests = [], titles = [], flagDefinitions = {}, focuses = [];
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
      input: async () => inputs.shift(), editor: async () => inputs.shift(), confirm: async () => confirms.shift() ?? true,
      custom: async factory => {
        let result, finished = false;
        const component = await factory({ requestRender() {} }, ctx.ui.theme, {}, value => { result = value; finished = true; });
        const original = component.list.getSelectedItem()?.value;
        focuses.push(original);
        const choices = [];
        for (let index = 0; index < 110; index++) {
          component.list.setSelectedIndex(index);
          const value = component.list.getSelectedItem()?.value;
          if (choices.includes(value)) break;
          choices.push(value);
        }
        component.list.setSelectedIndex(choices.indexOf(original));
        titles.push(component.render(200).join('\n'));
        const planned = selections.shift();
        const choice = typeof planned === 'function' ? planned(choices) : planned;
        if (choice && typeof choice === 'object') {
          if (choice.task !== undefined) component.list.setSelectedIndex(choices.findIndex(value => value.startsWith(`#${choice.task} `)));
          component.handleInput(choice.key);
        } else if (choice === undefined) component.handleInput('\x1b');
        else {
          assert.ok(choices.includes(choice), `Menu choice absent: ${choice}`);
          component.list.setSelectedIndex(choices.indexOf(choice)); component.handleInput('\r');
        }
        assert.equal(finished, true); return result;
      },
    },
  };
  extension({
    on: (name, handler) => { handlers[name] = handler; }, registerTool: tool => { tools[tool.name] = tool; },
    registerCommand: (name, command) => { commands[name] = command; },
    registerFlag: (name, options) => { flagDefinitions[name] = options; },
    getFlag: name => flags[name] ?? flagDefinitions[name]?.default,
    appendEntry: (customType, data) => branch.push({ type: 'custom', customType, data: structuredClone(data) }),
    sendMessage: (...args) => sent.push(args),
  });
  handlers.session_start({}, ctx);
  return { handlers, tools, commands, ctx, branch, sent, notices, widgets, requests, titles, focuses,
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
  input(s); // A stopped menu offers Resume; genuine input enables the Pause action.
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
  input(s); // Exercise the real visible Pause action after the stop assertion.
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

test('blocked/completed ledgers retire stale reminders and never schedule another turn', async t => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  for (const statuses of [['blocked', 'blocked'], ['complete', 'complete'], ['blocked', 'complete']]) {
    const s = setup(); await add(s, 'First'); await add(s, 'Second'); input(s);
    const stale = settle(s).entries[0];
    s.handlers.agent_settled({}, s.ctx);
    for (const [index, status] of statuses.entries()) {
      if (status === 'blocked') await call(s, 'tasks_block', { task_id: index + 1, reason: 'Waiting for an existing decision' });
      else { s.select(pickTask(index + 1), 'Mark complete (user override)', 'Close'); await s.commands.tasks.handler('', s.ctx); }
    }
    s.sent.length = 0;
    const userMessage = { role: 'user', content: 'User text must remain unchanged' };
    const messages = s.handlers.context({ messages: [{ role: 'custom', customType: stale.customType, content: stale.content }, userMessage] }, s.ctx).messages;
    assert.equal(messages.some(message => message.customType === 'tasks-reminder-v1'), false, 'historical pending snapshots must not re-prompt');
    assert.deepEqual(messages[0], userMessage);
    if (statuses.includes('blocked')) {
      assert.match(messages.at(-1).content, /No actionable tasks/);
      assert.doesNotMatch(messages.at(-1).content, /Work on pending|Submit each task/);
    } else assert.equal(messages.length, 1, 'completed-only context needs no task prompt');
    assert.equal(settle(s), undefined);
    s.handlers.agent_settled({}, s.ctx); t.mock.timers.tick(180000); assert.equal(s.sent.length, 0);
    s.select('Pause reminders', 'Resume reminders', 'Close'); await s.commands.tasks.handler('', s.ctx); assert.equal(s.sent.length, 0);
  }
});

test('context has only the current ledger and respects suspension without stripping explicit user/list messages', async () => {
  const s = setup(); await add(s); input(s);
  const stale = settle(s).entries[0];
  s.handlers.input({ source: 'interactive', text: 'Stop looping' }, s.ctx);
  const explicit = [{ role: 'user', content: stale.content }, { role: 'custom', customType: 'tasks-list', content: 'Explicit list request' }];
  const context = s.handlers.context({ messages: [{ role: 'custom', customType: stale.customType, content: stale.content }, ...explicit] }, s.ctx);
  assert.equal(context.messages.some(message => message.customType === 'tasks-reminder-v1'), false);
  assert.deepEqual(context.messages.slice(0, 2), explicit);
  assert.match(context.messages.at(-1).content, /paused or suspended/);
  assert.doesNotMatch(context.messages.at(-1).content, /Work on pending|Submit each task/);
  input(s);
  const active = s.handlers.context({ messages: [] }, s.ctx);
  assert.match(active.messages[0].content, /Work on pending/);
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

test('widget prioritizes progress, blocked, pending, complete stably without reordering the ledger', async () => {
  const tasks = [
    { id: 1, summary: 'Pending A', acceptance: 'A', status: 'pending' },
    { id: 2, summary: 'Complete A', acceptance: 'A', status: 'complete', completedBy: 'user' },
    { id: 3, summary: 'Blocked A', acceptance: 'A', status: 'blocked', blocker: 'Waiting A' },
    { id: 4, summary: 'Progress A', acceptance: 'A', status: 'progress' },
    { id: 5, summary: 'Pending B', acceptance: 'A', status: 'pending' },
    { id: 6, summary: 'Blocked B', acceptance: 'A', status: 'blocked', blocker: 'Waiting B' },
    { id: 7, summary: 'Complete B', acceptance: 'A', status: 'complete', completedBy: 'user' },
  ];
  const s = setup([{ type: 'custom', customType: 'tasks-state-v1', data: { version: 1, nextId: 8, paused: false, tasks } }]);
  const visible = () => s.widgets.at(-1).slice(1).map(row => Number(/^#(\d+)/.exec(row)[1]));
  assert.deepEqual(visible(), [4, 3, 6]);
  await call(s, 'tasks_unblock', { task_id: 3 });
  assert.deepEqual(visible(), [4, 6, 1]);
  assert.deepEqual((await list(s)).tasks.map(task => task.id), [1, 2, 3, 4, 5, 6, 7]);
  assert.equal((await list(s)).tasks[2].status, 'pending');
  const few = setup([{ type: 'custom', customType: 'tasks-state-v1', data: { version: 1, nextId: 3, paused: false,
    tasks: [tasks[1], tasks[0]] } }]);
  assert.deepEqual(few.widgets.at(-1).slice(1).map(row => Number(/^#(\d+)/.exec(row)[1])), [1, 2]);
  const completeOnly = setup([{ type: 'custom', customType: 'tasks-state-v1', data: { version: 1, nextId: 3, paused: false,
    tasks: [tasks[1]] } }]);
  assert.match(completeOnly.widgets.at(-1)[0], /^0 open tasks/);
  assert.match(completeOnly.widgets.at(-1)[1], /^#2 \[complete\]/);
  assert.deepEqual(settle(completeOnly), undefined);
  assert.equal(completeOnly.sent.length, 0);
});

test('menu keyboard priority moves a task first and posts exactly one explicit next-work steer', async () => {
  const s = setup(); await add(s, 'First'); await add(s, 'Second'); await add(s, 'Third');
  await call(s, 'tasks_block', { task_id: 2, reason: 'Resolve prerequisite first' });
  s.select({ task: 2, key: 'p' }); await s.commands.tasks.handler('', s.ctx);
  const tasks = (await list(s)).tasks;
  assert.deepEqual(tasks.map(task => task.id), [2, 1, 3]);
  assert.equal(tasks[0].status, 'blocked');
  assert.equal(s.sent.length, 1); assert.equal(s.sent[0][0].customType, 'tasks-user-control-v1');
  assert.match(s.sent[0][0].content, /work on this task next/);
  assert.match(s.sent[0][0].content, /blocked task remains blocked/);
  assert.deepEqual(s.sent[0][1], { deliverAs: 'steer', triggerTurn: true });
  assert.equal(s.requests.length, 0);
  const restored = setup(s.branch); assert.deepEqual((await list(restored)).tasks.map(task => task.id), [2, 1, 3]);
});

test('cancel shortcut requests a safe stop only for progress, without waking an idle agent or completing it', async () => {
  const s = setup(); await add(s); await call(s, 'tasks_progress', { task_id: 1 }); input(s);
  s.select({ task: 1, key: 'c' }); await s.commands.tasks.handler('', s.ctx);
  assert.equal((await list(s)).tasks[0].status, 'pending');
  assert.equal(s.sent.length, 1); assert.match(s.sent[0][0].content, /Stop work on this task at the next safe boundary/);
  assert.match(s.sent[0][0].content, /does not terminate an already-running tool/);
  assert.deepEqual(s.sent[0][1], { deliverAs: 'steer', triggerTurn: false });
  assert.equal(settle(s), undefined); assert.equal(s.requests.length, 0);
  s.sent.length = 0;
  s.select({ task: 1, key: 'c' }); await s.commands.tasks.handler('', s.ctx);
  assert.equal(s.sent.length, 0); assert.match(s.notices.at(-1)[0], /unavailable/);
});

test('delete excludes progress, confirms other states, and rejects stale targets', async () => {
  const s = setup(); await add(s); await call(s, 'tasks_progress', { task_id: 1 });
  let confirmations = 0; s.ctx.ui.confirm = async () => { confirmations++; return true; };
  s.select({ task: 1, key: 'x' }); await s.commands.tasks.handler('', s.ctx);
  assert.equal(confirmations, 0); assert.equal((await list(s)).tasks.length, 1);
  await call(s, 'tasks_progress', { task_id: 1, active: false });
  s.ctx.ui.confirm = async () => { confirmations++; await call(s, 'tasks_progress', { task_id: 1 }); return true; };
  s.select({ task: 1, key: 'x' }); await s.commands.tasks.handler('', s.ctx);
  assert.equal((await list(s)).tasks[0].status, 'progress'); assert.match(s.notices.at(-1)[0], /Task changed/);
  await call(s, 'tasks_progress', { task_id: 1, active: false });
  s.ctx.ui.confirm = async () => false;
  s.select({ task: 1, key: 'x' }); await s.commands.tasks.handler('', s.ctx);
  assert.equal((await list(s)).tasks.length, 1);
  s.ctx.ui.confirm = async () => true;
  s.select({ task: 1, key: 'x' }); await s.commands.tasks.handler('', s.ctx);
  assert.equal((await list(s)).tasks.length, 0); assert.equal(s.sent.length, 0);
  await add(s, 'Completed'); await complete(s, 2);
  s.select({ task: 2, key: 'x' }); await s.commands.tasks.handler('', s.ctx);
  assert.equal((await list(s)).tasks.length, 0);
});

test('cancel invalidates a pending assessment and priority never reopens completed work', async () => {
  const s = setup(); await add(s); await call(s, 'tasks_progress', { task_id: 1 });
  let resolve; s.judge(() => new Promise(done => { resolve = done; })); const assessment = complete(s);
  s.select({ task: 1, key: 'c' }); await s.commands.tasks.handler('', s.ctx);
  assert.equal(s.requests[0][2].signal.aborted, true);
  resolve({ verdict: 'accepted', reason: 'Late result.', missing: [] }); await assessment;
  assert.equal((await list(s)).tasks[0].status, 'pending');
  s.judge(async () => ({ verdict: 'accepted', reason: 'Output verified.', missing: [] })); await complete(s);
  s.sent.length = 0; s.select({ task: 1, key: 'p' }); await s.commands.tasks.handler('', s.ctx);
  assert.equal((await list(s)).tasks[0].status, 'complete'); assert.equal(s.sent.length, 0);
});

test('menu n creates a task, q and Escape close, and focus defaults to progress or Add', async () => {
  const s = setup(); await add(s, 'First'); await add(s, 'Second');
  await call(s, 'tasks_progress', { task_id: 2 });
  s.select({ key: 'q' }); await s.commands.tasks.handler('', s.ctx);
  assert.match(s.focuses.at(-1), /^#2 /); assert.equal(s.sent.length, 0);
  await call(s, 'tasks_progress', { task_id: 2, active: false });
  s.select({ key: 'q' }); await s.commands.tasks.handler('', s.ctx);
  assert.equal(s.focuses.at(-1), 'Add task');
  s.inputs('New via keyboard', 'A concrete criterion', ''); s.select({ key: 'n' }, { key: 'q' });
  await s.commands.tasks.handler('', s.ctx);
  assert.equal((await list(s)).tasks.at(-1).summary, 'New via keyboard');
  assert.equal((await list(s)).tasks.at(-1).acceptance, 'A concrete criterion');
  const configured = setup(s.branch, { 'tasks-menu-focus': 'add' });
  await call(configured, 'tasks_progress', { task_id: 2 });
  configured.select({ key: 'q' }); await configured.commands.tasks.handler('', configured.ctx);
  assert.equal(configured.focuses.at(-1), 'Add task');
  const before = configured.sent.length;
  configured.select(undefined); await configured.commands.tasks.handler('', configured.ctx);
  assert.equal(configured.sent.length, before);
});

test('preview count and initial-focus options validate bounds and preserve ledger order', async () => {
  for (const value of ['-1', '1.5', '101', 'bad', '']) assert.throws(() => setup([], { 'tasks-widget-count': value }), /integer 0–100/);
  assert.throws(() => setup([], { 'tasks-menu-focus': 'anything' }), /progress or add/);
  const s = setup([], { 'tasks-widget-count': '5' });
  for (let i = 0; i < 6; i++) await add(s, `Item ${i}`);
  assert.equal(s.widgets.at(-1).length, 6);
  assert.deepEqual((await list(s)).tasks.map(task => task.id), [1, 2, 3, 4, 5, 6]);
  const hidden = setup(s.branch, { 'tasks-widget-count': '0' }); assert.equal(hidden.widgets.at(-1), undefined);
});

test('single-line preview uses terminal-cell and ANSI-aware truncation without changing task data', async () => {
  const { visibleWidth } = createJiti(__filename)(require.resolve('@earendil-works/pi-tui', { paths: [path.resolve(__dirname, '../extensions')] }));
  const s = setup([], { 'tasks-widget-single-line': true });
  s.ctx.ui.theme.fg = (_color, value) => `\x1b[34m${value}\x1b[0m`;
  const summary = '漢字🙂'.repeat(50); await add(s, summary);
  assert.equal(typeof s.widgets.at(-1), 'function');
  const component = s.widgets.at(-1)();
  for (const width of [1, 5, 20, 80]) {
    const rows = component.render(width);
    assert.equal(rows.length, 2); for (const row of rows) assert.ok(visibleWidth(row) <= width);
  }
  assert.equal((await list(s)).tasks[0].summary, summary);
  const normal = setup(s.branch); assert.equal(Array.isArray(normal.widgets.at(-1)), true);
});

test('criteria editing keeps title/ID, preserves evidence, clears obsolete verdict and confirms reopening', async () => {
  const s = setup(); await add(s, 'Write-once title', { acceptance: 'Old criterion' }); await complete(s);
  const evidence = (await list(s)).tasks[0].evidence;
  s.inputs('New clarified criterion'); s.confirms(false);
  s.select(pickTask(1), 'Edit acceptance criteria'); await s.commands.tasks.handler('', s.ctx);
  assert.equal((await list(s)).tasks[0].acceptance, 'Old criterion'); assert.equal((await list(s)).tasks[0].status, 'complete');
  s.inputs('New clarified criterion'); s.confirms(true);
  s.select(pickTask(1), 'Edit acceptance criteria'); await s.commands.tasks.handler('', s.ctx);
  const task = (await list(s)).tasks[0];
  assert.equal(task.id, 1); assert.equal(task.summary, 'Write-once title'); assert.equal(task.acceptance, 'New clarified criterion');
  assert.equal(task.status, 'pending'); assert.equal(task.assessment, undefined); assert.equal(task.completedBy, undefined);
  assert.equal(task.evidence, evidence); assert.equal(s.sent.length, 0);
  const restored = setup(s.branch); assert.equal((await list(restored)).tasks[0].acceptance, 'New clarified criterion');
});

test('criteria editing rejects blank/oversized/cancelled/stale edits and aborts only accepted changes', async () => {
  const s = setup(); await add(s);
  const initial = (await list(s)).tasks[0];
  for (const value of [' ', 'x'.repeat(1001), undefined]) {
    s.inputs(value); s.select(pickTask(1), 'Edit acceptance criteria'); await s.commands.tasks.handler('', s.ctx);
    assert.deepEqual((await list(s)).tasks[0], initial);
  }
  s.ctx.ui.editor = async () => { await call(s, 'tasks_block', { task_id: 1, reason: 'Changed while dialog open' }); return 'Stale edit'; };
  s.select(pickTask(1), 'Edit acceptance criteria'); await s.commands.tasks.handler('', s.ctx);
  assert.equal((await list(s)).tasks[0].acceptance, initial.acceptance); assert.match(s.notices.at(-1)[0], /Task changed/);
  let resolve; s.judge(() => new Promise(done => { resolve = done; })); const assessment = complete(s);
  s.ctx.ui.editor = async () => 'Updated criterion';
  s.select(pickTask(1), 'Edit acceptance criteria'); await s.commands.tasks.handler('', s.ctx);
  assert.equal(s.requests[0][2].signal.aborted, true);
  resolve({ verdict: 'accepted', reason: 'Stale result.', missing: [] }); await assessment;
  assert.equal((await list(s)).tasks[0].acceptance, 'Updated criterion'); assert.equal((await list(s)).tasks[0].status, 'blocked');
});

test('real menu component navigates with arrows, selects with Enter and scopes shortcuts to its popup', () => {
  const { taskMenuComponent } = createJiti(__filename)(path.resolve(__dirname, '../extensions/tasks.ts'));
  const choices = ['One', 'Two', 'Three'], results = []; let redraws = 0;
  const component = taskMenuComponent('Tasks', choices, 1, { fg: (_color, value) => value }, value => results.push(value), () => redraws++);
  assert.equal(component.list.getSelectedItem().value, 'Two');
  component.handleInput('\x1b[B'); assert.equal(component.list.getSelectedItem().value, 'Three');
  component.handleInput('\x1b[B'); assert.equal(component.list.getSelectedItem().value, 'One');
  component.handleInput('\x1b[A'); assert.equal(component.list.getSelectedItem().value, 'Three');
  component.handleInput('p'); assert.deepEqual(results.at(-1), { value: 'Three', key: 'p' });
  component.handleInput('\r'); assert.deepEqual(results.at(-1), { value: 'Three' });
  component.handleInput('\x1b'); assert.deepEqual(results.at(-1), {});
  assert.ok(component.render(40).some(row => row.includes('n new'))); assert.ok(redraws >= 3);
});

test('new user controls cannot cross session changes or publish after failed persistence', async () => {
  const s = setup(); await add(s, 'Old session');
  s.select(choices => { s.handlers.session_start({}, { ...s.ctx, sessionManager: { getBranch: () => [] } }); return { task: 1, key: 'p' }; });
  await s.commands.tasks.handler('', s.ctx);
  assert.equal((await list(s)).tasks.length, 0); assert.equal(s.sent.length, 0);
  await add(s, 'A'); await add(s, 'B');
  const original = await list(s); const push = s.branch.push;
  s.branch.push = () => { throw new Error('Persistence failed'); };
  s.select({ task: 2, key: 'p' });
  await assert.rejects(s.commands.tasks.handler('', s.ctx), /Persistence failed/);
  s.branch.push = push;
  assert.deepEqual(await list(s), original); assert.equal(s.sent.length, 0);
});

test('criteria reopening preserves the open-task cap and rejected reopen leaves completion intact', async () => {
  const tasks = Array.from({ length: 20 }, (_, i) => ({ id: i + 1, summary: `Pending ${i}`, acceptance: 'Existing', status: 'pending' }));
  tasks.push({ id: 21, summary: 'Done', acceptance: 'Existing', status: 'complete', completedBy: 'user' });
  const s = setup([{ type: 'custom', customType: 'tasks-state-v1', data: { version: 1, nextId: 22, paused: false, tasks } }]);
  s.inputs('Changed'); s.select(pickTask(21), 'Edit acceptance criteria');
  await s.commands.tasks.handler('', s.ctx);
  assert.equal((await list(s)).tasks.at(-1).status, 'complete'); assert.equal((await list(s)).tasks.at(-1).acceptance, 'Existing');
  assert.match(s.notices.at(-1)[0], /At most 20 open tasks/); assert.equal(s.sent.length, 0);
});

test('task inspector also supports cancel and new-task shortcuts, with progress deletion absent', async () => {
  const s = setup(); await add(s); await call(s, 'tasks_progress', { task_id: 1 });
  s.select(pickTask(1), choices => { assert.equal(choices.includes('Remove task'), false); assert.ok(choices.includes('Cancel task')); return { key: 'c' }; });
  await s.commands.tasks.handler('', s.ctx);
  assert.equal((await list(s)).tasks[0].status, 'pending'); assert.equal(s.sent.length, 1);
  s.inputs('From inspector', '', ''); s.select(pickTask(1), { key: 'n' }, { key: 'q' });
  await s.commands.tasks.handler('', s.ctx);
  assert.equal((await list(s)).tasks.at(-1).summary, 'From inspector');
});

test('compact blocked preview keeps its reason separate, wraps at most two lines and counts tasks not rows', async () => {
  const { visibleWidth } = createJiti(__filename)(require.resolve('@earendil-works/pi-tui', { paths: [path.resolve(__dirname, '../extensions')] }));
  const s = setup([], { 'tasks-widget-single-line': true, 'tasks-widget-count': '2' });
  await add(s, 'Long title '.repeat(40)); await add(s, 'Second task'); await add(s, 'Excluded third');
  const reason = 'Review approval needed before continuing; ' + '漢字🙂'.repeat(60);
  await call(s, 'tasks_block', { task_id: 1, reason });
  const render = width => s.widgets.at(-1)().render(width);
  const rows = render(40);
  assert.equal(rows.length, 5, 'header + blocked title + two reason lines + second task');
  assert.match(rows[1], /^#1 \[blocked\]/); assert.doesNotMatch(rows[1], /Blocker:/);
  assert.match(rows[2], /^ {4}Blocker: Review approval/);
  assert.ok(rows[3].startsWith('    ')); assert.ok(rows[3].endsWith('…'));
  assert.match(rows[4], /^#2 /); assert.equal(rows.some(row => row.startsWith('#3 ')), false);
  for (const width of [1, 5, 20, 80]) for (const row of render(width)) assert.ok(visibleWidth(row) <= width);
  assert.deepEqual(render(0), []);
  assert.equal((await list(s)).tasks[0].blocker, reason, 'full reason is stored, not display-truncated');
  const normal = setup(s.branch); assert.equal(Array.isArray(normal.widgets.at(-1)), true);
  assert.equal(normal.widgets.at(-1).length, 4, 'ordinary wrapping path remains one source row per task');
});

test('short compact blockers need no ellipsis or spare reason row', async () => {
  const s = setup([], { 'tasks-widget-single-line': true }); await add(s, 'Pending review');
  await call(s, 'tasks_block', { task_id: 1, reason: 'Waiting for review' });
  const rows = s.widgets.at(-1)().render(80);
  assert.equal(rows.length, 3); assert.equal(rows[2], '    Blocker: Waiting for review');
});
