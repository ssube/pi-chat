# Optional agent skills

Load a skill explicitly with `pi --skill ./skills/NAME` from the repository root.
Nothing is installed, configured or authorized merely by adding these files.

| Skill | Use |
| --- | --- |
| [environment-doctor](environment-doctor/SKILL.md) | Bounded local tool, interpreter, skill-link and supplied MCP-evidence inventory; explicit readiness checks only. |
| [tmux](tmux/SKILL.md) | Discover/read panes and insert literal single-line input into an exact authorized pane. |
| [matrix](matrix/SKILL.md) | Canonical configured Matrix operations. |
| [matrix-read](matrix-read/SKILL.md) | Compatibility guidance for read-only Matrix operations. |
| [message-agent](message-agent/SKILL.md) | Messaging through configured identities and routes. |
| [bridge-awareness](bridge-awareness/SKILL.md) | Bridge attention, awareness and transport context. |
| [review-pr](review-pr/SKILL.md) | Scoped PR review runner and attributable verdicts. |
| [pi-error-ledger](pi-error-ledger/SKILL.md) | Configured ledger evidence and reviewed fixes. |

## Support-skill requirements and limits

Doctor and tmux use Python 3.10+ and the standard library. Both are verified on
Linux; other POSIX platforms have not been tested, and Windows is not supported.
Tmux additionally requires its binary and the intended owner's socket. Their
helpers and tests live inside each skill directory, with no bridge dependency.

Doctor performs no automatic repair, install or model call. It does execute
trusted local version probes and Python imports, so it is not a sandbox. Missing
optional capabilities are not installation requirements, and exit 0 can include
unknown/missing coverage. Cron integration requires a separately supplied helper;
none is bundled here. Reports and pane captures can contain private paths, names
or conversation text: inspect them before sharing.

Tmux sends Enter by default, which can execute a command in a shell pane. Read
first, retain the selected server, and verify the result afterward. Delivery is
not completion. The [bell snippet](../docs/attention-bells.md) is independent of
this pane-control skill.
