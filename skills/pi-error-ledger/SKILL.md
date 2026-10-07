---
name: pi-error-ledger
description: Inspect captured Pi tool failures and trends, look up reviewed fixes, and prepare evidence-based fix proposals in the configured ledger.
---

Run `python -m tools.ledger.error_cli` from the repository root. Read-only commands use `PI_LEDGER_DSN`; ingestion, migrations, review marks, and fix publication use a separately scoped `PI_LEDGER_WRITER_DSN`. Keep DSNs and owner-local details out of prompts and public artifacts.

Start with `errors`, `trends`, `patterns`, or `fixes`; use an `err-...` reference for targeted lookup. Check existing fixes before proposing another. Structured `match` results are ranked candidates, not calibrated confidence or authorization to change anything.

Use `draft-fix`, `check-fix`, and `fix-evidence` to prepare and inspect a proposal. Cite occurrence IDs; keep raw logs owner-local. A changed pattern, instruction, or applicability needs renewed owner review and a fresh export. Publish only after the required owner approval using a mode-0600 JSON document and publisher-scoped credentials. Reuse the fix ID for updates. `corroborate-fix` adds evidence without silently changing reviewed content.

Read [ledger operations](../../docs/ledger.md) for migration, ingestion, matching, export, and private-storage boundaries. If the database or matcher dependency is missing, report that limitation instead of fabricating a lookup result.
