import test from 'node:test';
import assert from 'node:assert/strict';
import { redactText } from '../../common/redact.mjs';
import { parseTopic, recentActivity, topicPrompt } from '../topic.mjs';
import { scenarios } from './scenarios.mjs';

test('shared redactor masks Slack tokens as well as capture credentials', () => {
  assert.equal(redactText('xoxb-1234567890123456'), '[REDACTED]');
});

test('fixture activity is bounded, newest-task weighted and excludes logs and arguments', () => {
  const shift = recentActivity(scenarios[0].entries);
  assert.match(shift, /session topic updater/);
  assert.match(shift, /tools: write/);
  assert.doesNotMatch(shift, /private.ts|token:|secret/);
  const investigation = recentActivity(scenarios[1].entries);
  assert.doesNotMatch(investigation, /secret database output|cat \/home/);
  const privateInput = recentActivity(scenarios[3].entries);
  assert.doesNotMatch(privateInput, /alice@|secret123|abc123|\/home\/alice|example.invalid/);
  assert.match(privateInput, /\[url omitted\]/);
  assert.match(topicPrompt(shift), /<activity>/);
});

test('rejects malformed, unsafe and oversized provider outputs', () => {
  assert.equal(parseTopic('```json\n{}\n```'), null);
  assert.equal(parseTopic('{"name":"","topic":"ok","label":"ok"}'), null);
  assert.equal(parseTopic(JSON.stringify({ name: 'x'.repeat(49), topic: 'safe', label: 'safe' })), null);
  assert.equal(parseTopic(JSON.stringify({ name: 'Safe work', topic: 'safe', label: 'https://example.invalid' })), null);
  assert.deepEqual(parseTopic('{"name":"Validate event IDs","topic":"Duplicate event validation","label":"Event IDs"}'), {
    name: 'Validate event IDs', topic: 'Duplicate event validation', label: 'Event IDs',
  });
});
