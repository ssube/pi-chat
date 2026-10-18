const { test } = require('node:test');
const assert = require('node:assert/strict');
const { existsSync, mkdtempSync } = require('node:fs');
const { tmpdir } = require('node:os');
const path = require('node:path');
const { pathToFileURL } = require('node:url');
const { createRequire } = require('node:module');

// Optional contract test against an explicitly selected Pi SDK. No user config,
// credentials, extensions, network, or inference are used. The model stream is fake.
test('Pi SDK: real settlement boundary continues early stops, injects task context, and registers /tasks', {
  skip: !process.env.PI_TASKS_SDK_MODULE,
}, async () => {
  const sdkPath = path.resolve(process.env.PI_TASKS_SDK_MODULE);
  const sdk = await import(pathToFileURL(sdkPath).href);
  const sdkRequire = createRequire(sdkPath);
  const aiPath = sdkRequire.resolve.paths('@earendil-works/pi-ai').map(dir => path.join(dir, '@earendil-works/pi-ai/dist/index.js')).find(existsSync);
  assert.ok(aiPath, 'Pi SDK must have pi-ai installed');
  const ai = await import(pathToFileURL(aiPath).href);
  const dir = mkdtempSync(path.join(tmpdir(), 'pi-tasks-offline-'));
  const settingsManager = sdk.SettingsManager.inMemory({ compaction: { enabled: false }, retry: { enabled: false } });
  const runtime = await sdk.ModelRuntime.create({ authPath: path.join(dir, 'auth.json'), modelsPath: null, modelsStorePath: path.join(dir, 'models.json'), refreshOnCreate: false });
  const model = runtime.getModels()[0]; assert.ok(model, 'SDK must have a static chat model');
  runtime.getAuth = async () => ({ auth: { apiKey: 'offline-test-not-a-credential' }, env: {} });
  runtime.checkAuth = async () => ({ authenticated: true });
  const resourceLoader = new sdk.DefaultResourceLoader({
    cwd: dir, agentDir: dir, settingsManager,
    noExtensions: true, noSkills: true, noPromptTemplates: true, noThemes: true, noContextFiles: true,
    additionalExtensionPaths: [path.resolve(__dirname, '../extensions/tasks.ts')],
  });
  await resourceLoader.reload();
  assert.deepEqual(resourceLoader.getExtensions().errors, []);
  const manager = sdk.SessionManager.inMemory(dir);
  manager.appendCustomEntry('tasks-state-v1', { version: 1, nextId: 2, paused: false,
    tasks: [{ id: 1, summary: 'Finish the example', acceptance: 'Example has verified output', status: 'pending' }] });
  const { session } = await sdk.createAgentSession({ cwd: dir, agentDir: dir, modelRuntime: runtime, model,
    resourceLoader, sessionManager: manager, settingsManager, tools: ['tasks_list', 'tasks_add', 'tasks_complete', 'tasks_block', 'tasks_unblock', 'tasks_progress', 'tasks_summary'] });
  const requests = [], errors = [], asideRequests = []; let nextTool;
  runtime.complete = async (_model, context) => {
    asideRequests.push(context);
    return { role: 'assistant', content: [{ type: 'text', text: 'Task #1 remains pending; first inspect the missing output.' }],
      stopReason: 'stop', usage: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } } };
  };
  session.subscribe(event => { if (event.type === 'extension_error') errors.push(event); });
  session.agent.streamFunction = (_model, context) => {
    requests.push(context);
    const stream = ai.createAssistantMessageEventStream();
    const message = { role: 'assistant', content: [{ type: 'text', text: 'Stopping prematurely.' }],
      api: model.api, provider: model.provider, model: model.id, stopReason: 'stop', timestamp: Date.now(),
      usage: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } } };
    if (nextTool) { message.content = [nextTool]; message.stopReason = 'toolUse'; nextTool = undefined; }
    stream.push({ type: 'done', reason: message.stopReason, message }); stream.end(); return stream;
  };
  try {
    await session.bindExtensions({});
    assert.ok(session.getAllTools().some(tool => tool.name === 'tasks_complete'));
    await session.prompt('Continue the pending task.');
    assert.equal(requests.length, 3, 'initial request plus exactly two bounded continuations');
    for (const request of requests) assert.match(JSON.stringify(request), /Finish the example/);
    assert.equal(manager.getBranch().filter(entry => entry.type === 'custom_message' && entry.customType === 'tasks-reminder-v1').length, 2);
    assert.deepEqual(errors, []);
    // Direct stop requests must not receive an automatic continuation.
    await session.prompt('Please stop working for now.');
    assert.equal(requests.length, 4);
    // Invoke the actual slash-command path in headless mode: it must not send inference.
    await session.prompt('/tasks'); assert.equal(requests.length, 4);
    await session.prompt('/tasks-send'); await session.waitForIdle();
    const listMessage = manager.getBranch().find(entry => entry.type === 'custom_message' && entry.customType === 'tasks-list');
    assert.ok(listMessage); assert.match(listMessage.content, /- \[ \] #1 Finish the example/);
    assert.equal(requests.length, 5, 'list steering starts an idle main agent');
    await session.prompt('/tasks-summary What blocks the next step?'); await session.waitForIdle();
    assert.equal(asideRequests.length, 1); assert.equal(asideRequests[0].tools, undefined);
    assert.equal(JSON.parse(asideRequests[0].messages[0].content).question, 'What blocks the next step?');
    assert.equal(requests.length, 6, 'summary slash command steers an idle main agent');
    assert.match(JSON.stringify(requests.at(-1)), /Task #1 remains pending/);
    nextTool = { type: 'toolCall', id: 'aside-tool', name: 'tasks_summary', arguments: { prompt: 'What help do we need?' } };
    await session.prompt('Use the summary tool for help.');
    assert.equal(asideRequests.length, 2);
    const summaries = manager.getBranch().filter(entry => entry.type === 'custom_message' && entry.customType === 'tasks-summary');
    assert.equal(summaries.length, 2, 'slash and agent tool both publish visible summary messages');
    assert.ok(summaries.every(entry => entry.display === true));
    assert.ok(JSON.parse(asideRequests[1].messages[0].content).recent_messages.some(message => /Use the summary tool/.test(message.text)));
    const latestState = manager.getBranch().filter(entry => entry.type === 'custom' && entry.customType === 'tasks-state-v1').at(-1).data;
    assert.equal(latestState.tasks[0].status, 'pending', 'advice never completes a task');
    nextTool = { type: 'toolCall', id: 'block-tool', name: 'tasks_block', arguments: { task_id: 1, reason: 'Waiting for prerequisite' } };
    await session.prompt('Block the task.');
    assert.equal(manager.getBranch().filter(entry => entry.type === 'custom' && entry.customType === 'tasks-state-v1').at(-1).data.tasks[0].status, 'blocked');
    nextTool = { type: 'toolCall', id: 'unblock-tool', name: 'tasks_unblock', arguments: { task_id: 1 } };
    await session.prompt('The prerequisite is resolved; unblock the task.');
    const resumed = manager.getBranch().filter(entry => entry.type === 'custom' && entry.customType === 'tasks-state-v1').at(-1).data.tasks[0];
    assert.equal(resumed.status, 'pending'); assert.equal(resumed.blocker, undefined);
    nextTool = { type: 'toolCall', id: 'progress-tool', name: 'tasks_progress', arguments: { task_id: 1 } };
    await session.prompt('Start working on the task.');
    assert.equal(manager.getBranch().filter(entry => entry.type === 'custom' && entry.customType === 'tasks-state-v1').at(-1).data.tasks[0].status, 'progress');
    assert.deepEqual(errors, []);
  } finally { session.dispose(); }
});
