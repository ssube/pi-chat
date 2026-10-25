#!/usr/bin/env python3
"""Bounded owner-local inventory. Never repairs, installs or invokes model tools."""

from __future__ import annotations

import argparse
import json
import os
import re
import selectors
import shutil
import signal
import subprocess
import sys
import time
import urllib.parse
from collections import Counter
from pathlib import Path

MAX_BYTES = 128 * 1024
MAX_FILE = 1024 * 1024
MAX_ENTRIES = 10000
TOOLS = {
    "git": "--version",
    "pi": "--version",
    "node": "--version",
    "npm": "--version",
    "make": "--version",
    "helm": "version",
    "promtool": "--version",
    "curl": "--version",
}
IMPORTS = ["dotenv", "psycopg", "yaml", "requests", "discord", "playwright"]
VERSION = re.compile(
    r"(?<![A-Za-z0-9])v?(\d+\.\d+(?:\.\d+)?(?:[-+][A-Za-z0-9.]+)?)(?![A-Za-z0-9])"
)


def run(argv, timeout=5, env=None):
    """Drain pipes with a hard output/time cap; kill descendants on failure."""
    try:
        process = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env=env,
        )
    except OSError:
        return "unavailable", b""
    output = bytearray()
    reason = None
    deadline = time.monotonic() + timeout
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                reason = "timeout"
                break
            for key, _ in selector.select(min(remaining, 0.1)):
                chunk = os.read(key.fd, 8192)
                if not chunk:
                    selector.unregister(key.fileobj)
                elif len(output) + len(chunk) > MAX_BYTES:
                    reason = "output-limit"
                    break
                else:
                    output.extend(chunk)
            if reason:
                break
        if reason:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        try:
            process.wait(timeout=max(0.1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            reason = "timeout"
    process.stdout.close()
    return reason or ("ok" if process.returncode == 0 else "nonzero"), bytes(output)


def read_json(path):
    if not Path(path).is_file():
        raise ValueError("not-regular-file")
    with Path(path).open("rb") as handle:
        content = handle.read(MAX_FILE + 1)
    if len(content) > MAX_FILE:
        raise ValueError("file-limit")
    try:
        return json.loads(content)
    except RecursionError as error:
        raise ValueError("json-depth-limit") from error


def tool_check(name, flag, timeout):
    executable = shutil.which(name)
    if not executable:
        return {"check": "tool", "name": name, "state": "missing"}
    state, output = run([executable, flag], timeout)
    match = (
        VERSION.search(output.decode("utf-8", errors="replace"))
        if state == "ok"
        else None
    )
    return {
        "check": "tool",
        "name": name,
        "path": executable,
        "state": "ok" if match else "unknown",
        "version": match.group(1) if match else None,
        "evidence": "version-only" if match else state,
    }


PYTHON_PROBE = r"""
import contextlib, importlib, importlib.metadata as md, json, os, sys
result = {'executable': sys.executable, 'version': sys.version.split()[0],
          'prefix': sys.prefix, 'base_prefix': sys.base_prefix, 'imports': []}
with open(os.devnull, 'w') as quiet, contextlib.redirect_stdout(quiet), contextlib.redirect_stderr(quiet):
    owners = md.packages_distributions()
    for name in json.loads(sys.argv[1]):
        row = {'module': name, 'distributions': []}
        try:
            importlib.import_module(name)
            row['state'] = 'ok'
        except ModuleNotFoundError as exc:
            row['state'] = 'missing' if exc.name == name else 'failed'
        except Exception:
            row['state'] = 'failed'
        for distribution in sorted(set(owners.get(name, []))):
            try: row['distributions'].append({'name': distribution, 'version': md.version(distribution)})
            except md.PackageNotFoundError: pass
        result['imports'].append(row)
print(json.dumps(result))
"""


def python_check(executable, timeout):
    state, output = run(
        [executable, "-B", "-c", PYTHON_PROBE, json.dumps(IMPORTS)], timeout
    )
    if state != "ok":
        return {
            "check": "python",
            "interpreter": executable,
            "state": "unknown",
            "evidence": state,
        }
    try:
        data = json.loads(output)
        if not isinstance(data.get("imports"), list):
            raise ValueError("format")
    except (ValueError, AttributeError):
        return {
            "check": "python",
            "interpreter": executable,
            "state": "unknown",
            "evidence": "invalid-json",
        }
    return {
        "check": "python",
        "state": "ok",
        "inventory_only": True,
        "import_coverage": dict(Counter(item["state"] for item in data["imports"])),
        **data,
    }


def skill_name(content, path):
    """Read the bounded scalar name without conflating validation and I/O."""
    front = content.decode("utf-8-sig").split("---", 2)
    if len(front) != 3 or front[0]:
        raise ValueError("missing-frontmatter")
    match = re.search(r"^name:[ \t]*(.*?)[ \t]*$", front[1], re.M)
    value = match.group(1) if match else ""
    if value.startswith('"'):
        value = json.loads(value)
    elif value.startswith("'"):
        if len(value) < 2 or not value.endswith("'"):
            raise ValueError("unsupported-name-scalar")
        value = value[1:-1].replace("''", "'")
    else:
        value = value.split(" #", 1)[0].rstrip()
        if value.startswith(("[", "{", "|", ">", "&", "*")):
            raise ValueError("unsupported-name-scalar")
    if not isinstance(value, str):
        raise ValueError("unsupported-name-scalar")
    # Pi uses the parent directory when name is absent or empty, and still loads
    # readable invalid names with warnings rather than treating them as I/O errors.
    name = value or path.parent.name
    warnings = []
    if len(name) > 64:
        warnings.append("name-too-long")
    if not re.fullmatch(r"[a-z0-9-]+", name):
        warnings.append("invalid-name-characters")
    if name.startswith("-") or name.endswith("-"):
        warnings.append("name-edge-hyphen")
    if "--" in name:
        warnings.append("name-consecutive-hyphens")
    return name, warnings


def skill_check(root):
    root = Path(root).expanduser()
    result = {
        "check": "skills",
        "root": str(root),
        "state": "ok",
        "broken_links": [],
        "cycles": [],
        "duplicate_names": [],
        "name_warnings": [],
        "skill_count": 0,
    }
    if not root.exists():
        result["state"] = "unconfigured" if not root.is_symlink() else "failed"
        if root.is_symlink():
            result["broken_links"].append(str(root))
        return result
    if not root.is_dir():
        return {**result, "state": "unknown", "evidence": "root-is-not-directory"}
    names = {}
    pending = [(root, set(), 0, True)]
    visited = 0
    while pending:
        path, ancestors, depth, discover = pending.pop()
        visited += 1
        if visited > MAX_ENTRIES or depth > 32:
            result.update(state="unknown", evidence="scan-limit")
            break
        try:
            resolved = path.resolve()
            if path.is_symlink() and not path.exists():
                result["broken_links"].append(str(path))
            elif path.is_dir():
                if resolved in ancestors:
                    result["cycles"].append(str(path))
                    continue
                # Pi stops skill discovery below a directory containing SKILL.md.
                # Continue the filesystem walk there to catch broken script links.
                skill_root = discover and (path / "SKILL.md").is_file()
                for child in path.iterdir():
                    if child.name in {".git", "node_modules", "__pycache__", ".venv"}:
                        continue
                    if len(pending) + visited >= MAX_ENTRIES:
                        result.update(state="unknown", evidence="scan-limit")
                        break
                    pending.append(
                        (
                            child,
                            ancestors | {resolved},
                            depth + 1,
                            discover and (not skill_root or child.name == "SKILL.md"),
                        )
                    )
            elif discover and path.name == "SKILL.md" and path.is_file():
                with path.open("rb") as handle:
                    content = handle.read(65537)
                if len(content) > 65536:
                    result.update(state="unknown", evidence="skill-file-limit")
                    continue
                try:
                    name, warnings = skill_name(content, path)
                except (ValueError, TypeError):
                    result.update(state="unknown", evidence="unreadable-skill-name")
                    continue
                names.setdefault(name, set()).add(str(resolved))
                if warnings:
                    result["name_warnings"].append(
                        {"path": str(resolved), "codes": warnings}
                    )
                    result.update(state="unknown", evidence="skill-name-warning")
        except (OSError, RuntimeError, UnicodeError):
            result.update(state="unknown", evidence="unreadable-entry")
    result["skill_count"] = sum(len(paths) for paths in names.values())
    result["duplicate_names"] = [
        {"name": name, "paths": sorted(paths)}
        for name, paths in sorted(names.items())
        if len(paths) > 1
    ]
    if result["broken_links"] or result["cycles"] or result["duplicate_names"]:
        result["state"] = "failed"
    result["entries_checked"] = min(visited, MAX_ENTRIES)
    return result


def mcp_check(config_paths, snapshot):
    """Read declarations and supplied status only; never start an MCP server."""
    declarations = {}
    issues = []
    for path in config_paths:
        if not Path(path).exists():
            continue
        try:
            servers = read_json(path)["mcpServers"]
            if not isinstance(servers, dict):
                raise ValueError("format")
            for name, item in servers.items():
                if not isinstance(item, dict):
                    raise ValueError("format")
                declarations[name] = not (
                    item.get("disabled", False) or item.get("enabled") is False
                )
        except (OSError, ValueError, KeyError, TypeError):
            issues.append("unreadable-config")
    rows = []
    observed = {}
    if snapshot:
        try:
            data = read_json(snapshot)
            if not isinstance(data.get("servers"), list):
                raise ValueError("format")
            if any(
                not isinstance(item, dict) or not isinstance(item.get("name"), str)
                for item in data["servers"]
            ):
                raise ValueError("format")
            observed = {item["name"]: item for item in data["servers"]}
            if data.get("errors"):
                issues.append("status-reports-errors")
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            issues.append("unreadable-status")
    for name in sorted(declarations.keys() | observed.keys()):
        item = observed.get(name)
        enabled = declarations.get(name, item.get("enabled", True) if item else True)
        state = "dormant" if not enabled else "unknown"
        if enabled and item:
            raw = item.get("state")
            state = (
                "ok"
                if raw == "connected"
                else "failed"
                if raw in {"failed", "error"}
                else "unknown"
            )
        rows.append(
            {
                "name": name,
                "configured": name in declarations,
                "state": state,
                "tool_count": len(item.get("tools", []))
                if item and isinstance(item.get("tools", []), list)
                else None,
            }
        )
    return {
        "check": "mcp",
        "state": "failed"
        if any(row["state"] == "failed" for row in rows)
        else "unknown"
        if issues or any(row["state"] == "unknown" for row in rows)
        else "ok"
        if rows
        else "unconfigured",
        "servers": rows,
        "issues": issues,
        "evidence": "supplied-status-snapshot" if snapshot else "declarations-only",
        "snapshot_freshness": "not-verified" if snapshot else None,
    }


SERVICE_PROBE = r"""
import sys, urllib.request, urllib.error
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl): return None
try:
    req = urllib.request.Request(sys.argv[1], method='GET', headers={'User-Agent': 'environment-doctor'})
    with urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect).open(req, timeout=float(sys.argv[2])) as response: print(response.status)
except urllib.error.HTTPError as exc: print(exc.code)
except Exception: print('connection-error')
"""


def service_check(spec, timeout):
    name, sep, url = spec.partition("=")
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        parsed = urllib.parse.urlsplit("")
    if (
        not sep
        or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", name)
        or parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or "?" in url
        or "#" in url
    ):
        return {
            "check": "service",
            "state": "unknown",
            "evidence": "invalid-endpoint; use NAME=http(s)://host/path without credentials/query/fragment",
        }
    result = {"check": "service", "name": name, "state": "unknown"}
    state, output = run(
        [sys.executable, "-B", "-c", SERVICE_PROBE, url, str(timeout)], timeout
    )
    if state == "ok" and re.fullmatch(rb"\d{3}\s*", output):
        status = int(output)
        result.update(
            state="ok" if 200 <= status < 300 else "failed", http_status=status
        )
    else:
        result.update(
            state="failed", evidence=state if state != "ok" else "connection-error"
        )
    return result


def git_env():
    """Drop ambient GIT_* overrides that would answer for a different repository."""
    return {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }


def git_read(root, arguments, timeout):
    """Run one read-only git query; --no-optional-locks keeps the index untouched."""
    return run(
        ["git", "--no-optional-locks", "-C", str(root), *arguments], timeout, git_env()
    )


def link_state(root, relative, timeout):
    """Read-only git state for one checkout/path pair; None fields mean unverified."""
    state, output = git_read(root, ["ls-files", "-z", "--", relative], timeout)
    if state != "ok":
        return {"tracked": None, "modified": None, "evidence": "git-ls-files-" + state}
    tracked = bool(output.strip(b"\0"))
    state, output = git_read(
        root,
        ["status", "--porcelain", "-z", "--untracked-files=normal", "--", relative],
        timeout,
    )
    if state != "ok":
        return {"tracked": tracked, "modified": None, "evidence": "git-status-" + state}
    return {"tracked": tracked, "modified": bool(output), "evidence": None}


def checkout_version(root, timeout):
    """Report the checkout's HEAD and whether that exact commit is a tag."""
    state, output = git_read(root, ["rev-parse", "--short", "HEAD"], timeout)
    if state != "ok":
        return {"state": "unknown", "evidence": "git-head-" + state}
    head = output.decode("utf-8", "replace").strip()
    state, output = git_read(
        root, ["describe", "--exact-match", "--tags", "HEAD"], timeout
    )
    # A non-matching describe is an ordinary answer (HEAD is not a tag), not a failure.
    describe = output.decode("utf-8", "replace").strip() if state == "ok" else None
    return {
        "state": state if state in {"ok", "nonzero"} else "unknown",
        "head": head,
        "tagged": state == "ok",
        "describe": describe,
    }


def link_check(paths, checkout, timeout):
    """Report declared links that must resolve inside one caller-named checkout.

    A target outside the declared checkout is confirmed, not guessed, so it fails.
    Dirtiness and absolute link text warn: an advanced or edited checkout is normal
    operation, and only the declared containment rule makes a link wrong.
    """
    result = {"check": "links", "state": "ok", "links": []}
    root = Path(checkout).expanduser() if checkout else None
    if root is None:
        return {**result, "state": "unknown", "evidence": "no-checkout-declared"}
    if not root.is_dir():
        return {
            **result,
            "state": "unknown",
            "evidence": "checkout-unavailable",
            "checkout": str(root),
        }
    root = root.resolve()
    result["checkout"] = str(root)
    result["version"] = checkout_version(root, timeout)
    cache = {}
    for raw in paths:
        link = Path(raw).expanduser()
        entry = {"link": str(link), "state": "ok", "findings": []}
        result["links"].append(entry)
        if not link.is_symlink():
            entry.update(state="unknown", evidence="not-a-symlink")
            continue
        try:
            text = os.readlink(link)
        except OSError:
            entry.update(state="unknown", evidence="unreadable-link")
            continue
        entry["kind"] = "absolute" if os.path.isabs(text) else "relative"
        try:
            target = link.resolve(strict=True)
        except (OSError, RuntimeError):
            entry.update(state="failed", evidence="broken-link")
            continue
        entry["target"] = str(target)
        if entry["kind"] == "absolute":
            entry["findings"].append("absolute-target")
        try:
            relative = target.relative_to(root).as_posix()
        except ValueError:
            entry["findings"].append("outside-checkout")
            entry.update(
                state="failed", evidence="outside-checkout", tracked=None, modified=None
            )
            continue
        entry["repo_path"] = relative
        if relative not in cache:
            cache[relative] = link_state(root, relative, timeout)
        state = cache[relative]
        entry.update(tracked=state["tracked"], modified=state["modified"])
        if state["evidence"]:
            entry.update(state="unknown", evidence=state["evidence"])
        elif state["tracked"] is False:
            entry["findings"].append("untracked-target")
        elif state["modified"]:
            entry["findings"].append("modified-target")
    if any(entry["state"] == "failed" for entry in result["links"]):
        result["state"] = "failed"
    elif any(entry["state"] == "unknown" for entry in result["links"]):
        result["state"] = "unknown"
    return result


def cron_check(script, config, timeout):
    if not script or not Path(script).is_file():
        return {
            "check": "cron",
            "state": "unconfigured",
            "evidence": "persistence-status-entrypoint-not-selected",
        }
    env = os.environ.copy()
    if config:
        env["AGENT_CRON_CONFIG"] = str(config)
    state, output = run([sys.executable, "-B", str(script), "status"], timeout, env)
    try:
        data = json.loads(output)
        if not isinstance(data, dict):
            raise ValueError("format")
    except ValueError:
        return {
            "check": "cron",
            "state": "unknown",
            "evidence": state if state != "ok" else "invalid-json",
        }
    # Keep source status provenance; never include crontab commands, env or errors.
    fields = {
        key: data[key]
        for key in (
            "owner",
            "source",
            "source_sha256",
            "rendered_sha256",
            "installed_sha256",
            "source_jobs",
            "installed_jobs",
            "matches",
        )
        if key in data and isinstance(data[key], (str, int, bool, type(None)))
    }
    return {
        "check": "cron",
        "state": "ok" if state == "ok" and data.get("matches") is True else "unknown",
        "status_exit": state,
        **fields,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--python",
        action="append",
        dest="pythons",
        help="Supported interpreter to inspect; repeat up to four times.",
    )
    parser.add_argument(
        "--skill-root",
        action="append",
        help="Installed discovery root; repeat up to four times.",
    )
    parser.add_argument(
        "--mcp-config",
        action="append",
        help="Pi mcp.json declarations (credentials are never reported).",
    )
    parser.add_argument(
        "--mcp-status",
        help="Existing pi mcp list --json snapshot; no MCP process is launched.",
    )
    parser.add_argument(
        "--service",
        action="append",
        default=[],
        help="Explicit safe readiness GET, NAME=URL; no auth, redirects, body output.",
    )
    parser.add_argument(
        "--link",
        action="append",
        default=[],
        help="Declared skill/extension link that must resolve inside --checkout; repeat up to sixteen times.",
    )
    parser.add_argument(
        "--checkout",
        type=Path,
        help="The one shared checkout declared links must resolve inside.",
    )
    parser.add_argument(
        "--cron-script",
        type=Path,
        help="Reviewed shared cron persistence.py status entrypoint, if installed.",
    )
    parser.add_argument(
        "--cron-config",
        type=Path,
        help="Owner AGENT_CRON_CONFIG override for optional status check.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=5,
        help="Per process/request deadline, 0.1 to 10 seconds.",
    )
    args = parser.parse_args(argv)
    agent_dir = Path(
        os.environ.get("PI_CODING_AGENT_DIR", str(Path.home() / ".pi/agent"))
    )
    pythons = args.pythons or [sys.executable]
    roots = args.skill_root or [str(agent_dir / "skills")]
    configs = args.mcp_config or [str(agent_dir / "mcp.json")]
    if (
        not 0.1 <= args.timeout <= 10
        or max(map(len, (pythons, roots, configs, args.service))) > 4
        or len(args.link) > 16
    ):
        parser.error(
            "timeout must be 0.1..10 seconds; four values per interpreter/root/config/service option and sixteen links"
        )
    if args.link and args.checkout is None:
        parser.error(
            "--link requires --checkout; links are judged against one declared checkout"
        )
    checks = [tool_check(name, flag, args.timeout) for name, flag in TOOLS.items()]
    checks.extend(python_check(executable, args.timeout) for executable in pythons)
    checks.extend(skill_check(root) for root in roots)
    checks.append(mcp_check(configs, args.mcp_status))
    checks.append(
        link_check(args.link, args.checkout, args.timeout)
        if args.link
        else {
            "check": "links",
            "state": "unconfigured",
            "evidence": "no-links-selected",
        }
    )
    checks.append(cron_check(args.cron_script, args.cron_config, args.timeout))
    checks.extend(service_check(spec, args.timeout) for spec in args.service)
    if not args.service:
        checks.append(
            {
                "check": "service",
                "state": "unconfigured",
                "evidence": "no-readiness-endpoints-selected",
            }
        )
    print(
        json.dumps(
            {
                "schema_version": 1,
                "coverage": dict(Counter(check["state"] for check in checks)),
                "checks": checks,
                "interpretation": "Inventory is not an installation requirement. Missing optional tools/imports are not a runtime defect until a supported task requires them. Unknown/unconfigured checks are not verified healthy.",
            },
            indent=2,
        )
    )
    return 1 if any(check["state"] == "failed" for check in checks) else 0


if __name__ == "__main__":
    raise SystemExit(main())
