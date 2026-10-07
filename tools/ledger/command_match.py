"""Structured matching for redacted Pi Bash commands. No private details enter SQL."""

from __future__ import annotations

import os
import re
import shlex
from typing import Any

from tree_sitter import Language, Parser
import tree_sitter_bash

PARSER_VERSION = 1
MATCHER_VERSION = 1
PARSER = Parser(Language(tree_sitter_bash.language()))
SHELL_OPS = {";", "&&", "||", "|"}
GLOBAL_VALUE_OPTIONS = {"-n", "--namespace", "--context", "--kubeconfig", "-C"}
SAFE_SHORT_BUNDLES = {"-sh", "-rf", "-qr", "-fsS", "-czf",
                      "-name", "-type", "-exec", "-maxdepth", "-mindepth"}


def safe_option(value):
    """Keep option names while dropping attached values from normalized SQL."""
    if value.startswith("--"):
        return value.split("=", 1)[0][:64]
    if len(value) <= 3 or value in SAFE_SHORT_BUNDLES:
        return value
    return value[:2] + "$VALUE"

def unquote(source):
    try:
        values = shlex.split(source, posix=True)
        return values[0] if len(values) == 1 else source
    except ValueError:
        return source


def shell_parts(command, depth=0):
    if depth > 2:
        raise ValueError("shell wrapper nesting too deep")
    source = command.encode()
    root = PARSER.parse(source).root_node
    if root.has_error:
        raise ValueError("Bash syntax error or unsupported form")
    result = []

    def walk(node):
        if node.type == "command":
            values = [unquote(source[child.start_byte:child.end_byte].decode())
                      for child in node.named_children
                      if child.type in ("command_name", "word", "string", "raw_string", "concatenation", "simple_expansion")]
            if not values:
                raise ValueError("command without executable")
            if values[0] in ("bash", "sh"):
                for index, value in enumerate(values[1:], 1):
                    if value.startswith("-") and "c" in value and index + 1 < len(values):
                        result.extend(shell_parts(values[index + 1], depth + 1))
                        return
            result.append(("step", values))
            return
        if node.type == "for_statement":
            result.append(("loop", "for $VAR in $VALUES"))
            for child in node.named_children:
                if child.type == "do_group":
                    walk(child)
            return
        if node.type in ("program", "list", "pipeline", "redirected_statement", "do_group"):
            for child in node.children:
                if not child.is_named:
                    operator = source[child.start_byte:child.end_byte].decode()
                    if operator in SHELL_OPS:
                        result.append(("op", operator))
                elif child.type == "file_redirect":
                    result.append(("redirect", source[child.start_byte:child.end_byte].decode()))
                else:
                    walk(child)
            return
        raise ValueError("unsupported shell node " + node.type)

    walk(root)
    if not any(kind == "step" for kind, _ in result):
        raise ValueError("no command step")
    return result


def peel_global(args, program):
    flags, rest = [], []
    index = 0
    while index < len(args):
        value = args[index]
        if value in GLOBAL_VALUE_OPTIONS and index + 1 < len(args):
            flags.append((value, args[index + 1]))
            index += 2
        elif value.startswith("--") and "=" in value:
            name, data = value.split("=", 1)
            flags.append((name, data))
            index += 1
        elif value.startswith("-") and (program == "kubectl" or value == "-C"):
            flags.append((safe_option(value), None))
            index += 1
        else:
            rest = args[index:]
            break
    return flags, rest


def option_form(flag, value):
    if value is None:
        return safe_option(flag)
    role = "$NAMESPACE" if flag in ("-n", "--namespace") else "$PATH" if flag in ("-C", "--kubeconfig") else "$MESSAGE" if flag == "-m" else "$VALUE"
    return flag + " " + role


