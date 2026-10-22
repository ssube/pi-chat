# Short-context model aliases

`extensions/short-context-models.ts` adds opt-in half-context copies of existing chat models to Pi's model picker. No provider or model is selected by default. It does not change your current model or the original model definitions.

## Setup

Load the extension through your existing Pi extension settings, or try it with:

```sh
pi -e /path/to/pi-chat/extensions/short-context-models.ts
```

Create `short-context-models.json` in Pi's agent directory (as resolved by `getAgentDir()`, including Pi's agent-directory override). Use actual provider/model IDs from your local Pi catalog in place of these placeholders:

```json
[
  { "provider": "your-provider", "id": "your-model" }
]
```

Restart or reload extensions after changing the file. Each selection adds `your-model-short`, displayed as the original name followed by `(Short Context)`. Select that alias through Pi's normal model picker. Missing configuration or an empty array is a no-op: no model runtime is created. Malformed JSON, duplicate entries, unknown fields, unavailable models/providers, alias collisions and invalid source context windows fail loading before any provider is registered.

Requires a Pi SDK exposing `getAgentDir`, `ModelRuntime.create`, and native `registerProvider(provider)`, plus provider `stream`/`streamSimple` payload callbacks. Older SDKs without those interfaces are not supported. Models must already be present in the local runtime; models supplied only by another extension are not discovered by this separate runtime.

## Behavior and boundaries

- The alias advertises `floor(original.contextWindow / 2)` tokens. Its output-token limit is capped at that window; pricing, capabilities, endpoint and other model metadata are retained.
- Both provider streaming entrypoints route to the canonical model ID, including providers that encode IDs in request URLs. Caller options and asynchronous payload callbacks are preserved; the final JSON payload model ID is canonicalized after the callback. Original-model requests pass through untouched.
- Wrapping the provider boundary covers direct registry requests and compaction, rather than relying on session-only request events. Other provider operations are left unchanged.
- Runtime initialization disables model-catalog network refresh. It uses local Pi model/auth paths; no credentials, private paths, provider endpoints or selected models are shipped in this repository. Loading does not submit inference. Selecting and using an alias still uses your existing provider credentials and can incur the same charges as the original model.
- This is a smaller **client-side context budget**, not a different model, cheaper pricing tier, privacy filter or hard server-side token limit. It does not truncate an existing conversation on load. Pi's normal context management determines compaction; callers must still respect token limits and provider/spending permissions.

Tests use synthetic catalogs, a fake SDK, and temporary configuration files. They require no real credentials or paid inference:

```sh
npm ci --prefix extensions --ignore-scripts
node --test tests/short-context-models.test.cjs
```
