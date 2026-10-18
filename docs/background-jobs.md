# Experimental background jobs

`extensions/background-jobs.ts` is an opt-in **draft systemd backend**. It requires Linux, `/bin/bash`, `systemd-run`, `systemctl`, a reachable systemd user manager, and a Node-compatible runtime. No direct-process fallback is provided. Do not load it alongside another extension registering the same tools or `/jobs`.

```sh
npm ci --prefix extensions --ignore-scripts
pi --extension ./extensions/background-jobs.ts
node --test tests/background-jobs.test.cjs
```

The integration tests use real transient services and skip when no user manager is reachable. Passing skipped tests is not proof that the backend works on that host. Source presence does not install it or enable lingering or reboot survival.

## Tools and interaction

| Tool | Contract |
| --- | --- |
| `background_start` | Run a noninteractive Bash command in Pi's cwd with required `timeout_seconds`, optional `label`, `unsafe_to_terminate` (default false), and `progress_interval` (default `off`) |
| `background_list` | Paginated job counts, status and bounded observations |
| `background_status` | Actual exit status/signal, elapsed time, metadata paths and bounded combined-output tail |
| `background_cancel` | Stop a safe job's service/cgroup; unsafe jobs refuse cancellation |

Commands must wait for their actual work instead of daemonizing it; stdin is closed and there is no PTY. Foreground tool cancellation after launch does not cancel the submitted job. Use `/jobs` to inspect jobs, confirm cancellation of a safe job, or explicitly send a bounded output tail as steering. Enabled progress sampling (`5s`, `30s`, `1m`, `5m`, `15m`, `1h`) updates observations and the UI; automatic steering is reserved for completion. Completion messages identify the extension and treat command/output as untrusted data.

## Lifecycle and recovery

Each job owns a uniquely named `pi-bg-<UUID>.service`, grouped in a session-specific slice, plus a mode-0700 temporary artifact directory. systemd runs a oneshot service with no automatic restart, closed stdin and private disk-backed stdout/stderr. Safe jobs use the requested execution deadline and stop the whole cgroup, including descendants, with a one-second stop timeout. Unsafe jobs use no execution deadline and detach on session exit instead of being stopped by the extension. Their deadline is an observation, not an invented exit or an automatic steering event. External manager stops, shutdown and reboot can still terminate them.

A generated standalone `ExecStopPost` hook writes the terminal result to `completion.json`, independent of Pi and unit garbage collection. The extension retains session mappings and can reconcile on resume. Completion records retain the service result, exit kind/status and timestamp; successful submission is not successful completion. Submission failure can leave an already-created service, so creation is never retried automatically. Unit absence without a completion record is `unknown`, not success. No cross-session adoption or permanent retention guarantee is provided.

The service receives a snapshot of conventionally named environment variables in a mode-0600 EnvironmentFile. This file can contain credentials; it stays private and the standalone hook removes it. Uncertain submission or a hook failure can leave it behind. Logs, metadata and command scripts may also contain private data. They remain on disk for inspection, without a disk quota or automatic artifact cleanup, and temporary storage may be purged by the OS.

Inspect the recorded unit with `systemctl --user show UNIT` and read only the job's recorded private output paths. If no completion record exists, establish actual service state before retrying or cancelling work. Never stop a whole session slice if it contains unsafe jobs. Starting or cancelling a background command requires the same authorization as running or stopping that command in the foreground.

## Capacity and retained history

Per-session active capacity defaults to **5** jobs. Configure `PI_BACKGROUND_MAX_ACTIVE` (integer ≥ 1). Restored active/unknown jobs count toward capacity; reaching the cap refuses submission without stopping existing jobs.

Completed registry history defaults to the newest **100** jobs and **24 hours**. Configure `PI_BACKGROUND_HISTORY_LIMIT` (integer ≥ 0) and `PI_BACKGROUND_HISTORY_TTL_SECONDS` (integer ≥ 0, default 86400 seconds). Pruning does not delete private artifact files or historical Pi session entries, cancel jobs, or change active capacity. Expired/pruned jobs are omitted from the registry view; retained artifact paths remain private.
