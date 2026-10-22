# Pi extensions

Load only the entrypoints you need, not this entire directory. Dependencies are installed with `npm ci --prefix extensions --ignore-scripts` from the repository root; a compatible Pi SDK must also be available. See [installation](../docs/installation.md) and [component details](../docs/components.md).

**Interactive** means Pi's terminal UI (`ctx.mode === "tui"`). **Headless** means non-TUI sessions, including bridge/RPC sessions. **Both** describes the core behavior, not a promise that interactive menus are available over RPC. Nothing here is installed or enabled by updating this repository.

| Entrypoint | Useful context | Purpose and limitations |
| --- | --- | --- |
| [`pi-time.ts`](pi-time.ts) | **Both** | `clock` and abortable `sleep_until`; headless callers receive tool progress/results, while the TUI can display a countdown status. |
| [`short-context-models.ts`](short-context-models.ts) | **Both** | Adds half-context aliases for explicitly selected existing models. TUI users can choose them in the model picker; headless callers can select the registered aliases through their normal model-selection mechanism. Missing local config is a no-op. No inference on load; using an alias still uses the original provider/pricing. See [setup and limits](../docs/short-context-models.md). |
| [`tasks.ts`](tasks.ts) | **Both** | Branch-persistent task tools, context injection, continuation reminders, evidence judging and advisory summaries. `/tasks` editing requires TUI; tools and ledger behavior work headlessly. Judging/summaries use the selected model and can incur charges. See [task ledger](../docs/tasks.md). |
| [`background-jobs.ts`](background-jobs.ts) | **Both** | Start, inspect and cancel local systemd jobs through tools; completion can steer the session. `/jobs` menu and widgets require TUI. Requires Linux, a working user systemd manager and a Node-compatible runtime; not a generic remote-job runner. Commands execute with the user's existing access. See [runbook](../docs/background-jobs.md). |
| [`web-search/index.ts`](web-search/index.ts) | **Both** | `web_search` and `web_read`; no UI required. Search uses the existing Codex CLI authentication or a configured SearXNG endpoint. Search may incur provider costs; reading fetches public URLs and treats returned content as untrusted. See [web behavior](../docs/components.md). |
| [`observability/pi-capture/index.ts`](observability/pi-capture/index.ts) | **Both** | Records redacted tool outcomes, durations and skill events; supports reviewed fix notifications and optional structured matching. Requires explicit capture agent/origin configuration; records remain private runtime data, and redaction is not a disclosure guarantee. Optional service integrations require deliberate configuration. See [ledger](../docs/ledger.md). |
| [`follow-up-questions.ts`](follow-up-questions.ts) | **Interactive-first; partial headless** | `queue_question` can add/clear persistent optional questions in either context, but the `/questions` answer/clear menu and count widget are TUI-only. Headless use does not supply a user-answer interface. Never use this queue for required approval. |
| [`bash-history.ts`](bash-history.ts) | **Interactive-first; partial headless** | Completes native `!` shell commands, reviews proposed commands, and can request agent follow-up after shell execution. Completion and `/shell-suggestions` insertion require TUI. The proposal tool can record suggestions headlessly, but does not execute them or provide a headless approval interface. Follow-up depends on native `user_bash` events; `!!` output is excluded from it. |
| [`room-lease.ts`](room-lease.ts) | **Interactive only** | Lets a desktop TUI temporarily receive Matrix messages in the same session through the configured bridge socket. Explicitly inactive outside TUI and in bridge invocations; do not load it expecting a headless Matrix adapter. Requires the bridge and local socket setup. See [attention and continuity](../docs/attention.md). |
| [`matrix-room-attention.ts`](matrix-room-attention.ts) | **Interactive only; companion** | Loads an optional owner-only room-label file for `room-lease.ts`. Does not implement the inbox or handoff itself; inactive outside TUI and in bridge invocations. See [label setup](../docs/installation.md). |
| [`observability/pi-topic/index.ts`](observability/pi-topic/index.ts) | **Interactive only** | Names the session, terminal title and current tmux pane using bounded recent activity. Naming requests are TUI-gated and require an explicitly configured naming provider/model and existing authentication; they can incur charges and disclose conversation text. No naming request without the model configuration. See [topic behavior](../docs/components.md). |

## Not extension entrypoints: neither context

The adjacent `.mjs` modules under `web-search/` and `observability/` are implementation helpers, not standalone Pi extensions. This includes `observability/common/redact.mjs`; load the relevant `index.ts`, not the helpers. `web-search/html_to_markdown.py` is a subprocess helper. Test files, test directories, dependency manifests and `node_modules/` are not extensions either.

## Choosing a set

- **Desktop TUI:** choose general tools as needed; add shell history/questions for human interaction, room lease for Matrix handoff, or topic labels if authorized naming inference is wanted.
- **Bridge/RPC or automation:** start with the **Both** entries you actually need. TUI menus do not become chat commands merely by loading an extension. Questions and shell proposals need an additional user interaction path to be useful headlessly.
- **No provider or spending authorization:** do not enable model-backed judging, summaries, naming or search on the strength of installed credentials alone. Loading an extension does not authorize its consequential actions, telemetry disclosure or shell commands.

Extension state, model selections, auth files, telemetry and real transcripts belong outside this public source tree. See [installation](../docs/installation.md) for explicit loading and configuration rather than copying private runtime files here.
