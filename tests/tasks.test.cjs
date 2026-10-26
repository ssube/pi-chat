const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { createJiti } = require(require.resolve('jiti', { paths: [path.resolve(__dirname, '../extensions')] }));
const { default: extension, parseVerdict, taskListMarkdown, recentTaskMessages, recentJudgeMessages, readEvidenceFile, evidenceUrls } = createJiti(__filename)(path.resolve(__dirname, '../extensions/tasks.ts'));

function setup(branch = [], flags = {}, readUrl) {
  const handlers = {}, tools = {}, commands = {}, sent = [], notices = [], widgets = [], requests = [], titles = [], editors = [], flagDefinitions = {}, focuses = [];
  const listeners = new Map();
  const events = {
    on: (channel, handler) => { const handlers = listeners.get(channel) ?? []; handlers.push(handler); listeners.set(channel, handlers); return () => listeners.set(channel, handlers.filter(item => item !== handler)); },
    emit: (channel, data) => { for (const handler of listeners.get(channel) ?? []) handler(data); },
  };
  let selections = [], inputs = [], confirms = [], idle = true, queued = false, activeTools = [];
  let judge = async () => ({ verdict: 'accepted', reason: 'The supplied test output satisfies the criterion.', missing: [] });
  const ctx = {
    mode: 'tui', cwd: process.cwd(), model: { provider: 'test', id: 'offline-judge' },
    sessionManager: { getBranch: () => branch, getSessionId: () => "test-session" }, isIdle: () => idle, hasPendingMessages: () => queued,
    modelRegistry: { complete: async (...args) => {
      requests.push(args);
      const value = await judge(...args);
      return value?.content ? value : { content: [{ type: 'text', text: JSON.stringify(value) }], stopReason: 'stop', usage: { totalTokens: 42 } };
    } },
    ui: {
      theme: { fg: (_color, value) => value },
      setWidget: (key, value) => {
        if (!value) { widgets.push(undefined); return; }
        let component;
        const capture = () => widgets.push(flags['tasks-widget-single-line'] ? () => component : component.render(2000).map(row => row.slice(1)));
        component = value({ terminal: { rows: 60 }, requestRender: capture }, {});
        capture();
      }, notify: (...args) => notices.push(args),
      select: async (title, choices) => {
        titles.push(title);
        const value = selections.shift();
        return typeof value === 'function' ? value(choices) : value;
      },
      input: async () => inputs.shift(), editor: async (...args) => { editors.push(args); return inputs.shift(); }, confirm: async () => confirms.shift() ?? true,
      custom: async factory => {
        let result, finished = false;
        const component = await factory({ terminal: { rows: 24 }, requestRender() {} }, ctx.ui.theme, {}, value => { result = value; finished = true; });
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
    sendMessage: (...args) => sent.push(args), events, getAllTools: () => Object.values(tools), getActiveTools: () => activeTools,
  }, readUrl);
  handlers.session_start({}, ctx);
  return { handlers, tools, commands, ctx, branch, sent, notices, widgets, requests, titles, editors, focuses, events,
    activeTools: (...names) => { activeTools = names; },
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

test('task lookup for queued questions exposes only current open IDs', async () => {
  const s = setup(); await add(s, 'Open task');
  const lookup = id => { const query = { task_id: id }; s.events.emit('tasks:lookup-v1', query); return query.available; };
  assert.equal(lookup(1), true);
  assert.equal(lookup(2), false);
  assert.equal(lookup(-1), undefined);
  await complete(s, 1);
  assert.equal(lookup(1), false, 'completed tasks cannot receive new linked questions');
  s.handlers.session_tree({}, { ...s.ctx, sessionManager: { getBranch: () => [] } });
  assert.equal(lookup(1), false, 'navigating to another branch cannot reuse the old task');
});

test('registered guidance distinguishes tracked review dependencies from routine waits', async () => {
  const s = setup(); await add(s, 'Wait for CI result');
  assert.match(s.tools.tasks_block.description, /For a review that is the next dependency, use tasks_review/);
  assert.match(s.tools.tasks_progress.description, /Routine CI, review and quick-question waits may stay pending or in progress/);
  input(s);
  const context = s.handlers.context({ messages: [] }, s.ctx).messages.at(-1).content;
  assert.match(context, /tasks_review records the reviewer, artifact and next check/);
  assert.match(context, /Ordinary CI, review or quick-question waits with independent work need no block/);
  assert.doesNotMatch(context, /queue_question/);
  assert.equal((await list(s)).tasks[0].status, 'pending', 'the guidance makes no automatic state change');
});

test('question guidance follows tool availability, including a fully blocked ledger', async () => {
  const s = setup(); await add(s, 'Choose the report format'); input(s);
  const context = () => s.handlers.context({ messages: [] }, s.ctx).messages.at(-1).content;
  assert.doesNotMatch(context(), /queue_question/);
  s.activeTools('queue_question');
  for (const blocked of [false, true]) {
    if (blocked) await call(s, 'tasks_block', { task_id: 1, reason: 'Need preferred format', waiting_on: 'user' });
    const guidance = context();
    assert.match(guidance, /prefer queue_question\(\{action: "add", task_id, question, context, wait_for_answer: true\}\)/);
    assert.match(guidance, /For optional clarification, omit wait_for_answer/);
    assert.match(guidance, /Do not queue required approval, spending, or consequential decisions; ask those directly/);
    assert.match(guidance, /Reuse an existing linked question/);
    assert.match(guidance, /After an answer resolves a persisted blocker, use tasks_unblock/);
    assert.equal((await list(s)).tasks[0].status, blocked ? 'blocked' : 'pending');
  }
  s.activeTools();
  assert.doesNotMatch(context(), /queue_question/);
});

test('generic blockers retain who or what can resolve them without inventing an approval gate', async () => {
  const s = setup(); await add(s, 'Wait for CI');
  const blocked = await call(s, 'tasks_block', { task_id: 1, reason: 'Build result needed', waiting_on: 'CI job #42' });
  assert.equal(blocked.details.task.waiting_on, 'CI job #42');
  const task = (await list(s)).tasks[0];
  assert.equal(task.status, 'blocked'); assert.equal(task.effective_waiting_on, 'CI job #42');
  assert.match(s.widgets.at(-1).join('\n'), /waiting on CI job #42/);
  assert.match(s.handlers.context({ messages: [] }, s.ctx).messages.at(-1).content, /CI job #42/);
  assert.equal((await list(setup(s.branch))).tasks[0].waiting_on, 'CI job #42');
  for (const who of [' ', 'x'.repeat(121)]) {
    await assert.rejects(call(s, 'tasks_block', { task_id: 1, reason: 'New reason', waiting_on: who }));
    assert.equal((await list(s)).tasks[0].blocker, 'Build result needed');
  }
  const malformed = structuredClone(s.branch);
  malformed.at(-1).data.upsert[0].status = 'pending';
  await assert.rejects(list(setup(malformed)), /unavailable/, 'a pending task cannot retain a blocker owner');
  await call(s, 'tasks_block', { task_id: 1, reason: 'Different prerequisite' });
  assert.equal((await list(s)).tasks[0].waiting_on, undefined, 'generic re-blocking clears stale owner');
  await call(s, 'tasks_unblock', { task_id: 1 });
  assert.equal((await list(s)).tasks[0].effective_waiting_on, undefined);
});

test('review marker persists a blocked checkpoint and shows a due review without starting inference', async () => {
  const s = setup(); await add(s, 'Review the task change'); input(s);
  const check_at = new Date(Date.now() - 60_000).toISOString();
  const result = await call(s, 'tasks_review', { task_id: 1, reviewer: 'Reviewer', artifact: 'example/project PR #42', check_at });
  assert.equal(result.details.task.status, 'blocked');
  assert.deepEqual(result.details.task.review, { reviewer: 'Reviewer', artifact: 'example/project PR #42', check_at });
  assert.equal(result.details.task.waiting_on, 'Reviewer');
  assert.equal((await list(s)).tasks[0].effective_waiting_on, 'Reviewer');
  assert.equal(result.details.due, true);
  assert.deepEqual((await list(s)).review_due_ids, [1]);
  assert.match(s.widgets.at(-1).join('\n'), /blocked due/);
  assert.match(s.handlers.context({ messages: [] }, s.ctx).messages.at(-1).content, /Review check due for task #1/);
  assert.equal(settle(s), undefined, 'blocked review cannot start an automatic continuation');
  assert.equal(s.sent.length, 0);
  assert.equal(s.requests.length, 0, 'review marker does not call a provider');
  const restored = setup(s.branch); assert.deepEqual((await list(restored)).tasks[0].review, result.details.task.review);
  assert.deepEqual((await list(restored)).review_due_ids, [1]);
  const branched = setup([]); assert.deepEqual((await list(branched)).review_due_ids, []);
  await call(s, 'tasks_unblock', { task_id: 1 });
  assert.equal((await list(s)).tasks[0].review, undefined);
  assert.equal((await list(s)).tasks[0].waiting_on, undefined);
  assert.deepEqual((await list(s)).review_due_ids, []);
});

test('review checkpoint is visible in task menu and clears on resume or changed criteria', async () => {
  const s = setup(); await add(s, 'Review menu');
  const future = new Date(Date.now() + 86_400_000).toISOString();
  const mark = () => call(s, 'tasks_review', { task_id: 1, reviewer: 'Reviewer', artifact: 'PR #42', check_at: future });
  await mark();
  s.select(pickTask(1), 'View diagnostics (read-only)', 'Close'); await s.commands.tasks.handler('', s.ctx);
  assert.match(s.editors.at(-1)[1], /Reviewer/);
  assert.match(s.editors.at(-1)[1], /PR #42/);
  assert.match(s.titles.join('\n'), /Waiting on: Reviewer/);
  s.select(pickTask(1), 'Resume task', 'Close'); await s.commands.tasks.handler('', s.ctx);
  assert.equal((await list(s)).tasks[0].status, 'pending');
  assert.equal((await list(s)).tasks[0].review, undefined);
  await mark();
  s.inputs('Changed criterion'); s.select(pickTask(1), 'Edit acceptance criteria');
  await s.commands.tasks.handler('', s.ctx);
  const task = (await list(s)).tasks[0];
  assert.equal(task.status, 'pending'); assert.equal(task.acceptance, 'Changed criterion');
  assert.equal(task.review, undefined, 'changed acceptance invalidates an outstanding review');
  assert.equal(s.requests.length, 0);
});

test('review checkpoint validates inputs and clears on generic block or completion', async () => {
  const s = setup(); await add(s, 'Review checkpoint');
  const future = new Date(Date.now() + 86_400_000).toISOString();
  const malformed = structuredClone(s.branch);
  malformed.at(-1).data.upsert[0].review = { reviewer: 'Reviewer', artifact: 'PR #1', check_at: future };
  await assert.rejects(list(setup(malformed)), /unavailable/, 'restoration refuses a review marker on a pending task');
  for (const args of [
    { task_id: 9, reviewer: 'Reviewer', artifact: 'PR #1', check_at: future },
    { task_id: 1, reviewer: ' ', artifact: 'PR #1', check_at: future },
    { task_id: 1, reviewer: 'Reviewer', artifact: ' ', check_at: future },
    { task_id: 1, reviewer: 'Reviewer', artifact: 'PR #1', check_at: '2026-10-19T12:00:00' },
    { task_id: 1, reviewer: 'Reviewer', artifact: 'PR #1', check_at: 'not a date' },
  ]) await assert.rejects(call(s, 'tasks_review', args));
  assert.equal((await list(s)).tasks[0].status, 'pending');
  await call(s, 'tasks_review', { task_id: 1, reviewer: 'Reviewer', artifact: 'PR #1', check_at: future });
  const mismatch = structuredClone(s.branch);
  mismatch.at(-1).data.upsert[0].waiting_on = 'Someone else';
  await assert.rejects(list(setup(mismatch)), /unavailable/, 'review owner cannot disagree with reviewer');
  assert.deepEqual((await list(s)).review_due_ids, []);
  await call(s, 'tasks_block', { task_id: 1, reason: 'Need a separate owner decision' });
  assert.equal((await list(s)).tasks[0].review, undefined);
  await assert.rejects(call(s, 'tasks_review', { task_id: 1, reviewer: 'Reviewer', artifact: 'PR #1', check_at: future }), /another blocker/);
  assert.equal((await list(s)).tasks[0].blocker, 'Need a separate owner decision');
  await call(s, 'tasks_unblock', { task_id: 1 });
  await call(s, 'tasks_review', { task_id: 1, reviewer: 'Reviewer', artifact: 'PR #1', check_at: future });
  await complete(s);
  assert.equal((await list(s)).tasks[0].status, 'complete');
  assert.equal((await list(s)).tasks[0].review, undefined);
  await assert.rejects(call(s, 'tasks_review', { task_id: 1, reviewer: 'Reviewer', artifact: 'PR #1', check_at: future }), /already complete/);
});

test('optional linked questions stay actionable; only explicit waiting suppresses reminders', async () => {
  const s = setup(); await add(s, 'Continue independent work'); input(s);
  let waiting = false;
  s.events.on('questions:lookup-v1', query => { query.ids = [7]; query.waiting_ids = waiting ? [7] : []; });
  assert.equal((await list(s)).tasks[0].effective_status, 'pending');
  assert.deepEqual((await list(s)).tasks[0].question_ids, [7]);
  assert.equal(settle(s).continue, true, 'an optional question does not block the task');
  waiting = true;
  assert.equal((await list(s)).tasks[0].effective_status, 'question');
  assert.equal(settle(s), undefined, 'waiting suppresses automatic reminders');
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
  s.select('More actions…', 'Pause reminders', 'Back', 'Close'); await s.commands.tasks.handler('', s.ctx);
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
  s.select('More actions…', 'Pause reminders', 'Back', 'Close'); await s.commands.tasks.handler('', s.ctx); input(s);
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

test('declared workspace files provide bounded judge evidence without another model call', async t => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'pi-judge-file-'));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  fs.writeFileSync(path.join(dir, 'result.txt'), 'Verified: 8 tests passed, 0 failed.');
  const s = setup(); s.ctx.cwd = dir; await add(s, 'Run tests', { acceptance: 'Eight tests pass' });
  const response = await call(s, 'tasks_complete', { task_id: 1, evidence: 'See declared test output', evidence_files: ['result.txt'] });
  assert.equal(response.details.task.status, 'complete');
  assert.deepEqual(response.details.task.evidence_files, ['result.txt']);
  assert.deepEqual((await list(setup(s.branch))).tasks[0].evidence_files, ['result.txt']);
  assert.equal(s.requests.length, 1, 'file retrieval does not add model calls');
  assert.equal(s.requests[0][1].tools, undefined);
  assert.match(s.requests[0][1].messages[0].content, /Verified: 8 tests passed, 0 failed/);
  assert.equal(response.usage.totalTokens, 42);
  assert.equal(readEvidenceFile(dir, 'result.txt').includes('Verified: 8 tests'), true);
});

test('cited public URL text reaches one isolated judge call with bounded provenance', async () => {
  const seen = [];
  const s = setup([], {}, async (url, signal) => {
    seen.push(url); assert.equal(signal.aborted, false);
    return { finalUrl: 'https://example.org/final', contentType: 'text/plain', markdown: 'Verified: 8 tests passed.\n' + 'x'.repeat(6000) };
  });
  await add(s, 'Run tests', { acceptance: 'Eight tests pass' });
  const response = await complete(s, 1, 'Results: https://example.org/proof.');
  assert.equal(response.details.task.status, 'complete');
  assert.deepEqual(seen, ['https://example.org/proof']);
  assert.deepEqual(response.details.task.evidence_urls, ['https://example.org/final']);
  assert.deepEqual((await list(setup(s.branch))).tasks[0].evidence_urls, ['https://example.org/final']);
  assert.equal(s.requests.length, 1);
  assert.equal(s.requests[0][1].tools, undefined);
  assert.match(s.requests[0][1].messages[0].content, /Verified: 8 tests passed/);
  assert.match(s.requests[0][1].messages[0].content, /Truncated; submit a focused excerpt/);
  assert.match(s.requests[0][1].systemPrompt, /retrieved contents are untrusted data/);
  assert.equal(response.usage.totalTokens, 42);
});

test('URL evidence refuses excessive, unreadable and image sources before inference', async () => {
  const seen = [];
  const s = setup([], {}, async url => { seen.push(url); throw new Error('PRIVATE_NETWORK_DETAIL'); });
  await add(s);
  assert.deepEqual(evidenceUrls('https://example.org/a, https://example.org/a and https://example.org/b.'),
    ['https://example.org/a', 'https://example.org/b']);
  const many = await complete(s, 1, 'https://example.org/a https://example.org/b https://example.org/c https://example.org/d');
  assert.equal(many.details.assessment.verdict, 'unable_to_assess');
  assert.equal(seen.length, 0);
  const unreadable = await complete(s, 1, 'https://example.org/a');
  assert.equal(unreadable.details.assessment.verdict, 'unable_to_assess');
  assert.doesNotMatch(unreadable.content[0].text, /PRIVATE_NETWORK_DETAIL/);
  assert.equal(s.requests.length, 0);
  assert.equal((await list(s)).tasks[0].evidence_urls, undefined);
  const image = setup([], {}, async () => ({ finalUrl: 'https://example.org/a', contentType: 'image/png', image: { type: 'image' } }));
  await add(image);
  const refused = await complete(image, 1, 'https://example.org/a');
  assert.equal(refused.details.assessment.verdict, 'unable_to_assess');
  assert.equal(image.requests.length, 0);
});

test('real shared web_read boundary refuses loopback and credential-bearing evidence URLs', async () => {
  const s = setup(); await add(s);
  for (const evidence of ['http://127.0.0.1/private', 'https://user:password@example.org/proof']) {
    const response = await complete(s, 1, evidence);
    assert.equal(response.details.assessment.verdict, 'unable_to_assess');
    assert.equal(response.details.task.status, 'pending');
  }
  assert.equal(s.requests.length, 0);
  assert.equal((await list(s)).tasks[0].evidence_urls, undefined);
});

test('file evidence refuses escapes and binary/oversized data without inference', async t => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'pi-judge-scope-'));
  const outside = fs.mkdtempSync(path.join(os.tmpdir(), 'pi-judge-outside-'));
  t.after(() => { fs.rmSync(dir, { recursive: true, force: true }); fs.rmSync(outside, { recursive: true, force: true }); });
  fs.writeFileSync(path.join(outside, 'secret.txt'), 'SECRET_SENTINEL');
  fs.symlinkSync(path.join(outside, 'secret.txt'), path.join(dir, 'escape.txt'));
  fs.writeFileSync(path.join(dir, 'binary.txt'), Buffer.from([0, 1, 2]));
  fs.writeFileSync(path.join(dir, 'large.txt'), 'x'.repeat(64 * 1024 + 1));
  fs.writeFileSync(path.join(dir, 'invalid.txt'), Buffer.from([0xff]));
  fs.writeFileSync(path.join(dir, 'long.txt'), 'x'.repeat(9000));
  assert.match(readEvidenceFile(dir, 'long.txt'), /Truncated; submit a focused excerpt/);
  for (const file of ['../secret.txt', path.join(outside, 'secret.txt'), 'escape.txt', 'binary.txt', 'large.txt', 'invalid.txt']) {
    assert.throws(() => readEvidenceFile(dir, file));
  }
  const s = setup([], {}, async () => { throw new Error('unavailable'); }); s.ctx.cwd = dir; await add(s);
  for (const evidence_files of [['escape.txt'], ['binary.txt'], ['large.txt'], ['invalid.txt'], ['a', 'b', 'c', 'd'], ['bad\npath']]) {
    const response = await call(s, 'tasks_complete', { task_id: 1, evidence: 'See file', evidence_files });
    assert.equal(response.details.assessment.verdict, 'unable_to_assess');
  }
  const url = await complete(s, 1, 'https://example.org/proof');
  assert.equal(url.details.assessment.verdict, 'unable_to_assess');
  assert.equal(s.requests.length, 0, 'unsafe files and unreadable URL evidence never reach the provider');
  assert.equal((await list(s)).tasks[0].status, 'pending');
  assert.equal((await list(s)).tasks[0].evidence_files, undefined, 'failed reads are not recorded as inspected evidence');
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

test('versioned one-task deltas replay legacy history, forks and compaction without new snapshots', async () => {
  const legacy = { type: 'custom', customType: 'tasks-state-v1', data: { version: 1, nextId: 2, paused: false,
    tasks: [{ id: 1, summary: 'Legacy task', acceptance: 'Observe result', status: 'pending' }] } };
  const s = setup([legacy]);
  await add(s, 'Second task');
  const fork = structuredClone(s.branch);
  await call(s, 'tasks_progress', { task_id: 2 });
  await call(s, 'tasks_block', { task_id: 1, reason: 'Need the result' });
  assert.deepEqual(s.branch.slice(1).map(entry => entry.customType), Array(3).fill('tasks-delta-v2'));
  assert.deepEqual(s.branch.slice(1).map(entry => entry.data.revision), [1, 2, 3]);
  assert.deepEqual(s.branch.slice(1).map(entry => entry.data.upsert.length), [1, 1, 1]);
  assert.deepEqual(await list(setup(s.branch)), await list(s));
  const length = s.branch.length;
  s.handlers.context({ messages: [] }, s.ctx);
  s.handlers.context({ messages: [] }, s.ctx);
  assert.equal(s.branch.length, length, 'compaction-context refresh does not append a checkpoint');
  const other = setup(fork); await add(other, 'Fork only');
  assert.deepEqual((await list(other)).tasks.map(task => task.id), [1, 2, 3]);
  assert.deepEqual((await list(s)).tasks.map(task => task.id), [1, 2]);
  s.handlers.session_tree({}, { ...s.ctx, sessionManager: { getBranch: () => fork } });
  assert.deepEqual((await list(s)).tasks.map(task => task.id), [1, 2, 3]);
  s.handlers.session_tree({}, s.ctx);
  assert.deepEqual(await list(s), await list(setup(s.branch)));
});

test('invalid or out-of-order deltas fail closed, including a legacy snapshot after v2', async () => {
  const s = setup(); await add(s, 'Keep this task');
  const invalid = [
    [...structuredClone(s.branch), { type: 'custom', customType: 'tasks-delta-v2', data: { ...s.branch[0].data, revision: 3 } }],
    [...structuredClone(s.branch), { type: 'custom', customType: 'tasks-delta-v2', data: { ...s.branch[0].data, version: 3, revision: 2 } }],
    [...structuredClone(s.branch), { type: 'custom', customType: 'tasks-delta-v2', data: { ...s.branch[0].data, revision: 2, order: [999] } }],
    [...structuredClone(s.branch), { type: 'custom', customType: 'tasks-delta-v2', data: { ...s.branch[0].data, revision: 2, junk: 'x'.repeat(192 * 1024) } }],
    [...structuredClone(s.branch), { type: 'custom', customType: 'tasks-state-v1', data: { version: 1, nextId: 1, paused: false, tasks: [] } }],
  ];
  for (const branch of invalid) {
    const restored = setup(branch);
    await assert.rejects(list(restored), /unavailable/);
    await assert.rejects(add(restored, 'Must not erase old work'), /unavailable/);
    input(restored); assert.equal(settle(restored), undefined);
    assert.match(restored.notices[0][0], /invalid saved state/);
  }
});

test('maximal retained ledger writes a bounded one-task delta, not another full snapshot', async () => {
  const tasks = Array.from({ length: 100 }, (_, i) => ({ id: i + 1, summary: 'S'.repeat(500), acceptance: 'A'.repeat(1000),
    status: i < 20 ? 'pending' : 'complete', ...(i < 20 ? {} : { completedBy: 'user' }), evidence: 'E'.repeat(12000),
    assessment: { verdict: 'needs_work', reason: 'R'.repeat(2000), missing: Array(10).fill('M'.repeat(500)) } }));
  const s = setup([{ type: 'custom', customType: 'tasks-state-v1', data: { version: 1, nextId: 101, paused: false, tasks } }]);
  const oldBytes = Buffer.byteLength(JSON.stringify(s.branch[0].data));
  await call(s, 'tasks_block', { task_id: 1, reason: 'B'.repeat(1000) });
  const delta = s.branch[1];
  assert.equal(delta.customType, 'tasks-delta-v2');
  assert.deepEqual(delta.data.upsert.map(task => task.id), [1]);
  assert.ok(oldBytes > 1_000_000);
  assert.ok(Buffer.byteLength(JSON.stringify(delta.data)) < 48_000);
  assert.deepEqual(await list(setup(s.branch)), await list(s));
});

test('six automatic continuations survive accepted progress and restoration; only user input or Resume resets', async () => {
  const s = setup(); for (let i = 0; i < 8; i++) await add(s, `Task ${i + 1}`); input(s);
  for (let id = 1; id <= 6; id++) {
    assert.equal(settle(s).continue, true);
    assert.equal(s.branch.at(-1).data.automaticContinuations, id);
    assert.equal((await complete(s, id)).details.task.status, 'complete');
  }
  assert.equal(settle(s), undefined, 'accepted completions reset only the no-progress budget');
  s.handlers.input({ source: 'extension', text: 'Continue' }, s.ctx);
  assert.equal(settle(s), undefined, 'extension input does not reset the hard cap');
  const restored = setup(s.branch);
  restored.inputs('Another task', '', ''); restored.select('Add task', 'Close');
  await restored.commands.tasks.handler('', restored.ctx);
  assert.equal(restored.sent.length, 0, 'menu add is not a budget reset or unbounded follow-up');
  assert.equal(settle(restored), undefined, 'restoration retained the exhausted count');
  restored.select('More actions…', 'Resume reminders', 'Back', 'Close');
  await restored.commands.tasks.handler('', restored.ctx);
  assert.equal(restored.sent.length, 1, 'explicit Resume starts one counted follow-up');
  assert.equal(restored.branch.at(-1).data.automaticContinuations, 1);
  input(restored);
  assert.equal(restored.branch.at(-1).data.automaticContinuations, 0, 'genuine user input resets the count');
  assert.equal(settle(restored).continue, true);
});

test('idle fallback consumes the same six-count even when judges accept each task', async t => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const s = setup(); for (let i = 0; i < 7; i++) await add(s, `Task ${i + 1}`); input(s);
  for (let id = 1; id <= 6; id++) {
    s.handlers.agent_settled({}, s.ctx); t.mock.timers.tick(180000);
    assert.equal(s.sent.length, id);
    assert.equal(s.branch.at(-1).data.automaticContinuations, id);
    await complete(s, id);
  }
  s.handlers.agent_settled({}, s.ctx); t.mock.timers.tick(180000);
  assert.equal(s.sent.length, 6);
  assert.equal((await list(s)).tasks[6].status, 'pending');
});

test('failed budget persistence cannot emit an automatic continuation', async () => {
  const s = setup(); await add(s); input(s);
  const original = s.branch.push;
  s.branch.push = () => { throw new Error('Persistence failed'); };
  assert.equal(settle(s), undefined);
  assert.equal(s.notices.at(-1)[1], 'error');
  s.branch.push = original;
  assert.equal(settle(s), undefined, 'failure suspends further reminders');
  input(s); assert.equal(settle(s).continue, true);
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
    s.select('More actions…', 'Pause reminders', 'Resume reminders', 'Back', 'Close'); await s.commands.tasks.handler('', s.ctx); assert.equal(s.sent.length, 0);
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

test('maximal task fields stay bounded in injected context while tasks_list retains full details', async () => {
  const tasks = Array.from({ length: 20 }, (_, index) => ({
    id: index + 1, status: index < 10 ? 'pending' : 'blocked',
    summary: '\\'.repeat(500), acceptance: 'A'.repeat(1000), blocker: index < 10 ? undefined : 'B'.repeat(1000),
    evidence: 'E'.repeat(12000), assessment: { verdict: 'needs_work', reason: 'R'.repeat(2000), missing: Array(10).fill('M'.repeat(500)) },
  }));
  const s = setup([{ type: 'custom', customType: 'tasks-state-v1', data: { version: 1, nextId: 21, paused: false, tasks } }]);
  input(s);
  const injected = s.handlers.context({ messages: [] }, s.ctx).messages[0].content;
  assert.ok(injected.length < 18000, `injected task context grew to ${injected.length} characters`);
  assert.match(injected, /\"truncated\":true/);
  assert.match(injected, /\"id\":20/);
  assert.match(injected, /tasks_list for full criteria/);
  assert.doesNotMatch(injected, /E{100}/, 'submitted evidence is not injected');
  const reminder = settle(s).entries[0].content;
  assert.ok(reminder.length < 18000, 'automatic reminder must use the same bounded snapshot');
  const full = (await list(s)).tasks;
  assert.equal(full.length, 20);
  assert.equal(full[19].acceptance.length, 1000);
  assert.equal(full[19].evidence.length, 12000);
  assert.equal(full[19].assessment.missing.length, 10);
});

test('task inspector truncates the header and offers full private criteria/evidence views without mutation', async () => {
  const s = setup(); await add(s, 'S'.repeat(500), { acceptance: 'A'.repeat(1000) });
  s.judge(async () => ({ verdict: 'needs_work', reason: 'R'.repeat(2000), missing: ['M'.repeat(500)] }));
  await complete(s, 1, 'E'.repeat(12000));
  const snapshots = s.branch.length;
  s.inputs('attempted criteria change');
  s.select(pickTask(1), 'View full criteria (read-only)', 'Close');
  await s.commands.tasks.handler('', s.ctx);
  assert.equal(s.editors[0][1], 'A'.repeat(1000));
  assert.match(s.editors[0][0], /changes discarded/);
  const inspector = s.titles.find(title => title.includes('View diagnostics (read-only)'));
  assert.ok(inspector, `inspector heading missing; captured title lengths: ${s.titles.map(title => title.length).join(', ')}`);
  // Retain the criteria-only action alongside Edit task: one additional padded menu row.
  assert.ok(inspector.length < 3200, `inspector popup grew to ${inspector.length} characters`);
  assert.ok(s.titles.every(title => !title.includes('E'.repeat(300))), 'menu header does not render full evidence');
  s.inputs('attempted evidence change');
  s.select(pickTask(1), 'View full evidence (read-only)', 'Close');
  await s.commands.tasks.handler('', s.ctx);
  assert.equal(s.editors[1][1], 'E'.repeat(12000));
  assert.equal(s.branch.length, snapshots, 'viewing full details does not persist edits');
  assert.equal((await list(s)).tasks[0].acceptance, 'A'.repeat(1000));
  assert.equal((await list(s)).tasks[0].evidence, 'E'.repeat(12000));
  assert.equal(s.sent.length, 0, 'private detail views do not post model-visible messages');
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
  s.select('More actions…', 'Pause reminders', 'Back', 'Close'); await s.commands.tasks.handler('', s.ctx);
  assert.equal(settle(s), undefined); assert.equal((await list(setup(s.branch))).paused, true);
  s.select('More actions…', 'Resume reminders', 'Back', 'Close'); await s.commands.tasks.handler('', s.ctx);
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
  s.select('More actions…', 'Pause reminders', 'Back', 'Close'); await s.commands.tasks.handler('', s.ctx); assert.equal(s.sent.length, 1);
  s.select('More actions…', 'Resume reminders', 'Back', 'Close'); await s.commands.tasks.handler('', s.ctx); assert.equal(s.sent.length, 2);
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
  const output = taskListMarkdown([{ id: 1, summary: '*bold*\n- [x] <img>\x1b', status: 'pending', question_ids: [2] }]);
  assert.match(output, /\\\*bold\\\*/); assert.match(output, /&lt;img&gt;/);
  assert.match(output, /question #2 queued/);
  assert.match(taskListMarkdown([{ id: 1, summary: 'Wait', status: 'pending', question_ids: [2], waiting_question_ids: [2] }]), /question #2 waiting for answer/);
  assert.match(taskListMarkdown([{ id: 1, summary: 'Wait', status: 'blocked', blocker: 'Need result', waiting_on: 'CI <job>' }]), /waiting on CI &lt;job&gt;/);
  const mixed = taskListMarkdown([{ id: 1, summary: 'Both', status: 'pending', question_ids: [2, 3], waiting_question_ids: [3] }]);
  assert.match(mixed, /question #3 waiting for answer/); assert.match(mixed, /question #2 queued/);
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
  s.select(pickTask(1), 'Ask AI about this task… (extra model call)'); s.inputs('What help is needed?');
  await s.commands.tasks.handler('', s.ctx);
  assert.equal(JSON.parse(s.requests[1][1].messages[0].content).question, 'What help is needed?');
  assert.equal(s.sent.length, 2);
  s.select(pickTask(1), 'Send this task to session'); await s.commands.tasks.handler('', s.ctx);
  assert.equal(s.sent.at(-1)[0].customType, 'tasks-list'); assert.equal(s.requests.length, 2);
  s.select(pickTask(1), 'Ask AI about this task… (extra model call)'); s.inputs(undefined); await s.commands.tasks.handler('', s.ctx);
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

test('clear completed confirms exact IDs and atomically retains open tasks and monotonic IDs', async () => {
  const s = setup(); await add(s, 'Done A'); await add(s, 'Open'); await add(s, 'Done B');
  await complete(s, 1); await complete(s, 3);
  const before = await list(s), beforeEntries = s.branch.length, beforeRequests = s.requests.length;
  s.ctx.ui.confirm = async (title, message) => { assert.equal(title, 'Clear completed tasks'); assert.match(message, /#1, #3/); return false; };
  s.select('More actions…', 'Clear completed tasks', 'Back', 'Close'); await s.commands.tasks.handler('', s.ctx);
  assert.deepEqual(await list(s), before); assert.equal(s.branch.length, beforeEntries);
  s.ctx.ui.confirm = async (title, message) => { assert.equal(title, 'Clear completed tasks'); assert.match(message, /#1, #3/); return true; };
  s.select('More actions…', 'Clear completed tasks', 'Back', 'Close'); await s.commands.tasks.handler('', s.ctx);
  const after = await list(s);
  assert.deepEqual(after.tasks.map(task => task.id), [2]); assert.equal(after.tasks[0].status, 'pending');
  assert.equal(after.nextId, 4); assert.deepEqual(s.branch.at(-1).data.remove, [1, 3]);
  assert.deepEqual(s.branch.at(-1).data.upsert, []); assert.deepEqual(s.branch.at(-1).data.order, [2]);
  assert.equal(s.requests.length, beforeRequests, 'clearing does not call the judge'); assert.equal(s.sent.length, 0);
  assert.deepEqual(await list(setup(s.branch)), after, 'bulk removal replays after restart');
  const malformed = structuredClone(s.branch);
  malformed.at(-1).data.remove = [1, 2]; malformed.at(-1).data.order = [3];
  await assert.rejects(list(setup(malformed)), /unavailable/, 'bulk removal of an open task is invalid');
  await add(s, 'New task'); assert.equal((await list(s)).tasks.at(-1).id, 4, 'removed IDs are not reused');

  const stale = setup(); await add(stale, 'Done'); await add(stale, 'Not yet'); await complete(stale, 1);
  stale.ctx.ui.confirm = async () => { await complete(stale, 2); return true; };
  stale.select('More actions…', 'Clear completed tasks', 'Back', 'Close'); await stale.commands.tasks.handler('', stale.ctx);
  assert.deepEqual((await list(stale)).tasks.map(task => task.id), [1, 2]);
  assert.match(stale.notices.at(-1)[0], /Completed tasks changed/);

  const failed = setup(); await add(failed, 'Done A'); await add(failed, 'Done B');
  await complete(failed, 1); await complete(failed, 2);
  const snapshot = await list(failed), push = failed.branch.push;
  failed.branch.push = () => { throw new Error('Persistence failed'); };
  failed.ctx.ui.confirm = async () => true;
  failed.select('More actions…', 'Clear completed tasks', 'Back', 'Close'); await failed.commands.tasks.handler('', failed.ctx);
  failed.branch.push = push;
  assert.deepEqual(await list(failed), snapshot, 'failed append leaves all completed tasks intact');
  assert.match(failed.notices.at(-1)[0], /Persistence failed/);
});

test('/tasks-add prompts directly without opening the menu and preserves cancellation', async () => {
  const s = setup(); s.inputs('Direct task', 'Show evidence', '');
  await s.commands['tasks-add'].handler('', s.ctx);
  assert.deepEqual((await list(s)).tasks.map(task => task.summary), ['Direct task']);
  assert.equal((await list(s)).tasks[0].acceptance, 'Show evidence');
  assert.equal(s.titles.length, 0, 'no task menu was opened');
  assert.equal(s.sent.length, 1); assert.equal(s.sent[0][0].customType, 'tasks-reminder-v1');
  assert.deepEqual(s.sent[0][1], { deliverAs: 'followUp', triggerTurn: true });
  const before = await list(s);
  s.inputs(undefined); await s.commands['tasks-add'].handler('', s.ctx);
  s.inputs('Invalid position', 'Criterion', '99'); await s.commands['tasks-add'].handler('', s.ctx);
  assert.deepEqual(await list(s), before); assert.equal(s.sent.length, 1);
  assert.match(s.notices.at(-1)[0], /order must be between/);
  await s.commands['tasks-add'].handler('', { ...s.ctx, mode: 'rpc' });
  assert.deepEqual(await list(s), before); assert.match(s.notices.at(-1)[0], /requires interactive/);

  const paused = setup(); input(paused); paused.select('More actions…', 'Pause reminders', 'Back', 'Close'); await paused.commands.tasks.handler('', paused.ctx);
  paused.inputs('Paused task', '', ''); await paused.commands['tasks-add'].handler('', paused.ctx);
  assert.equal((await list(paused)).tasks[0].summary, 'Paused task'); assert.equal(paused.sent.length, 0);

  const stale = setup(); stale.ctx.ui.input = async () => {
    stale.handlers.session_tree({}, { ...stale.ctx, sessionManager: { getBranch: () => [] } }); return 'Old session task';
  };
  await stale.commands['tasks-add'].handler('', stale.ctx);
  assert.deepEqual((await list(stale)).tasks, []); assert.equal(stale.sent.length, 0);

  const failed = setup(); failed.inputs('Unsaved task', '', '');
  const push = failed.branch.push; failed.branch.push = () => { throw new Error('Persistence failed'); };
  await failed.commands['tasks-add'].handler('', failed.ctx); failed.branch.push = push;
  assert.deepEqual((await list(failed)).tasks, []); assert.equal(failed.sent.length, 0);
  assert.match(failed.notices.at(-1)[0], /Persistence failed/);
});

test('menu n creates a task, q and Escape close, and focus defaults to progress or Add', async () => {
  const s = setup(); await add(s, 'First'); await add(s, 'Second');
  await call(s, 'tasks_progress', { task_id: 2 });
  s.select({ key: 'q' }); await s.commands.tasks.handler('', s.ctx);
  assert.match(s.focuses.at(-1), /^ ?#2 /); assert.equal(s.sent.length, 0);
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
  component.handleInput('\x1b[B'); assert.equal(component.list.getSelectedItem().value, 'Three');
  component.handleInput('\x1b[A'); assert.equal(component.list.getSelectedItem().value, 'Two');
  component.handleInput('\x1b[B');
  component.handleInput('p'); assert.deepEqual(results.at(-1), { value: 'Three', key: 'p' });
  component.handleInput('\r'); assert.deepEqual(results.at(-1), { value: 'Three' });
  component.handleInput('\x1b'); assert.deepEqual(results.at(-1), { value: undefined });
  component.handleInput('?');
  assert.ok(component.render(80).join('\n').includes('keys')); assert.ok(redraws >= 3);
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
  s.select(pickTask(1), choices => { assert.equal(choices.includes('Remove task'), false); assert.ok(choices.includes('Request stop…')); return { key: 'c' }; });
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
  assert.match(rows[1], /^ #1 \[blocked\]/); assert.doesNotMatch(rows[1], /Blocker:/);
  assert.match(rows[2], /^ {5}Blocker: Review approval/);
  assert.ok(rows[3].startsWith('    ')); assert.ok(rows[3].endsWith('…'));
  assert.match(rows[4], /^ ?#2 /); assert.equal(rows.some(row => row.startsWith('#3 ')), false);
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
  assert.equal(rows.length, 3); assert.equal(rows[2], '     Blocker: Waiting for review');
});

test('counts-only metrics use effective question states and never expose task content', async () => {
  const s = setup(); await add(s, 'PRIVATE pending'); await add(s, 'PRIVATE progress'); await add(s, 'PRIVATE blocked');
  await call(s, 'tasks_progress', { task_id: 2 });
  await call(s, 'tasks_block', { task_id: 3, reason: 'PRIVATE reason' });
  const metrics = () => { const query = { version: 1 }; s.events.emit('tasks:metrics-v1', query); return query; };
  assert.deepEqual(metrics(), { version: 1, counts: { pending: 1, progress: 1, blocked: 1, question: 0, complete: 0 } });
  s.events.on('questions:lookup-v1', query => { if (query.task_id === 2) query.waiting_ids = [1]; });
  assert.deepEqual(metrics().counts, { pending: 1, progress: 0, blocked: 1, question: 1, complete: 0 });
  assert.doesNotMatch(JSON.stringify(metrics()), /PRIVATE|task_id|summary|reason/);
  const wrongVersion = { version: 2 }; s.events.emit('tasks:metrics-v1', wrongVersion); assert.equal(wrongVersion.counts, undefined);
  s.handlers.session_shutdown({}, s.ctx); assert.equal(metrics().counts, undefined);
  const invalid = setup([{ type: 'custom', customType: 'tasks-state-v1', data: { version: 99 } }]);
  const query = { version: 1 }; invalid.events.emit('tasks:metrics-v1', query); assert.equal(query.counts, undefined);
});


test('completion context selects 0–10 recent branch messages with tool traffic but no hidden content', async () => {
  const s = setup(); await add(s);
  for (let i = 0; i < 11; i++) s.branch.push({ type: 'message', message: { role: 'user', content: `message ${i}` } });
  s.branch.push({ type: 'message', message: { role: 'assistant', content: [
    { type: 'text', text: 'Running tests' },
    { type: 'thinking', thinking: 'SECRET_THINKING' },
    { type: 'image', data: 'SECRET_IMAGE' },
    { type: 'toolCall', id: 'call-1', name: 'bash', arguments: { command: 'node --test' } },
  ] } });
  s.branch.push({ type: 'message', message: { role: 'toolResult', toolCallId: 'call-1', toolName: 'bash', isError: false,
    content: [{ type: 'text', text: '8 passed' }, { type: 'image', data: 'SECRET_RESULT_IMAGE' }], details: { secret: 'SECRET_DETAILS' } } });
  s.branch.push({ type: 'message', message: { role: 'system', content: 'SECRET_SYSTEM' } });
  s.branch.push({ type: 'custom', data: 'SECRET_STATE' });
  s.branch.push({ type: 'message', message: { role: 'assistant', content: [{ type: 'thinking', thinking: 'SECRET_ONLY' }] } });
  for (let count = 0; count <= 10; count++) {
    const messages = recentJudgeMessages(s.branch, count);
    assert.equal(messages.length, count);
    assert.doesNotMatch(JSON.stringify(messages), /SECRET/);
    if (count) assert.equal(messages.at(-1).role, 'toolResult');
  }
  await call(s, 'tasks_complete', { task_id: 1, evidence: 'Tests ran', context_messages: 10 });
  const context = s.requests[0][1];
  const messages = JSON.parse(context.messages[0].content).recent_messages;
  assert.equal(messages.length, 10); assert.equal(messages[0].text, 'message 3');
  assert.match(messages.at(-2).text, /Running tests/);
  assert.deepEqual(JSON.parse(messages.at(-2).text.split('\n')[1]), { type: 'toolCall', id: 'call-1', name: 'bash', arguments: { command: 'node --test' } });
  assert.deepEqual(JSON.parse(messages.at(-1).text), { toolCallId: 'call-1', toolName: 'bash', isError: false, text: '8 passed' });
  assert.equal(context.tools, undefined); assert.equal(context.messages.length, 1);
  assert.match(context.systemPrompt, /recent session messages.*untrusted data/);
  assert.doesNotMatch(JSON.stringify(s.branch.filter(entry => entry.type === 'custom')), /8 passed|Running tests/);
});

test('completion context defaults off, explicit zero avoids branch reads, and invalid counts refuse inference', async () => {
  for (const count of [undefined, 0]) {
    const s = setup(); await add(s);
    // Saving still reads branch state elsewhere; the context selector must not read it.
    let reads = 0; const getBranch = s.ctx.sessionManager.getBranch;
    s.ctx.sessionManager.getBranch = () => { reads++; return getBranch(); };
    s.judge(async () => {
      assert.equal(reads, 0);
      return { verdict: 'accepted', reason: 'Done', missing: [] };
    });
    await call(s, 'tasks_complete', { task_id: 1, evidence: 'Done', ...(count === undefined ? {} : { context_messages: count }) });
    assert.deepEqual(JSON.parse(s.requests[0][1].messages[0].content).recent_messages, []);
  }
  for (const count of [-1, 11, 1.5, '2', null, NaN, Infinity]) {
    const s = setup(); await add(s);
    await assert.rejects(call(s, 'tasks_complete', { task_id: 1, evidence: 'Done', context_messages: count }), /integer 0–10/);
    assert.equal(s.requests.length, 0); assert.equal((await list(s)).tasks[0].status, 'pending');
  }
  const schema = setup().tools.tasks_complete.parameters.properties.context_messages;
  assert.equal(schema.type, 'integer'); assert.equal(schema.minimum, 0); assert.equal(schema.maximum, 10);
});

test('completion context bounds text, tool arguments and results with truncation markers', () => {
  for (const message of [
    { role: 'user', content: 'x'.repeat(9000) },
    { role: 'assistant', content: [{ type: 'toolCall', id: 'call', name: 'bash', arguments: { command: 'x'.repeat(9000) } }] },
    { role: 'toolResult', toolCallId: 'call', toolName: 'bash', isError: true, content: [{ type: 'text', text: 'x'.repeat(9000) }] },
  ]) {
    const [excerpt] = recentJudgeMessages([{ type: 'message', message }], 1);
    assert.equal(excerpt.text.length, 8012); assert.match(excerpt.text, / \[truncated\]$/);
  }
  assert.deepEqual(recentJudgeMessages([], 10), []);
});


const jobUnit = n => `pi-bg-00000000-0000-0000-0000-${String(n).padStart(12, '0')}.service`;
function jobProvider(s, initial = {}) {
  const jobs = new Map(Object.entries(initial)); let available = true;
  s.events.on('background:lookup-v1', query => {
    if (!available || query.version !== 1 || query.session_id !== 'test-session') return;
    query.available = true; query.job = jobs.get(query.job_id);
  });
  return { jobs, available: value => { available = value; },
    changed: (session_id = 'test-session') => s.events.emit('background:changed-v1', { version: 1, session_id }) };
}
const observedJob = (status = 'running', id = 'job-1', unit = jobUnit(1)) => ({ id, unit, session_id: 'test-session', status,
  finished_at: ['succeeded', 'failed', 'timed_out', 'cancelled'].includes(status) ? new Date().toISOString() : null });
const blockOnJob = (s, task_id = 1, background_job_id = 'job-1') => call(s, 'tasks_block', { task_id, background_job_id, reason: 'Wait for test output' });

test('background-job links are optional; absent provider, missing job and invalid IDs refuse without mutation', async () => {
  const s = setup(); await add(s);
  await assert.rejects(blockOnJob(s), /provider unavailable/);
  const provider = jobProvider(s);
  await assert.rejects(blockOnJob(s), /missing from this session/);
  for (const id of ['', 'job-0', 'not-a-job', 3]) await assert.rejects(blockOnJob(s, 1, id), /Invalid background_job_id/);
  assert.equal((await list(s)).tasks[0].status, 'pending');
  assert.equal(s.sent.length, 0);
  await call(s, 'tasks_block', { task_id: 1, reason: 'Ordinary manual blocker' });
  provider.jobs.set('job-1', observedJob());
  await assert.rejects(blockOnJob(s), /another blocker/);
  assert.equal((await list(s)).tasks[0].blocker, 'Ordinary manual blocker');
});

test('terminal background results restore prior status, never completion, with one progress slot and no wake', async () => {
  for (const status of ['succeeded', 'failed', 'timed_out', 'cancelled']) {
    const s = setup(); await add(s); await call(s, 'tasks_progress', { task_id: 1 });
    const p = jobProvider(s, { 'job-1': observedJob() });
    const blocked = (await blockOnJob(s)).details.task;
    assert.equal(blocked.status, 'blocked'); assert.equal(blocked.background_job.resume_status, 'progress');
    assert.equal(blocked.background_job_availability, 'available');
    assert.equal((await list(s)).tasks[0].background_job.unit, jobUnit(1));
    assert.equal(await settle(s), undefined);
    p.jobs.set('job-1', observedJob(status)); p.changed();
    const restored = (await list(s)).tasks[0];
    assert.equal(restored.status, 'progress'); assert.equal(restored.background_job, undefined);
    assert.equal(restored.blocker, undefined); assert.equal(restored.completedBy, undefined);
    const length = s.branch.length; p.changed(); assert.equal(s.branch.length, length, 'duplicate event is idempotent');
    assert.equal(s.sent.length, 0, 'background plugin alone owns the completion wake');
  }
  const s = setup(); await add(s); await add(s, 'Other work');
  const p = jobProvider(s, { 'job-1': observedJob() });
  await call(s, 'tasks_progress', { task_id: 1 }); await blockOnJob(s);
  await call(s, 'tasks_progress', { task_id: 2 });
  p.jobs.set('job-1', observedJob('succeeded')); p.changed();
  assert.deepEqual((await list(s)).tasks.map(task => task.status), ['pending', 'progress']);
});

test('already-finished jobs and completion during link save are reconciled without a lost wake', async () => {
  const s = setup(); await add(s); jobProvider(s, { 'job-1': observedJob('failed') });
  assert.equal((await blockOnJob(s)).details.task.status, 'pending');
  const race = setup(); await add(race); const p = jobProvider(race, { 'job-1': observedJob() });
  race.events.on('tasks:changed-v1', () => {
    const job = p.jobs.get('job-1'); if (job.status !== 'running') return;
    p.jobs.set('job-1', observedJob('succeeded')); p.changed();
  });
  assert.equal((await blockOnJob(race)).details.task.status, 'pending');
});

test('missing provider after restore is visible; delayed provider-ready event reconciles a retained result', async () => {
  const s = setup(); await add(s); jobProvider(s, { 'job-1': observedJob() }); await blockOnJob(s);
  const saved = structuredClone(s.branch);
  const restored = setup(s.branch);
  let task = (await list(restored)).tasks[0];
  assert.equal(task.status, 'blocked'); assert.equal(task.background_job_availability, 'unavailable');
  assert.match(restored.handlers.context({ messages: [] }, restored.ctx).messages[0].content, /background_job_availability.*unavailable/);
  assert.match(restored.widgets.at(-1).join('\n'), /unavailable/);
  const p = jobProvider(restored, { 'job-1': observedJob('succeeded') });
  p.changed(); task = (await list(restored)).tasks[0];
  assert.equal(task.status, 'pending'); assert.equal(restored.sent.length, 0);
  const without = setup(saved);
  // Independent manual recovery is always available, with no provider.
  assert.equal((await list(without)).tasks[0].status, 'blocked');
  await call(without, 'tasks_unblock', { task_id: 1 });
  assert.equal((await list(without)).tasks[0].background_job, undefined);
});

test('restore reconciles terminal jobs already registered before tasks and preserves paused reminders', async () => {
  const s = setup(); await add(s); const p = jobProvider(s, { 'job-1': observedJob() }); await blockOnJob(s);
  input(s); s.select('More actions…', 'Pause reminders', 'Back', 'Close'); await s.commands.tasks.handler('', s.ctx);
  p.jobs.set('job-1', observedJob('succeeded'));
  s.handlers.session_start({}, s.ctx);
  assert.equal((await list(s)).tasks[0].status, 'pending'); assert.equal((await list(s)).paused, true);
  assert.equal(s.sent.length, 0);
});

test('foreign session, replaced unit, uncertain status and missing completion timestamp never release a dependency', async () => {
  const s = setup(); await add(s); const p = jobProvider(s, { 'job-1': observedJob() }); await blockOnJob(s);
  for (const job of [observedJob('unknown'), observedJob('deadline_exceeded'), observedJob('detached'),
    observedJob('succeeded', 'job-1', jobUnit(2)), { ...observedJob('succeeded'), session_id: 'foreign' },
    { ...observedJob('succeeded'), finished_at: null }, { ...observedJob('succeeded'), id: 'job-2' }]) {
    p.jobs.set('job-1', job); p.changed(); assert.equal((await list(s)).tasks[0].status, 'blocked');
  }
  p.jobs.set('job-1', observedJob('succeeded')); p.changed('foreign');
  assert.equal((await list(s)).tasks[0].status, 'blocked');
  s.handlers.session_before_switch({}, s.ctx); p.changed();
  assert.equal((await list(s)).tasks[0].status, 'blocked');
});

test('manual unblock/reblock, completion, removal and criteria changes detach old job dependencies', async () => {
  for (const action of ['unblock', 'reblock', 'complete', 'override', 'resume', 'remove', 'criteria']) {
    const s = setup(); await add(s); const p = jobProvider(s, { 'job-1': observedJob() }); await blockOnJob(s);
    if (action === 'unblock') await call(s, 'tasks_unblock', { task_id: 1 });
    if (action === 'reblock') await call(s, 'tasks_block', { task_id: 1, reason: 'Different prerequisite' });
    if (action === 'complete') await complete(s);
    if (['override', 'resume', 'remove', 'criteria'].includes(action)) {
      const choices = { override: 'Mark complete (user override)', resume: 'Resume task', remove: 'Remove task', criteria: 'Edit acceptance criteria' };
      if (action === 'criteria') s.inputs('Revised criterion');
      s.select(pickTask(1), choices[action], 'Close'); await s.commands.tasks.handler('', s.ctx);
    }
    const before = (await list(s)).tasks;
    assert.ok(before.every(task => task.background_job === undefined), action);
    p.jobs.set('job-1', observedJob('succeeded')); p.changed();
    assert.deepEqual((await list(s)).tasks, before, action);
  }
});

test('restoring corrupted background dependencies fails closed', async () => {
  const s = setup(); await add(s); jobProvider(s, { 'job-1': observedJob() }); await blockOnJob(s);
  const task = (await list(s)).tasks[0]; delete task.background_job_availability; delete task.effective_status;
  for (const patch of [{ status: 'pending' }, { background_job: { ...task.background_job, resume_status: 'complete' } },
    { background_job: { ...task.background_job, unit: 'wrong' } }, { background_job: { ...task.background_job, session_id: '' } }]) {
    const restored = setup([{ type: 'custom', customType: 'tasks-state-v1', data: { version: 1, nextId: 2, paused: false, tasks: [{ ...task, ...patch }] } }]);
    await assert.rejects(list(restored), /ledger unavailable/);
  }
});


test('/tasks-prune confirms exact completed IDs without opening the menu or touching open tasks', async () => {
  const s = setup();
  for (const title of ['Done A', 'Pending', 'Done B', 'Progress', 'Blocked']) await add(s, title);
  await complete(s, 1); await complete(s, 3);
  await call(s, 'tasks_progress', { task_id: 4 });
  await call(s, 'tasks_block', { task_id: 5, reason: 'External prerequisite' });
  const before = await list(s), requests = s.requests.length;
  s.ctx.ui.confirm = async (title, message) => {
    assert.equal(title, 'Clear completed tasks'); assert.match(message, /#1, #3/); return false;
  };
  await s.commands['tasks-prune'].handler('', s.ctx);
  assert.deepEqual(await list(s), before);
  s.ctx.ui.confirm = async (_title, message) => { assert.match(message, /#1, #3/); return true; };
  await s.commands['tasks-prune'].handler('', s.ctx);
  const after = await list(s);
  assert.deepEqual(after.tasks, before.tasks.filter(task => task.status !== 'complete'));
  assert.equal(after.nextId, 6); assert.deepEqual(s.branch.at(-1).data.remove, [1, 3]);
  assert.equal(s.titles.length, 0); assert.equal(s.requests.length, requests); assert.equal(s.sent.length, 0);
  assert.deepEqual(await list(setup(s.branch)), after);
  await add(s, 'New'); assert.equal((await list(s)).tasks.at(-1).id, 6);
});

test('/tasks-prune is a no-op for empty/open ledgers and refuses headless confirmation', async () => {
  const s = setup(); let confirms = 0;
  s.ctx.ui.confirm = async () => { confirms++; return true; };
  await s.commands['tasks-prune'].handler('', s.ctx);
  await add(s); const before = await list(s), length = s.branch.length;
  await s.commands['tasks-prune'].handler('', s.ctx);
  assert.equal(confirms, 0); assert.match(s.notices.at(-1)[0], /No completed tasks/);
  assert.deepEqual(await list(s), before); assert.equal(s.branch.length, length);
  await complete(s); const completed = await list(s);
  await s.commands['tasks-prune'].handler('', { ...s.ctx, mode: 'rpc' });
  assert.deepEqual(await list(s), completed); assert.equal(confirms, 0);
  assert.match(s.notices.at(-1)[0], /interactive confirmation/);
});

test('/tasks-prune refuses stale targets, session changes and failed persistence', async () => {
  const stale = setup(); await add(stale); await add(stale); await complete(stale, 1);
  stale.ctx.ui.confirm = async () => { await complete(stale, 2); return true; };
  await stale.commands['tasks-prune'].handler('', stale.ctx);
  assert.deepEqual((await list(stale)).tasks.map(task => task.id), [1, 2]);
  assert.match(stale.notices.at(-1)[0], /Completed tasks changed/);
  const switched = setup(); await add(switched); await complete(switched);
  const before = await list(switched), length = switched.branch.length;
  switched.ctx.ui.confirm = async () => { switched.handlers.session_before_switch({}, switched.ctx); return true; };
  await switched.commands['tasks-prune'].handler('', switched.ctx);
  assert.deepEqual(await list(switched), before); assert.equal(switched.branch.length, length);
  const failed = setup(); await add(failed); await complete(failed);
  const saved = await list(failed), push = failed.branch.push;
  failed.ctx.ui.confirm = async () => true;
  failed.branch.push = () => { throw new Error('Persistence failed'); };
  await failed.commands['tasks-prune'].handler('', failed.ctx); failed.branch.push = push;
  assert.deepEqual(await list(failed), saved); assert.match(failed.notices.at(-1)[0], /Persistence failed/);
});

function subagentProvider(s, options = {}) {
  const requests = [], runs = new Map();
  let hook;
  s.tools.subagents_enable = { name: 'subagents_enable' };
  s.events.on('subagents:rpc:v1:request', req => {
    requests.push(req);
    if (hook?.(req) === false) return;
    let data;
    if (req.method === 'ping') data = { session: { sessionId: options.sessionId ?? 'test-session' }, capabilities: { asyncSpawn: true, asyncStatusSnapshot: { version: 1 } } };
    if (req.method === 'spawn') {
      assert.equal(s.branch.at(-1).data.upsert[0].subagent.state, 'launching', 'recovery identity persisted before dispatch');
      const id = `run-${requests.length}`;
      runs.set(id, { id, state: 'running' });
      runs.set(`rpc-spawn-${req.requestId}`, runs.get(id));
      data = { details: { runId: id } };
    }
    if (req.method === 'status') {
      const run = runs.get(req.params.id);
      data = { details: run ? { runId: run.id } : {}, asyncSnapshot: { kind: 'pi-subagents.async-status-snapshot', version: 1, runs: run ? [run] : [] } };
    }
    s.events.emit(`subagents:rpc:v1:reply:${req.requestId}`, { version: 1, requestId: req.requestId, success: true, data });
  });
  return { requests, runs, hook: fn => { hook = fn; } };
}
const assign = (s, action = 'launch', extra = {}) => call(s, 'tasks_assign', { task_id: 1, action, ...(action === 'launch' || action === 'link' ? { agent: 'worker' } : {}), ...extra });

test('subagent launch persists correlation, passes scoped brief, frees progress slot and blocks duplicate work', async () => {
  const s = setup(); const provider = subagentProvider(s);
  await add(s, 'Implement the feature', { acceptance: 'Tests pass' }); await call(s, 'tasks_progress', { task_id: 1 });
  const assigned = (await assign(s)).details.task;
  assert.equal(assigned.status, 'blocked'); assert.equal(assigned.subagent.state, 'assigned');
  const launch = provider.requests.find(req => req.method === 'spawn');
  assert.equal(launch.params.agent, 'worker'); assert.equal(launch.params.context, 'fresh');
  assert.equal(launch.params.cwd, s.ctx.cwd);
  assert.equal(JSON.parse(launch.params.task).acceptance, 'Tests pass');
  assert.equal(launch.requestId, assigned.subagent.request_id);
  await add(s, 'Independent work'); await call(s, 'tasks_progress', { task_id: 2 });
  await assert.rejects(assign(s), /existing blockers/);
  await assert.rejects(complete(s), /delegated/);
  await assert.rejects(call(s, 'tasks_block', { task_id: 1, reason: 'Other' }), /delegated/);
  await assert.rejects(call(s, 'tasks_review', { task_id: 1, reviewer: 'reviewer', artifact: 'PR', check_at: new Date().toISOString() }), /another blocker/);
  assert.match(s.handlers.context({ messages: [] }, s.ctx).messages.at(-1).content, /tasks_assign\(action=launch/);
  assert.match(taskListMarkdown((await list(s)).tasks), /assigned:.*worker.*run-/);
  assert.equal(provider.requests.filter(req => req.method === 'spawn').length, 1);
});

test('link requires exact current-session active run and never launches or steers it', async () => {
  const s = setup(); await add(s); const p = subagentProvider(s);
  p.runs.set('existing', { id: 'existing', state: 'running' });
  await assign(s, 'link', { run_id: 'existing' });
  assert.equal((await list(s)).tasks[0].subagent.run_id, 'existing');
  assert.equal(p.requests.some(req => ['spawn', 'steer'].includes(req.method)), false);
  for (const run of [{ id: 'not-exact', state: 'running' }, { id: 'existing', state: 'complete' }, undefined]) {
    const other = setup(); await add(other); const provider = subagentProvider(other);
    if (run) provider.runs.set('existing', run);
    await assert.rejects(assign(other, 'link', { run_id: 'existing' }), /Exact current-session active run unavailable/);
    assert.equal((await list(other)).tasks[0].status, 'pending');
  }
  const foreign = setup(); await add(foreign); subagentProvider(foreign, { sessionId: 'other-session' });
  await assert.rejects(assign(foreign), /current-session/);
});

test('terminal subagent states return pending for parent assessment; paused/partial remain blocked', async () => {
  for (const state of ['complete', 'failed', 'stopped', 'rejected', 'paused', 'partial', 'running', 'queued']) {
    const s = setup(); await add(s); const p = subagentProvider(s);
    const linked = (await assign(s)).details.task.subagent;
    p.runs.get(linked.run_id).state = state;
    await assign(s, 'refresh');
    const task = (await list(s)).tasks[0];
    assert.equal(task.status, ['paused', 'partial', 'running', 'queued'].includes(state) ? 'blocked' : 'pending', state);
    assert.equal(task.subagent.last_state, state); assert.equal(task.completedBy, undefined);
    assert.equal(s.sent.length, 0, 'plugin owns completion wake');
    if (state === 'complete') { await complete(s); assert.equal((await list(s)).tasks[0].status, 'complete'); }
  }
});

test('completion hints must be reconciled against RPC; unrelated or stale hints cannot finish a task', async () => {
  const s = setup(); await add(s); const p = subagentProvider(s);
  const { run_id } = (await assign(s)).details.task.subagent;
  s.events.emit('subagent:async-complete', { runId: 'other', status: 'complete' });
  await s.handlers.agent_start({}, s.ctx);
  assert.equal((await list(s)).tasks[0].status, 'blocked');
  p.runs.get(run_id).state = 'complete';
  s.events.emit('subagent:async-complete', { runId: run_id });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal((await list(s)).tasks[0].status, 'pending');
  assert.equal(s.sent.length, 0);
});

test('restore retains assignment; missing provider/run fails closed; detach/unblock never stop the child', async () => {
  const s = setup(); await add(s); subagentProvider(s); await assign(s);
  const restored = setup(structuredClone(s.branch));
  const p = subagentProvider(restored);
  assert.equal((await list(restored)).tasks[0].status, 'blocked');
  await assert.rejects(assign(restored, 'refresh'), /unavailable or omitted/);
  assert.equal((await list(restored)).tasks[0].status, 'blocked');
  await assign(restored, 'detach');
  assert.equal((await list(restored)).tasks[0].subagent, undefined);
  assert.equal(p.requests.some(req => req.method === 'stop'), false);
  const again = setup(structuredClone(s.branch)); const p2 = subagentProvider(again);
  await call(again, 'tasks_unblock', { task_id: 1 });
  assert.equal((await list(again)).tasks[0].subagent, undefined);
  assert.equal(p2.requests.length, 0);
  const noPlugin = setup(); await add(noPlugin); input(noPlugin);
  assert.doesNotMatch(noPlugin.handlers.context({ messages: [] }, noPlugin.ctx).messages.at(-1).content, /tasks_assign\(/);
  await assert.rejects(assign(noPlugin), /reply unavailable/);
  assert.equal((await list(noPlugin)).tasks[0].status, 'pending');
});

test('uncertain launch preserves alias and blocks retries; refresh recovers without a second spawn', async () => {
  const s = setup(); await add(s); const p = subagentProvider(s);
  p.hook(req => {
    if (req.method !== 'spawn') return;
    const run = { id: 'recovered', state: 'running' };
    p.runs.set(`rpc-spawn-${req.requestId}`, run); p.runs.set(run.id, run);
    s.events.emit(`subagents:rpc:v1:reply:${req.requestId}`, { version: 1, requestId: req.requestId, success: true, data: { details: {} } });
    return false;
  });
  await assert.rejects(assign(s), /Missing launch identity/);
  const task = (await list(s)).tasks[0]; assert.equal(task.status, 'blocked'); assert.equal(task.subagent.state, 'launching');
  await assert.rejects(assign(s), /existing blockers/);
  await assign(s, 'refresh');
  assert.equal((await list(s)).tasks[0].subagent.run_id, 'recovered');
  assert.equal(p.requests.filter(req => req.method === 'spawn').length, 1);
});

test('persistence failure, cancellation and session changes before dispatch never spawn', async () => {
  for (const mode of ['save', 'cancel', 'session']) {
    const s = setup(); await add(s); const p = subagentProvider(s); const controller = new AbortController();
    p.hook(req => {
      if (req.method !== 'ping') return;
      if (mode === 'cancel') controller.abort();
      if (mode === 'session') s.handlers.session_before_switch({}, s.ctx);
      if (mode === 'save') s.branch.push = () => { throw new Error('append failed'); };
    });
    await assert.rejects(call(s, 'tasks_assign', { task_id: 1, action: 'launch', agent: 'worker' }, controller.signal));
    assert.equal(p.requests.some(req => req.method === 'spawn'), false, mode);
  }
});

test('a session change after dispatch cannot overwrite a new ledger', async () => {
  const s = setup(); await add(s); const p = subagentProvider(s);
  p.hook(req => { if (req.method === 'spawn') s.handlers.session_tree({}, { ...s.ctx, sessionManager: { ...s.ctx.sessionManager, getBranch: () => [] } }); });
  await assert.rejects(assign(s), /Launch occurred but task\/session changed/);
  assert.equal((await list(s)).tasks.length, 0);
  assert.equal(p.requests.filter(req => req.method === 'spawn').length, 1);
});

test('invalid assignment input and corrupt saved links fail closed', async () => {
  for (const args of [{ agent: ' ' }, { action: 'link' }, { action: 'link', run_id: ' ' }, { run_id: 'extra' }, { action: 'unknown' }]) {
    const s = setup(); await add(s); const p = subagentProvider(s);
    await assert.rejects(assign(s, args.action ?? 'launch', args));
    assert.equal(p.requests.length, 0);
  }
  const s = setup(); await add(s); subagentProvider(s); await assign(s);
  for (const change of [{ state: 'bogus' }, { session_id: '' }, { run_id: '' }, { request_id: 'bad' }, { last_state: 'unknown' }]) {
    const branch = structuredClone(s.branch);
    const task = branch.at(-1).data.upsert[0]; Object.assign(task.subagent, change);
    await assert.rejects(list(setup(branch)), /unavailable/);
  }
});

test('optional installed pi-subagents RPC contract: launch, exact link and terminal reconciliation without inference', {
  skip: !process.env.PI_TASKS_SUBAGENTS_RPC_MODULE,
}, async () => {
  const { pathToFileURL } = require('node:url');
  const aliases = JSON.parse(process.env.PI_TASKS_SUBAGENTS_PEER_ALIASES ?? '{}');
  const hook = require('node:module').registerHooks({ resolve(specifier, ctx, next) {
    const alias = aliases[specifier] ?? (specifier.startsWith('typebox/') && aliases.typebox ? path.join(path.dirname(aliases.typebox), specifier.slice(8), 'index.mjs') : undefined);
    return alias ? { url: pathToFileURL(alias).href, shortCircuit: true } : next(specifier, ctx);
  } });
  let registerSubagentRpcBridge;
  try { ({ registerSubagentRpcBridge } = await import(pathToFileURL(path.resolve(process.env.PI_TASKS_SUBAGENTS_RPC_MODULE)).href)); }
  finally { hook.deregister(); }
  const s = setup(); await add(s);
  s.ctx.sessionManager.getSessionFile = () => '/offline/parent.jsonl';
  const state = { currentSessionId: '/offline/parent.jsonl', foregroundControls: new Map(), asyncJobs: new Map() };
  const calls = [];
  const bridge = registerSubagentRpcBridge({ events: s.events, getContext: () => s.ctx, state,
    execute: async (toolCallId, params) => {
      calls.push({ toolCallId, params });
      if (params.action === 'status') return { content: [], details: { mode: 'management', results: [] } };
      const id = 'offline-run';
      state.asyncJobs.set(id, { asyncId: id, sessionId: '/offline/parent.jsonl', status: 'running', mode: 'single', startedAt: Date.now(), agents: ['worker'] });
      return { content: [], details: { mode: 'single', results: [], runId: id, asyncId: id } };
    },
  });
  try {
    await assign(s);
    assert.equal((await list(s)).tasks[0].subagent.run_id, 'offline-run');
    assert.ok(calls[0].toolCallId.startsWith('rpc-spawn-'));
    assert.equal(calls[0].params.agent, 'worker');
    assert.equal(calls[0].params.async, true);
    await assign(s, 'detach');
    await assign(s, 'link', { run_id: 'offline-run' });
    state.asyncJobs.get('offline-run').status = 'complete';
    await assign(s, 'refresh');
    assert.equal((await list(s)).tasks[0].status, 'pending');
    assert.equal((await list(s)).tasks[0].subagent.last_state, 'complete');
    assert.equal(calls.filter(call => call.params.async === true).length, 1);
  } finally { bridge.dispose(); }
});

test('lost spawn reply times out without cancelling work; refresh recovers its durable alias', async t => {
  const s = setup(); await add(s); const p = subagentProvider(s);
  let lost;
  p.hook(req => {
    if (req.method !== 'spawn') return;
    lost = req;
    p.runs.set(`rpc-spawn-${req.requestId}`, { id: 'lost-reply', state: 'complete' });
    return false;
  });
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const pending = assert.rejects(assign(s), /reply unavailable/);
  await new Promise(resolve => setImmediate(resolve));
  assert.ok(lost);
  t.mock.timers.tick(60_000); await pending;
  t.mock.timers.reset();
  assert.equal((await list(s)).tasks[0].status, 'blocked');
  await assign(s, 'refresh');
  const task = (await list(s)).tasks[0];
  assert.equal(task.status, 'pending'); assert.equal(task.subagent.run_id, 'lost-reply');
  assert.equal(p.requests.filter(req => req.method === 'spawn').length, 1);
});

test('post-launch append failure retains pre-launch recovery identity and cannot redispatch', async () => {
  const s = setup(); await add(s); const p = subagentProvider(s);
  const originalPush = s.branch.push;
  p.hook(req => {
    if (req.method === 'spawn') s.branch.push = () => { throw new Error('append after launch failed'); };
  });
  await assert.rejects(assign(s), /append after launch failed/);
  s.branch.push = originalPush;
  assert.equal((await list(s)).tasks[0].subagent.state, 'launching');
  await assert.rejects(assign(s), /existing blockers/);
  await assign(s, 'refresh');
  assert.equal((await list(s)).tasks[0].subagent.state, 'assigned');
  assert.equal(p.requests.filter(req => req.method === 'spawn').length, 1);
});

test('several delegated completions reconcile in independent one-task deltas without a reminder reset', async () => {
  const s = setup(); const p = subagentProvider(s);
  await add(s, 'A'); await add(s, 'B');
  await assign(s); await call(s, 'tasks_assign', { task_id: 2, action: 'launch', agent: 'worker' });
  assert.equal(settle(s), undefined, 'delegated-only ledger never prompts duplicate work');
  for (const run of p.runs.values()) run.state = 'complete';
  await s.handlers.agent_start({}, s.ctx);
  assert.deepEqual((await list(s)).tasks.map(task => task.status), ['pending', 'pending']);
  assert.ok(s.branch.filter(entry => entry.customType === 'tasks-delta-v2').every(entry => entry.data.upsert.length <= 1));
  assert.equal(s.sent.length, 0);
});

test('user edits summary and criteria atomically without changing IDs/order or starting work', async () => {
  const s = setup(); await add(s, 'First', { acceptance: 'Old criteria' }); await add(s, 'Second');
  await call(s, 'tasks_progress', { task_id: 1 });
  const before = await list(s); const deltas = s.branch.length;
  s.inputs('  Updated summary  ', 'Updated criteria'); s.select(pickTask(1), 'Edit task');
  await s.commands.tasks.handler('', s.ctx);
  const after = await list(s);
  assert.deepEqual(after.tasks.map(task => task.id), [1, 2]); assert.equal(after.nextId, before.nextId);
  assert.equal(after.tasks[0].summary, 'Updated summary'); assert.equal(after.tasks[0].acceptance, 'Updated criteria');
  assert.equal(after.tasks[0].status, 'progress'); assert.deepEqual(after.tasks[1], before.tasks[1]);
  assert.equal(s.branch.length, deltas + 1, 'one atomic task delta'); assert.equal(s.sent.length, 0);
  assert.deepEqual(s.editors.slice(-2).map(args => args[1]), ['First', 'Old criteria'], 'both editors are prefilled');
  assert.match(s.notices.at(-1)[0], /ID and order unchanged/);
  assert.equal((await list(setup(s.branch))).tasks[0].summary, 'Updated summary');
  assert.equal(s.tools.tasks_edit, undefined, 'editing remains user-only');
});

test('user edit cancellation, invalid input and unchanged fields never partially save', async () => {
  for (const values of [[undefined], [' '], ['x'.repeat(501)], ['New title', undefined], ['New title', ' '], ['New title', 'x'.repeat(1001)], ['Original', 'Criterion']]) {
    const s = setup(); await add(s, 'Original', { acceptance: 'Criterion' });
    const before = await list(s), length = s.branch.length;
    s.inputs(...values); s.select(pickTask(1), 'Edit task'); await s.commands.tasks.handler('', s.ctx);
    assert.deepEqual(await list(s), before); assert.equal(s.branch.length, length); assert.equal(s.sent.length, 0);
  }
});

test('summary-only edits reopen completed tasks only with confirmation and preserve evidence', async () => {
  const s = setup(); await add(s, 'Old summary', { acceptance: 'Same criteria' }); await complete(s);
  const original = (await list(s)).tasks[0];
  s.inputs('New summary', 'Same criteria'); s.confirms(false); s.select(pickTask(1), 'Edit task'); await s.commands.tasks.handler('', s.ctx);
  assert.deepEqual((await list(s)).tasks[0], original);
  s.inputs('New summary', 'Same criteria'); s.confirms(true); s.select(pickTask(1), 'Edit task'); await s.commands.tasks.handler('', s.ctx);
  const task = (await list(s)).tasks[0];
  assert.equal(task.summary, 'New summary'); assert.equal(task.status, 'pending'); assert.equal(task.acceptance, original.acceptance);
  assert.equal(task.evidence, original.evidence); assert.equal(task.assessment, undefined); assert.equal(task.completedBy, undefined);
  assert.equal(s.sent.length, 0);
});

test('task editing protects both fields from intervening task/session changes and append failures', async () => {
  for (const boundary of ['summary', 'criteria', 'session', 'save']) {
    const s = setup(); await add(s, 'Original', { acceptance: 'Criterion' });
    const before = await list(s); let calls = 0;
    s.ctx.ui.editor = async () => {
      calls++;
      if ((boundary === 'summary' && calls === 1) || (boundary === 'criteria' && calls === 2)) await call(s, 'tasks_block', { task_id: 1, reason: 'Changed during edit' });
      if (boundary === 'session' && calls === 2) s.handlers.session_tree({}, { ...s.ctx, sessionManager: { ...s.ctx.sessionManager, getBranch: () => [] } });
      return calls === 1 ? 'New summary' : 'New criteria';
    };
    const push = s.branch.push;
    if (boundary === 'save') s.branch.push = () => { throw new Error('Save failed'); };
    s.select(pickTask(1), 'Edit task');
    if (boundary === 'save') await assert.rejects(s.commands.tasks.handler('', s.ctx), /Save failed/);
    else await s.commands.tasks.handler('', s.ctx);
    s.branch.push = push;
    const tasks = (await list(s)).tasks;
    if (boundary === 'session') assert.equal(tasks.length, 0);
    else { assert.equal(tasks[0].summary, before.tasks[0].summary); assert.equal(tasks[0].acceptance, before.tasks[0].acceptance); }
    assert.equal(s.sent.length, 0);
  }
});

test('accepted task edit invalidates in-flight assessment but cancelled edit does not', async () => {
  const s = setup(); await add(s); let resolve;
  s.judge(() => new Promise(done => { resolve = done; })); const pending = complete(s);
  s.inputs('Changed summary', undefined); s.select(pickTask(1), 'Edit task'); await s.commands.tasks.handler('', s.ctx);
  assert.equal(s.requests[0][2].signal.aborted, false);
  s.inputs('Changed summary', 'Changed criterion'); s.select(pickTask(1), 'Edit task'); await s.commands.tasks.handler('', s.ctx);
  assert.equal(s.requests[0][2].signal.aborted, true);
  resolve({ verdict: 'accepted', reason: 'Old result', missing: [] }); await pending;
  assert.equal((await list(s)).tasks[0].status, 'pending'); assert.equal((await list(s)).tasks[0].summary, 'Changed summary');
});

test('editing task text preserves manual blockers and detaches stale review/job/subagent tracking without cancellation', async () => {
  for (const kind of ['manual', 'review', 'job', 'subagent']) {
    const s = setup(); await add(s);
    if (kind === 'manual') await call(s, 'tasks_block', { task_id: 1, reason: 'Real prerequisite', waiting_on: 'owner' });
    if (kind === 'review') await call(s, 'tasks_review', { task_id: 1, reviewer: 'owner', artifact: 'PR', check_at: new Date().toISOString() });
    if (kind === 'job') {
      s.events.on('background:lookup-v1', query => { query.available = true; query.job = { id: 'job-1', session_id: 'test-session', unit: 'pi-bg-00000000-0000-0000-0000-000000000000.service', status: 'running', finished_at: null }; });
      await call(s, 'tasks_block', { task_id: 1, reason: 'Wait for job', background_job_id: 'job-1' });
    }
    const provider = kind === 'subagent' ? subagentProvider(s) : undefined;
    if (provider) await assign(s);
    s.inputs('Revised task', 'Revised criteria'); s.select(pickTask(1), 'Edit task'); await s.commands.tasks.handler('', s.ctx);
    const task = (await list(s)).tasks[0];
    assert.equal(task.status, kind === 'manual' ? 'blocked' : 'pending');
    assert.equal(task.review, undefined); assert.equal(task.background_job, undefined); assert.equal(task.subagent, undefined);
    if (kind === 'manual') { assert.equal(task.blocker, 'Real prerequisite'); assert.equal(task.waiting_on, 'owner'); }
    assert.equal(provider?.requests.some(req => ['stop', 'steer'].includes(req.method)) ?? false, false);
    assert.equal(s.sent.length, 0);
  }
});

test('user edit respects open-task limit when reopening a completed task', async () => {
  const tasks = Array.from({ length: 20 }, (_, i) => ({ id: i + 1, summary: `Task ${i}`, acceptance: 'Existing', status: 'pending' }));
  tasks.push({ id: 21, summary: 'Completed', acceptance: 'Existing', status: 'complete', completedBy: 'user' });
  const s = setup([{ type: 'custom', customType: 'tasks-state-v1', data: { version: 1, nextId: 22, paused: false, tasks } }]);
  s.inputs('Changed summary', 'Existing'); s.select(pickTask(21), 'Edit task'); await s.commands.tasks.handler('', s.ctx);
  assert.equal((await list(s)).tasks.at(-1).summary, 'Completed'); assert.equal((await list(s)).tasks.at(-1).status, 'complete');
  assert.match(s.notices.at(-1)[0], /At most 20 open tasks/);
});

test('persisted new blockers ring once, not edits, restores, failed persistence or headless blockers', async () => {
  await require('./bell-capture.cjs')(async count => {
    const s = setup();
    await call(s, 'tasks_add', { summary: 'Bell test' });
    assert.equal(count(), 0);
    await call(s, 'tasks_block', { task_id: 1, reason: 'Missing input' });
    assert.equal(count(), 1);
    await call(s, 'tasks_block', { task_id: 1, reason: 'Updated reason' });
    s.handlers.session_tree({}, s.ctx);
    assert.equal(count(), 1);
    await call(s, 'tasks_unblock', { task_id: 1 });
    await call(s, 'tasks_review', { task_id: 1, reviewer: 'reviewer', artifact: 'ready PR', check_at: '2030-01-01T00:00:00Z' });
    assert.equal(count(), 2);
    for (const mode of ['rpc', 'json', 'print']) {
      s.ctx.mode = mode;
      await call(s, 'tasks_unblock', { task_id: 1 });
      await call(s, 'tasks_block', { task_id: 1, reason: mode });
    }
    assert.equal(count(), 2);
    s.ctx.mode = 'tui';
    await call(s, 'tasks_unblock', { task_id: 1 });
    s.branch.push = () => { throw Error('persistence failed'); };
    await assert.rejects(call(s, 'tasks_block', { task_id: 1, reason: 'not saved' }), /persistence failed/);
    assert.equal(count(), 2);
    s.handlers.session_shutdown({}, s.ctx);
  });
});


test('request stop discloses its boundary before confirmation and persists an honest marker', async () => {
  const s = setup(); await add(s); await call(s, 'tasks_progress', { task_id: 1 });
  s.ctx.ui.confirm = async (title, body) => {
    assert.equal(title, 'Request stop?'); assert.match(body, /Running tools and external jobs may continue/);
    return false;
  };
  s.select({ task: 1, key: 'c' }); await s.commands.tasks.handler('', s.ctx);
  assert.equal((await list(s)).tasks[0].status, 'progress'); assert.equal(s.sent.length, 0);
  s.ctx.ui.confirm = async () => true;
  s.select({ task: 1, key: 'c' }); await s.commands.tasks.handler('', s.ctx);
  assert.equal((await list(s)).tasks[0].stop_requested, true);
  assert.match(JSON.stringify(s.widgets.at(-1)), /stop requested/);
  const restored = setup(s.branch);
  assert.equal((await list(restored)).tasks[0].stop_requested, true);
  assert.equal(settle(restored), undefined, 'restoration must not resume a stopped task');
  assert.match(taskListMarkdown((await list(restored)).tasks), /execution not confirmed stopped/);
  restored.select(pickTask(1), 'Mark in progress', 'Close');
  await restored.commands.tasks.handler('', restored.ctx);
  assert.equal((await list(restored)).tasks[0].stop_requested, undefined);
});


test('Escape returns to the remembered task, q closes and diagnostics stay separate', async () => {
  const s = setup(); await add(s, 'First'); await add(s, 'Second');
  s.select(pickTask(2), undefined, { key: 'q' });
  await s.commands.tasks.handler('', s.ctx);
  assert.match(s.focuses.at(-1), /^ ?#2 /);
  assert.equal(s.titles.length, 3, 'list, inspector, then list after Escape');
  assert.doesNotMatch(s.titles[1], /Assessment:|Completed by:|Evidence:|Review:/);
  assert.match(s.titles[1], /Acceptance:|Blocker:/);
  s.select(pickTask(2), 'View diagnostics (read-only)', 'Close');
  await s.commands.tasks.handler('', s.ctx);
  assert.match(s.editors.at(-1)[1], /Assessment: none/);
  assert.match(s.focuses.at(-1), /^ ?#2 /);
  const before = s.titles.length;
  s.select(pickTask(2), { key: 'q' }); await s.commands.tasks.handler('', s.ctx);
  assert.equal(s.titles.length, before + 2, 'q exits directly from the inspector');
});


test('global task actions are grouped behind More actions and Escape returns to the root', async () => {
  const s = setup(); await add(s);
  s.select(choices => {
    assert.deepEqual(choices.slice(1), ['Add task', 'More actions…', 'Close']);
    return 'More actions…';
  }, choices => {
    assert.equal(choices.includes('Send task list to session'), false);
    assert.equal(choices.includes('Summarize tasks with AI'), false);
    assert.ok(choices.some(value => /reminders/.test(value)));
    return undefined;
  }, 'Close');
  await s.commands.tasks.handler('', s.ctx);
  assert.equal(s.titles.length, 3);
  assert.match(s.titles[1], /reminder settings/);
  assert.equal(s.sent.length, 0); assert.equal(s.requests.length, 0);
});

test('task list window shrinks for short terminals', () => {
  const { taskMenuComponent } = createJiti(__filename)(path.resolve(__dirname, '../extensions/tasks.ts'));
  const choices = Array.from({ length: 100 }, (_, i) => `Task ${i}`);
  for (const rows of [8, 12, 24]) {
    const component = taskMenuComponent('Tasks', choices, 50, { fg: (_color, value) => value }, () => {}, () => {}, rows);
    assert.ok(component.render(80).length <= rows);
  }
});


test('task-level send and AI help scope their task data to the selected task', async () => {
  const s = setup(); await add(s, 'First'); await add(s, 'Second');
  s.select(pickTask(2), 'Send this task to session'); await s.commands.tasks.handler('', s.ctx);
  assert.deepEqual(s.sent.at(-1)[0].details.task_ids, [2]);
  assert.match(s.sent.at(-1)[0].content, /Second/); assert.doesNotMatch(s.sent.at(-1)[0].content, /First/);
  s.judge(async () => summaryResponse()); s.inputs('What is blocking this task?');
  s.select(pickTask(2), 'Ask AI about this task… (extra model call)'); await s.commands.tasks.handler('', s.ctx);
  const data = JSON.parse(s.requests.at(-1)[1].messages[0].content);
  assert.deepEqual(data.tasks.map(task => task.id), [2]);
  assert.deepEqual(s.sent.at(-1)[0].details.task_ids, [2]);
  assert.equal((await list(s)).tasks.length, 2);
});

test('task mnemonics edit/send/ask AI directly from the selected task and m opens list controls', async () => {
  const s = setup(); await add(s, 'First'); await add(s, 'Second');
  s.select({ task: 2, key: 's' }); await s.commands.tasks.handler('', s.ctx);
  assert.deepEqual(s.sent.at(-1)[0].details.task_ids, [2]);
  s.judge(async () => summaryResponse()); s.inputs('Next step?');
  s.select({ task: 2, key: 'a' }); await s.commands.tasks.handler('', s.ctx);
  assert.deepEqual(s.sent.at(-1)[0].details.task_ids, [2]);
  s.inputs('Edited second', 'New acceptance'); s.select({ task: 2, key: 'e' }, 'Close');
  await s.commands.tasks.handler('', s.ctx);
  assert.equal((await list(s)).tasks[1].summary, 'Edited second');
  s.select({ key: 'm' }, 'Back', 'Close'); await s.commands.tasks.handler('', s.ctx);
  assert.match(s.titles.at(-2), /List actions/);
});


test('an authorized tool-based restart clears the stop marker and restores actionable progress', async () => {
  const s = setup(); await add(s); await call(s, 'tasks_progress', { task_id: 1 });
  s.select({ task: 1, key: 'c' }); await s.commands.tasks.handler('', s.ctx);
  const passive = s.handlers.context({ messages: [] }, s.ctx).messages.at(-1).content;
  assert.match(passive, /Do not resume that task without user authorization/);
  assert.match(passive, /tasks_progress\(task_id\) clears the marker/);
  s.handlers.input({ source: 'interactive', text: 'Resume task 1' }, s.ctx);
  await call(s, 'tasks_progress', { task_id: 1 });
  const restarted = await list(s);
  assert.equal(restarted.tasks[0].status, 'progress'); assert.equal(restarted.tasks[0].stop_requested, undefined);
  assert.ok(settle(s), 'authorized restart restores automatic continuation eligibility');
  const restored = setup(s.branch); assert.equal((await list(restored)).tasks[0].stop_requested, undefined);
});

test('task menus cannot bypass the shared cap through the native selector', async () => {
  const s = setup(); await add(s, 'Long task '.repeat(40));
  s.ctx.ui.select = async () => { throw Error('Unbounded menu bypass'); };
  const custom = s.ctx.ui.custom; let frames = 0;
  s.ctx.ui.custom = factory => custom((tui, theme, keys, done) => {
    tui.terminal.rows = 8;
    const view = factory(tui, theme, keys, done);
    for (const width of [20, 40, 80]) assert.ok(view.render(width).length <= 4);
    frames++; return view;
  });
  s.select(pickTask(1), 'Back', 'More actions…', 'Back', 'Close');
  await s.commands.tasks.handler('', s.ctx);
  assert.ok(frames >= 4);
});

test('actual task menu keeps every status color while the HUD retains its existing styling', async () => {
  const { stripVTControlCharacters } = require('node:util');
  const s = setup();
  s.ctx.ui.theme.fg = (role, value) => `\x1b[${({ accent: 36, mdLink: 34, warning: 33, success: 32, dim: 90 })[role] ?? 37}m${value}\x1b[0m`;
  await add(s, 'Pending'); await add(s, 'Running'); await add(s, 'Blocked'); await add(s, 'Done');
  await call(s, 'tasks_progress', { task_id: 2 }); await call(s, 'tasks_block', { task_id: 3, reason: 'Needs input' }); await complete(s, 4);
  s.select('Close'); await s.commands.tasks.handler('', s.ctx);
  const menu = s.titles.at(-1);
  for (const [sgr, status] of [[36, 'pending'], [34, 'progress'], [33, 'blocked'], [32, 'complete']]) assert.ok(menu.includes(`\x1b[${sgr}m[${status}]\x1b[0m`), status);
  assert.equal(/\[\d+m/.test(stripVTControlCharacters(menu)), false, 'no visible SGR debris');
  assert.ok(s.widgets.at(-1).join('\n').includes('\x1b[34m[progress]\x1b[0m'), 'existing HUD color preserved');
});
