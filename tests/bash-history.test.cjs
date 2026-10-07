const { test } = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const { createJiti } = require(require.resolve('jiti', { paths: [path.resolve(__dirname, '../extensions')] }));
const mockPath = path.resolve(__dirname, 'pi-shell-mock.cjs');
const shell = require(mockPath);
const extension = createJiti(__filename, { alias: { '@earendil-works/pi-coding-agent': mockPath } })(path.resolve(__dirname, '../extensions/bash-history.ts')).default;

function setup(branch = []) {
  shell.setExecutor(async () => ({ exitCode: 0 }));
  const handlers = {}, tools = {}, commands = {}, sent = [], notices = [], inserts = [];
  let factory, draft = '', selection, approve = () => true;
  const ui = {
    addAutocompleteProvider(fn) { factory = fn; },
    notify(message) { notices.push(message); },
    select: async (_title, choices) => selection === undefined ? choices[0] : selection,
    confirm: async (...args) => approve(...args),
    getEditorText: () => draft,
    setEditorText(text) { draft = text; inserts.push(text); },
  };
  extension({
    on(name, handler) { handlers[name] = handler; },
    getSettings: () => ({}),
    sendUserMessage(message) { sent.push(message); },
    registerTool(tool) { tools[tool.name] = tool; },
    registerCommand(name, command) { commands[name] = command; },
  });
  const ctx = { mode: 'tui', sessionManager: { getBranch: () => branch, getLeafId: () => branch.at(-1)?.id ?? null }, ui };
  handlers.session_start({}, ctx);
  const idle = { ...ctx, isIdle: () => true, hasPendingMessages: () => false };
  return {
    handlers, tools, commands, ctx, idle, ui, branch, sent, notices, inserts,
    setDraft(value) { draft = value; },
    getDraft() { return draft; },
    setSelection(value) { selection = value; },
    setApproval(fn) { approve = fn; },
    getProvider: () => factory({
      getSuggestions: async () => null,
      applyCompletion: () => { throw Error('unexpected fallback'); },
      shouldTriggerFileCompletion: () => true,
    }),
  };
}

const opts = { signal: new AbortController().signal, force: true };
const propose = (s, command, reason = 'Inspect safely') => s.tools.suggest_shell_command.execute('id', { command, reason });

async function runBash(s, command, options = {}) {
  const event = { command, excludeFromContext: false };
  const operations = s.handlers.user_bash(event, s.idle)?.operations;
  assert.ok(operations, 'ordinary idle ! uses wrapped local operations');
  const previous = s.ctx.sessionManager.getLeafId();
  const result = await operations.exec(command, '/working/directory', { onData: () => {} });
  if (!options.noRecord) {
    s.branch.push({ id: `bash${s.branch.length}`, parentId: previous, type: 'message',
      message: { role: 'bashExecution', command, output: options.output ?? 'observed output',
        exitCode: result.exitCode, cancelled: options.cancelled ?? false } });
  }
  await new Promise(setImmediate);
  return result;
}

test('history is branch-local, deduplicated, capped, and excludes !!', async () => {
  const s = setup([
    { type: 'message', message: { role: 'bashExecution', command: 'git status', excludeFromContext: false } },
    { type: 'message', message: { role: 'bashExecution', command: 'private', excludeFromContext: true } },
  ]);
  const provider = s.getProvider();
  const suggestions = await provider.getSuggestions(['! git s'], 0, 7, opts);
  assert.deepEqual(suggestions.items.map(item => item.value), ['git status']);
  assert.deepEqual(provider.applyCompletion(['! git s'], 0, 7, suggestions.items[0], suggestions.prefix), {
    lines: ['! git status'], cursorLine: 0, cursorCol: 12,
  });
  for (const [lines, col] of [[['!! private'], 10], [['! private'], 9], [['normal text'], 11], [['! git s', 'more'], 7]]) {
    assert.equal(await provider.getSuggestions(lines, 0, col, opts), null);
  }
  assert.equal(provider.shouldTriggerFileCompletion(['! git'], 0, 5), true);
  s.handlers.user_bash({ command: 'git status', excludeFromContext: false }, { isIdle: () => false });
  s.handlers.user_bash({ command: 'git log', excludeFromContext: false }, { isIdle: () => false });
  assert.deepEqual((await provider.getSuggestions(['! git'], 0, 5, opts)).items.map(item => item.value), ['git log', 'git status']);
  for (let i = 0; i < 110; i++) s.handlers.user_bash({ command: `test-${i}`, excludeFromContext: false }, { isIdle: () => false });
  assert.equal(await provider.getSuggestions(['! test-0'], 0, 8, opts), null);
  s.handlers.session_tree({}, { ...s.ctx, sessionManager: { getBranch: () => [] } });
  assert.equal(await provider.getSuggestions(['! git'], 0, 5, opts), null);
});

test('proposal records without execution, deduplicates and bounds the queue', async () => {
  const s = setup();
  const first = await propose(s, 'git status', 'Check status');
  assert.deepEqual(first.details.proposals, [{ command: 'git status', reason: 'Check status' }]);
  assert.equal(s.getDraft(), '');
  assert.deepEqual(s.sent, []);
  assert.equal(await s.getProvider().getSuggestions(['! git s'], 0, 7, opts), null);
  await propose(s, 'git status', 'Updated reason');
  assert.equal((await propose(s, 'git log')).details.proposals.length, 2);
  assert.equal((await propose(s, 'git status', 'Updated reason')).details.proposals[0].reason, 'Updated reason');
  for (let i = 0; i < 21; i++) await propose(s, `echo ${i}`);
  const last = await propose(s, 'echo 21');
  assert.equal(last.details.proposals.length, 20);
  assert.equal(last.details.proposals[0].command, 'echo 21');
  assert.ok(!last.details.proposals.some(p => p.command === 'git status'));
});

