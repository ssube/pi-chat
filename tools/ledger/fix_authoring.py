"""Private proposal preparation and evidence-only updates to approved fixes."""

from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path

from .error_ledger import fix_content_sha256, validate_fix_document

REFERENCE_RE = re.compile(r"(?:err|pierr|pitool)-[0-9a-f][0-9a-f-]{5,35}")


def read_private_document(path: Path) -> dict:
    """Read a bounded, owner-private regular file without following symlinks."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "r") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            raise ValueError("Fix document must be a regular owner-private file")
        text = handle.read(65537)
        if len(text) > 65536:
            raise ValueError("Fix document is too large")
        value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("Fix document must be an object")
    return value


def write_private_document(path: Path, value: dict) -> None:
    """Never overwrite an existing draft or follow an output symlink."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        json.dump(value, handle, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def resolve_evidence(connection, references: list[str], *, errors_only: bool = False) -> list[tuple]:
    if not references or len(references) > 16:
        raise ValueError("Provide between 1 and 16 evidence references")
    found = {}
    with connection.cursor() as cursor:
        for reference in references:
            if not isinstance(reference, str) or not REFERENCE_RE.fullmatch(reference):
                raise ValueError("Invalid evidence reference")
            prefix = "pi" + reference if reference.startswith("err-") else reference
            cursor.execute("""SELECT id, agent, signature, outcome
                              FROM pi_tool_call.pi_tool_call_occurrence
                              WHERE id LIKE %s ORDER BY id LIMIT 2""", (prefix + "%",))
            rows = cursor.fetchall()
            if not rows:
                raise ValueError("Evidence not found; it may still await ingestion")
            if len(rows) != 1:
                raise ValueError("Ambiguous evidence reference; use a longer prefix")
            row = rows[0]
            if errors_only and row[3] != "error":
                raise ValueError("Drafts must start from error occurrences")
            found[row[0]] = row
    return [found[key] for key in sorted(found)]


def approved_fix(connection, fix_id: str, *, lock: bool = False) -> dict | None:
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{2,79}", fix_id):
        raise ValueError("Invalid fix ID")
    with connection.cursor() as cursor:
        cursor.execute("""SELECT f.title, f.signature, f.match_pattern,
                                 f.redacted_instruction, f.source, f.approved_digest,
                                 array(SELECT agent FROM pi_tool_call.pi_tool_call_fix_owner
                                       WHERE fix_id = f.id ORDER BY agent),
                                 array(SELECT agent FROM pi_tool_call.pi_tool_call_fix_applies_to
                                       WHERE fix_id = f.id ORDER BY agent),
                                 array(SELECT skill FROM pi_tool_call.pi_tool_call_fix_required_skill
                                       WHERE fix_id = f.id ORDER BY skill)
                          FROM pi_tool_call.pi_tool_call_known_fix f
                          WHERE f.id = %s AND f.active AND f.approved_at IS NOT NULL""" +
                       (" FOR UPDATE OF f" if lock else ""), (fix_id,))
        rows = cursor.fetchall()
    if not rows:
        return None
    title, signature, pattern, instruction, source, digest, owners, applies, skills = rows[0]
    if fix_content_sha256(pattern, instruction, owners, applies, skills) != digest:
        raise ValueError("Fix content changed since approval; fresh review required")
    return dict(id=fix_id, title=title, signature=signature, pattern=pattern,
                fix=instruction, source=source, owners=owners, applies_to_agents=applies,
                required_skills=skills, base_content_sha256=digest)


def draft_fix(connection, fix_id: str, references: list[str]) -> dict:
    evidence = resolve_evidence(connection, references, errors_only=True)
    value = approved_fix(connection, fix_id)
    if value is None:
        if len({row[2] for row in evidence}) != 1:
            raise ValueError("New fix evidence has different signatures; choose one error class")
        value = dict(id=fix_id, title="", signature=evidence[0][2], pattern="", fix="",
                     owners=sorted({row[1] for row in evidence}),
                     applies_to_agents=[], required_skills=[], source="")
    # Approval is deliberately absent. Neither stderr nor exit codes are copied
    # into instructions or guessed into a regex.
    return dict(value, occurrence_ids=[row[0] for row in evidence])


def check_fix(connection, value: dict) -> dict:
    proposal = validate_fix_document(value, require_approval=False)
    if proposal["occurrence_ids"]:
        resolve_evidence(connection, proposal["occurrence_ids"])
    return proposal


def fix_evidence(connection, fix_id: str) -> list[tuple]:
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{2,79}", fix_id):
        raise ValueError("Invalid fix ID")
    with connection.cursor() as cursor:
        cursor.execute("""SELECT e.id, e.agent, e.outcome, e.occurred_at
                          FROM pi_tool_call.pi_tool_call_occurrence_fix link
                          JOIN pi_tool_call.pi_tool_call_occurrence e ON e.id = link.occurrence_id
                          WHERE link.fix_id = %s ORDER BY e.occurred_at, e.id""", (fix_id,))
        return cursor.fetchall()


def corroborate_fix(connection, value: dict) -> int:
    """Attach cited evidence only; never replace content, scope, or approval."""
    proposal = validate_fix_document(value, require_approval=False)
    with connection.transaction():
        current = approved_fix(connection, proposal["id"], lock=True)
        if current is None:
            raise ValueError("Fix is not active and approved")
        digest = fix_content_sha256(proposal["pattern"], proposal["fix"],
                                    proposal["owners"], proposal["applies_to_agents"],
                                    proposal["required_skills"])
        if (proposal.get("base_content_sha256") != current["base_content_sha256"] or
                digest != current["base_content_sha256"] or
                any(proposal[key] != current[key] for key in ("title", "signature", "source"))):
            raise ValueError("Fix or draft changed; review and publish changes separately")
        evidence = resolve_evidence(connection, proposal["occurrence_ids"])
        inserted = 0
        with connection.cursor() as cursor:
            for occurrence_id, _, _, _ in evidence:
                cursor.execute("""INSERT INTO pi_tool_call.pi_tool_call_occurrence_fix
                                  (occurrence_id, fix_id) VALUES (%s, %s)
                                  ON CONFLICT DO NOTHING""", (occurrence_id, proposal["id"]))
                inserted += cursor.rowcount
    return inserted
