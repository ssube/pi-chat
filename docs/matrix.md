# Matrix operations

The canonical entrypoint is `python3 tools/matrix.py --config "$MATRIX_CONFIG" COMMAND`. Keep the repository layout intact. Start from `examples/matrix.json`, store the real configuration outside the checkout with mode `0600`, and use absolute paths. With no `--config`, the default is `~/.config/pi-chat/matrix.json`.

Configuration accepts only `profile_file`, optional `identity_file`, and optional `contacts_file`. The profile's Matrix `rooms` is the authority for enabled rooms. Read credentials come from the optional matching identity JSON (`user_id`, `token`), or the environment variable named by the profile's `token_env`. If any inherited `MATRIX_URL`, `MATRIX_USER_ID`, or `MATRIX_TOKEN` value is present, that complete set must match the profile and any profile token. The tool does not load dotenv files or create accounts.

Writes always use the profile's `token_env`, even when a separate identity file is configured for reads. They verify `whoami`, room membership, and any recipient's membership before acquiring interactive attention. `check` performs the checks without writing or acquiring a lease.

```sh
python3 tools/matrix.py --config "$MATRIX_CONFIG" rooms
python3 tools/matrix.py --config "$MATRIX_CONFIG" contacts
python3 tools/matrix.py --config "$MATRIX_CONFIG" event "$ROOM_ID" --event-id "$EVENT_ID"
python3 tools/matrix.py --config "$MATRIX_CONFIG" send --contact peer --message-file "$MESSAGE_FILE"
python3 tools/matrix.py --config "$MATRIX_CONFIG" attach "$ROOM_ID" --files /tmp/chart.png --caption 'Latest chart'
python3 tools/matrix.py --config "$MATRIX_CONFIG" batch "$ROOM_ID" --files /tmp/chart.png /tmp/report.pdf --interval 1
python3 tools/matrix.py --config "$MATRIX_CONFIG" react "$ROOM_ID" --event-id "$EVENT_ID" --key '👍'
python3 tools/matrix.py --config "$MATRIX_CONFIG" reactions "$ROOM_ID" --event-id "$EVENT_ID" --pages 2 --limit 20
```

A contact mapping is owner-only JSON mapping arbitrary names to `room_id` and `user_id`; see `examples/contacts.json`. Contact routes must still target enabled rooms. Without `contacts_file`, the default is `~/.config/pi-chat/contacts.json`. There is no fixed list of agents or implicit relay.

`send ROOM [RECIPIENT]` uses a structured mention when a recipient is given. `--stdin` is an alternative to `--message-file`. A send without a recipient posts an ordinary room update. A full alias (`#room:server`) must resolve to an enabled room. The standalone `tools/message_agent.py` still requires `--to` with `--room-id` unless explicitly passed `--general`.

Media and reactions require unencrypted rooms. Media limits are 32 MiB per regular file, 64 MiB per batch and 20 files; captions are at most 8000 characters. Files are checked before uploads. A failed or uncertain write stops the batch and prints receipts; uploads can remain orphaned and timed-out sends may have committed. Retry only after inspecting the room. HTTP 429 retries are bounded; GET/PUT server-error retries are bounded, and uncertain upload POST failures are not replayed.

Use `history` for recent context and `wait` for new messages from an exact sender with a literal marker. Wait timeout exits 2. History/watch/wait support previews, metadata-only output, JSON, full bodies and progress filtering; use `--help` for bounds. Reaction scans report `source`, `next_batch`, and `limited`; a bounded scan cannot establish a total.

The existing `tools/message_agent.py` and `tools/room_history.py` commands remain available. All writes preserve [interactive attention](attention.md); this tool does not install extensions, change membership, or authorize contacting someone.
