---
name: message-agent
description: Send a targeted Matrix message to an agent in a configured room, preserve interactive attention, and verify its reply.
---

Use the configured account and room for an authorized message. Loading this skill does not authorize contacting someone or changing providers.

Run the sender from the retained repository layout:

```sh
python3 "{baseDir}/../../tools/message_agent.py" --profile-file "$PROFILE" \
  --contact "$CONTACT" --contacts-file "$CONTACTS" --message-file "$MESSAGE_FILE"
```

Alternatively use `--room-id "$ROOM_ID" --to "$RECIPIENT"`. The profile is an owner-only JSON file; its Matrix section defines `homeserver`, `user_id`, `token_env`, and enabled `rooms`. Contacts map names to `room_id` and `user_id`. The helper verifies the account and both memberships and attaches a structured mention. Write the exact message to a UTF-8 file instead of composing a shell argument.

Use `--check` without a message file to verify access without sending or claiming attention. With interactive room-lease configuration, a send acquires the lease before posting. Do not bypass a refused lease by changing identities or removing the extension. An absent or inconsistent configured extension stops the send.

A sent event ID proves acceptance by Matrix, not completion by the recipient. Read the actual reply in the same room with the matrix-read tool. Avoid repeated pings during an active turn.

While interactive attention is active, mobile posts in the room enter the same desktop Pi session. Reply through this sender to make the response visible on mobile; ordinary terminal output is not mirrored. Bridge RPC sessions already publish their final response and should not send a duplicate through this helper.

Read [attention and continuity](../../docs/attention.md) for expiry, fallback, and delivery limits.