def form_step(values, resource_ids):
    program = os.path.basename(values[0])
    args = values[1:]
    if program in ("git", "kubectl"):
        global_flags, args = peel_global(args, program)
        options = [option_form(flag, value) for flag, value in global_flags]
        sub = args[0] if args else ""
        command_name = program + " " + sub
        args = args[1:]
        if program == "kubectl":
            kind = args[0] if args else ""
            resource = args[1] if len(args) > 1 else None
            if resource is not None and resource not in resource_ids:
                resource_ids[resource] = len(resource_ids) + 1
            suffix = [kind] if kind else []
            if resource:
                suffix.append(f"$RESOURCE{resource_ids[resource]}")
            return command_name, " ".join([program, *options, sub, *suffix]).strip(), options, ["kind", "resource"] if resource else ["kind"], resource
        if sub == "commit":
            forms = []
            index = 0
            while index < len(args):
                if args[index] == "-m" and index + 1 < len(args):
                    forms.extend(["-m", "$MESSAGE"])
                    options.append("-m")
                    index += 2
                else:
                    forms.append("$PATH" if "/" in args[index] else "$ARG")
                    index += 1
            return command_name, " ".join([program, *options[:len(global_flags)], sub, *forms]).strip(), options, ["message"] if "-m" in options else [], None
        forms = ["$PATH" if sub == "add" or "/" in arg or "." in arg else "$ARG" for arg in args]
        return command_name, " ".join([program, *options, sub, *forms]).strip(), options, ["path"] * len(forms), None
    command_name = program
    options, operands = [], []
    index = 0
    while index < len(args):
        value = args[index]
        if program == "du" and value in ("-d", "--max-depth") and index + 1 < len(args):
            options.append(option_form(value, args[index + 1]))
            index += 2
        elif value.startswith("-") and value != "-":
            options.append(safe_option(value))
            index += 1
        else:
            operands.append(value)
            index += 1
    roles = []
    forms = []
    for position, operand in enumerate(operands):
        if program in ("grep", "rg") and position == 0:
            role = "pattern"
        elif program.startswith("python") and "-c" in options:
            role = "code"
        elif re.search(r"[*?\[]", operand) and program in ("ls", "du", "grep", "rg", "find"):
            role = "glob"
        elif program in ("ls", "du", "grep", "rg", "find", "cat", "tail"):
            role = "path"
        else:
            role = "arg"
        roles.append(role)
        forms.append("$" + role.upper())
    return command_name, " ".join([program, *options, *forms]).strip(), options, roles, None


def error_class(output):
    err = re.sub(r"(?:\n\s*)?Command exited with code \d+\s*$", "", output, flags=re.I).strip().lower()
    if err == "(no output)":
        err = ""
    if "permission denied" in err:
        return "permission_denied"
    if "notfound" in err or "not found" in err:
        return "not_found"
    if "no such file or directory" in err:
        return "missing_path"
    if "syntaxerror" in err and "f-string" in err:
        return "fstring_syntax"
    if "syntaxerror" in err and "<string>" in err and "for " in err:
        return "python_inline_for_syntax"
    if "jq: error" in err:
        return "jq_runtime_error"
    if "fatal: bad object" in err:
        return "git_bad_object"
    if "traceback" in err and any(name in err for name in ("attributeerror", "nameerror")):
        return "python_traceback"
    if "nameerror" in err:
        return "name_error"
    if "missing argument" in err or "invalid regular expression" in err:
        return "usage_error"
    return "other" if err else "none"


def parse_command(command, output="", exit_code=None):
    parts = shell_parts(command)
    resources = {}
    steps = []
    whole = []
    operators = []
    for kind, value in parts:
        if kind == "step":
            name, form, options, roles, resource = form_step(value, resources)
            steps.append(dict(command_name=name, command_form=form, options=options, arg_roles=roles,
                              resource=resource))
            whole.append(form)
        elif kind == "op":
            whole.append(value)
            operators.append(value)
        elif kind == "redirect":
            whole.append("$REDIRECT")
        elif kind == "loop":
            whole.append(value)
    final = steps[-1]
    prior = steps[-2] if len(steps) > 1 else None
    same_resource = bool(prior and prior["resource"] and final["resource"] and prior["resource"] == final["resource"])
    conditional_chain = any(op in ("&&", "||", "|") for op in operators)
    loop = any(kind == "loop" for kind, _ in parts)
    stderr_suppressed = any(kind == "redirect" and re.search(r"(?:^|\s)2\s*>\s*/dev/null", value)
                            for kind, value in parts)
    early_exit_risk = any(step["command_name"] in ("exit", "false", "set") for step in steps[:-1])
    return dict(command=command, command_name=final["command_name"], command_form=final["command_form"],
                whole_form=" ".join(whole), step_count=len(steps), steps=steps,
                operators=operators, execution_uncertain=len(steps) > 1 or conditional_chain or early_exit_risk or loop,
                stderr_suppressed=stderr_suppressed,
                options=final["options"], arg_roles=final["arg_roles"],
                prior_command=prior["command_name"] if prior else None,
                same_resource=same_resource, exit_code=exit_code,
                error_class=error_class(output), output_present=error_class(output) != "none",
                output=output)


