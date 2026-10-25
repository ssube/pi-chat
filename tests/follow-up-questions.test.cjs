const { test } = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const { createJiti } = require(require.resolve('jiti', { paths: [path.resolve(__dirname, '../extensions')] }));
const extension = createJiti(__filename)(path.resolve(__dirname, '../extensions/follow-up-questions.ts')).default;
const tasksExtension = createJiti(__filename)(path.resolve(__dirname, '../extensions/tasks.ts')).default;

function createBus() {
  const listeners = new Map();
  return {
    on: (channel, handler) => { const handlers = listeners.get(channel) ?? []; handlers.push(handler); listeners.set(channel, handlers); return () => {}; },
    emit: (channel, data) => { for (const handler of listeners.get(channel) ?? []) handler(data); },
  };
}

function setup(branch = [], events = createBus(), onSend) {
  const handlers = {}, tools = {}, commands = {}, sent = [], widgets = [], selectTitles = [], notices = [];
  let selections = [], answer, confirmed = true;
  const ctx = {
    mode: 'tui', sessionManager: { getBranch: () => branch },
    ui: {
      setWidget: (key, value) => widgets.push(value),
      select: async (title) => { selectTitles.push(title); return selections.shift(); },
      editor: async () => answer,
      confirm: async () => confirmed,
      notify: (...args) => notices.push(args),
    },
  };
  extension({
    on: (name, handler) => { handlers[name] = handler; },
    registerTool: (tool) => { tools[tool.name] = tool; },
    registerCommand: (name, cmd) => { commands[name] = cmd; },
    appendEntry: (customType, data) => branch.push({ type: 'custom', customType, data }),
    sendUserMessage: (...args) => { onSend?.(...args); sent.push(args); }, events,
  });
  handlers.session_start({}, ctx);
  return { ctx, handlers, tools, commands, branch, sent, widgets, selectTitles, notices, events,
    choose: (...values) => { selections = values; },
    answer: (value) => { answer = value; },
    confirm: (value) => { confirmed = value; },
  };
}
async function call(s, args) {
  const result = await s.tools.queue_question.execute('id', args, undefined, undefined, s.ctx);
  s.branch.push({ type: 'message', message: { role: 'toolResult', toolName: 'queue_question', details: result.details } });
  return result;
}

test('queues multiple questions and answers out of order as quoted steering prompts', async () => {
  const s = setup();
  await call(s, { action: 'add', question: 'First?' });
  await call(s, { action: 'add', question: 'Second?' });
  assert.deepEqual(s.widgets.at(-1), ['? 2 questions · /questions to answer or clear']);
  s.choose('#2 Second?', 'Answer');
  s.answer('Yes, thanks.');
  await s.commands.questions.handler('', s.ctx);
  assert.deepEqual(s.sent, [['> Second?\n\nYes, thanks.', { deliverAs: 'steer' }]]);
  assert.deepEqual(s.branch.at(-1).data.questions, [{ id: 1, text: 'First?' }]);
  assert.deepEqual(s.widgets.at(-1), ['? 1 question · /questions to answer or clear']);
  s.handlers.session_tree({}, s.ctx);
  assert.deepEqual(s.widgets.at(-1), ['? 1 question · /questions to answer or clear']);
});

test('cancelled and empty answers leave questions queued; user clears one or all', async () => {
  const s = setup();
  await call(s, { action: 'add', question: 'One?' });
  await call(s, { action: 'add', question: 'Two?' });
  s.choose('#1 One?', 'Answer'); s.answer(' ');
  await s.commands.questions.handler('', s.ctx);
  assert.equal(s.sent.length, 0);
  s.choose('#1 One?', 'Clear this question', undefined);
  await s.commands.questions.handler('', s.ctx);
  assert.equal(s.branch.at(-1).data.questions.length, 1);
  s.choose('Clear all questions'); s.confirm(false);
  await s.commands.questions.handler('', s.ctx);
  assert.equal(s.branch.at(-1).data.questions.length, 1);
  s.confirm(true); s.choose('Clear all questions');
  await s.commands.questions.handler('', s.ctx);
  assert.deepEqual(s.branch.at(-1).data.questions, []);
  assert.equal(s.widgets.at(-1), undefined);
});

