# Optional session assistants

Both extensions are opt-in and target the repository's Pi 0.99.1 SDK. Install
extension dependencies as described in [installation](installation.md). No saved
model defaults, provider accounts or running services are changed by this repo.

## Model approval

Load `extensions/model-control.ts` explicitly. `model_control` supports:

- `current`: selected model, capabilities, catalog pricing and thinking level.
- `list`: authenticated models, with favorite/selected flags; optional exact
  `provider` and case-insensitive `filter` narrow the list.
- `select`: exact `provider`, `model` and `reason`; target must be a session
  scoped model (favorite), including when changing providers.

Current/list work headlessly. Selection requires the local TUI approval menu:
**Approve**, **Deny**, or **Send to judge**. Dismissal, Escape or `q` denies;
`a`, `d`, `j` are local shortcuts, subordinate to configured navigation.
The bounded menu shows source/target, context capacity, catalog pricing and
provider-transfer warnings. Subsequent requests can send existing context to
the new provider. Catalog pricing is metadata, not a billing guarantee.

Only explicitly choosing **Send to judge** makes an extra isolated, tool-free
request to the current model. Evidence includes source/target metadata, reason,
context usage and up to three recent user messages (2,000 characters each), not
tool results or files. Prose can still contain private information. The judge
has a 60-second deadline and 1,024-token output cap. Only a valid `accepted`
verdict allows switching; failure, uncertainty, cancellation or a stale
model/session leaves selection unchanged. `/model-judge-cancel` cancels judging.
A current `-short` alias is resolved to its original for the isolated judge call;
see [short-context aliases](short-context-models.md).

Human approval or delegated judging never grants new spending or disclosure
permission. The tool does not save defaults or purchase anything.

## Draft the next prompt

Load `extensions/next-prompt/index.ts` with an explicitly chosen configured model:

```sh
PI_SUGGEST_PROVIDER="your-provider" PI_SUGGEST_MODEL="your-model" \
  pi --extension ./extensions/next-prompt/index.ts
```

No model is selected by default. `/suggest-next` runs only in the TUI, with an
empty editor. It sends the session name and up to 25 recent user/assistant text
messages from the active branch (800 characters each, 12,000 total) to that
model, then inserts a draft of at most 800 characters **without submitting it**.
Edit or discard the draft. Tools, thinking, images and fenced code are omitted;
redaction is best-effort and prose/session names may still disclose private data.
Use only with existing provider, spending and disclosure authorization.

`/suggest-next cancel` cancels drafting. Duplicate requests do not start another
call. Requests have a 30-second deadline and 300-token output cap; navigation,
shutdown, a changed session or an edited draft prevents stale insertion.
Cancellation, timeout and provider failure leave the editor unchanged. Merely
loading the extension makes no inference request.

## Offline checks

```sh
npm ci --prefix extensions --ignore-scripts
npm ci --prefix tests/pi-sdk --ignore-scripts
node --test tests/model-control.test.mjs extensions/next-prompt/*.test.mjs
```

Tests use the pinned public SDK loader and mocked model/UI calls. They do not
perform inference or change a live session.
