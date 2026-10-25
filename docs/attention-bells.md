# Attention bells

Load `pi --extension ./extensions/attention-bell.ts` after installing the adjacent
extension dependencies. Pi 0.99.1 (or compatible lifecycle hooks) is required.
It emits BEL when the agent fully settles, not between tool calls, and after
successful compaction. Aborted runs and failed/cancelled compactions stay silent.

The task, question and background-job extensions independently share the adjacent
`lib/attention-bell.ts` helper. They ring on persisted non-delegated blocked/review
transitions, newly queued questions and deduplicated terminal job notifications.
Restoration, blocker edits, delegation, rejected questions, status queries and
progress redraws do not ring. Distinct meaningful events can each ring.

All bells require TUI mode and a TTY stdout. RPC/JSON/print modes and redirected
output never receive BEL. Loading the extension does not run a timer, model,
subprocess or notification. Terminal write errors do not fail the original action.
Keep `lib/` adjacent when copying entrypoints.

## Minimal tmux configuration

Copy these settings into your own tmux configuration, or source
[`examples/tmux-bells.conf`](../examples/tmux-bells.conf) using its absolute path:

```tmux
set-window-option -g monitor-activity off
set-window-option -g monitor-bell on
set-option -g bell-action other
```

Reload your configuration with `tmux source-file /absolute/path/to/your/tmux.conf`.
This disables generic activity alerts, monitors BEL and forwards bells from
background windows. It changes global defaults; existing per-window overrides
still take precedence. Audio/urgency is terminal-specific: enable the desired
bell behavior in your terminal if necessary. This does not install desktop
notification services, change terminal preferences or modify your live tmux server.

Tests exercise BEL gating and lifecycle events with fake outputs; a separate
isolated tmux server can validate the snippet without touching existing sessions.
No audible desktop notification is claimed by those checks.