test('agent clears by id or all; restore follows current branch, IDs stay unique', async () => {
  const s = setup();
  await call(s, { action: 'add', question: 'One?' });
  await call(s, { action: 'add', question: 'Two?' });
  await call(s, { action: 'clear', id: 1 });
  assert.deepEqual(s.branch.at(-1).message.details.questions.map(q => q.id), [2]);
  const oldBranch = [];
  s.handlers.session_tree({}, { ...s.ctx, sessionManager: { getBranch: () => oldBranch } });
  assert.equal(s.widgets.at(-1), undefined);
  s.handlers.session_tree({}, s.ctx);
  assert.deepEqual(s.widgets.at(-1), ['? 1 question · /questions to answer or clear']);
  await call(s, { action: 'clear' });
  const next = await call(s, { action: 'add', question: 'Three?' });
  assert.equal(next.details.questions[0].id, 3);
});

test('optional context is stored, validated, and shown when reviewing the question', async () => {
  const s = setup();
  await call(s, { action: 'add', question: 'Plain?' });
  const withContext = await call(s, { action: 'add', question: 'Why me?', context: 'The choice changes the design.' });
  assert.equal(withContext.details.questions[0].context, undefined);
  assert.equal(withContext.details.questions[1].context, 'The choice changes the design.');
  const blank = await call(s, { action: 'add', question: 'Blank?', context: '   ' });
  assert.equal(blank.details.questions[2].context, undefined);
  const long = await call(s, { action: 'add', question: 'Long?', context: 'x'.repeat(2001) });
  assert.match(long.content[0].text, /rejected/);
  assert.equal(long.details.questions.length, 3);
  s.choose('#2 Why me?', undefined);
  await s.commands.questions.handler('', s.ctx);
  assert.equal(s.selectTitles.at(-2), '#2 Why me?\n\nThe choice changes the design.');
});

