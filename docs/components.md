# Extensions and tools

| Entrypoint | Behavior |
| --- | --- |
| `extensions/short-context-models.ts` | Opt-in half-context aliases for user-selected local models; see [setup and limits](short-context-models.md) |
| `extensions/pi-time.ts` | Clock formats and abortable waits with one adaptive countdown and an explicit expected wake time |
| `extensions/attention-bell.ts` | TUI/TTY-only meaningful attention bells; see [events and tmux setup](attention-bells.md) |
| `extensions/bridge-workload-metrics.ts` | RPC-only counts observer; see [metrics and coverage](workload-metrics.md) |
| `extensions/tasks.ts` | Session task ledger, evidence judging, user menu, list sharing and advisory summary steering; see [tasks](tasks.md) |
| `extensions/background-jobs.ts` | Stable, opt-in systemd jobs, status, cancellation and completion steering; see [runbook](background-jobs.md) |
| `extensions/room-lease.ts` | Temporary interactive Matrix inbox, steering, edit coalescing, lease status and release |
| `extensions/matrix-room-attention.ts` | Optional owner-only room label file |
| `extensions/follow-up-questions.ts` | `queue_question` and `/questions`; branch-persistent optional questions, answered as steering |
| `extensions/bash-history.ts` | Shell completion, reviewed command proposals, optional follow-up after native `!` commands |
| `extensions/web-search/index.ts` | `web_search` and `web_read`; public read-only pages and configured search backend |
| `extensions/observability/pi-capture/index.ts` | Tool outcomes, durations, errors, skill events, reviewed fix matches, optional structured matcher |
| `extensions/observability/pi-topic/index.ts` | Session/terminal/topic labels using a configured naming model |

Shell proposals do not execute until the user submits the native `!` command. `/shell-suggestions` selects a proposal and confirms replacement of an unsent draft. `!!` excludes output from context and automatic follow-up. `/shell-followup off` pauses automatic follow-up. Optional queued questions are not a mechanism for required approvals.

Web search defaults to an ephemeral Codex CLI search using the installer's existing authentication; it checks for a real search event and validates results. Alternatively set `PI_WEB_SEARCH_BACKEND=searxng` and `SEARXNG_URL`. No instance is predefined. Web reading fetches public pages, checks destination addresses at validation and connection time, validates redirects, prefers Markdown, and extracts HTML locally through `uv run --script` with the converter's declared `beautifulsoup4` dependency. Set `PI_WEB_READ_PYTHON` to an explicit runner command when needed. JSON/text responses remain text; supported PNG/JPEG/GIF/WebP responses return private image artifacts after format-signature validation. Longer pages are written to private temporary files. Configure additional blocked suffixes with `PI_WEB_READ_BLOCKED_SUFFIXES`. Returned content is untrusted.

Topic labels require `PI_TOPIC_PROVIDER` and `PI_TOPIC_MODEL`. Without both, the extension makes no request. `/topic-refresh` requests a refresh; automatic requests are limited to once per five minutes when activity changes. Input is a bounded, redacted window of user/assistant text and tool names, excluding tool arguments/results, reasoning, images, and abandoned branches. Redaction cannot remove all private facts from prose. Only the current tmux pane is renamed. Failure preserves the previous title.

| Tool | Usage |
| --- | --- |
| Canonical Matrix operations | `python tools/matrix.py --help`; see [setup](matrix.md) |
| Member discovery | `python -m bridge.members_cli --help` inside a scoped bridge session |
| Standalone sender | `python tools/message_agent.py --help` |
| Standalone Matrix reader | `python tools/room_history.py --help` |
| Scoped bridge history | `python -m tools.history --help` |
| Scoped reactions | `python -m tools.react --help` |
| Public activity phrase | `python -m tools.activity --help` |
| Session status | `python -m tools.sessions --help` |
| Session HTML export | `python tools/export_session.py --help`; see [export guide](session-export.md) |
| Ledger and fixes | `python -m tools.ledger.error_cli --help` |

Bridge RPC prompts provide scope-specific helper commands and capabilities. History, reaction, and activity services retain transport credentials in the bridge. Session status uses its separate owner-local socket. The standalone Matrix reader uses a matching private identity file or the profile's configured token environment and can wait for new replies outside a bridge RPC session.

The bridge supports an optional temporary image-description Pi session when `image_model` is configured. The selected image model uses the configured provider. Provider/RPC failures are recorded with bounded structured metadata in the same local observability outbox. Session archives, evidence files, credentials, and model configuration remain private runtime data.

`pi-time` registers `clock` (`epoch_ms`, `iso_utc`, `human_utc`, `human_local`) and `sleep_until` (a positive duration or ISO timestamp with explicit timezone). Waits use Pi's abort signal and announce the expected wake time before waiting. The countdown refreshes more often near its deadline and uses Pi's update callback instead of duplicating the same display in status. They require no credentials.

Bridge member discovery returns canonical mention targets and aliases through the existing history capability, including bounded pagination. Missing or incomplete membership is not proof of absence. Outgoing ambiguous or unknown mentions are refused with feedback; broadcast pings require an explicit enabled-room/channel allowlist.

Matrix and Discord expose working/terminal turn indicators and retained progress summaries. The bridge's progress summarizer uses the configured provider/model in a temporary tool-free session with bounded recent public conversation/progress text. It excludes private identity files and tool arguments/results. Periodic summary inference can incur provider costs when the bridge is run; no paid inference is used by offline tests.

Turn indicators are single-flight per physical Matrix room or Discord channel,
with at most two concurrent emits per transport. Validated Matrix limiter replies
persist a transport-wide deferral (bounded to 30 seconds on reload); unrelated
rooms proceed only while that transport is not limited. Request acknowledgments
retain their separate retry budget. Shutdown drains indicators for up to ten
seconds before bounded adapter shutdown; retained intents survive replacement.

Quoted Matrix/Discord reply context is preserved without treating authored quotes
as reply fallbacks. A rejected provider sign-in produces an actionable generic
notice without retaining its diagnostic text. An authoritative pass marker still
suppresses a duplicate final after a chat-tool reply, including preserved tool
narration; new direct requests retain their own reply opportunity.
