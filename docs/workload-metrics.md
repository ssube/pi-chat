# Bridge workload observer

`extensions/bridge-workload-metrics.ts` is opt-in, RPC-only source. Merging does
not install it or restart a bridge. It registers no tools or commands and makes
no model, network, filesystem, or transcript calls. TUI and print modes do nothing.

## Sources and wire contract

The observer emits a counts-only JSON string via Pi's existing fire-and-forget
`ctx.ui.setStatus("agent-bridge/workload/counts/v1", json)` channel. This is a
reserved display-protocol slot, not a new RPC command. Another RPC client could
render it; it must never contain task text, identifiers, names or goals.

```json
{"version":1,"tasks":{"pending":0,"progress":1,"blocked":0,"question":0,"complete":2},"subagents":{"active":2,"capacity_used":1,"capacity_limit":4}}
```

Each source may independently be `null` (unavailable), never a fabricated zero:

- Tasks: synchronous `tasks:metrics-v1` event-bus query `{version:1}`. A loaded,
  successfully restored `tasks.ts` attaches `counts` from its in-memory ledger;
  there is no history replay or I/O per query. Exact effective-state vocabulary:
  pending, progress, blocked, question, complete. Waiting questions take
  precedence over unfinished base states; completed tasks remain complete.
  Missing, extra or invalid count fields reject that source, not just one field.
- Subagents: untargeted native `subagents:rpc:v1:request` method `status`, empty
  params, matching reply channel/request ID, version 1. Only `fleet.totalActive`
  and `fleet.topLevelAsyncCapacity.{used,limit}` are projected. Pending work and
  workflow controllers are included in native active: **delegated work
  outstanding, not running processes or total concurrency**. Capacity is only
  top-level async slots, so active/limit is not a utilization calculation.
  Missing/invalid replies or one-second timeout mean unavailable. A valid native
  limit zero means unlimited capacity, not unavailable; outstanding counts remain
  measurable without imposing a new workload cap.
  No target ID/path is passed and no spawn/manage/control method is used.

Publish after source changes with a one-second coalescing delay, plus a
15-second heartbeat. Session switching/shutdown clears the reserved slot and
cancels timers/replies; a cancelled update cannot republish into a later session.
The payload is well below 1 KiB. This is receipt-ordered observation, not a
sequence/replay protocol or authentication boundary against installed extensions.

## Receiving bridge and rollout

The matching bundled bridge consumer reads this exact key while idle or
running, independently of prompt/progress handling. Its denominator is bridge
scopes with `scope.client is not None`, not all loaded scopes, standalone Pi,
archived sessions or native subagent grandchildren. Counts summarize the parent
session's own task ledger and native fleet projection, not a recursive process
inventory or external task system. Missing observer/source is visible coverage.

The consumer expires observations after 75 seconds using local receipt time;
75 > heartbeat(15) + bridge push(30), and 75 >= 3 * heartbeat. It withholds a
source's total if any resident child lacks fresh valid coverage. It exports
coverage counts, covered/resident and coverage_complete. Its existing whole-group
HTTP PUT removes omitted totals; legacy metrics remain in every replacement body.
Prometheus/Grafana queries must also gate on bridge push freshness: a dead bridge
cannot send an expiry snapshot. Session lifecycle counts come directly from the
bridge, not this extension, and work without it.

Install reviewed `tasks.ts` and this observer into the owner's existing Pi load-out
(or use explicit `-e /approved/path/bridge-workload-metrics.ts`); retain the native
subagents package if fleet observations are wanted. Do not change another owner's
settings or enable a chat-disabled role merely to improve coverage. Existing
resident processes need their owner's reviewed idle/recovery rollout; SIGHUP
profile reload does not reload code. Verify actual per-owner extension load-out,
not repository presence. Old/missing versions remain visibly uncovered.

## Verification

With the extensions' dependencies installed:

```sh
node --test tests/bridge-workload-metrics.test.cjs tests/tasks.test.cjs
```

Set `PI_TASKS_SDK_MODULE` to the pinned Pi SDK's `dist/index.js` to run the real
idle RPC subprocess contract too (enabled in CI). That uses isolated configuration,
fixture count sources and no prompt/inference. It verifies the actual SDK emits
the reserved status frame; it does not claim native fleet semantics are newly
tested. Unit tests cover real task effective-state queries, unavailable/invalid
sources, timeout cleanup, burst coalescing, heartbeats and switch cancellation.

## Prometheus output

Set `AGENT_PUSHGATEWAY_URL` explicitly in the bridge launch environment to enable
its existing 30-second Pushgateway publisher. No endpoint is supplied here.
`agent_bridge_sessions{agent,state}` partitions loaded conversation scopes by
checkpoint lifecycle state (plus `compacting` and `unknown`), independently of
the optional observer.

With the observer loaded, the bridge also exports:

- `agent_bridge_workload_resident_sessions`: parent scopes with resident Pi clients.
- `agent_bridge_tasks{agent,state}`: effective task states.
- `agent_bridge_subagents_active`: delegated work outstanding, not running processes.
- `agent_bridge_subagents_capacity_used` and `agent_bridge_subagents_capacity_limit`:
  top-level async slots only; a native zero limit means unlimited.
- `agent_bridge_workload_coverage{agent,source,status}` and
  `agent_bridge_workload_sources_covered`, `agent_bridge_workload_sources_resident`,
  `agent_bridge_workload_coverage_complete`: source completeness and freshness.
- `agent_bridge_workload_oldest_age_seconds`: oldest frame's local receipt age.

Existing presence, scope and operation metrics stay in the same replacement PUT.
Incomplete source coverage omits that entire source's totals, not its coverage
metrics. Zero resident parents is a known empty set; graceful disconnect omits
observer totals and clears coverage completeness. Missing data is not zero.

Gate queries on both coverage and publisher freshness, for example:

```promql
agent_bridge_tasks
  and on(agent) (agent_bridge_workload_coverage_complete{source="tasks"} == 1)
  and on(agent) (time() - agent_bridge_last_push_timestamp_seconds < 90)
```

The publisher freshness gate is necessary because a dead bridge cannot expire
its retained Pushgateway data. No session IDs, task text or child goals become
metric labels.