def stderr_suppression_check(command, failed):
    """Independent conditional diagnostic for failed commands with hidden stderr."""
    if not failed:
        return None
    source = command.encode()
    root = PARSER.parse(source).root_node
    if root.has_error:
        return None
    suppressed = False
    pending = [root]
    while pending:
        node = pending.pop()
        if node.type == "file_redirect":
            redirect = source[node.start_byte:node.end_byte].decode()
            if not re.search(r"(?:^|\s)2\s*>\s*/dev/null(?:\s|$)", redirect):
                continue
            parent = node.parent
            in_condition = False
            while parent is not None:
                if parent.type in ("if_statement", "while_statement"):
                    condition = parent.child_by_field_name("condition")
                    if condition and condition.start_byte <= node.start_byte and node.end_byte <= condition.end_byte:
                        in_condition = True
                        break
                parent = parent.parent
            if not in_condition:
                suppressed = True
        else:
            pending.extend(node.named_children)
    if not suppressed:
        return None
    return {
        "check_id": "stderr-suppressed-on-failure",
        "disposition": "conditional_hint",
        "message": "At least one shell command or pipeline stage sent stderr to /dev/null, which may hide details of the failure. Avoid suppressing stderr while diagnosing an error unless the noise is expected; rerun the narrow command with stderr directed to a private file or a non-shared terminal, and inspect the result privately.",
        "reviewed_for_injection": False,
    }


def exit_code_from_output(output: str) -> int | None:
    match = re.search(r"(?:^|\n)Command exited with code (\d+)\s*$", output)
    return int(match.group(1)) if match else None


def parse_or_abstain(command: str, output: str, exit_code: int | None) -> dict[str, Any]:
    try:
        return {"status": "parsed", **parse_command(command, output, exit_code)}
    except ValueError:
        return {"status": "unsupported", "command": command, "steps": [],
                "exit_code": exit_code, "error_class": error_class(output),
                "output_present": error_class(output) != "none",
                "stderr_suppressed": stderr_suppression_check(command, True) is not None}


def store_parse(cursor, occurrence_id: str, parsed: dict[str, Any]) -> None:
    """Store only normalized forms and outcome metadata, never the source text."""
    cursor.execute("""INSERT INTO pi_tool_call.pi_command_parse
        (occurrence_id, parser_version, parse_status, whole_form,
         final_command_name, final_command_form, step_count, operators,
         options, arg_roles, prior_command, same_resource,
         execution_uncertain, stderr_suppressed, exit_code, error_class,
         output_present)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (occurrence_id) DO NOTHING""",
        (occurrence_id, PARSER_VERSION, parsed["status"], parsed.get("whole_form"),
         parsed.get("command_name"), parsed.get("command_form"), parsed.get("step_count", 0),
         parsed.get("operators", []), parsed.get("options", []), parsed.get("arg_roles", []),
         parsed.get("prior_command"), parsed.get("same_resource", False),
         parsed.get("execution_uncertain", True), parsed["stderr_suppressed"],
         parsed["exit_code"], parsed["error_class"], parsed["output_present"]))
    for ordinal, step in enumerate(parsed["steps"]):
        cursor.execute("""INSERT INTO pi_tool_call.pi_command_step
            (occurrence_id, ordinal, command_name, command_form, options, arg_roles)
            VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING""",
            (occurrence_id, ordinal, step["command_name"], step["command_form"],
             step["options"], step["arg_roles"]))


