import assert from 'node:assert/strict';
import { test } from 'node:test';
import { recentConversation, suggestionPrompt } from './context.mjs';

const entry = (role, content) => ({ type: 'message', message: { role, content } });
const text = value => [{ type: 'text', text: value }];

test('uses only the last 25 user and assistant prose messages', () => {
  const entries = [entry('user', text('old')),
    ...Array.from({ length: 25 }, (_, i) => entry(i % 2 ? 'assistant' : 'user', text(`message ${i}`))),
    entry('toolResult', text('secret result')),
    entry('assistant', [{ type: 'thinking', thinking: 'secret thought' }, { type: 'toolCall', name: 'bash', arguments: { command: 'secret input' } }])];
  const result = recentConversation(entries);
  assert.equal(result.split('\n').length, 25);
  assert.doesNotMatch(result, /old|secret/);
  assert.match(result, /message 24/);
});

test('omits code and common credentials, bounds input, and handles empty history', () => {
  assert.equal(recentConversation([entry('toolResult', text('only tools'))]), '');
  const result = recentConversation([entry('user', text('task ```\nsk-example-in-code\n``` Bearer abcdefghijklmnop TOKEN=hiddenvalue MY_TOKEN=hiddenvalue DB_PASSWORD=hunter2 AWS_SECRET_ACCESS_KEY=anothersecret https://example.org/?api_key=moresecret ' + 'x'.repeat(2000)))]);
  assert.match(result, /\[code omitted\]/);
  assert.match(result, /Bearer \[redacted\]/);
  assert.match(result, /TOKEN=\[redacted\]/);
  assert.match(result, /MY_TOKEN=\[redacted\]/);
  assert.match(result, /DB_PASSWORD=\[redacted\]/);
  assert.match(result, /AWS_SECRET_ACCESS_KEY=\[redacted\]/);
  assert.match(result, /api_key=\[redacted\]/);
  assert.ok(result.length <= 806);
  assert.doesNotMatch(result, /sk-example-in-code|abcdefghijklmnop|hiddenvalue|hunter2|anothersecret|moresecret/);
});

test('prompt includes a bounded, redacted topic and current conversation', () => {
  const result = suggestionPrompt('Fix tests MY_TOKEN=hiddenvalue ' + 'x'.repeat(300), 'user: What next?');
  const topic = result.match(/Session topic: (.*)\n/)[1];
  assert.match(topic, /Fix tests MY_TOKEN=\[redacted\]/);
  assert.ok(topic.length <= 200);
  assert.doesNotMatch(result, /hiddenvalue/);
  assert.match(result, /user: What next\?/);
});