test('task-linked questions validate the open task and carry the link into the answer', async () => {
  const s = setup();
  const unavailable = await call(s, { action: 'add', question: 'No ledger?', task_id: 7 });
  assert.match(unavailable.content[0].text, /task #7 is not open/);
  assert.equal(unavailable.details.nextId, 1);
  s.events.on('tasks:lookup-v1', request => { request.available = request.task_id === 7; });
  const rejected = await call(s, { action: 'add', question: 'Wrong task?', task_id: 8 });
  assert.match(rejected.content[0].text, /task #8 is not open/);
  assert.equal(rejected.details.questions.length, 0);
  const linked = await call(s, { action: 'add', question: 'Which approach?', task_id: 7, context: 'The result changes task #7.' });
  assert.match(linked.content[0].text, /Queued #1 for task #7/);
  assert.equal(linked.details.questions[0].task_id, 7);
  const restored = setup(s.branch);
  assert.equal(restored.widgets.at(-1)[0], '? 1 question · /questions to answer or clear');
  restored.choose('#1 [task #7] Which approach?', 'Answer'); restored.answer('Use the small patch.');
  await restored.commands.questions.handler('', restored.ctx);
  assert.deepEqual(restored.sent, [['Task #7 — queued question\n\n> Which approach?\n\nUse the small patch.', { deliverAs: 'steer' }]]);
  assert.deepEqual(restored.branch.at(-1).data.questions, []);
});

test('answer clears a waiting question before steering and restores it after synchronous send refusal', async () => {
  const events = createBus(); events.on('tasks:lookup-v1', query => { query.available = query.task_id === 1; });
  let observed;
  const s = setup([], events, () => {
    const lookup = { task_id: 1 }; events.emit('questions:lookup-v1', lookup);
    observed = { waiting_ids: lookup.waiting_ids, persisted: s.branch.at(-1).data.questions };
  });
  await call(s, { action: 'add', question: 'Ready?', task_id: 1, wait_for_answer: true });
  s.choose('#1 [task #1] [waiting] Ready?', 'Answer'); s.answer('Yes.');
  await s.commands.questions.handler('', s.ctx);
  assert.deepEqual(observed, { waiting_ids: [], persisted: [] }, 'the answer turn sees the cleared queue');
  assert.equal(s.sent.length, 1);

  const refusedEvents = createBus(); refusedEvents.on('tasks:lookup-v1', query => { query.available = query.task_id === 1; });
  const refused = setup([], refusedEvents, () => { throw new Error('synchronous refusal'); });
  await call(refused, { action: 'add', question: 'Retry?', task_id: 1, wait_for_answer: true });
  refused.choose('#1 [task #1] [waiting] Retry?', 'Answer'); refused.answer('Not yet.');
  await refused.commands.questions.handler('', refused.ctx);
  assert.equal(refused.sent.length, 0);
  assert.equal(refused.branch.at(-1).data.questions[0].id, 1, 'refusal restores the durable question');
  assert.match(refused.notices.at(-1)[0], /question restored/);
});

test('real shared bus derives question state, suppresses reminders, and preserves the progress slot', async () => {
  const branch = [], events = createBus();
  const s = setup(branch, events);
  const handlers = {}, tools = {}, flags = {}, taskWidgets = [], taskSent = [];
  const ctx = { mode: 'tui', sessionManager: { getBranch: () => branch },
    hasPendingMessages: () => false, isIdle: () => true,
    ui: { notify() {}, setWidget: (_key, value) => taskWidgets.push(value), theme: { fg: (_color, value) => value } } };
  tasksExtension({
    events, on: (name, handler) => { handlers[name] = handler; },
    registerFlag: (name, options) => { flags[name] = options; },
    getFlag: name => flags[name]?.default,
    registerTool: tool => { tools[tool.name] = tool; }, registerCommand() {},
    appendEntry: (customType, data) => branch.push({ type: 'custom', customType, data: structuredClone(data) }),
    sendMessage: (...args) => taskSent.push(args),
  });
  handlers.session_start({}, ctx);
  const taskCall = (name, args = {}) => tools[name].execute('tool', args, undefined, undefined, ctx);
  const taskList = async () => (await taskCall('tasks_list')).details.tasks;
  await taskCall('tasks_add', { summary: 'Review the result' });
  const beforeQuestion = structuredClone(branch);
  const linked = await call(s, { action: 'add', question: 'Which result?', task_id: 1, wait_for_answer: true });
  const queuedBranch = structuredClone(branch);
  assert.equal(linked.details.questions[0].task_id, 1);
  assert.equal((await taskList())[0].status, 'pending', 'persisted task state is unchanged');
  assert.equal((await taskList())[0].effective_status, 'question');
  assert.equal((await taskList())[0].effective_waiting_on, 'user');
  assert.deepEqual((await taskList())[0].question_ids, [1]);
  assert.deepEqual((await taskList())[0].waiting_question_ids, [1]);
  assert.match(taskWidgets.at(-1).join('\n'), /\[question\]/);
  s.handlers.session_tree({}, { ...s.ctx, sessionManager: { getBranch: () => beforeQuestion } });
  handlers.session_tree({}, { ...ctx, sessionManager: { getBranch: () => beforeQuestion } });
  assert.equal((await taskList())[0].effective_status, 'pending', 'a branch without the question has no overlay');
  s.handlers.session_tree({}, { ...s.ctx, sessionManager: { getBranch: () => queuedBranch } });
  handlers.session_tree({}, { ...ctx, sessionManager: { getBranch: () => queuedBranch } });
  assert.equal((await taskList())[0].effective_status, 'question', 'an unanswered link restores from its branch');
  handlers.input({ source: 'interactive', text: 'Continue' }, ctx);
  assert.equal(handlers.agent_before_settle({ outcome: 'completed', context: { canContinue: true } }, ctx), undefined);
  assert.equal(taskSent.length, 0, 'the unanswered question causes no duplicate task reminder');
  const missing = await call(s, { action: 'add', question: 'Unknown?', task_id: 2 });
  assert.match(missing.content[0].text, /task #2 is not open/);

  await taskCall('tasks_progress', { task_id: 1 });
  await taskCall('tasks_add', { summary: 'Second task' });
  await assert.rejects(taskCall('tasks_progress', { task_id: 2 }), /At most 1 task/,
    'the question overlay does not silently free a progress slot');
  await taskCall('tasks_progress', { task_id: 1, active: false });
  await taskCall('tasks_progress', { task_id: 2 });
  assert.equal((await taskList())[0].effective_status, 'question');
  s.choose('#1 [task #1] [waiting] Which result?', 'Answer'); s.answer('Use the small patch.');
  await s.commands.questions.handler('', s.ctx);
  assert.match(s.sent.at(-1)[0], /^Task #1 — queued question/);
  assert.equal((await taskList())[0].status, 'pending');
  assert.equal((await taskList())[0].effective_status, 'pending');
  assert.equal((await taskList())[0].effective_waiting_on, undefined);
  assert.deepEqual((await taskList())[0].question_ids, []);

  await call(s, { action: 'add', question: 'Another?', task_id: 1, wait_for_answer: true });
  assert.equal((await taskList())[0].effective_status, 'question');
  await call(s, { action: 'clear', id: 2 });
  assert.equal((await taskList())[0].effective_status, 'pending');
  await call(s, { action: 'add', question: 'Optional detail?', task_id: 1 });
  assert.equal((await taskList())[0].effective_status, 'pending', 'an ordinary linked question is non-blocking');
  assert.equal((await taskList())[0].effective_waiting_on, undefined);
  assert.deepEqual((await taskList())[0].question_ids, [3]);
  assert.deepEqual((await taskList())[0].waiting_question_ids, []);
  await call(s, { action: 'clear', id: 3 });
  await taskCall('tasks_review', { task_id: 1, reviewer: 'Reviewer', artifact: 'PR #43',
    check_at: new Date(Date.now() + 86_400_000).toISOString() });
  assert.equal((await taskList())[0].effective_status, 'blocked');
  assert.equal((await taskList())[0].effective_waiting_on, 'Reviewer');
  await call(s, { action: 'add', question: 'Reviewer detail?', task_id: 1, wait_for_answer: true });
  assert.equal((await taskList())[0].status, 'blocked');
  assert.equal((await taskList())[0].effective_status, 'question');
  assert.equal((await taskList())[0].effective_waiting_on, 'user');
  await call(s, { action: 'clear', id: 4 });
  assert.equal((await taskList())[0].effective_status, 'blocked', 'clearing the question does not unblock review');
  assert.equal((await taskList())[0].effective_waiting_on, 'Reviewer');

  s.handlers.session_tree({}, { ...s.ctx, sessionManager: { getBranch: () => beforeQuestion } });
  handlers.session_tree({}, { ...ctx, sessionManager: { getBranch: () => beforeQuestion } });
  assert.equal((await taskList())[0].effective_status, 'pending', 'branch without question has no overlay');
  s.handlers.session_tree({}, s.ctx); handlers.session_tree({}, ctx);
  assert.equal((await taskList())[0].effective_status, 'blocked', 'answered/cleared questions stay absent after restore');
});

test('invalid and full queues do not add questions', async () => {
  const s = setup();
  assert.equal((await call(s, { action: 'add', question: '   ' })).details.questions.length, 0);
  assert.match((await call(s, { action: 'add', question: 'Unlinked wait?', wait_for_answer: true })).content[0].text, /requires task_id/);
  assert.match((await call(s, { action: 'add', question: 'Invalid wait?', wait_for_answer: 'yes' })).content[0].text, /must be boolean/);
  assert.equal(s.branch.at(-1).message.details.nextId, 1, 'invalid waiting choices do not consume IDs');
  for (let i = 0; i < 20; i++) await call(s, { action: 'add', question: `Question ${i}?` });
  assert.match((await call(s, { action: 'add', question: 'Extra?' })).content[0].text, /full/);
  assert.equal(s.branch.at(-1).message.details.questions.length, 20);
});

test('only accepted new questions ring; rejection, clearing, restoration and headless modes stay silent', async () => {
  await require('./bell-capture.cjs')(async count => {
    const s = setup();
    assert.equal(count(), 0);
    await call(s, { action: 'add', question: 'New?' });
    assert.equal(count(), 1);
    s.handlers.session_tree({}, s.ctx);
    await call(s, { action: 'add', question: ' ' });
    await call(s, { action: 'add', question: 'Invalid link?', task_id: 999 });
    await call(s, { action: 'clear' });
    assert.equal(count(), 1);
    for (const mode of ['rpc', 'json', 'print']) {
      s.ctx.mode = mode;
      await call(s, { action: 'add', question: mode });
    }
    assert.equal(count(), 1);
  });
});
