# Source provenance

## Tagged refresh — 2026-10-25

| Component source | Verified tag | Peeled commit |
| --- | --- | --- |
| Shared skills and extensions | `v0.15.0` | `d210d210cd9514074bbd86cc0a09159f9f2ea809` |
| Shared bridge | `v0.9.0` | `aef51fd31f04deb3490cb34e9ea3e192252a7edb` |

Release signatures were checked against the pinned release certificate. This is
a selective public port, not an exact tag mirror or an installed-runtime claim.
The approved additions are task upgrades, attention bells (including a minimal
tmux example), workload metrics and chat-control/indicator improvements, with
their existing-component fixes, regression tests and documentation.

**Announcement-feed policy and the environment-doctor skill are excluded.** The
bridge code delta ends at signed `v0.8.5`
(`37e37e9073895ac62a19f80cea4bf66b101cfdd0`); all subsequent changes through
`v0.9.0` concern the excluded feed feature. Existing exported Matrix skills have
no applicable non-feed changes in the selected skills delta, so their public
adaptations are retained. No unrelated skill is added.

Private routing, deployment/council/owner-review orchestration and identities
remain excluded. Profiles keep configurable providers; fixtures use synthetic
names. No live settings, transcripts, logs, credentials or generated exports are
included. Public ledger SQL migrations and checksums are unchanged. Components
remain opt-in; this refresh does not install or restart services.

## Previous source baseline

This refresh was prepared from the following upstream `main` refs, fetched and checked on 2026-10-18. These are exact source snapshots, not automatic synchronization with later changes.

| Component source | Commit | Snapshot date |
| --- | --- | --- |
| Shared bridge | `0d8cca133b7ab61ba38f898e3cc73c574cad9b39` | 2026-10-18 |
| Shared skills and extensions | `73dac95574cecb0eb7686fe871b28274fc898397` | 2026-10-18 |
| Task/time extension and test follow-up | `ed79894eda1498369f6bae242e9ba825c262037b` | 2026-10-18 |
| Room-lease integration and Matrix operations | `f2c7dd71c37c9fee948c7b461933d37b88f3d724` | 2026-10-18 |

The public tree adapts these sources: profiles and contacts remain generic, model/provider selection stays configurable, and the standalone review runner and ledger interfaces remain supported. Fixtures contain synthetic identities. Deployment files, operational profiles, private routing and council orchestration are excluded.

Capture retains the public ledger's reviewed-fix notification contract. Deployment-specific plan injection, calibration and shadow-observer services are excluded; their private schema/authorization assumptions are not imported. Existing public SQL migration files and checksums are unchanged. Unrelated shared skills and proprietary material are not part of the chat export.

Background jobs are included as a stable, opt-in systemd backend; read [the runbook](background-jobs.md) before loading it. Components remain opt-in. Updating this repository does not install or replace a running service.

This refresh adds profile reloading, conversation modes and bounded Discord reconnect recovery, the session task ledger and advisory sharing, standalone session HTML export, and background-job capacity/history bounds. Portable profiles retain explicit configurable providers instead of an upstream provider default. Optional session export uses only synthetic fixtures; no real transcripts or generated exports are included.

The task extension snapshot includes agent unblocking, an explicit colored progress state, and a one-task progress limit enforced by both tools and menu. Existing over-limit snapshots are preserved and can drain. This follow-up adds stale-reminder filtering, status-priority widget display, popup-scoped menu shortcuts, criteria editing and preview options. It also updates the timer countdown and background-job manager-less CI refusal coverage. Background-job runtime behavior and its safety/privacy limits are unchanged.
