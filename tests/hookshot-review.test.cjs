const { test } = require('node:test');
const assert = require('node:assert/strict');
const { transformReview } = require('../tools/hookshot-review-transform.js');
test('review webhook requires configured identity, repository and exact server URL', () => {
  const config = { baseUrl: 'https://git.example.invalid', repositories: ['team/project'],
    reviewerIds: { reviewer: '@reviewer:example.invalid' } };
  const data = { action: 'review_requested', requested_reviewer: { login: 'reviewer' },
    repository: { full_name: 'team/project' }, number: 1,
    pull_request: { title: 'Change\nwith newline', html_url: 'https://git.example.invalid/team/project/pulls/1' } };
  assert.deepEqual(transformReview(data, config).mentions, { user_ids: ['@reviewer:example.invalid'] });
  assert.equal(transformReview({ ...data, requested_reviewer: { login: 'unknown' } }, config).empty, true);
  assert.equal(transformReview({ ...data, action: 'opened' }, config).empty, true);
  assert.equal(transformReview({ ...data, pull_request: { ...data.pull_request, html_url: 'https://other.example.invalid/team/project/pulls/1' } }, config).empty, true);
});
