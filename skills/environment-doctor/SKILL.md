---
name: environment-doctor
description: Use when checking an agent's local tools, Python interpreter dependencies, installed skill links and discovery, MCP evidence, or explicitly selected readiness endpoints without repairs or model calls.
license: MIT
compatibility: Python 3.10+ on Linux/POSIX; trusted local executables on PATH. Windows is not supported.
metadata:
  author: Morrow
---

# Environment doctor

Portable source with no required third-party Python packages. Verified on Linux;
POSIX process groups, pipe selectors and symlink semantics are required. Other
POSIX systems have not been tested; Windows is not supported. Version probes run
local executables and import probes load installed Python modules: this is not
a sandbox for untrusted launchers or packages.

Run the portable read-only inventory in the agent's normal shell/Pi environment:

```bash
python3 {baseDir}/scripts/doctor.py
```

The JSON includes safe tool versions (including `promtool`), the **actual selected
Python executable/prefix**, common module imports and their owning distribution
versions (`discord` is the import; `discord.py` is its distribution), recursive
broken skill links, distinct sources declaring duplicate skill names, and explicit
coverage states. It never installs, repairs, loads dotenv files, starts MCP servers,
submits generation, or calls model providers. Morning briefs remain owner-specific.

Defaults inspect the current Python and `PI_CODING_AGENT_DIR` (otherwise
`~/.pi/agent`) skills/MCP declarations. To compare a skill's documented interpreter,
repeat `--python /path/to/its/venv/bin/python`; do not guess every available venv
or add packages simply to equalize inventories. Resolve the skill entrypoint/shebang
and its declared requirements first. The import list is a common capability
inventory, not a requirement that every interpreter contain every package.

Use `--skill-root PATH` for each installed discovery root, including a separate
Codex root where relevant. Repeated references to the same resolved `SKILL.md`
are deduplicated; different files claiming one name within a root are reported.
Skill names are discovered only down to the first `SKILL.md` in each directory
branch, matching Pi's skill-root boundary. Recursive script-link checking continues
below that boundary. Two different discoverable sources declaring one name remain
a collision, even if Pi chooses one winner. Readable invalid names appear in
`name_warnings` with validation codes and `unknown` coverage; they are distinct
from unreadable or unsupported frontmatter. This bounded scalar parser does not
implement Pi's complete YAML/ignore-file configuration.

Scanning follows skill symlinks, including nested script links, but skips `.git`,
`node_modules`, `.venv`, and `__pycache__`. Limits and unreadable entries produce
`unknown`, not a clean result. This filesystem check does not prove a running
Pi session has reloaded its commands.

MCP declarations alone cannot establish connectivity. Supply an **existing**
`pi mcp list --json` snapshot with `--mcp-status PATH`; only names, enabled state,
connection classification and tool counts are emitted. Snapshot freshness remains
explicitly unverified; obtain a new snapshot through the owner's normal supported
MCP entrypoint when needed. That command can start configured transports (including
package-manager launchers), so this doctor does not invoke it automatically.
No configured server means `unconfigured`; a disabled server is `dormant`.
Unknown list states stay `unknown`; raw transport configuration, errors and tool
arguments are never copied. This adapter covers Pi JSON, not Codex TOML or other
MCP clients; use their native evidence separately.

Optional checks require deliberate selection:

```bash
python3 {baseDir}/scripts/doctor.py \
  --python /path/to/supported/venv/bin/python \
  --skill-root /path/to/installed/skills \
  --checkout /path/to/the/one/shared/checkout \
  --link ~/.pi/agent/skills/example \
  --cron-script /path/to/shared/cron/persistence.py \
  --cron-config /path/to/owner/cron/source.json \
  --service chroma=http://localhost:8000/api/v2/heartbeat
```

The link adapter takes exactly one declared `--checkout` and up to sixteen
`--link` paths; `--link` without `--checkout` is an invocation error. Installed
links are expected to resolve inside that checkout, so containment is decided on
resolved targets rather than on link text: a `..` escape, a staging directory or an
extracted release tree is reported as `outside-checkout` and **fails**, as a broken
link does with `broken-link`. Warnings never fail: `modified-target` and
`untracked-target` describe a checkout that moved or was edited, which is ordinary
operation, and `absolute-target` describes link text that is portable-bad but still
resolves correctly. The checkout's `version` reports `head`, `tagged` and `describe`
from `describe --exact-match`, because a clean checkout advanced to `main` is still
not the reviewed bytes; a checkout held at a release tag names its own version. Git
is read with `--no-optional-locks` and ambient `GIT_*` variables removed, so the
index is not refreshed, nothing is fetched, and no other repository can answer a
query for this one.

The cron persistence helper is **not bundled in pi-chat**. Omit both cron flags
unless you separately provide and trust a compatible helper. This is an optional
adapter, not an installation prerequisite.

The optional cron adapter runs only the reviewed persistence helper's `status`
operation, passing `AGENT_CRON_CONFIG` by reference; missing/unreleased helpers are
not dependencies. It reports hashes/counts/source/owner/match only, never schedules.
An unsuccessful status is `unknown`; inspect the helper directly for its bounded
diagnosis. No install or schedule replacement occurs.

Select only known harmless readiness URLs; service GETs emit status only and do
not use environment proxies, follow redirects or read response bodies. Credentials, query strings and
fragments are rejected. No endpoints are inferred from credentials or scanned.
Checks default to five seconds each (maximum ten); process output is capped while
running. Interpreter/root/config/service arguments accept at most four values each.

`coverage` and Python `import_coverage` must accompany a summary. `missing`,
`unconfigured`, `dormant` and `unknown` are distinct; an absent optional capability
is not a runtime defect without a failing supported task. Exit 1 indicates a
confirmed failed selected check, 0 means the inventory completed **and can still
contain unknown or missing coverage**, and 2 means invalid invocation. **Exit 0
is not a clean-health verdict**: summarize coverage states alongside the exit
code. Keep results owner-local unless reviewed: paths, installed skill names and
service labels can
still describe a private environment even though credential values are suppressed.

## Offline tests

From the repository root:

```sh
python3 -m unittest discover -s skills/environment-doctor/tests -q
```

Tests use temporary files, synthetic Git repositories and loopback/mocked service
evidence. They do not scan a real owner's environment or contact model providers.
