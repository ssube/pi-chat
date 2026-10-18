# Conversation model modes and reconnect recovery

## Explicit conversation modes

Add an optional map to a private bridge profile using provider/model IDs already available to Pi:

```json
{
  "provider": "configured-provider",
  "model": "configured-model",
  "default_mode": "fast",
  "modes": {
    "fast": {"provider": "configured-provider", "model": "configured-model"},
    "smart": {"provider": "other-configured-provider", "model": "other-configured-model"}
  }
}
```

The names/models are examples, not supplied credentials or availability guarantees. Without `modes`, `fast` uses the profile's existing **provider and model**. This portable default differs from the upstream provider-specific fallback. `default_mode` defaults to `fast` and must name a configured entry. Names are lowercase, start with a letter, and contain up to 32 letters, digits, underscores or hyphens. `debug`, `stop`, `cancel`, `recover`, and `mode` are reserved. Each entry accepts exactly `provider` and `model`.

On Matrix or Discord, send `!smart request text`, `!fast request text`, or another configured name. The selection happens before the attached request; the token is removed from the prompt. Bare `!smart` switches without inference. Sleeping mention-only rooms/channels require addressing the agent; ordinary admission rules still apply. Peer metadata, bots and webhook notifications cannot select modes. Quoted/prose tokens are not commands.

Modes are scoped to the room/channel/thread, not the whole bridge. Live switches preserve the Pi process/session/epoch and use `set_model`; busy turns refuse switching without replaying or aborting work. Pending switches are bounded and remain interruptible by existing stop/cancel controls. Automatic in-session compaction preserves mode. Closing for sleep, explicit stop/recovery, or bridge restart resets it to the default. A selection made while already sleeping applies on the next wake.

Auxiliary image, status and progress clients keep the profile's existing provider/model settings. Credentials and environment allowlists are unchanged. There is no automatic escalation or fallback. An unavailable model fails safely; an uncertain live switch requires a successful explicit selection or stop before new direct prompts. `!debug` shows recorded selection and available names without waking Pi. Switching providers may resend context, change context limits, and incur different inference charges. Existing provider, disclosure and spending authorization is required.

## Discord reconnect recovery

On reconnect, the Discord adapter reconstructs **bounded** gaps using retained per-scope cursors and available channel/thread history. It includes configured parents, active threads, and bounded archived-thread discovery. It does not promise complete historical replay or exactly-once delivery.

Recovery holds and retries are scoped: a failed/deferred thread does not block unrelated parent traffic. Failed submissions preserve their scope for retry; cap-deferred scopes remain held instead of expiring silently. Recovery distinguishes observed messages from accepted input and respects checkpoint failure/admission boundaries. Missing permissions, history limits, deleted posts, and fetch/admission budgets can leave gaps; inspect bounded logs rather than assuming reconnect means everything was replayed.

Matrix uses its existing sync/history behavior; Discord's gap reconstruction is transport-specific, not a new Matrix recovery protocol. Offline tests cover paired mode commands and Discord recovery faults without live accounts or inference.

## Profile reload (operator reference)

`SIGHUP` re-reads `AGENT_BRIDGE_PROFILE`, then checkpoints idle conversations without interrupting active turns. Supported routing, model/mode, image, identity and child-environment settings apply to subsequent work. A resident turn keeps its process/selection until checkpoint; removed modes require an explicit selection before new work.

Already-open transport identities, credentials, homeserver/guild, working/state directories, Pi executable/agent directory, review runner configuration and enabling/disabling the review queue require a restart. One restart-required change rejects the entire reload; invalid profiles also keep the running configuration. The idle checkpoint still runs after a refused reload. A successful reload is not a service restart or new spending authorization.
