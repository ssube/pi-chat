#!/usr/bin/env python3
"""Read or briefly watch an authorized, unencrypted Matrix room (no sends)."""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def private_json(path: Path) -> dict[str, Any]:
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError(f"{path.name} must be a regular, owner-only file")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    return value


def inherited_token(profile: dict[str, Any]) -> str:
    """Use existing owner startup credentials; never load dotenv or persist tokens."""
    name = profile.get("token_env")
    if name is not None and (not isinstance(name, str) or
                             not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name)):
        raise ValueError("profile Matrix token_env must be an environment variable name")
    token = os.environ.get(name, "") if name else ""
    startup = {key: os.environ.get("MATRIX_" + key, "")
               for key in ("URL", "USER_ID", "TOKEN")}
    if any(startup.values()):
        if (not all(startup.values()) or startup["USER_ID"] != profile.get("user_id") or
                startup["URL"].rstrip("/") != profile.get("homeserver", "").rstrip("/")):
            raise ValueError("inherited Matrix sender does not match the owner bridge profile")
        if token and token != startup["TOKEN"]:
            raise ValueError("inherited Matrix token sources conflict")
        token = startup["TOKEN"]
    if not token:
        raise ValueError("owner startup Matrix credentials are unavailable")
    return token


def profile_account(profile_path: Path):
    profile = private_json(profile_path).get("matrix")
    if not isinstance(profile, dict):
        raise ValueError("profile has no Matrix configuration")
    url, user, rooms = profile.get("homeserver"), profile.get("user_id"), profile.get("rooms")
    if (not isinstance(url, str) or not url.startswith("https://") or
            not isinstance(user, str) or not user.startswith("@") or
            not isinstance(rooms, list)):
        raise ValueError("profile has no usable Matrix account")
    return profile, url.rstrip("/"), user, set(room for room in rooms if isinstance(room, str))


def credentials(profile_path: Path, identity_path: Path | None = None) -> tuple[str, str, str, set[str]]:
    profile, url, user, rooms = profile_account(profile_path)
    identity = private_json(identity_path) if identity_path is not None else None
    token = identity.get("token") if identity is not None else inherited_token(profile)
    if (not isinstance(token, str) or not token or
            (identity is not None and identity.get("user_id") != user)):
        raise ValueError("profile and identity do not describe the same usable Matrix account")
    return url, user, token, rooms


def env_credentials(path: Path) -> tuple[str, str, str, set[str]]:
    """Read only bounded Matrix settings, without shell evaluation."""
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError(f"{path.name} must be a regular, owner-only file")
    names = {"MATRIX_URL", "MATRIX_ROOM_ID", "MATRIX_USER_ID",
             "MATRIX_TOKEN"}
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        key, separator, raw = line.partition("=")
        if separator and key.strip() in names:
            value = raw.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                value = value[1:-1]
            values[key.strip()] = value
    if any(not values.get(key) for key in names) or not values["MATRIX_URL"].startswith("https://"):
        raise ValueError("owner-only Matrix environment is incomplete")
    room = values["MATRIX_ROOM_ID"]
    user = values["MATRIX_USER_ID"]
    if not room.startswith("!") or ":" not in room or not user.startswith("@"):
        raise ValueError("owner-only Matrix environment has invalid room or user")
    return values["MATRIX_URL"].rstrip("/"), user, values["MATRIX_TOKEN"], {room}


def get(url: str, token: str, path: str, params: dict[str, str | int] | None = None) -> dict[str, Any]:
    address = url + path + ("?" + urllib.parse.urlencode(params) if params else "")
    request = urllib.request.Request(address, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            value = json.load(response)
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"Matrix request failed (HTTP {error.code})") from None
    except (urllib.error.URLError, TimeoutError):
        raise RuntimeError("Matrix request failed (network/timeout)") from None
    if not isinstance(value, dict):
        raise RuntimeError("Matrix returned an unexpected response")
    return value


