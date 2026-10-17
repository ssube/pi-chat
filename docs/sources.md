# Source provenance

This refresh was prepared from the following cached upstream `main` refs. Network access was unavailable during preparation, so these are exact source snapshots, not a claim that remote heads were rechecked.

| Component source | Commit | Snapshot date |
| --- | --- | --- |
| Shared bridge | `9057de1c4cacc488d81355d04a13310bcc5833dc` | 2026-10-16 |
| Shared skills and extensions | `0b0589b4761aa1ee3c9b788e25c74c0a6a04c126` | 2026-10-16 |
| Room-lease integration and Matrix operations | `927f2fa0c694b87ed70276f8ea597d350a964550` | 2026-10-16 |

The public tree adapts these sources: profiles and contacts remain generic, model/provider selection stays configurable, and the standalone review runner and ledger interfaces remain supported. Fixtures contain synthetic identities. Deployment files, operational profiles, private routing and council orchestration are excluded.

Capture retains the public ledger's reviewed-fix notification contract. Deployment-specific plan injection, calibration and shadow-observer services are excluded; their private schema/authorization assumptions are not imported. Existing public SQL migration files and checksums are unchanged. Unrelated shared skills and proprietary material are not part of the chat export.

Background jobs are included as an experimental systemd backend; read [the runbook](background-jobs.md) before loading it. Components remain opt-in. Updating this repository does not install or replace a running service.