def backfill_parses(connection, batch_size: int = 500) -> int:
    """Idempotently index historical shared redacted Bash commands."""
    if not 1 <= batch_size <= 5000:
        raise ValueError("batch size must be between 1 and 5000")
    total = 0
    while True:
        with connection.transaction(), connection.cursor() as cursor:
            cursor.execute("""SELECT o.id, o.redacted_command, o.redacted_summary
                FROM pi_tool_call.pi_tool_call_occurrence o
                LEFT JOIN pi_tool_call.pi_command_parse p ON p.occurrence_id = o.id
                WHERE o.tool_name = 'bash' AND o.redacted_command IS NOT NULL
                  AND p.occurrence_id IS NULL
                ORDER BY o.occurred_at, o.id LIMIT %s""", (batch_size,))
            rows = cursor.fetchall()
            for occurrence_id, command, summary in rows:
                parsed = parse_or_abstain(command, summary,
                                          exit_code_from_output(summary))
                store_parse(cursor, occurrence_id, parsed)
        total += len(rows)
        if len(rows) < batch_size:
            return total


def command_forms(connection, *, since: str = "7 days", scope: str = "whole",
                  limit: int = 50) -> list[tuple]:
    """Count normalized whole chains or individual steps across shared calls."""
    from .error_ledger import _parse_since
    if scope not in {"whole", "step"} or not 1 <= limit <= 500:
        raise ValueError("Invalid form query")
    time_floor = _parse_since(since)
    if scope == "whole":
        table = ""
        form = "p.whole_form"
    else:
        table = "JOIN pi_tool_call.pi_command_step s ON s.occurrence_id = p.occurrence_id"
        form = "s.command_form"
    with connection.cursor() as cursor:
        cursor.execute(f"""SELECT {form},
            count(*) FILTER (WHERE o.outcome = 'success') AS successes,
            count(*) FILTER (WHERE o.outcome = 'error') AS errors,
            array_agg(DISTINCT o.agent ORDER BY o.agent) AS agents,
            max(o.occurred_at) AS last_seen
            FROM pi_tool_call.pi_command_parse p
            {table}
            JOIN pi_tool_call.pi_tool_call_occurrence o ON o.id = p.occurrence_id
            WHERE p.parse_status = 'parsed' AND o.occurred_at >= %s
            GROUP BY {form}
            ORDER BY errors DESC, successes DESC, last_seen DESC LIMIT %s""",
            (time_floor, limit))
        return cursor.fetchall()


def recent_trials(connection, *, since: str = "24 hours", agent: str | None = None,
                  limit: int = 50) -> list[tuple]:
    """Read dry-run candidates without exposing captured command text."""
    from .error_ledger import _parse_since
    if agent is not None and not (isinstance(agent, str) and re.fullmatch(r"[a-z][a-z0-9_-]{0,79}", agent)):
        raise ValueError("Invalid agent")
    if not 1 <= limit <= 500:
        raise ValueError("Invalid trial limit")
    where_agent = "AND o.agent = %s" if agent else ""
    params = [_parse_since(since)]
    if agent:
        params.append(agent)
    params.append(limit)
    with connection.cursor() as cursor:
        cursor.execute(f"""SELECT o.id, o.agent, o.occurred_at, t.fix_id,
            t.score, t.disposition, t.shown_to_user, t.matcher_version
            FROM pi_tool_call.pi_match_trial t
            JOIN pi_tool_call.pi_tool_call_occurrence o ON o.id = t.occurrence_id
            WHERE o.occurred_at >= %s {where_agent}
            ORDER BY o.occurred_at DESC, t.score DESC LIMIT %s""", params)
        return cursor.fetchall()