def since_ms(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("--since must be an ISO 8601 timestamp with timezone") from None
    if parsed.tzinfo is None:
        raise ValueError("--since needs a timezone (such as +00:00 or Z)")
    return int(parsed.timestamp() * 1000)


def progress(body: str) -> bool:
    if len(body) > 120 or "\n" in body or "\r" in body:
        return False
    return (body in {"Still working…", "I’m working on it.", "Added to the current response."} or
            re.fullmatch(r"Using `(bash|read|write|edit|grep|find|ls)`(?:…|\.\.\.)?", body) is not None)


def select(events: list[dict[str, Any]], *, sender: str | None, after_ms: int | None,
           skip_progress: bool, contains: str | None = None) -> list[dict[str, Any]]:
    selected = []
    for event in events:
        content = event.get("content", {})
        body = content.get("body") if isinstance(content, dict) else None
        timestamp = event.get("origin_server_ts")
        if (event.get("type") != "m.room.message" or not isinstance(body, str) or
                not isinstance(timestamp, int) or
                content.get("msgtype") not in {"m.text", "m.notice"} or
                (sender and event.get("sender") != sender) or
                (after_ms is not None and timestamp < after_ms) or
                (skip_progress and progress(body)) or
                (contains is not None and contains.casefold() not in body.casefold())):
            continue
        selected.append(event)
    return selected


def history(url: str, token: str, room_id: str, *, limit: int, pages: int,
            sender: str | None, after_ms: int | None, skip_progress: bool,
            contains: str | None = None) -> list[dict[str, Any]]:
    path = "/_matrix/client/v3/rooms/" + urllib.parse.quote(room_id, safe="") + "/messages"
    cursor: str | None = None
    chosen: list[dict[str, Any]] = []
    seen: set[str] = set()
    for _ in range(pages):
        params: dict[str, str | int] = {"dir": "b", "limit": 100}
        if cursor:
            params["from"] = cursor
        result = get(url, token, path, params)
        chunk = result.get("chunk")
        if not isinstance(chunk, list):
            raise RuntimeError("Matrix history response has no event list")
        for event in chunk:
            if not isinstance(event, dict):
                continue
            event_id = event.get("event_id")
            if not isinstance(event_id, str) or event_id in seen:
                continue
            seen.add(event_id)
            chosen.extend(select([event], sender=sender, after_ms=after_ms,
                                 skip_progress=skip_progress, contains=contains))
            if len(chosen) >= limit:
                return list(reversed(chosen[:limit]))
        cursor = result.get("end") if isinstance(result.get("end"), str) else None
        if (not chunk or not cursor or
                (after_ms is not None and isinstance(chunk[-1], dict) and
                 isinstance(chunk[-1].get("origin_server_ts"), int) and
                 chunk[-1]["origin_server_ts"] < after_ms)):
            break
    return list(reversed(chosen[:limit]))


def wait_for_message(url: str, token: str, room_id: str, *, sender: str, keyword: str,
                     timeout: int, interval: int, skip_progress: bool = False) -> dict[str, Any] | None:
    """Start at the room's latest token, then scan only subsequent events."""
    path = "/_matrix/client/v3/rooms/" + urllib.parse.quote(room_id, safe="") + "/messages"
    baseline = get(url, token, path, {"dir": "b", "limit": 1})
    cursor = baseline.get("start")
    if not isinstance(cursor, str) or not cursor:
        raise RuntimeError("Matrix did not provide a room pagination token")
    old_ids = {e.get("event_id") for e in baseline.get("chunk", []) if isinstance(e, dict)}
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = get(url, token, path, {"dir": "f", "from": cursor, "limit": 100})
        chunk = result.get("chunk")
        end = result.get("end")
        if not isinstance(chunk, list) or (chunk and not isinstance(end, str)):
            raise RuntimeError("Matrix forward history response is incomplete")
        for event in chunk:
            if not isinstance(event, dict) or event.get("event_id") in old_ids:
                continue
            matched = select([event], sender=sender, after_ms=None,
                             skip_progress=skip_progress, contains=keyword)
            if matched:
                return matched[0]
        if isinstance(end, str) and end != cursor:
            cursor = end
            # A full page can have more unseen messages; drain it before sleeping.
            if len(chunk) >= 100:
                continue
        time.sleep(min(interval, max(0, deadline - time.monotonic())))
    return None


def output(events: list[dict[str, Any]], *, full: bool, metadata: bool, json_lines: bool) -> None:
    for event in events:
        body = event["content"]["body"]
        if not full:
            body = body[:240] + ("…" if len(body) > 240 else "")
        row = {"time": datetime.fromtimestamp(event["origin_server_ts"] / 1000, timezone.utc)
               .isoformat(timespec="seconds"),
               "sender": event.get("sender"), "event_id": event["event_id"]}
        if not metadata:
            row["body"] = body
        if json_lines:
            print(json.dumps(row, ensure_ascii=True))
        else:
            print(" ".join(str(row[key]) for key in ("time", "sender", "event_id")) +
                  (" " + json.dumps(body, ensure_ascii=True) if not metadata else ""))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile-file", type=Path)
    parser.add_argument("--identity-file", type=Path)
    parser.add_argument("--env-file", type=Path, help="owner-only chair Matrix .env alternative")
    route = parser.add_mutually_exclusive_group(required=True)
    route.add_argument("--room-id", help="full room ID, only if enabled in the profile")
    route.add_argument("--contact", help="key in the owner-only connector mapping")
    parser.add_argument("--contacts-file", type=Path)
    parser.add_argument("--sender", help="filter by an exact Matrix user ID")
    parser.add_argument("--contains", help="literal, case-insensitive body substring")
    parser.add_argument("--since", help="ISO 8601 timestamp with timezone")
    parser.add_argument("--limit", type=int, default=20, help="matching messages to show (1–100)")
    parser.add_argument("--pages", type=int, default=2, help="max 100-event pages to scan (1–5)")
    parser.add_argument("--skip-progress", action="store_true")
    parser.add_argument("--full-body", action="store_true", help="opt in to full private message bodies")
    parser.add_argument("--metadata-only", action="store_true")
    parser.add_argument("--json", action="store_true", help="one JSON object per matching event")
    parser.add_argument("--watch", type=int, metavar="SECONDS", help="show recent and new matches for up to 600 seconds")
    parser.add_argument("--wait-for", metavar="KEYWORD", help="wait for a NEW message containing this literal keyword")
    parser.add_argument("--timeout", type=int, default=120, help="--wait-for deadline in seconds (1–3600)")
    parser.add_argument("--interval", type=int, default=5, help="polling interval (2–60 seconds)")
    args = parser.parse_args(argv)
    if not 1 <= args.limit <= 100 or not 1 <= args.pages <= 5:
        parser.error("--limit must be 1–100 and --pages 1–5")
    if args.watch is not None and not 1 <= args.watch <= 600:
        parser.error("--watch must be 1–600 seconds")
    if not 2 <= args.interval <= 60:
        parser.error("--interval must be 2–60 seconds")
    if not 1 <= args.timeout <= 3600:
        parser.error("--timeout must be 1–3600 seconds")
    if args.wait_for is not None:
        if (not args.wait_for or not args.sender or not args.sender.startswith("@") or
                ":" not in args.sender or args.watch is not None or
                args.contains is not None or args.since is not None):
            parser.error("--wait-for needs an exact --sender and cannot combine with "
                         "--watch, --contains, or --since")
    if args.contact and not args.contacts_file:
        parser.error("--contact requires --contacts-file")
    if bool(args.env_file) == bool(args.profile_file):
        parser.error("use either --env-file or --profile-file")
    if args.identity_file and not args.profile_file:
        parser.error("--identity-file requires --profile-file")
    try:
        url, user, token, allowed_rooms = (env_credentials(args.env_file) if args.env_file
                                           else credentials(args.profile_file, args.identity_file))
        room_id = (private_json(args.contacts_file).get(args.contact, {}).get("room_id")
                   if args.contact else args.room_id)
        if not isinstance(room_id, str) or room_id not in allowed_rooms:
            raise ValueError("room is not enabled in this owner's Matrix profile")
        if get(url, token, "/_matrix/client/v3/account/whoami").get("user_id") != user:
            raise ValueError("Matrix token does not belong to the configured account")
        if args.wait_for is not None:
            found = wait_for_message(url, token, room_id, sender=args.sender,
                                     keyword=args.wait_for, timeout=args.timeout,
                                     interval=args.interval, skip_progress=args.skip_progress)
            if found is None:
                print("matrix-read: timed out waiting for a new matching message", file=sys.stderr)
                return 2
            output([found], full=args.full_body, metadata=args.metadata_only,
                   json_lines=args.json)
            return 0
        after_ms = since_ms(args.since)
        entries = history(url, token, room_id, limit=args.limit, pages=args.pages,
                          sender=args.sender, after_ms=after_ms,
                          skip_progress=args.skip_progress, contains=args.contains)
        output(entries, full=args.full_body, metadata=args.metadata_only, json_lines=args.json)
        if args.watch is not None:
            observed = {e["event_id"] for e in entries}
            deadline = time.monotonic() + args.watch
            while time.monotonic() < deadline:
                time.sleep(min(args.interval, max(0, deadline - time.monotonic())))
                recent = history(url, token, room_id, limit=100, pages=1, sender=args.sender,
                                 after_ms=after_ms, skip_progress=args.skip_progress,
                                 contains=args.contains)
                fresh = [e for e in recent if e["event_id"] not in observed]
                output(fresh, full=args.full_body, metadata=args.metadata_only,
                       json_lines=args.json)
                observed.update(e["event_id"] for e in recent)
        return 0
    except (OSError, ValueError, RuntimeError) as error:
        print(f"matrix-read: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
