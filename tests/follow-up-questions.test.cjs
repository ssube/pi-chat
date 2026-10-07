const { test } = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const { createJiti } = require(require.resolve('jiti', { paths: [path.resolve(__dirname, '../extensions')] }));
const extension = createJiti(__filename)(path.resolve(__dirname, '../extensions/follow-up-questions.ts')).default;

function setup(branch = []) {
  const handlers = {}, tools = {}, commands = {}, sent = [], widgets = [];
  let selections = [], answer, confirmed = true;
  const ctx = {
    mode: 'tui', sessionManager: { getBranch: () => branch },
    ui: {
      setWidget: (key, value) => widgets.push(value),
      select: async () => selections.shift(),
      editor: async () => answer,
      confirm: async () => confirmed,
      notify() {},
    },
  };
  extension({
    on: (name, handler) => { handlers[name] = handler; },
    registerTool: (tool) => { tools[tool.name] = tool; },
    registerCommand: (name, cmd) => { commands[name] = cmd; },
    appendEntry: (customType, data) => branch.push({ type: 'custom', customType, data }),
    sendUserMessage: async (...args) => sent.push(args),
  });
  handlers.session_start({}, ctx);
  return { ctx, handlers, tools, commands, branch, sent, widgets,
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

test('invalid and full queues do not add questions', async () => {
  const s = setup();
  assert.equal((await call(s, { action: 'add', question: '   ' })).details.questions.length, 0);
  for (let i = 0; i < 20; i++) await call(s, { action: 'add', question: `Question ${i}?` });
  assert.match((await call(s, { action: 'add', question: 'Extra?' })).content[0].text, /full/);
  assert.equal(s.branch.at(-1).message.details.questions.length, 20);
});
