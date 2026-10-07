// Reusable Hookshot generic-webhook v2 transformation. Supply private config
// in the installed wrapper; source contains no account or server configuration.
function transformReview(data, config) {
  const reviewer = data?.requested_reviewer?.login;
  const userId = typeof reviewer === 'string' ? config.reviewerIds?.[reviewer] : undefined;
  const repo = data?.repository?.full_name;
  const number = data?.number;
  const title = data?.pull_request?.title;
  const url = data?.pull_request?.html_url;
  if (data?.action !== 'review_requested' || typeof userId !== 'string' ||
      !/^@[^:]+:.+$/.test(userId) || !config.repositories?.includes(repo) ||
      !Number.isSafeInteger(number) || number < 1 || typeof title !== 'string' ||
      typeof config.baseUrl !== 'string' || !config.baseUrl.startsWith('https://') ||
      url !== `${config.baseUrl.replace(/\/$/, '')}/${repo}/pulls/${number}`) {
    return { version: 'v2', empty: true };
  }
  return { version: 'v2', msgtype: 'm.text',
    plain: `Review requested for ${repo} #${number}: ${title.replace(/[\r\n\t]+/g, ' ').slice(0, 160)}\n${url}`,
    mentions: { user_ids: [userId] }, hints: { showUrlPreviews: false } };
}

if (typeof module !== 'undefined') module.exports = { transformReview };
