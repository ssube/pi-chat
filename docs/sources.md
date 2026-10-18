# Source provenance

This refresh was prepared from the following upstream `main` refs, fetched and checked on 2026-10-18. These are exact source snapshots, not automatic synchronization with later changes.

| Component source | Commit | Snapshot date |
| --- | --- | --- |
| Shared bridge | `0d8cca133b7ab61ba38f898e3cc73c574cad9b39` | 2026-10-18 |
| Shared skills and extensions | `73dac95574cecb0eb7686fe871b28274fc898397` | 2026-10-18 |
| Room-lease integration and Matrix operations | `f2c7dd71c37c9fee948c7b461933d37b88f3d724` | 2026-10-18 |

The public tree adapts these sources: profiles and contacts remain generic, model/provider selection stays configurable, and the standalone review runner and ledger interfaces remain supported. Fixtures contain synthetic identities. Deployment files, operational profiles, private routing and council orchestration are excluded.

Capture retains the public ledger's reviewed-fix notification contract. Deployment-specific plan injection, calibration and shadow-observer services are excluded; their private schema/authorization assumptions are not imported. Existing public SQL migration files and checksums are unchanged. Unrelated shared skills and proprietary material are not part of the chat export.

Background jobs are included as a stable, opt-in systemd backend; read [the runbook](background-jobs.md) before loading it. Components remain opt-in. Updating this repository does not install or replace a running service.

This refresh adds profile reloading, conversation modes and bounded Discord reconnect recovery, the session task ledger and advisory sharing, standalone session HTML export, and background-job capacity/history bounds. Portable profiles retain explicit configurable providers instead of an upstream provider default. Optional session export uses only synthetic fixtures; no real transcripts or generated exports are included.

The task extension snapshot includes agent unblocking, an explicit colored progress state, and a one-task progress limit enforced by both tools and menu. Existing over-limit snapshots are preserved and can drain. Background-job documentation and tool labels now match the stable systemd backend; its safety and privacy limits are unchanged.
