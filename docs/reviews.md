# PR review queue

Review automation is independent of meetings. A Matrix profile can declare `pr_review_rooms` and a `hookshot_user_id`. Only that sender's text events with exactly the configured reviewer's Matrix ID in structured mentions enter the queue. Configure Hookshot to transform authenticated Forgejo `review_requested` webhooks into that shape; titles and body text are not authority to select a reviewer.

The `reviews` profile section defines:

- `base_url`: credential-free HTTPS Forgejo origin (an installation prefix is allowed).
- `repositories`: allowed `owner/repository` pairs.
- `reviewer`: Forgejo login used to check review requests and submitted reviews.
- `token_env`: environment variable containing that account's scoped API token.
- `command`: argv array beginning with an absolute executable path.
- `work_dir`: trusted working directory for the review command.
- `pass_env`: explicit environment names the command needs, including its publishing credential if appropriate.

The worker invokes `command + ["--repo", "owner/repository", "--pr", "123"]`, without shell interpolation. It also supplies `REVIEW_BASE_URL`, `REVIEW_REVIEWER`, and `REVIEW_ROOM_ID`. It inherits basic process settings and only explicitly listed extra variables. The command is responsible for performing and posting the review under its own identity. This boundary lets an installation use its existing reviewer without a separate orchestration framework. A model-backed review command can consume inference credits and publish externally; configure it deliberately.

The queue itself does not invent a successful review when a runner is missing. Review rooms require complete runner configuration. The runner must fetch the exact current head, inspect the diff and relevant code, publish an attributable verdict tied to that head, and recheck for pushes immediately before posting. Do not run PR-supplied instructions as trusted operational guidance. `skills/review-pr` describes these responsibilities.

Queued jobs persist in owner-only SQLite storage. They serialize against ordinary chat turns across transports. Admission checks the approved server/repository, live requested reviewer, PR author, source/base repository, and base branch (`main`). Fork PRs, self-reviews, closed PRs, and unrequested work are ineligible. The worker reads bounded review-request history and existing reviews, handles re-requests, and rechecks head/request generation after the command exits.

An initial eye reaction means work started. A checkmark requires Forgejo to confirm an APPROVED review of the current head after the latest request. COMMENT and REQUEST_CHANGES can complete work without that checkmark. A zero exit status alone is insufficient.

Failed attempts retry with bounded backoff. Private failure artifacts retain bounded, redacted stdout/stderr tails and hashes; a broken redaction configuration suppresses both streams. Retention is bounded by age, count, and bytes. Raw details are never embedded in the generic retry notice.

Ordinary configured runtime CI notices can be admitted separately through `hookshot_notice_rooms`, with the same explicitly configured sender identity. They do not require or start meetings.

## Bundled Pi reviewer

`python -m tools.review_pr --config "$REVIEWER_CONFIG" --repo team/project --pr 123` implements the runner interface. Start from `examples/reviewer.json`, stored privately at mode `0600`. It verifies the token identity, fetches the exact base/head into an allowlisted local source repository, and creates separate detached checkouts. Pi runs from the base checkout and inspects the head checkout/diff. Its publishing token is excluded from the child environment; same-user filesystem access is not a credential sandbox.

The runner requires a valid VERDICT line, checks for obvious private/credential disclosures, rechecks the PR head before posting, and verifies submission state. Dirty worktrees or an oversized diff stop the attempt. A direct invocation is an explicit request to review; queued invocations also receive the queue's requested-reviewer/generation checks. For queue configuration, use an absolute Python executable followed by `-m`, `tools.review_pr`, `--config`, and the absolute private configuration path, with this repository as `work_dir`.

For Hookshot generic-webhook v2, `tools/hookshot-review-transform.js` supplies `transformReview(data, config)`. Include that function in the installed transformation and assign `result = transformReview(data, config)`. Keep `config` private: it contains `baseUrl`, allowed `repositories`, and a `reviewerIds` mapping from Forgejo login to full Matrix user ID. Unknown reviewers, other event actions, unapproved repositories, and mismatched URLs produce an empty result.
