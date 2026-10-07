"""Operator and timer entry point for the shared Pi error ledger."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path

from .fix_authoring import (check_fix, corroborate_fix, draft_fix, fix_evidence,
                            read_private_document, write_private_document)
from .error_ledger import (valid_agent, approved_fixes, command_patterns, connect, duration_stats,
                           fix_catalog, ingest_outbox, migrate, publish_fix,
                           recent_errors, recent_successes, resolve, review,
                           slowest_calls, trends, unreviewed_count, unreviewed_examples)


def daily_brief(pending: int, top: list, examples: list) -> str:
    """Keep the automatic agenda within the 300-character summary limit."""
    parts = [f"{pending} unreviewed Pi errors."]
    for row in top:
        item = f"Trend: {row[6]} pending {row[0][:12]}."
        if len(" ".join((*parts, item))) > 300:
            break
        parts.append(item)
    for id_, agent, _ in examples:
        item = f"Instance: {agent}:{id_}."
        if len(" ".join((*parts, item))) > 300:
            break
        parts.append(item)
    return " ".join(parts)


def error_suggestions(connection, reference: str, enabled_skills: set[str]) -> dict:
    """Resolve one error reference; return only applicable, digest-verified fixes."""
    if not re.fullmatch(r"(?:pierr|err)-[0-9a-f-]{6,36}", reference):
        raise ValueError("Invalid error reference")
    prefix = reference.removeprefix("pi").removeprefix("err-")
    with connection.cursor() as cursor:
        cursor.execute("""SELECT id, agent FROM pi_tool_call.pi_tool_call_occurrence
                          WHERE outcome = 'error' AND id LIKE %s ORDER BY id LIMIT 2""",
                       ("pierr-" + prefix + "%",))
        rows = cursor.fetchall()
    if not rows:
        raise ValueError("Error not found; it may still be awaiting ingestion")
    if len(rows) != 1:
        raise ValueError("Ambiguous error reference; use a longer prefix")
    occurrence_id, agent = rows[0]
    with connection.cursor() as cursor:
        cursor.execute("""SELECT fix_id FROM pi_tool_call.pi_tool_call_suggestion
                          WHERE occurrence_id = %s ORDER BY fix_id""", (occurrence_id,))
        ids = {row[0] for row in cursor.fetchall()}
    fixes = [fix for fix in approved_fixes(connection, agent, enabled_skills)
             if fix["id"] in ids]
    return dict(id=occurrence_id, fixes=fixes)


def structured_match(connection, reference: str | None, payload: dict | None,
                     enabled_skills: set[str]) -> dict:
    """Read-only structured lookup; never returns raw command or context."""
    from .command_match import parse_or_abstain, rank_examples, stderr_suppression_check
    if reference is not None:
        if not re.fullmatch(r"(?:pierr|err)-[0-9a-f-]{6,36}", reference):
            raise ValueError("Invalid error reference")
        prefix = reference.removeprefix("pi").removeprefix("err-")
        with connection.cursor() as cursor:
            cursor.execute("""SELECT o.id, o.agent, o.redacted_command,
                o.redacted_summary, p.exit_code, p.parse_status,
                p.whole_form, p.final_command_name, p.final_command_form,
                p.step_count, p.options, p.arg_roles, p.prior_command,
                p.same_resource, p.execution_uncertain, p.stderr_suppressed,
                p.error_class, p.output_present
                FROM pi_tool_call.pi_tool_call_occurrence o
                LEFT JOIN pi_tool_call.pi_command_parse p ON p.occurrence_id = o.id
                WHERE o.outcome = 'error' AND o.id LIKE %s ORDER BY o.id LIMIT 2""",
                ("pierr-" + prefix + "%",))
            rows = cursor.fetchall()
        if len(rows) != 1:
            raise ValueError("Error not found or reference is ambiguous")
        (occurrence_id, agent, command, output, exit_code, parse_status,
         whole_form, command_name, command_form, step_count, options, arg_roles,
         prior_command, same_resource, execution_uncertain, stderr_suppressed,
         observed_class, output_present) = rows[0]
        if parse_status is None:
            raise ValueError("Error has not been indexed yet")
        if command is None:
            return {"id": occurrence_id, "candidates": [], "stderr_check": None,
                    "parse_status": "unsupported"}
    else:
        if not isinstance(payload, dict) or set(payload) != {"agent", "command", "output", "exit_code"}:
            raise ValueError("Input needs agent, command, output, and exit_code")
        occurrence_id = None
        agent, command, output, exit_code = (payload[key] for key in
                                                ("agent", "command", "output", "exit_code"))
        if (not isinstance(agent, str) or agent not in {"agent_one", "agent_two", "agent_three"} or
            not isinstance(command, str) or len(command) > 2048 or
            not isinstance(output, str) or len(output) > 4096 or
            (exit_code is not None and
             (isinstance(exit_code, bool) or not isinstance(exit_code, int) or
              not 0 <= exit_code <= 255))):
            raise ValueError("Invalid match input")
    if not command:
        return {"id": occurrence_id, "candidates": [], "stderr_check": None,
                "parse_status": "unsupported"}
    if reference is not None:
        parsed = {"status": parse_status, "command_name": command_name,
                  "command_form": command_form, "whole_form": whole_form,
                  "step_count": step_count, "options": options, "arg_roles": arg_roles,
                  "prior_command": prior_command, "same_resource": same_resource,
                  "execution_uncertain": execution_uncertain,
                  "stderr_suppressed": stderr_suppressed, "exit_code": exit_code,
                  "error_class": observed_class, "output_present": output_present,
                  "output": output}
    else:
        parsed = parse_or_abstain(command, output, exit_code)
    candidates = rank_examples(connection, parsed, agent, enabled_skills)
    stderr_check = stderr_suppression_check(command, exit_code not in (None, 0))
    return {"id": occurrence_id, "candidates": candidates,
            "stderr_check": stderr_check, "parse_status": parsed["status"]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Pi error ledger")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("migrate")
    ingest = commands.add_parser("ingest")
    ingest.add_argument("--outbox", type=Path, required=True)
    ingest.add_argument("--processed", type=Path, required=True)
    commands.add_parser("trends")
    errors = commands.add_parser("errors")
    errors.add_argument("--since", default=None)
    errors.add_argument("--agent", dest="agent_for_errors", default=None)
    errors.add_argument("--unresolved-only", action="store_true")
    successes = commands.add_parser("successes")
    successes.add_argument("--since", default=None)
    slowest = commands.add_parser("slowest")
    slowest.add_argument("--since", default=None)
    slowest.add_argument("--agent", default=None)
    slowest.add_argument("--limit", type=int, default=15)
    stats = commands.add_parser("duration-stats")
    stats.add_argument("--since", default=None)
    stats.add_argument("--agent", default=None)
    commands.add_parser("patterns")
    forms = commands.add_parser("forms", help="Count normalized Bash chains or steps")
    forms.add_argument("--since", default="7 days")
    forms.add_argument("--scope", choices=("whole", "step"), default="whole")
    forms.add_argument("--limit", type=int, default=50)
    trials = commands.add_parser("trials", help="Inspect user-only structured dry-run candidates")
    trials.add_argument("--since", default="24 hours")
    trials.add_argument("--agent")
    trials.add_argument("--limit", type=int, default=50)
    commands.add_parser("fixes")
    lookup = commands.add_parser("lookup")
    lookup.add_argument("reference")
    lookup.add_argument("--enabled-skill", action="append", default=[])
    match = commands.add_parser("match", help="Rank owner-approved structured examples")
    match_input = match.add_mutually_exclusive_group(required=True)
    match_input.add_argument("--reference")
    match_input.add_argument("--stdin", action="store_true")
    match.add_argument("--enabled-skill", action="append", default=[])
    backfill = commands.add_parser("backfill-parses")
    backfill.add_argument("--batch-size", type=int, default=500)
    example = commands.add_parser("publish-example")
    example.add_argument("--file", type=Path, required=True)
    commands.add_parser("daily")
    mark = commands.add_parser("review")
    mark.add_argument("--by", required=True)
    mark.add_argument("--note", default="")
    mark.add_argument("ids", nargs="+")
    fixed = commands.add_parser("resolve")
    fixed.add_argument("--pr", required=True)
    fixed.add_argument("ids", nargs="+")
    export = commands.add_parser("export-fixes")
    export.add_argument("--agent", required=True)
    export.add_argument("--enabled-skill", action="append", default=[])
    export.add_argument("--output", type=Path, required=True)
    publish = commands.add_parser("publish-fix")
    publish.add_argument("--file", type=Path, required=True)
    draft = commands.add_parser("draft-fix", help="Prepare a private, unapproved fix document")
    draft.add_argument("--id", required=True)
    draft.add_argument("--error", action="append", required=True)
    draft.add_argument("--output", type=Path, required=True)
    check = commands.add_parser("check-fix", help="Validate a proposal without database writes")
    check.add_argument("--file", type=Path, required=True)
    corroborate = commands.add_parser("corroborate-fix", help="Attach evidence without republishing a fix")
    corroborate.add_argument("--file", type=Path, required=True)
    evidence = commands.add_parser("fix-evidence", help="Read back informing occurrences")
    evidence.add_argument("--id", required=True)
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0].startswith(("err-", "pierr-")):
        argv.insert(0, "lookup")
    args = parser.parse_args(argv)
    repo = Path(__file__).resolve().parent
    read_only = args.command in {"errors", "successes", "patterns", "forms", "trials", "fixes", "trends",
                                 "export-fixes", "slowest", "duration-stats", "lookup",
                                 "draft-fix", "check-fix", "fix-evidence", "match", "daily"}
    with connect(read_only=read_only) as connection:
        if args.command == "draft-fix":
            write_private_document(args.output, draft_fix(connection, args.id, args.error))
            print("Prepared private draft; no database changes or approval")
        elif args.command == "check-fix":
            value = check_fix(connection, read_private_document(args.file))
            print("Proposal content valid; cited evidence resolves; no database changes")
            if not value["occurrence_ids"]:
                print("No informing occurrences cited")
            if not valid_agent(value.get("approved_by")):
                print("Approval is absent; owner review is required before publish-fix")
        elif args.command == "fix-evidence":
            for id_, agent, outcome, occurred_at in fix_evidence(connection, args.id):
                print(json.dumps(dict(id=id_, agent=agent, outcome=outcome,
                                      occurred_at=occurred_at.isoformat())))
        elif args.command == "corroborate-fix":
            count = corroborate_fix(connection, read_private_document(args.file))
            print(f"Linked {count} new evidence occurrences; fix content and approval unchanged")
        elif args.command == "lookup":
            skills = set(args.enabled_skill) | {
                skill.strip() for skill in os.environ.get("PI_CAPTURE_ENABLED_SKILLS", "").split(",")
                if skill.strip()
            }
            try:
                print(json.dumps(error_suggestions(connection, args.reference, skills)))
            except ValueError as error:
                parser.error(str(error))
        elif args.command == "match":
            skills = set(args.enabled_skill) | {
                skill.strip() for skill in os.environ.get("PI_CAPTURE_ENABLED_SKILLS", "").split(",")
                if skill.strip()
            }
            payload = None
            if args.stdin:
                data = sys.stdin.read(8193)
                if len(data) > 8192:
                    parser.error("Match input is too large")
                try:
                    payload = json.loads(data)
                except json.JSONDecodeError:
                    parser.error("Invalid match JSON")
            try:
                print(json.dumps(structured_match(connection, args.reference, payload, skills)))
            except ValueError as error:
                parser.error(str(error))
        elif args.command == "migrate":
            print(migrate(connection, repo))
        elif args.command == "backfill-parses":
            from .command_match import backfill_parses
            print(f"Indexed {backfill_parses(connection, args.batch_size)} redacted Bash calls")
        elif args.command == "publish-example":
            from .command_match import publish_example
            print(f"Published example {publish_example(connection, read_private_document(args.file))}")
        elif args.command == "ingest":
            inserted, acknowledged = ingest_outbox(connection, args.outbox, args.processed)
            print(f"Inserted {inserted}; acknowledged {acknowledged}")
        elif args.command == "trends":
            for signature, total, recent, agents, first, last, pending in trends(connection):
                print(f"{pending:4} unreviewed | {recent:4} in 7d | {total:4} total | "
                      f"{','.join(agents)} | {first.isoformat()} .. {last.isoformat()} | {signature}")
        elif args.command == "errors":
            for id_, agent, origin, occurred, signature, summary, reviewed, pr, command, duration_ms, suggestions in recent_errors(
                    connection, since=args.since, agent_for_errors=args.agent_for_errors,
                    unresolved_only=args.unresolved_only):
                print(json.dumps(dict(id=id_, agent=agent, origin=origin,
                                      occurred_at=occurred.isoformat(), signature=signature,
                                      summary=summary, redacted_command=command,
                                      duration_ms=duration_ms,
                                      reviewed=reviewed is not None,
                                      resolved_by_pr=pr, suggested_fix_ids=suggestions)))
        elif args.command == "successes":
            for id_, agent, origin, occurred, tool_name, signature, command, duration_ms in recent_successes(
                    connection, since=args.since):
                print(json.dumps(dict(id=id_, agent=agent, origin=origin,
                                      occurred_at=occurred.isoformat(), tool_name=tool_name,
                                      signature=signature, redacted_command=command,
                                      duration_ms=duration_ms)))
        elif args.command == "slowest":
            for id_, agent, origin, occurred, tool_name, command, outcome, duration_ms in slowest_calls(
                    connection, since=args.since, agent=args.agent, limit=args.limit):
                print(json.dumps(dict(id=id_, agent=agent, origin=origin,
                                      occurred_at=occurred.isoformat(), tool_name=tool_name,
                                      outcome=outcome, duration_ms=duration_ms,
                                      redacted_command=command)))
        elif args.command == "duration-stats":
            for agent, tool_name, timed, avg_ms, p50_ms, p95_ms, max_ms in duration_stats(
                    connection, since=args.since, agent=args.agent):
                print(json.dumps(dict(agent=agent, tool_name=tool_name, timed_calls=timed,
                                      avg_ms=float(avg_ms), p50_ms=float(p50_ms),
                                      p95_ms=float(p95_ms), max_ms=max_ms)))
        elif args.command == "patterns":
            for command, successes, errors, agents, last_seen in command_patterns(connection):
                print(json.dumps(dict(redacted_command=command, successes=successes,
                                      errors=errors, agents=agents,
                                      last_seen=last_seen.isoformat())))
        elif args.command == "forms":
            from .command_match import command_forms
            try:
                rows = command_forms(connection, since=args.since, scope=args.scope,
                                     limit=args.limit)
            except ValueError as error:
                parser.error(str(error))
            for form, successes, errors, agents, last_seen in rows:
                print(json.dumps(dict(form=form, scope=args.scope,
                                      successes=successes, errors=errors,
                                      agents=agents, last_seen=last_seen.isoformat())))
        elif args.command == "trials":
            from .command_match import recent_trials
            try:
                rows = recent_trials(connection, since=args.since, agent=args.agent,
                                     limit=args.limit)
            except ValueError as error:
                parser.error(str(error))
            for occurrence_id, agent, occurred_at, fix_id, score, disposition, shown, version in rows:
                print(json.dumps(dict(id=occurrence_id, agent=agent,
                                      occurred_at=occurred_at.isoformat(), fix_id=fix_id,
                                      structural_score=score, disposition=disposition,
                                      shown_to_user=shown, matcher_version=version)))
        elif args.command == "fixes":
            for id_, title, instruction, active, owners, applies, skills, times_shown in fix_catalog(connection):
                print(json.dumps(dict(id=id_, title=title, fix=instruction, active=active,
                                      owners=owners, applies_to_agents=applies,
                                      required_skills=skills, times_shown=times_shown)))
        elif args.command == "review":
            print(f"Reviewed {review(connection, args.ids, args.by, args.note)} occurrences")
        elif args.command == "resolve":
            print(f"Resolved {resolve(connection, args.ids, args.pr)} occurrences")
        elif args.command == "export-fixes":
            fixes = approved_fixes(connection, args.agent, set(args.enabled_skill))
            args.output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            descriptor, name = tempfile.mkstemp(prefix=".fixes-", dir=args.output.parent)
            try:
                with os.fdopen(descriptor, "w") as handle:
                    json.dump({"version": 1, "fixes": fixes}, handle, indent=2)
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.chmod(name, 0o600)
                os.replace(name, args.output)
            finally:
                if os.path.exists(name):
                    os.unlink(name)
            print(f"Exported {len(fixes)} applicable fixes")
        elif args.command == "publish-fix":
            value = read_private_document(args.file)
            # Resolve cited evidence before publication; the transaction still
            # owns all catalog mutations and foreign-key checks.
            check_fix(connection, value)
            publish_fix(connection, value)
            print("Published approved fix")
        elif args.command == "daily":
            pending = unreviewed_count(connection)
            if pending == 0:
                print("No unreviewed Pi errors")
                return 0
            top = trends(connection, limit=3)
            examples = unreviewed_examples(connection)
            brief = daily_brief(pending, top, examples)
    if args.command == "daily":
        print(brief)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
