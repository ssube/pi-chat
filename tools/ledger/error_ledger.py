"""Shared metadata ledger. Detail files and ingestion queues stay with their owner."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .privacy import check_shared

def valid_agent(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9_-]{0,79}", value) is not None

ID_RE = re.compile(r"^(?:pierr|pitool)-[0-9a-fA-F-]{36}$")
SKILL_ID_RE = re.compile(r"^piskill-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
SKILL_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
REQUIRED = {"id", "agent", "origin", "occurred_at", "kind", "outcome",
            "tool_name", "signature", "detail_ref", "detail_sha256", "redacted_summary"}
SCHEMA_VERSION = 6


def connect(*, read_only: bool = False):
    dsn = os.environ.get("PI_LEDGER_DSN" if read_only else "PI_LEDGER_WRITER_DSN")
    if not dsn:
        raise RuntimeError("A scoped tool-call ledger DSN is required")
    try:
        import psycopg
    except ImportError as error:
        raise RuntimeError("Install psycopg[binary] for the Pi ledger") from error
    connection = psycopg.connect(dsn, connect_timeout=10)
    if read_only:
        connection.read_only = True
    return connection


def migrate(connection, repo: Path) -> str:
    """Apply the bundled migration exactly once; fail closed on drift/newer DBs."""
    migrations = [repo / "sql" / "001_error_ledger.sql",
                  repo / "sql" / "002_fix_suggestions.sql",
                  repo / "sql" / "003_redacted_command.sql",
                  repo / "sql" / "004_skill_events.sql",
                  repo / "sql" / "005_duration_ms.sql",
                  repo / "sql" / "006_structured_command_match.sql"]
    expected = [(version, hashlib.sha256(path.read_bytes()).hexdigest())
                for version, path in enumerate(migrations, start=1)]
    with connection.transaction():
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(79452113)")
            cursor.execute("CREATE SCHEMA IF NOT EXISTS pi_tool_call")
            cursor.execute("""CREATE TABLE IF NOT EXISTS pi_tool_call.pi_tool_call_schema_version (
                version integer PRIMARY KEY, migration text NOT NULL,
                sha256 text NOT NULL, applied_at timestamptz NOT NULL DEFAULT now())""")
            cursor.execute("SELECT version, sha256 FROM pi_tool_call.pi_tool_call_schema_version ORDER BY version")
            versions = cursor.fetchall()
            if any(version > SCHEMA_VERSION for version, _ in versions):
                raise RuntimeError("Database schema is newer than this ledger package")
            if versions != expected[:len(versions)]:
                raise RuntimeError("Database schema migration checksum or history differs")
            if len(versions) == len(migrations):
                return "already applied"
            for version, path in enumerate(migrations[len(versions):], start=len(versions) + 1):
                cursor.execute(path.read_text())
                cursor.execute("""INSERT INTO pi_tool_call.pi_tool_call_schema_version
                    (version, migration, sha256) VALUES (%s, %s, %s)""",
                    (version, path.name, expected[version - 1][1]))
    return f"applied version {SCHEMA_VERSION}"


def validate_occurrence(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or not REQUIRED <= value.keys():
        raise ValueError("Occurrence is missing required fields")
    item = {key: value[key] for key in REQUIRED}
    item["redacted_command"] = value.get("redacted_command")
    if item["redacted_command"] is not None and (
        not isinstance(item["redacted_command"], str) or
        len(item["redacted_command"]) > 2048
    ):
        raise ValueError("Invalid redacted_command")
    duration = value.get("duration_ms")
    if duration is not None and (
        isinstance(duration, bool) or not isinstance(duration, int)
        or duration < 0 or duration > 2147483647
    ):
        raise ValueError("Invalid duration_ms")
    item["duration_ms"] = duration
    exit_code = value.get("exit_code")
    if exit_code is not None and (isinstance(exit_code, bool) or not isinstance(exit_code, int)
                                  or not 0 <= exit_code <= 255):
        raise ValueError("Invalid exit code")
    item["exit_code"] = exit_code
    suggested = value.get("matched_fix_ids", [])
    if not isinstance(suggested, list) or len(suggested) > 16 or any(
        not isinstance(fix_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{2,79}", fix_id)
        for fix_id in suggested
    ):
        raise ValueError("Invalid matched fix IDs")
    item["matched_fix_ids"] = sorted(set(suggested))
    trials = value.get("candidate_matches", [])
    if not isinstance(trials, list) or len(trials) > 10:
        raise ValueError("Invalid candidate matches")
    seen_trials = set()
    for trial in trials:
        if not isinstance(trial, dict) or set(trial) != {"fix_id", "score", "matcher_version",
                                                       "disposition", "shown_to_user"}:
            raise ValueError("Invalid candidate match")
        if (not isinstance(trial["fix_id"], str) or
            not re.fullmatch(r"[a-z0-9][a-z0-9_-]{2,79}", trial["fix_id"]) or
            trial["fix_id"] in seen_trials or
            isinstance(trial["score"], bool) or not isinstance(trial["score"], int) or
            not 0 <= trial["score"] <= 100 or
            trial["matcher_version"] != 1 or
            trial["disposition"] != "conditional_hint" or
            not isinstance(trial["shown_to_user"], bool)):
            raise ValueError("Invalid candidate match")
        seen_trials.add(trial["fix_id"])
    item["candidate_matches"] = trials
    if not isinstance(item["id"], str) or not ID_RE.fullmatch(item["id"]):
        raise ValueError("Invalid occurrence ID")
    if not valid_agent(item["agent"]):
        raise ValueError("Invalid agent")
    if item["kind"] not in {"tool", "provider", "session"}:
        raise ValueError("Invalid kind")
    if item["outcome"] not in {"error", "success"}:
        raise ValueError("Invalid outcome")
    for key, limit in (("origin", 80), ("signature", 300), ("redacted_summary", 500)):
        if not isinstance(item[key], str) or not item[key].strip() or len(item[key]) > limit:
            raise ValueError(f"Invalid {key}")
    if not re.fullmatch(r"[a-z][a-z0-9_-]*", item["origin"]):
        raise ValueError("Invalid origin")
    if item["tool_name"] is not None and (
        not isinstance(item["tool_name"], str) or len(item["tool_name"]) > 100
    ):
        raise ValueError("Invalid tool_name")
    if (item["detail_ref"] is None) != (item["detail_sha256"] is None):
        raise ValueError("Detail reference and hash must be paired")
    if item["outcome"] == "success" and item["detail_ref"] is not None:
        raise ValueError("Successful calls must not include detail files")
    if item["detail_ref"] is not None:
        if (not isinstance(item["detail_ref"], str) or
                len(item["detail_ref"]) > 1024 or
                not Path(item["detail_ref"]).is_absolute() or
                not isinstance(item["detail_sha256"], str) or
                not HASH_RE.fullmatch(item["detail_sha256"])):
            raise ValueError("Invalid detail reference or hash")
    try:
        stamp = datetime.fromisoformat(item["occurred_at"].replace("Z", "+00:00"))
    except (TypeError, ValueError, AttributeError) as error:
        raise ValueError("Invalid occurred_at") from error
    if stamp.tzinfo is None:
        raise ValueError("occurred_at needs a timezone")
    item["occurred_at"] = stamp.astimezone(timezone.utc)
    return item


def validate_skill_event(value: Any) -> dict[str, Any]:
    """Accept only bounded skill metadata; reject prompts, paths, and extra fields."""
    keys = {"id", "agent", "origin", "occurred_at", "skill_name", "event_kind", "source"}
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError("Skill event must contain only the required metadata")
    item = dict(value)
    if not isinstance(item["id"], str) or not SKILL_ID_RE.fullmatch(item["id"]):
        raise ValueError("Invalid skill event ID")
    if not isinstance(item["agent"], str) or not valid_agent(item["agent"]):
        raise ValueError("Invalid skill event agent")
    if (not isinstance(item["origin"], str) or
            not re.fullmatch(r"[a-z][a-z0-9_-]{0,79}", item["origin"])):
        raise ValueError("Invalid skill event origin")
    if (not isinstance(item["skill_name"], str) or len(item["skill_name"]) > 64 or
            not SKILL_NAME_RE.fullmatch(item["skill_name"])):
        raise ValueError("Invalid skill name")
    if (not isinstance(item["event_kind"], str) or
            not isinstance(item["source"], str) or
            (item["event_kind"], item["source"]) not in {
                ("loaded", "read"), ("invoked", "skill_command")
            }):
        raise ValueError("Invalid skill event kind or source")
    try:
        stamp = datetime.fromisoformat(item["occurred_at"].replace("Z", "+00:00"))
    except (TypeError, ValueError, AttributeError) as error:
        raise ValueError("Invalid skill event occurred_at") from error
    if stamp.tzinfo is None:
        raise ValueError("Skill event occurred_at needs a timezone")
    item["occurred_at"] = stamp.astimezone(timezone.utc)
    return item


def _load_outbox_record(path: Path) -> tuple[bool, dict]:
    if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
        raise ValueError(f"Outbox entry must be a regular mode-0600 file: {path.name}")
    if path.stat().st_size > 65536:
        raise ValueError(f"Outbox entry exceeds 64 KiB: {path.name}")
    skill_event = path.name.startswith("piskill-")
    item = (validate_skill_event if skill_event else validate_occurrence)(json.loads(path.read_text()))
    if path.name != item["id"] + ".json":
        raise ValueError("Outbox filename must match record ID")
    return skill_event, item


def ingest_one(connection, path: Path, processed_dir: Path, *, record=None) -> bool:
    """Commit first, then acknowledge by moving the immutable outbox file."""
    skill_event, item = record if record is not None else _load_outbox_record(path)
    with connection.transaction():
        with connection.cursor() as cursor:
            if skill_event:
                cursor.execute("""INSERT INTO pi_tool_call.pi_skill_event
                    (id, agent, origin, occurred_at, skill_name, event_kind, source)
                    VALUES (%(id)s, %(agent)s, %(origin)s, %(occurred_at)s,
                            %(skill_name)s, %(event_kind)s, %(source)s)
                    ON CONFLICT (id) DO NOTHING""", item)
            else:
                cursor.execute("""INSERT INTO pi_tool_call.pi_tool_call_occurrence
                    (id, agent, origin, occurred_at, kind, outcome, tool_name,
                     signature, detail_ref, detail_sha256, redacted_summary,
                     redacted_command, duration_ms)
                    VALUES (%(id)s, %(agent)s, %(origin)s, %(occurred_at)s, %(kind)s,
                            %(outcome)s, %(tool_name)s, %(signature)s, %(detail_ref)s,
                            %(detail_sha256)s, %(redacted_summary)s,
                            %(redacted_command)s, %(duration_ms)s)
                    ON CONFLICT (id) DO NOTHING""", item)
            inserted = cursor.rowcount == 1
            if not skill_event:
                if item["tool_name"] == "bash" and item["redacted_command"] is not None:
                    from .command_match import parse_or_abstain, store_parse
                    parsed = parse_or_abstain(item["redacted_command"],
                                              item["redacted_summary"], item["exit_code"])
                    store_parse(cursor, item["id"], parsed)
                for fix_id in item["matched_fix_ids"]:
                    cursor.execute("""INSERT INTO pi_tool_call.pi_tool_call_suggestion
                        (occurrence_id, fix_id) VALUES (%s, %s) ON CONFLICT DO NOTHING""",
                        (item["id"], fix_id))
                for trial in item["candidate_matches"]:
                    cursor.execute("""INSERT INTO pi_tool_call.pi_match_trial
                        (occurrence_id, fix_id, matcher_version, score,
                         disposition, shown_to_user)
                        VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING""",
                        (item["id"], trial["fix_id"], trial["matcher_version"],
                         trial["score"], trial["disposition"], trial["shown_to_user"]))
    processed_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(processed_dir, 0o700)
    destination = processed_dir / path.name
    if destination.exists():
        if hashlib.sha256(destination.read_bytes()).digest() != hashlib.sha256(path.read_bytes()).digest():
            raise ValueError("Processed occurrence differs from queued occurrence")
        path.unlink()
    else:
        path.replace(destination)
    return inserted


def ingest_outbox(connection, outbox: Path, processed_dir: Path) -> tuple[int, int]:
    inserted = acknowledged = 0
    # Drain existing tool/error telemetry before new skill records. A missing
    # skill-table grant must never hold unrelated tool rows behind it.
    for path in sorted(outbox.glob("*.json"), key=lambda item: (item.name.startswith("piskill-"), item.name)):
        record = None
        if path.name.startswith("piskill-"):
            # Only malformed *payloads* are quarantined. Invalid file permissions,
            # symlinks, DB failures, and acknowledgement failures still fail closed.
            if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077 or path.stat().st_size > 65536:
                raise ValueError("Unsafe skill outbox entry")
            try:
                record = _load_outbox_record(path)
            except (ValueError, TypeError, UnicodeError, json.JSONDecodeError):
                rejected = outbox / "rejected"
                rejected.mkdir(mode=0o700, exist_ok=True)
                if rejected.is_symlink() or rejected.stat().st_mode & 0o077:
                    raise ValueError("Unsafe rejected skill directory") from None
                destination = rejected / path.name
                if destination.exists() or destination.is_symlink():
                    raise ValueError("Rejected skill record already exists") from None
                path.replace(destination)
                logging.getLogger(__name__).warning("Quarantined malformed skill record; inspect owner-local rejected directory")
                continue
        if ingest_one(connection, path, processed_dir, record=record):
            inserted += 1
        acknowledged += 1
    return inserted, acknowledged


def unreviewed_count(connection) -> int:
    with connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM pi_tool_call.pi_tool_call_occurrence WHERE outcome = 'error' AND reviewed_at IS NULL")
        return cursor.fetchone()[0]


def trends(connection, *, limit: int = 20) -> list[tuple]:
    with connection.cursor() as cursor:
        cursor.execute("""SELECT signature, count(*) AS total,
                           count(*) FILTER (WHERE occurred_at >= now() - interval '7 days') AS recent,
                           array_agg(DISTINCT agent ORDER BY agent) AS agents,
                           min(occurred_at) AS first_seen, max(occurred_at) AS last_seen,
                           count(*) FILTER (WHERE reviewed_at IS NULL) AS unreviewed
                        FROM pi_tool_call.pi_tool_call_occurrence WHERE outcome = 'error'
                        GROUP BY signature
                        ORDER BY unreviewed DESC, recent DESC, total DESC LIMIT %s""", (limit,))
        return cursor.fetchall()


_DEFAULT_SINCE = "14 days"


def _parse_since(since: str) -> datetime:
    """Translate a '<int> hours|days' window into a UTC cutoff timestamp."""
    parts = (since or "").strip().split()
    if (len(parts) != 2 or not parts[0].isdigit()
            or parts[1] not in {"hour", "hours", "day", "days"}):
        raise ValueError(f"unsupported since window: {since!r} "
                         "(use for example '24 hours' or '7 days')")
    delta = (timedelta(hours=int(parts[0])) if parts[1].startswith("hour")
             else timedelta(days=int(parts[0])))
    return datetime.now(timezone.utc) - delta


def recent_errors(connection, *, since: str | None = None,
                  agent_for_errors: str | None = None, unresolved_only: bool = False,
                  limit: int = 50) -> list[tuple]:
    """Shared redacted error index; detail files remain with their owner."""
    clauses = ["e.outcome = 'error'"]
    params: list = []
    if since is not None:
        clauses.append("e.occurred_at >= %s")
        params.append(_parse_since(since))
    if agent_for_errors is not None:
        clauses.append("e.agent = %s")
        params.append(agent_for_errors)
    if unresolved_only:
        clauses.append("e.reviewed_at IS NULL AND e.resolved_by_pr IS NULL")
    params.append(limit)
    with connection.cursor() as cursor:
        cursor.execute(f"""SELECT e.id, e.agent, e.origin, e.occurred_at,
                               e.signature, e.redacted_summary, e.reviewed_at,
                               e.resolved_by_pr, e.redacted_command, e.duration_ms,
                               array(SELECT s.fix_id FROM pi_tool_call.pi_tool_call_suggestion s
                                     WHERE s.occurrence_id = e.id ORDER BY s.fix_id)
                        FROM pi_tool_call.pi_tool_call_occurrence e
                        WHERE {' AND '.join(clauses)}
                        ORDER BY e.occurred_at DESC LIMIT %s""", params)
        return cursor.fetchall()


def recent_successes(connection, *, since: str | None = None,
                     limit: int = 50) -> list[tuple]:
    """Lightweight successes for comparing working commands with failures."""
    clauses = ["outcome = 'success'"]
    params: list = []
    if since is not None:
        clauses.append("occurred_at >= %s")
        params.append(_parse_since(since))
    params.append(limit)
    with connection.cursor() as cursor:
        cursor.execute(f"""SELECT id, agent, origin, occurred_at, tool_name,
                               signature, redacted_command, duration_ms
                        FROM pi_tool_call.pi_tool_call_occurrence
                        WHERE {' AND '.join(clauses)}
                        ORDER BY occurred_at DESC LIMIT %s""", params)
        return cursor.fetchall()


def command_patterns(connection, *, limit: int = 50) -> list[tuple]:
    """Group exact redacted bash commands by observed outcome."""
    with connection.cursor() as cursor:
        cursor.execute("""SELECT redacted_command,
                               count(*) FILTER (WHERE outcome = 'success') AS successes,
                               count(*) FILTER (WHERE outcome = 'error') AS errors,
                               array_agg(DISTINCT agent ORDER BY agent) AS agents,
                               max(occurred_at) AS last_seen
                        FROM pi_tool_call.pi_tool_call_occurrence
                        WHERE kind = 'tool' AND tool_name = 'bash'
                          AND redacted_command IS NOT NULL
                        GROUP BY redacted_command
                        ORDER BY errors DESC, successes DESC, last_seen DESC
                        LIMIT %s""", (limit,))
        return cursor.fetchall()


def fix_catalog(connection) -> list[tuple]:
    """All reviewed fixes are visible; applicability controls injection only."""
    with connection.cursor() as cursor:
        cursor.execute("""SELECT f.id, f.title, f.redacted_instruction, f.active,
                               array(SELECT o.agent FROM pi_tool_call.pi_tool_call_fix_owner o
                                     WHERE o.fix_id = f.id ORDER BY o.agent),
                               array(SELECT a.agent FROM pi_tool_call.pi_tool_call_fix_applies_to a
                                     WHERE a.fix_id = f.id ORDER BY a.agent),
                               array(SELECT s.skill FROM pi_tool_call.pi_tool_call_fix_required_skill s
                                     WHERE s.fix_id = f.id ORDER BY s.skill),
                               (SELECT count(*) FROM pi_tool_call.pi_tool_call_suggestion shown
                                WHERE shown.fix_id = f.id) AS times_shown
                        FROM pi_tool_call.pi_tool_call_known_fix f ORDER BY f.id""")
        return cursor.fetchall()


def unreviewed_examples(connection, *, limit: int = 4) -> list[tuple[str, str, str]]:
    with connection.cursor() as cursor:
        cursor.execute("""SELECT id, agent, signature FROM pi_tool_call.pi_tool_call_occurrence
                        WHERE outcome = 'error' AND reviewed_at IS NULL
                        ORDER BY occurred_at DESC LIMIT %s""", (limit,))
        return cursor.fetchall()


def review(connection, ids: list[str], reviewer: str, note: str) -> int:
    if not ids or any(not ID_RE.fullmatch(value) for value in ids):
        raise ValueError("Give one or more valid occurrence IDs")
    if not valid_agent(reviewer) or len(note) > 500:
        raise ValueError("Invalid reviewer or note")
    with connection.transaction():
        with connection.cursor() as cursor:
            cursor.execute("""UPDATE pi_tool_call.pi_tool_call_occurrence SET reviewed_at = now(),
                           reviewed_by = %s, review_note = %s
                           WHERE id = ANY(%s) AND outcome = 'error' AND reviewed_at IS NULL""",
                           (reviewer, note, ids))
            return cursor.rowcount


def resolve(connection, ids: list[str], pr: str) -> int:
    if not ids or any(not ID_RE.fullmatch(value) for value in ids):
        raise ValueError("Give one or more valid occurrence IDs")
    if not re.fullmatch(r"https://[^\s]+/pulls/[0-9]+", pr):
        raise ValueError("Resolution must cite a PR URL")
    with connection.transaction():
        with connection.cursor() as cursor:
            cursor.execute("""UPDATE pi_tool_call.pi_tool_call_occurrence SET resolved_at = now(), resolved_by_pr = %s
                           WHERE id = ANY(%s) AND outcome = 'error' AND reviewed_at IS NOT NULL
                             AND resolved_at IS NULL""", (pr, ids))
            return cursor.rowcount


def approved_fixes(connection, agent: str, enabled_skills: set[str]) -> list[dict[str, Any]]:
    if not valid_agent(agent):
        raise ValueError("Invalid agent")
    with connection.cursor() as cursor:
        cursor.execute("""SELECT f.id, f.match_pattern, f.redacted_instruction,
                               f.approved_digest,
                               array(SELECT o.agent FROM pi_tool_call.pi_tool_call_fix_owner o
                                     WHERE o.fix_id = f.id ORDER BY o.agent),
                               array(SELECT a2.agent FROM pi_tool_call.pi_tool_call_fix_applies_to a2
                                     WHERE a2.fix_id = f.id ORDER BY a2.agent),
                               array(SELECT s.skill FROM pi_tool_call.pi_tool_call_fix_required_skill s
                                     WHERE s.fix_id = f.id ORDER BY s.skill),
                               f.signature,
                               (SELECT count(*) FROM pi_tool_call.pi_tool_call_occurrence e
                                WHERE e.outcome = 'error' AND (
                                    e.signature = f.signature OR
                                    EXISTS (SELECT 1 FROM pi_tool_call.pi_tool_call_suggestion s
                                            WHERE s.fix_id = f.id AND s.occurrence_id = e.id) OR
                                    EXISTS (SELECT 1 FROM pi_tool_call.pi_tool_call_occurrence_fix link
                                            WHERE link.fix_id = f.id AND link.occurrence_id = e.id)))
                        FROM pi_tool_call.pi_tool_call_known_fix f
                        WHERE f.active AND f.approved_at IS NOT NULL
                        ORDER BY f.id""")
        rows = cursor.fetchall()
    result = []
    for id_, pattern, instruction, approved_digest, owners, applies_to_agents, skills, signature, seen_count in rows:
        current_digest = fix_content_sha256(pattern, instruction, owners, applies_to_agents, skills)
        if current_digest != approved_digest:
            raise RuntimeError(f"Approved fix {id_} changed since review; fresh approval required")
        if agent not in applies_to_agents or not set(skills) <= enabled_skills:
            continue
        re.compile(pattern)  # Reject an invalid reviewed pattern before publishing.
        result.append(dict(id=id_, pattern=pattern, fix=instruction, owners=owners,
                           applies_to_agents=applies_to_agents,
                           required_skills=skills, reviewed=True,
                           content_sha256=current_digest, signature=signature,
                           seen_count=seen_count))
    return result


def fix_content_sha256(pattern: str, fix: str, owners: list[str] | set[str],
                       applies_to_agents: list[str] | set[str],
                       required_skills: list[str] | set[str]) -> str:
    content = {
        "applies_to_agents": sorted(applies_to_agents),
        "fix": fix,
        "owners": sorted(owners),
        "pattern": pattern,
        "required_skills": sorted(required_skills),
    }
    canonical = json.dumps(content, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def validate_fix_document(value: Any, *, require_approval: bool = True) -> dict[str, Any]:
    """Validate a private proposal or approved publication before any writes."""
    keys = {"id", "title", "signature", "pattern", "fix", "owners",
            "applies_to_agents", "required_skills", "source"}
    if require_approval:
        keys.add("approved_by")
    if not isinstance(value, dict) or not keys <= value.keys():
        raise ValueError("Fix document is missing required fields")
    if not isinstance(value["id"], str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{2,79}", value["id"]):
        raise ValueError("Invalid fix ID")
    for key, limit in (("title", 120), ("signature", 300), ("pattern", 500),
                       ("fix", 2000), ("source", 500)):
        if not isinstance(value[key], str) or not value[key].strip() or len(value[key]) > limit:
            raise ValueError(f"Invalid {key}")
        check_shared(value[key])
    try:
        re.compile(value["pattern"])
    except re.error as error:
        raise ValueError("Invalid match pattern") from error
    if any(not isinstance(value[key], list) or
           any(not isinstance(item, str) for item in value[key])
           for key in ("owners", "applies_to_agents", "required_skills")):
        raise ValueError("Owners, applicability, and skills must be string lists")
    owners = set(value["owners"])
    applies = set(value["applies_to_agents"])
    skills = set(value["required_skills"])
    if require_approval or "approved_by" in value:
        if not isinstance(value.get("approved_by"), str) or not valid_agent(value["approved_by"]):
            raise ValueError("Invalid approver")
    if (not owners or not applies or not all(valid_agent(item) for item in owners | applies) or
            any(not re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,79}", skill) for skill in skills)):
        raise ValueError("Invalid owners, applicability, skills, or approver")
    ids = value.get("occurrence_ids", [])
    if not isinstance(ids, list) or any(not isinstance(item, str) or not ID_RE.fullmatch(item) for item in ids):
        raise ValueError("Invalid informing occurrence IDs")
    result = dict(value)
    result.update(owners=sorted(owners), applies_to_agents=sorted(applies),
                  required_skills=sorted(skills), occurrence_ids=sorted(set(ids)))
    return result


def publish_fix(connection, value: dict[str, Any]) -> None:
    """Approve a redacted portable fix with explicit owner/applicability sets."""
    value = validate_fix_document(value)
    owners, applies, skills = value["owners"], value["applies_to_agents"], value["required_skills"]
    ids = value["occurrence_ids"]
    approved_digest = fix_content_sha256(value["pattern"], value["fix"], owners, applies, skills)
    with connection.transaction():
        with connection.cursor() as cursor:
            cursor.execute("""INSERT INTO pi_tool_call.pi_tool_call_known_fix
                (id, title, signature, match_pattern, redacted_instruction,
                 approved_digest, approved_at, approved_by, source)
                VALUES (%s, %s, %s, %s, %s, %s, now(), %s, %s)
                ON CONFLICT (id) DO UPDATE SET title = EXCLUDED.title,
                  signature = EXCLUDED.signature, match_pattern = EXCLUDED.match_pattern,
                  redacted_instruction = EXCLUDED.redacted_instruction,
                  approved_digest = EXCLUDED.approved_digest,
                  approved_at = now(), approved_by = EXCLUDED.approved_by,
                  source = EXCLUDED.source, active = true, updated_at = now()""",
                (value["id"], value["title"], value["signature"], value["pattern"],
                 value["fix"], approved_digest, value["approved_by"], value["source"]))
            for table in ("pi_tool_call.pi_tool_call_fix_owner", "pi_tool_call.pi_tool_call_fix_applies_to",
                          "pi_tool_call.pi_tool_call_fix_required_skill"):
                cursor.execute(f"DELETE FROM {table} WHERE fix_id = %s", (value["id"],))
            for table, column, values in (
                ("pi_tool_call.pi_tool_call_fix_owner", "agent", owners),
                ("pi_tool_call.pi_tool_call_fix_applies_to", "agent", applies),
                ("pi_tool_call.pi_tool_call_fix_required_skill", "skill", skills),
            ):
                for item in sorted(values):
                    cursor.execute(f"INSERT INTO {table} (fix_id, {column}) VALUES (%s, %s)",
                                   (value["id"], item))
            for occurrence_id in ids:
                cursor.execute("""INSERT INTO pi_tool_call.pi_tool_call_occurrence_fix
                    (occurrence_id, fix_id) VALUES (%s, %s)
                    ON CONFLICT DO NOTHING""", (occurrence_id, value["id"]))


def slowest_calls(conn, since: str | None = None, agent: str | None = None,
                  limit: int = 15):
    """Return the slowest recorded tool calls, optionally scoped to one agent."""
    resolved_since = since or _DEFAULT_SINCE
    safe_limit = max(1, min(limit, 500))
    clauses = ["duration_ms IS NOT NULL", "occurred_at >= %s"]
    params: list = [_parse_since(resolved_since)]
    if agent is not None:
        clauses.append("agent = %s")
        params.append(agent)
    params.append(safe_limit)
    with conn.cursor() as cursor:
        cursor.execute(f"""
            SELECT id, agent, origin, occurred_at, tool_name,
                   redacted_command, outcome, duration_ms
            FROM pi_tool_call.pi_tool_call_occurrence
            WHERE {' AND '.join(clauses)}
            ORDER BY duration_ms DESC, occurred_at DESC
            LIMIT %s
            """, params)
        return cursor.fetchall()


def duration_stats(conn, since: str | None = None, agent: str | None = None):
    """Summarize recorded tool-call durations per agent and tool."""
    resolved_since = since or _DEFAULT_SINCE
    clauses = ["duration_ms IS NOT NULL", "occurred_at >= %s"]
    params: list = [_parse_since(resolved_since)]
    if agent is not None:
        clauses.append("agent = %s")
        params.append(agent)
    with conn.cursor() as cursor:
        cursor.execute(f"""
            SELECT agent, tool_name, COUNT(duration_ms) AS timed_calls,
                   ROUND(AVG(duration_ms)::numeric, 1) AS avg_ms,
                   ROUND((percentile_cont(0.5) WITHIN GROUP (ORDER BY duration_ms))::numeric, 1) AS p50_ms,
                   ROUND((percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms))::numeric, 1) AS p95_ms,
                   MAX(duration_ms) AS max_ms
            FROM pi_tool_call.pi_tool_call_occurrence
            WHERE {' AND '.join(clauses)}
            GROUP BY agent, tool_name
            ORDER BY max_ms DESC
            """, params)
        return cursor.fetchall()
