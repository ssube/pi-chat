---
name: matrix-read
description: Read bounded Matrix history, filter messages, or wait for a new reply without sending messages.
---

Use the standalone reader with owner-only profile and identity files:

```sh
python3 "{baseDir}/../../tools/room_history.py" --profile-file "$PROFILE" \
  --identity-file "$IDENTITY" --room-id "$ROOM_ID" --sender "$RECIPIENT" \
  --limit 25 --skip-progress
```

The profile supplies the homeserver, user ID, and enabled rooms. Identity JSON supplies matching `user_id` and `token`. The helper checks `whoami` and restricts rooms to the profile. It reads unencrypted rooms only. `--contact` with `--contacts-file` can select a configured room.

Use `--wait-for 'literal phrase' --timeout 180` for a new matching message: old history cannot satisfy the wait. Exit codes are 0 for a match, 2 for timeout, and 1 for an error. `--watch` includes recent history and polls for a bounded interval, so it may miss bursts. `--since`, `--pages`, and `--limit` bound reads; no match means no match within that scan.

Default bodies are short previews. Use `--metadata-only` for event discovery, `--json` for structured output, and `--full-body` only when needed. A progress notice or send event is not evidence of completed work.

Inside a bridge RPC session, prefer the injected history command: it uses the bridge's scope capability and requires no transport credentials in Pi. The standalone reader is for interactive/operator use outside that scope.

The [matrix skill](../matrix/SKILL.md) is the canonical interface for room/member/event reads, contacts, and reaction scans. This standalone reader remains compatible. `--identity-file` is optional when the profile names an existing token environment variable; inherited `MATRIX_URL`, `MATRIX_USER_ID`, and `MATRIX_TOKEN`, when present, must form a complete matching set. `--skip-progress` filters progress notices in both history and waits. Empty bounded results establish only that the scan found no match.