test('rejects empty, multiline, control-character, leading-!, or oversized proposals', async () => {
  const s = setup();
  for (const [command, reason] of [
    ['', 'why'], ['echo hi\nrm file', 'why'], ['! echo hi', 'why'], ['echo \x1b[31m', 'why'],
    ['echo hi', 'reason\nspoof'], ['echo hi', '  '], ['x'.repeat(4097), 'why'], ['echo hi', 'r'.repeat(501)],
  ]) {
    const result = await propose(s, command, reason);
    assert.match(result.content[0].text, /rejected/);
    assert.deepEqual(result.details.proposals, []);
  }
});

test('picker reviews full text and reason; cancellation and draft replacement are safe', async () => {
  const s = setup();
  await s.commands['shell-suggestions'].handler('', s.ctx);
  assert.match(s.notices[0], /No shell command/);
  await propose(s, 'git diff --stat', 'Inspect changes');
  s.setSelection(undefined);
  s.setDraft('draft');
  s.setApproval(() => false);
  await s.commands['shell-suggestions'].handler('', s.ctx);
  assert.equal(s.getDraft(), 'draft');
  assert.deepEqual(s.inserts, []);
  s.setDraft('');
  await s.commands['shell-suggestions'].handler('', s.ctx);
  assert.deepEqual(s.inserts, []);
  const reviews = [];
  s.setApproval((title, body) => { reviews.push([title, body]); return true; });
  await s.commands['shell-suggestions'].handler('', s.ctx);
  assert.match(reviews[0][1], /git diff --stat/);
  assert.match(reviews[0][1], /Inspect changes/);
  assert.deepEqual(s.inserts, ['! git diff --stat']);
  assert.deepEqual(s.sent, []);
});

test('proposals restore only from the active branch; non-TUI does not insert', async () => {
  const s = setup();
  const result = await propose(s, 'git status');
  s.handlers.session_tree({}, { ...s.ctx, sessionManager: { getBranch: () => [
    { type: 'message', message: { role: 'toolResult', toolName: 'suggest_shell_command', details: result.details } },
  ] } });
  await s.commands['shell-suggestions'].handler('', s.ctx);
  assert.deepEqual(s.inserts, ['! git status']);
  s.handlers.session_tree({}, { ...s.ctx, sessionManager: { getBranch: () => [] } });
  await s.commands['shell-suggestions'].handler('', s.ctx);
  assert.equal(s.inserts.length, 1);
  await s.commands['shell-suggestions'].handler('', { ...s.ctx, mode: 'rpc' });
  assert.equal(s.inserts.length, 1);
});

test('completed ! result triggers exactly one turn after recording, even on nonzero exit', async () => {
  const s = setup();
  assert.deepEqual(s.sent, []);
  await runBash(s, 'echo hello');
  assert.equal(s.sent.length, 1);
  assert.match(s.sent[0], /immediately preceding shell command/);
  shell.setExecutor(async () => ({ exitCode: 7 }));
  await runBash(s, 'false');
  assert.equal(s.sent.length, 2);
  assert.equal(s.branch.at(-1).message.exitCode, 7);
});

test('!!, off toggle, busy agent, queued message, and missing result skip follow-up', async () => {
  const s = setup();
  assert.equal(s.handlers.user_bash({ command: 'secret', excludeFromContext: true }, s.idle), undefined);
  assert.equal(s.handlers.user_bash({ command: 'echo hi', excludeFromContext: false }, { ...s.idle, isIdle: () => false }), undefined);
  assert.equal(s.handlers.user_bash({ command: 'echo hi', excludeFromContext: false }, { ...s.idle, hasPendingMessages: () => true }), undefined);
  await s.commands['shell-followup'].handler('off', s.ctx);
  assert.equal(s.handlers.user_bash({ command: 'echo hi', excludeFromContext: false }, s.idle), undefined);
  assert.match(s.notices.at(-1), /off/);
  await s.commands['shell-followup'].handler('on', s.ctx);
  await runBash(s, 'echo hi', { noRecord: true });
  assert.deepEqual(s.sent, []);
});

test('cancelled, failed, stale, or racing command never triggers a turn', async () => {
  const s = setup();
  await runBash(s, 'echo hi', { cancelled: true });
  assert.deepEqual(s.sent, []);
  shell.setExecutor(async () => { throw Error('spawn failed'); });
  const wrapped = s.handlers.user_bash({ command: 'missing', excludeFromContext: false }, s.idle);
  await assert.rejects(wrapped.operations.exec('missing', '/cwd', { onData: () => {} }), /spawn failed/);
  await new Promise(setImmediate);
  assert.deepEqual(s.sent, []);
  shell.setExecutor(async () => ({ exitCode: 0 }));
  const stale = s.handlers.user_bash({ command: 'echo race', excludeFromContext: false }, s.idle);
  await stale.operations.exec('echo race', '/cwd', { onData: () => {} });
  s.branch.push({ type: 'message', id: 'other', parentId: 'different', message: { role: 'bashExecution', command: 'echo race', cancelled: false } });
  await new Promise(setImmediate);
  assert.deepEqual(s.sent, []);
});
