# Source provenance

## Extension parity refresh — 2026-10-26

The native-SelectList fix is ported from verified signed skills tag `v0.16.2`,
peeled commit `391c8b3cd0c6a5317b2da6b78ce5b127cd3b1009` (signature checked
against the repository-pinned release certificate). `tasks.ts` and
`lib/bounded-menu.ts` match that source byte-for-byte. Tests cover real Pi
SelectList behavior, wraparound, configured bindings, styles, resize and mouse
navigation; synthetic public task fixtures remain intact.

The attention-lease formatter also includes the authored-quote fix from bridge
commit `77f014f9c62404f4794d2cf6c74ab54db7c20eca`: only an MXID-marked reply
fallback is stripped. Its implementation matches current bridge main's extension;
no other bridge/server changes are imported.

Two previously owner-local helpers are now selectively ported with offline tests:
`model-control.ts` (explicit local approval or delegated judge) and
`next-prompt/` (bounded conversation-to-editor drafting). Model control resolves
the adjacent public menu helper, not an owner installation. Prompt drafting has
no hardcoded provider/model: both require explicit environment configuration.
Neither is installed/enabled by this refresh. See [session assistants](session-assistants.md).

The remaining shared extension, web and observability implementations were
compared with current private sources. Existing differences are retained public
adaptations, not missing fixes: configurable providers and room-label paths,
generic fixtures, and omission of deployment-specific plan injection, calibration,
shadow services and schema assumptions. Public short-context aliases already
include stricter validation and provider-boundary ID translation than the local
variant; its owner-specific model list is not copied. No runtime subagent config,
real identities, credentials, transcripts, logs or generated exports are included.
The previous bridge, runtime, ledger migration and unrelated-skill boundaries
remain unchanged. All components remain opt-in.

## TUI/task/job refresh — 2026-10-26

The existing public task, question, shell and background-job extensions and their
implementation helpers/tests are refreshed from verified signed skills tag
`v0.16.1`, peeled commit `59af0b4011dab6931b29d24a0e22f894cfab848b`.
This selective port includes bounded/themed menus, preserved ANSI status colors,
consistent navigation and local action keys, stable widgets, task-scoped actions,
request-stop semantics, and colored/human-readable job status/resources.

The public Pi SDK baseline remains `0.99.1`; no runtime image or provider defaults
are changed. Fixtures keep generic reviewer names/references. The bridge baseline
and all previous exclusions remain unchanged: no announcement-feed policy,
private routing, profiles, deployment orchestration, credentials, logs or generated
exports. Owner-local prompt-suggestion and model-approval helpers are not added.
Components stay opt-in; publishing does not install or restart a service.

## Previous tagged refresh — 2026-10-25

| Component source | Verified tag | Peeled commit |
| --- | --- | --- |
| Shared skills and extensions | `v0.15.0` | `d210d210cd9514074bbd86cc0a09159f9f2ea809` |
| Shared bridge | `v0.9.0` | `aef51fd31f04deb3490cb34e9ea3e192252a7edb` |

Release signatures were checked against the pinned release certificate. This is
a selective public port, not an exact tag mirror or an installed-runtime claim.
The approved additions are task upgrades, attention bells (including a minimal
tmux example), workload metrics and chat-control/indicator improvements, with
their existing-component fixes, regression tests and documentation.

**Announcement-feed policy remains excluded.** The later approved support-skill
addition includes environment-doctor and tmux, described below. The
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

## Approved support skills — 2026-10-25

`skills/environment-doctor/` and `skills/tmux/` are exported from the same verified
skills `v0.15.0` commit above, including their bundled helpers and tests. Morrow's
original authorship is retained; these public copies are released under this
repository's MIT license. No proprietary third-party skill is imported.

Public adaptations document Linux/POSIX limits, optional external cron-helper
requirements, report privacy and non-health-verdict exit semantics. Test fixtures
isolate Git identity/configuration and tmux server environments; doctor Python
files are formatted to the public lint rules. The tmux agent-pane
reference uses a Bash selector array to preserve socket paths with spaces and
remains explicitly proposed/manual guidance, not an automated control
implementation. Neither skill requires bridge credentials or a model provider.
Doctor version/import probes still execute trusted local programs/modules; it is
not an execution sandbox. No skill is installed by this export.

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

## Approved public runtime variant — 2026-10-25

The root `Dockerfile` and `runtime/` are a new minimal public implementation,
informed by the reviewed runtime `v0.4.0` design, not an exact export of its
image or service orchestration. Only public digest-pinned bases and package
sources are used. Pi is pinned to the public repository's existing `0.99.1`
SDK baseline, rather than silently upgrading the extension API. Python
Playwright is included without browsers; Chroma and Redis remain excluded.

Private browser-seed layers, roles, registry/deployment configuration, startup
hooks, SSH server, sudo and owner workspaces are not imported. Supervisor owns
the bridge and the bridge owns Pi RPC children. Generic examples require
explicit runtime credentials and selected providers; no live service is
installed or deployed, and no image is pushed to a registry by the test suite.
The systemd background-job extension remains unsupported in this container.
