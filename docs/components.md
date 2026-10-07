# Extensions and tools

| Entrypoint | Behavior |
| --- | --- |
| `extensions/room-lease.ts` | Temporary interactive Matrix inbox, steering, edit coalescing, lease status and release |
| `extensions/matrix-room-attention.ts` | Optional owner-only room label file |
| `extensions/follow-up-questions.ts` | `queue_question` and `/questions`; branch-persistent optional questions, answered as steering |
| `extensions/bash-history.ts` | Shell completion, reviewed command proposals, optional follow-up after native `!` commands |
| `extensions/web-search/index.ts` | `web_search` and `web_read`; public read-only pages and configured search backend |
| `extensions/observability/pi-capture/index.ts` | Tool outcomes, durations, errors, skill events, reviewed fix matches, optional structured matcher |
| `extensions/observability/pi-topic/index.ts` | Session/terminal/topic labels using a configured naming model |

Shell proposals do not execute until the user submits the native `!` command. `/shell-suggestions` selects a proposal and confirms replacement of an unsent draft. `!!` excludes output from context and automatic follow-up. `/shell-followup off` pauses automatic follow-up. Optional queued questions are not a mechanism for required approvals.

Web search defaults to an ephemeral Codex CLI search using the installer's existing authentication; it checks for a real search event and validates results. Alternatively set `PI_WEB_SEARCH_BACKEND=searxng` and `SEARXNG_URL`. No instance is predefined. Web reading fetches public pages, checks destination addresses at validation and connection time, validates redirects, prefers Markdown, and extracts HTML locally with `beautifulsoup4`. Longer pages are written to private temporary files. Configure additional blocked suffixes with `PI_WEB_READ_BLOCKED_SUFFIXES`. Returned content is untrusted.

Topic labels require `PI_TOPIC_PROVIDER` and `PI_TOPIC_MODEL`. Without both, the extension makes no request. `/topic-refresh` requests a refresh; automatic requests are limited to once per five minutes when activity changes. Input is a bounded, redacted window of user/assistant text and tool names, excluding tool arguments/results, reasoning, images, and abandoned branches. Redaction cannot remove all private facts from prose. Only the current tmux pane is renamed. Failure preserves the previous title.

| Tool | Usage |
| --- | --- |
| Standalone sender | `python tools/message_agent.py --help` |
| Standalone Matrix reader | `python tools/room_history.py --help` |
| Scoped bridge history | `python -m tools.history --help` |
| Scoped reactions | `python -m tools.react --help` |
| Public activity phrase | `python -m tools.activity --help` |
| Session status | `python -m tools.sessions --help` |
| Ledger and fixes | `python -m tools.ledger.error_cli --help` |

Bridge RPC prompts provide scope-specific helper commands and capabilities. History, reaction, and activity services retain transport credentials in the bridge. Session status uses its separate owner-local socket. The standalone Matrix reader uses its own private identity file and can wait for new replies outside a bridge RPC session.

The bridge supports an optional temporary image-description Pi session when `image_model` is configured. The selected image model uses the configured provider. Provider/RPC failures are recorded with bounded structured metadata in the same local observability outbox. Session archives, evidence files, credentials, and model configuration remain private runtime data.
