# Installation

Use Linux, Python 3.11+, Node 24, and a Pi installation exposing `ExtensionAPI`, `ctx.mode`, `ctx.isIdle()`, `sendUserMessage`, and the RPC `agent_settled` event. Imports currently target `@earendil-works/pi-coding-agent`, `@earendil-works/pi-ai`, and `@earendil-works/pi-tui`; install a compatible Pi distribution rather than renaming imports blindly. Topic labels additionally use `modelRegistry.streamSimple`. No particular provider or model is required.

## Bridge

From the repository root, create a virtual environment and install `requirements.txt`. Copy `examples/profile.json` to an owner-only location outside this checkout, replace its placeholders, and set mode `0600`. Use absolute local paths in a real profile. Set the Matrix/Discord token variables named by the profile in the launch environment.

Set `provider` and `model` to a configured Pi model. Provider authentication can live in Pi's private agent directory; any extra credential environment names must be explicitly included in `pi_pass_env`. Transport tokens and the configured review token cannot be passed through to bridge Pi. Optional `pi_env_file` loads explicitly selected tool environment values without interpolation or shell evaluation; token restrictions still apply and vision/status/progress sessions do not load this file. This environment filter is not isolation from files readable by the same OS user.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
AGENT_BRIDGE_PROFILE="$PROFILE" .venv/bin/python -m bridge
```

Matrix `rooms` is an allowlist. `wake_on_any_room_ids` must be a subset; otherwise a mention wakes a sleeping room. Discord uses `channel_ids` and `wake_on_any_channel_ids`, with threads inheriting their parent channel policy. Omit either transport to disable it. The agent identifier is a configurable lowercase identifier, not a transport policy.

Optional `matrix.startup_room_id` and `discord.startup_channel_id` select enabled destinations for startup notices. Broadcast mentions are refused unless the destination appears in `broadcast_allowed_room_ids` or `broadcast_allowed_channel_ids`. These lists must be subsets of the enabled destinations. The bridge also creates a private `bridge-health.sock` readiness socket.

## Interactive attention

Run interactive Pi as the same OS user as the bridge. Install extension dependencies and pass the extension explicitly:

```sh
npm ci --prefix extensions --ignore-scripts
AGENT_BRIDGE_ROOM_LEASE_SOCKET="$BRIDGE_STATE/bridge-room-lease.sock" \
  pi --extension ./extensions/room-lease.ts
```

The socket is created by the bridge. The extension creates its private inbox and exports `PI_ROOM_LEASE_INBOX`, `PI_ROOM_LEASE_SECRET`, and `PI_ROOM_LEASE_BRIDGE_SOCKET` to child tools. Do not set those three values manually. The extension does not activate in bridge RPC sessions.

For a friendly room label, also load `extensions/matrix-room-attention.ts` and set `PI_ROOM_LEASE_LABEL_FILE` to a mode-0600 JSON file with `room_id` and `name`. Labels are optional; this companion does not implement the handoff.

Use `tools/message_agent.py --profile-file "$PROFILE" --check --room-id "$ROOM_ID" --to "$RECIPIENT"` to check access. Remove `--check` and add `--message-file "$MESSAGE_FILE"` to send. A configured interactive launcher refuses sends when its lease extension is unavailable. Mobile users should use a separate human account in the same unencrypted Matrix room.

## Skills and optional components

Load the included skill directories with Pi's `--skill ./skills/message-agent` option (repeat for each desired skill), or configure their source directories in your compatible Pi installation. Keep the repository layout intact: skill-relative executable links point two directories up into `tools/`. Copying a lone skill directory breaks those links.

Load other extensions explicitly as needed; do not indiscriminately load the whole tree, which includes tests and helper modules. See [components](components.md) for each entrypoint. Capture and topic labeling can send data to configured services; enabling them is a deliberate installation choice. Topic and web-search model calls may consume inference credits.

Review automation and PostgreSQL ingestion are optional. Configure them using [reviews](reviews.md) and [ledger](ledger.md). No service installation, live-account operation, or paid model smoke test is performed by the test suite.

Use the [canonical Matrix tool](matrix.md) for bounded reads, media and reactions. The [stable background-jobs backend](background-jobs.md) requires a working Linux systemd user manager and must be loaded explicitly. Web HTML conversion requires `uv` with a resolvable/cached converter dependency, or an explicit `PI_WEB_READ_PYTHON` command.

For optional terminal alerts, load `extensions/attention-bell.ts` and use the
[minimal tmux configuration](attention-bells.md#minimal-tmux-configuration).
For counts-only bridge observability, explicitly load
`extensions/bridge-workload-metrics.ts` in bridge/RPC Pi's private extension
configuration; see [source coverage and Prometheus setup](workload-metrics.md).
Neither component is installed or enabled by merging this repository.

## Portable support skills

Load only the skills you want; adding their source does not install them:

```sh
pi --skill ./skills/environment-doctor --skill ./skills/tmux
```

Both use Python 3.10+ and its standard library; the repository's bridge still
requires Python 3.11+. They are verified on Linux, not Windows. Doctor's bounded
subprocess handling requires POSIX process groups/selectors; tmux requires an
existing tmux binary and access to the intended socket. Other POSIX systems have
not been tested.

[Environment doctor](../skills/environment-doctor/SKILL.md) inventories trusted
local tools, Python modules and skill discovery. Its optional cron adapter needs
a separately supplied helper (not bundled); it never installs one. Service checks
require explicit harmless readiness URLs. Exit 0 can include unknown/missing
coverage, and output can contain private local paths and names: review it before
sharing. It is not a sandbox for hostile executables or imported packages.

[Tmux](../skills/tmux/SKILL.md) lists/reads panes and inserts one literal line,
with Enter by default. Read first, target an exact pane ID, and retain the same
server selector. Sending is not proof of execution or completion. The advanced
agent-pane reference is manual guidance, not an automatic state classifier.