def rank_examples(connection, parsed: dict[str, Any], agent: str,
                  enabled_skills: set[str], limit: int = 10) -> list[dict[str, Any]]:
    """Retrieve approved examples with deterministic structural gates and ranking."""
    if parsed["status"] != "parsed" or parsed["exit_code"] is None:
        return []
    from .error_ledger import approved_fixes
    fixes = {fix["id"]: fix for fix in approved_fixes(connection, agent, enabled_skills)}
    if not fixes:
        return []
    with connection.cursor() as cursor:
        cursor.execute("""SELECT e.fix_id, e.command_form, e.whole_form,
            e.step_count, e.prior_command, e.same_resource, e.error_class,
            e.output_present, e.options, e.arg_roles, e.intent_sensitive,
            e.approved_digest
            FROM pi_tool_call.pi_fix_example e
            WHERE e.active AND e.command_name = %s AND e.exit_code = %s
              AND e.fix_id = ANY(%s) AND %s = ANY(e.agents)""",
            (parsed["command_name"], parsed["exit_code"], list(fixes), agent))
        rows = cursor.fetchall()
    candidates = {}
    for (fix_id, form, whole, steps, prior, same_resource, cls, output_present,
         options, roles, intent_sensitive, approved_digest) in rows:
        if approved_digest != fixes[fix_id]["content_sha256"]:
            continue
        observed_class = parsed["error_class"]
        if observed_class not in ("none", "other", "unknown") and cls != observed_class:
            continue
        if (observed_class in ("none", "other", "unknown") and
            form != parsed["command_form"] and
            not (parsed["command_name"] in ("grep", "rg") and parsed["exit_code"] == 1)):
            continue
        if fix_id == "bash-compound-last-cmd-fails" and parsed["step_count"] < 2:
            continue
        if fix_id == "bash-empty-exit-1" and parsed["step_count"] != 1:
            continue
        if fix_id == "bash-expected-nonzero-or-echo-gone" and not (
            parsed["prior_command"] == "kubectl delete" and parsed["same_resource"]
            and "deleted" in parsed["output"].lower()
        ):
            continue
        if fix_id == "bash-glob-no-match-exit-2" and "glob" not in parsed["arg_roles"]:
            continue
        if fix_id == "ls-exit-code-two-partial-missing" and parsed["arg_roles"].count("path") < 2:
            continue
        if fix_id == "du-missing-directories-exit-1" and not (
            "cannot access" in parsed["output"].lower() or parsed["stderr_suppressed"]
        ):
            continue
        overlap = len(set(options) & set(parsed["options"]))
        union = len(set(options) | set(parsed["options"]))
        score = (40 * (form == parsed["command_form"])
                 + 20 * (list(roles) == parsed["arg_roles"])
                 + 15 * (whole == parsed["whole_form"])
                 + (10 * overlap // max(union, 1))
                 + 5 * (steps == parsed["step_count"])
                 + 5 * (prior == parsed["prior_command"])
                 + 5 * (cls == observed_class))
        if same_resource and not parsed["same_resource"]:
            continue
        factors = [name for name, matches in (
            ("command_form", form == parsed["command_form"]),
            ("argument_roles", list(roles) == parsed["arg_roles"]),
            ("whole_chain", whole == parsed["whole_form"]),
            ("step_count", steps == parsed["step_count"]),
            ("prior_command", prior == parsed["prior_command"]),
            ("observed_error_class", cls == observed_class),
        ) if matches]
        candidate = {"fix_id": fix_id, "score": min(score, 100),
                     "disposition": "conditional_hint", "matcher_version": MATCHER_VERSION,
                     "factors": factors, "seen_count": fixes[fix_id]["seen_count"],
                     "context_required": bool(intent_sensitive or parsed["execution_uncertain"]
                                              or parsed["stderr_suppressed"]
                                              or observed_class in ("none", "other", "unknown"))}
        if fix_id not in candidates or candidate["score"] > candidates[fix_id]["score"]:
            candidates[fix_id] = candidate
    return sorted(candidates.values(), key=lambda item: (-item["score"], item["fix_id"]))[:limit]


def publish_example(connection, document: dict[str, Any]) -> str:
    """Publish one explicitly owner-approved portable example."""
    from .fix_authoring import approved_fix
    from .privacy import check_shared
    required = {"id", "fix_id", "approved_digest", "approved_by", "agents", "context_note",
                "command", "output", "exit_code"}
    if not isinstance(document, dict) or not required <= document.keys():
        raise ValueError("Example document is incomplete")
    example_id = document["id"]
    fix_id = document["fix_id"]
    note = document["context_note"]
    command = document["command"]
    output = document["output"]
    agents = document["agents"]
    code = document["exit_code"]
    if not isinstance(example_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_:-]{2,159}", example_id):
        raise ValueError("Invalid example ID")
    if (not isinstance(document["approved_digest"], str) or
        not re.fullmatch(r"[0-9a-f]{64}", document["approved_digest"])):
        raise ValueError("Invalid approved digest")
    if not all(isinstance(value, str) for value in (fix_id, note, command, output)):
        raise ValueError("Example text must be strings")
    if not 1 <= len(note) <= 500 or not 1 <= len(command) <= 2048 or len(output) > 500:
        raise ValueError("Example text is too long or empty")
    if isinstance(code, bool) or not isinstance(code, int) or not 1 <= code <= 255:
        raise ValueError("Example needs a nonzero exit code")
    if not isinstance(agents, list) or not agents or any(not isinstance(agent, str) for agent in agents):
        raise ValueError("Example needs applicable agents")
    if not isinstance(document.get("intent_sensitive", False), bool):
        raise ValueError("Invalid intent-sensitive flag")
    for value in (note, command, output):
        check_shared(value)
    parsed = parse_command(command, output, code)
    source_id = document.get("source_occurrence_id")
    if source_id is not None and (not isinstance(source_id, str) or
                                  not re.fullmatch(r"pierr-[0-9a-f-]{36}", source_id)):
        raise ValueError("Invalid source occurrence ID")
    with connection.transaction():
        fix = approved_fix(connection, fix_id, lock=True)
        if fix is None:
            raise ValueError("Fix is not active and approved")
        if fix["base_content_sha256"] != document["approved_digest"]:
            raise ValueError("Approved fix changed; example needs fresh owner review")
        if document["approved_by"] not in fix["owners"]:
            raise ValueError("Only a fix owner may approve its example")
        if not set(agents) <= set(fix["applies_to_agents"]):
            raise ValueError("Example scope exceeds approved fix scope")
        if source_id is not None:
            with connection.cursor() as cursor:
                cursor.execute("""SELECT outcome, reviewed_at FROM pi_tool_call.pi_tool_call_occurrence
                    WHERE id = %s""", (source_id,))
                row = cursor.fetchone()
            if row is None or row[0] != "error" or row[1] is None:
                raise ValueError("Source error needs owner review")
        with connection.cursor() as cursor:
            cursor.execute("""INSERT INTO pi_tool_call.pi_fix_example
                (id, fix_id, approved_digest, approved_by, approved_at,
                 source_occurrence_id, agents, context_note, command_name,
                 command_form, whole_form, step_count, prior_command, same_resource,
                 exit_code, error_class, output_present, options, arg_roles,
                 intent_sensitive, active)
                VALUES (%s, %s, %s, %s, now(), %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, true)
                ON CONFLICT (id) DO UPDATE SET
                  approved_digest = EXCLUDED.approved_digest,
                  approved_by = EXCLUDED.approved_by,
                  approved_at = EXCLUDED.approved_at,
                  source_occurrence_id = EXCLUDED.source_occurrence_id,
                  agents = EXCLUDED.agents, context_note = EXCLUDED.context_note,
                  command_name = EXCLUDED.command_name,
                  command_form = EXCLUDED.command_form,
                  whole_form = EXCLUDED.whole_form,
                  step_count = EXCLUDED.step_count,
                  prior_command = EXCLUDED.prior_command,
                  same_resource = EXCLUDED.same_resource,
                  exit_code = EXCLUDED.exit_code,
                  error_class = EXCLUDED.error_class,
                  output_present = EXCLUDED.output_present,
                  options = EXCLUDED.options, arg_roles = EXCLUDED.arg_roles,
                  intent_sensitive = EXCLUDED.intent_sensitive, active = true
                WHERE pi_fix_example.fix_id = EXCLUDED.fix_id""",
                (example_id, fix_id, fix["base_content_sha256"],
                 document["approved_by"], source_id, sorted(set(agents)), note,
                 parsed["command_name"], parsed["command_form"], parsed["whole_form"],
                 parsed["step_count"], parsed["prior_command"], parsed["same_resource"],
                 code, parsed["error_class"], parsed["output_present"], parsed["options"],
                 parsed["arg_roles"], bool(document.get("intent_sensitive", False))))
            if cursor.rowcount != 1:
                raise ValueError("Example ID already belongs to a different fix")
    return example_id
