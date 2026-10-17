"""Matrix sync and output adapter for the shared Pi runtime."""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import logging
import math
import os
import re
import tempfile
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, TYPE_CHECKING

from bridge import __version__
from bridge.config import MatrixSettings
from bridge.debug import debug_status
from bridge.activity import Activity, Presence
from bridge.filtering import SILENT_KINDS, metadata_rejection
from bridge.telemetry import Telemetry
from bridge.turn_status import Indicator, TURN_EMOJI, TurnStatus
from bridge.progress import sealed_progress
from bridge.history_service import HistoryPage, HistoryMessage
from bridge.mentions import (BROADCAST_REJECTION, Member, Roster, find_mentions, has_broadcast_ping,
                             replace_mentions)
from bridge.room_lease import RoomLeases
from bridge.reaction_service import ReactionRejected
from bridge.runtime.pi_rpc import RpcImage
from bridge.runtime.session import BridgeRuntime, Incoming, ReplyRejected, StopRequested
from bridge.runtime.state import SessionKey



if TYPE_CHECKING:
    from bridge.review_queue import ReviewQueue


log = logging.getLogger("agent_bridge.matrix")

# A rate-limited homeserver must not silently drop a reply. Retry a 429 a few
# times, honoring the delay the server asks for, before giving up to the caller.
MATRIX_MAX_ATTEMPTS = 4
# The acknowledgement PUT is the one control-plane write whose failure aborts an
# addressed request and leaves the room with "I could not start this request".
# Homeserver rate limiting (HTTP 429) can outlive the default four-attempt budget,
# so a 429 that identifies itself as a limiter breach is given a deeper, still
# hint-respecting budget of its own. A 429 that carries neither an errcode nor a
# retry hint is not a limiter -- and neither is a 5xx -- so both keep the shorter
# budget, as do progress edits and other traffic.
MATRIX_ACK_ATTEMPTS = 8
# The ack is written while the runtime holds the per-scope lock, and both cancel()
# and the emergency stop need that lock. A deeper attempt count on its own could
# therefore hold a scope for minutes (seven 30s hints plus every request timeout),
# so the ack's whole retry window is bounded instead: no wait may start once the
# budget is spent, and no attempt is longer than what remains of it. Only a
# validated limiter 429 may use the extra attempts; a 5xx backend keeps the
# default budget.
MATRIX_ACK_RETRY_BUDGET_SECONDS = 60.0
MATRIX_MAX_RETRY_SECONDS = 30.0
# A cancel or emergency stop queues on the scope lock the acknowledgement holds
# across its retries, so the acknowledgement's backoff wait is polled at this
# interval: the stop takes the lock instead of waiting out a 30s server hint.
# Only a stop authorized for this request's own author is allowed to interrupt
# it, so the poll is made with that requester.
MATRIX_STOP_POLL_SECONDS = 0.25
# A room roster is small and rarely changes; a short cache keeps every reply
# from costing an extra round trip.
ROSTER_TTL_SECONDS = 60.0




def _now() -> float:
    """Monotonic seconds. A seam so the retry-budget tests need no real waiting."""
    return time.monotonic()


def _retry_hint(payload: Any, headers: Any) -> float | None:
    """The delay the server itself asked for, or None when it asked for none.

    A hint is evidence that the server knows it is limiting this client: Synapse
    names a limiter breach with ``retry_after_ms`` (or ``Retry-After``). Its
    absence is equally informative, see ``_is_limiter_429``.
    """
    if isinstance(payload, dict):
        milliseconds = payload.get("retry_after_ms")
        if milliseconds is not None:
            try:
                seconds = float(milliseconds) / 1000.0
                if math.isfinite(seconds):
                    return min(max(seconds, 0.0), MATRIX_MAX_RETRY_SECONDS)
            except (TypeError, ValueError):
                pass
    header = headers.get("Retry-After") if headers is not None else None
    if header:
        try:
            seconds = float(header)
            if math.isfinite(seconds):
                return min(max(seconds, 0.0), MATRIX_MAX_RETRY_SECONDS)
        except ValueError:
            pass
    return None


def _retry_delay(payload: Any, headers: Any, attempt: int) -> float:
    """Seconds to wait before retrying a transient request failure."""
    hint = _retry_hint(payload, headers)
    if hint is not None:
        return hint
    return min(0.5 * (2 ** (attempt - 1)), MATRIX_MAX_RETRY_SECONDS)


async def _json_or_none(response: Any) -> Any:
    try:
        return await response.json(content_type=None)
    except Exception:
        return None


def _describe_429(payload: Any) -> str:
    """Describe a 429 body enough to tell a limiter breach from a dead backend.

    Synapse answers its own limiter with ``errcode`` ``M_LIMIT_EXCEEDED`` and a
    ``retry_after_ms`` hint. The HAProxy front door reuses 429 for
    ``M_UNKNOWN`` / "Server is unavailable" when Synapse has no live backend.
    The two need opposite responses, so log which one arrived instead of
    guessing from the status code alone.
    """
    if not isinstance(payload, dict):
        return "no JSON body"
    parts = []
    errcode = payload.get("errcode")
    if isinstance(errcode, str) and errcode:
        parts.append(f"errcode={errcode}")
    error = payload.get("error")
    if isinstance(error, str) and error:
        parts.append(f"error={error[:120]}")
    return ", ".join(parts) if parts else "empty JSON body"


def _is_limiter_429(payload: Any, headers: Any) -> bool:
    """Whether a 429 proves a limiter breach rather than an unavailable backend.

    Only the homeserver's limiter identifies itself, with ``M_LIMIT_EXCEEDED``
    and/or a retry hint. HAProxy reuses 429 with ``M_UNKNOWN``, "Server is
    unavailable" and no hint while Synapse has no live backend; that is an
    outage, not a limiter, and must not earn the acknowledgement's deeper
    attempt budget.
    """
    errcode = payload.get("errcode") if isinstance(payload, dict) else None
    if isinstance(errcode, str) and errcode.strip().upper() == "M_LIMIT_EXCEEDED":
        return True
    return _retry_hint(payload, headers) is not None


def progress_html(text: str) -> str | None:
    match = re.fullmatch(r"Using `([^`]+)`…", text)
    if match is None:
        return None
    return f"Using <code>{html.escape(match.group(1))}</code>…"
BRIDGE_META = "org.example.pi_bridge"
MAX_HANDOFF_HOPS = 3
MATRIX_ID = re.compile(r"^@[A-Za-z0-9._=/-]+:[A-Za-z0-9.-]+(?::[0-9]+)?$")


def leading_agent_mentions(text: str) -> list[str]:
    """Explicit bridge handoffs are full Matrix IDs at the start of a reply."""
    result = []
    for token in text.lstrip().split():
        user_id = token.lstrip("([{<\"'").rstrip(".,;:!?)]}>\"'")
        if not MATRIX_ID.fullmatch(user_id):
            break
        result.append(user_id)
    return list(dict.fromkeys(result))


def matrix_scope(room_id: str, event: dict[str, Any]) -> str:
    content = event.get("content", {})
    relates = content.get("m.relates_to", {}) if isinstance(content, dict) else {}
    if isinstance(relates, dict) and relates.get("rel_type") == "m.thread":
        root = relates.get("event_id")
        if isinstance(root, str) and root:
            return f"{room_id}:thread:{root}"
    return f"{room_id}:main"


def explicit_ping(content: dict[str, Any], user_id: str) -> bool:
    """Ignore the automatic mention added by a reply unless the new text pings us."""
    body = content.get("body")
    if not isinstance(body, str):
        return False
    relates = content.get("m.relates_to")
    parent = relates.get("m.in_reply_to") if isinstance(relates, dict) else None
    is_reply = isinstance(parent, dict) and isinstance(parent.get("event_id"), str)
    if is_reply and body.startswith("> "):
        body = body.split("\n\n", 1)[1] if "\n\n" in body else ""
    if user_id in body:
        return True
    mentions = content.get("m.mentions")
    user_ids = mentions.get("user_ids") if isinstance(mentions, dict) else None
    if not isinstance(user_ids, list) or user_id not in user_ids:
        return False
    if not is_reply:
        return True
    html = content.get("formatted_body")
    if not isinstance(html, str):
        return False
    authored = re.sub(r"<mx-reply\b[^>]*>.*?</mx-reply\s*>", "", html,
                      flags=re.IGNORECASE | re.DOTALL)
    target = re.escape(user_id)
    return bool(re.search(r"<a\b[^>]*\bhref\s*=\s*['\"]https://matrix\.to/#/" +
                          target + r"['\"]", urllib.parse.unquote(authored), re.IGNORECASE))


def addressed_to(event: dict[str, Any], user_id: str, aliases: tuple[str, ...] = ()) -> bool:
    content = event.get("content", {})
    if not isinstance(content, dict):
        return False
    mentions = content.get("m.mentions", {})
    user_ids = mentions.get("user_ids") if isinstance(mentions, dict) else None
    if isinstance(user_ids, list) and user_id in user_ids:
        return True
    body = content.get("body", "")
    if not isinstance(body, str):
        return False
    names = {user_id[1:].split(":", 1)[0].casefold(), *(alias.casefold() for alias in aliases)}
    return any(mention.user_id == user_id or
               (mention.server is None and mention.localpart.casefold() in names)
               for mention in find_mentions(body, names))


# Native room commands in every addressed spelling: bang commands for the
# pre-admission diagnostics and the plain words a requester already types for
# session control. Longer alternates sort first so alternation cannot shadow
# them. The same forms exist on Discord (mention prefix) and as Discord slash
# commands: one command surface across transports.
BRIDGE_COMMANDS = {
    "!debug": "debug",
    "!stop": "stop",
    "cancel": "cancel",
    "/cancel": "cancel",
    "!cancel": "cancel",
    "recover": "recover",
    "/recover": "recover",
    "!recover": "recover",
}
_COMMAND_TOKENS = "|".join(re.escape(token) for token in
                           sorted(BRIDGE_COMMANDS, key=len, reverse=True))


def bridge_command(content: dict[str, Any], user_id: str,
                   display_name: str | None = None) -> str | None:
    """Parse an explicitly addressed native command, including display-name pings."""
    if content.get("msgtype") != "m.text":
        return None
    body = content.get("body")
    if not isinstance(body, str):
        return None
    mentions = content.get("m.mentions", {})
    user_ids = mentions.get("user_ids") if isinstance(mentions, dict) else None
    if isinstance(user_ids, list) and user_id in user_ids:
        # Some clients send the command as a bare body with the mention only in
        # m.mentions.user_ids, so the address never reaches the text. Honour the
        # token then, but only when it is the whole message: ordinary prose that
        # merely mentions the agent stays ordinary text.
        bare = body.strip().lower()
        if bare in BRIDGE_COMMANDS:
            return BRIDGE_COMMANDS[bare]
        # Matrix clients may render a structured mention as "Agent_one: !debug"
        # instead of embedding the full ID in the plain-text body. Match the
        # localpart and, when the display name differs from it (e.g. "Agent_three"
        # vs "agent_three-agent"), the display name the client rendered the mention as.
        localpart = user_id.split(":", 1)[0].removeprefix("@")
        alternatives = [re.escape(user_id), "@?" + re.escape(localpart)]
        if display_name:
            alternatives.append(re.escape(display_name))
        # Element can serialize a structured pill as a literal Matrix-to
        # Markdown link in the plain body. Accept only a link whose label and
        # destination are this exact mentioned account; never a general URL.
        target = re.escape(user_id)
        alternatives.append(rf"(?-i:\[{target}\]\(https://matrix\.to/#/{target}\))")
        prefix = "(?:" + "|".join(alternatives) + ")"
    else:
        prefix = re.escape(user_id)
    match = re.fullmatch(rf"\s*{prefix}\s*[:,]?\s*({_COMMAND_TOKENS})\s*", body,
                         re.IGNORECASE)
    if match is None:
        return None
    return BRIDGE_COMMANDS[match.group(1).lower()]


def is_debug_command(content: dict[str, Any], user_id: str,
                     display_name: str | None = None) -> bool:
    return bridge_command(content, user_id, display_name) == "debug"






def agent_runtime_notice(event: dict[str, Any], room_id: str,
                         settings: MatrixSettings) -> bool:
    """Accept only room-scoped CI notices addressed to this agent."""
    if room_id not in settings.hookshot_notice_rooms:
        return False
    if not settings.hookshot_user_id or event.get("sender") != settings.hookshot_user_id:
        return False
    content = event.get("content", {})
    if content.get("msgtype") != "m.notice":
        return False
    webhook = content.get("uk.half-shot.hookshot.webhook_data")
    return (isinstance(webhook, dict) and
            webhook.get("username") == "Agent runtime CI" and
            addressed_to(event, settings.user_id))

def targeted_pr_review(event: dict[str, Any], room_id: str,
                       settings: MatrixSettings) -> bool:
    """Require Hookshot PR requests to name this agent in structured mentions."""
    if room_id not in settings.pr_review_rooms:
        return False
    if not settings.hookshot_user_id or event.get("sender") != settings.hookshot_user_id:
        return False
    content = event.get("content", {})
    mentions = content.get("m.mentions", {}) if isinstance(content, dict) else {}
    user_ids = mentions.get("user_ids") if isinstance(mentions, dict) else None
    return (isinstance(content, dict) and content.get("msgtype") == "m.text" and
            isinstance(user_ids, list) and settings.user_id in user_ids)



def event_timestamp(event: dict[str, Any]) -> str:
    value = event.get("origin_server_ts")
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000, timezone.utc).isoformat(timespec="seconds")
    return "timestamp unknown"


def _context_event(event: dict[str, Any]) -> bool:
    if event.get("type") != "m.room.message":
        return False
    content = event.get("content", {})
    if not isinstance(content, dict) or content.get("msgtype") not in {"m.text", "m.notice", "m.image"}:
        return False
    relation = content.get("m.relates_to", {})
    if isinstance(relation, dict) and relation.get("rel_type") == "m.replace":
        return False
    meta = content.get(BRIDGE_META)
    if isinstance(meta, dict) and isinstance(meta.get("kind"), str) and meta.get("kind") in SILENT_KINDS:
        return False
    return isinstance(content.get("body"), str) and bool(content["body"].strip())


def _in_scope(event: dict[str, Any], thread_root: str | None) -> bool:
    if thread_root is not None and event.get("event_id") == thread_root:
        return True
    relation = event.get("content", {}).get("m.relates_to", {})
    if not isinstance(relation, dict) or relation.get("rel_type") != "m.thread":
        return thread_root is None
    return thread_root is not None and relation.get("event_id") == thread_root


def format_context(events: list[dict[str, Any]], current_id: str,
                   *, last_id: str | None = None,
                   summary_saved_ms: int | None = None) -> str:
    """Bounded, timestamped context preceding the triggering Matrix event."""
    current_index = next((i for i, event in enumerate(events)
                          if event.get("event_id") == current_id), len(events))
    prior = events[:current_index]
    marker_index = next((i for i, event in enumerate(prior)
                         if event.get("event_id") == last_id), None) if last_id else None
    unseen = prior[marker_index + 1:] if marker_index is not None else prior
    relevant = [event for event in unseen if _context_event(event)]
    lines: list[str] = []
    size = 0
    for event in reversed(relevant):
        content = event["content"]
        label = "[image] " if content.get("msgtype") == "m.image" else ""
        line = (f"[{event_timestamp(event)}] {event.get('sender', 'unknown')} "
                f"[{event.get('event_id', '?')}]: {label}{content['body'][:1800]}")
        if len(lines) >= 30 or size + len(line) + 1 > 23_000:
            break
        lines.append(line)
        size += len(line) + 1
    lines.reverse()
    header: list[str] = []
    if summary_saved_ms:
        header.append(f"[previous private conversation summary saved {event_timestamp({'origin_server_ts': summary_saved_ms})}]")
    if marker_index is not None:
        omitted = len(relevant) - len(lines)
        if omitted:
            header.append(f"[skipping {omitted} messages]")
    elif last_id is not None:
        header.append("[earlier unseen message count unknown; read more history if needed]")
    elif len(relevant) > len(lines):
        header.append("[earlier message count unknown; read more history if needed]")
    return "\n".join(header + lines)[:24_000]


_PRESENCE = {
    Presence.AWAKE: "online",
    Presence.CONNECTED: "unavailable",
    Presence.DISCONNECTED: "offline",
}


class MatrixAdapter:
    def __init__(self, agent: str, settings: MatrixSettings,
                 runtime: BridgeRuntime, state_dir: Path,
                 review_queue: ReviewQueue | None = None,
                 room_leases: RoomLeases | None = None) -> None:
        self.agent = agent
        self.settings = settings
        self.runtime = runtime
        telemetry = getattr(runtime, "telemetry", None)
        self.telemetry = telemetry if isinstance(telemetry, Telemetry) else Telemetry()
        self._input_locks: dict[str, asyncio.Lock] = {}
        self.state_dir = state_dir
        self.review_queue = review_queue
        self.room_leases = room_leases
        self.token = os.environ[settings.token_env]
        self.session: Any = None
        self._presence_supported = True
        self._acks: dict[tuple[SessionKey, str], str] = {}
        self._summary_cards: dict[str, str] = {}
        self._progress_headers: dict[tuple[SessionKey, str], str] = {}
        self._requesters: dict[tuple[SessionKey, str], str] = {}
        self._scope_hops: dict[SessionKey, int] = {}
        self._rosters: dict[str, tuple[float, dict[str, dict[str, Any]]]] = {}
        self._seen_file = state_dir / "matrix-seen.json"
        self._seen: dict[str, None] = {}
        if self._seen_file.exists():
            try:
                seen = json.loads(self._seen_file.read_text())
                if not isinstance(seen, list):
                    raise ValueError("Matrix seen-event journal is not a list")
                for event_id in seen[-10_000:]:
                    if isinstance(event_id, str):
                        self._seen[event_id] = None
            except (OSError, UnicodeError, ValueError):
                # The sync cursor is stored separately. A damaged journal must
                # not prevent the bridge from reconnecting to Matrix.
                log.exception("Ignoring damaged Matrix seen-event journal: %s",
                              self._seen_file)
        self._stopping = asyncio.Event()
        self._since_file = state_dir / "matrix-since.txt"
        self._startup_sent = False
        self._startup_transaction = f"bridge-startup-{os.urandom(8).hex()}"

    def _url(self, path: str) -> str:
        return self.settings.homeserver.rstrip("/") + path

    async def publish_activity(self, activity: Activity) -> bool:
        if self.session is None or not getattr(self, "_presence_supported", True):
            return False
        user = urllib.parse.quote(self.settings.user_id, safe="")
        try:
            await self._request("PUT", f"/_matrix/client/v3/presence/{user}/status",
                                data={"presence": _PRESENCE[activity.presence],
                                      "status_msg": activity.text})
        except RuntimeError as error:
            if any(f"HTTP {code}" in str(error) for code in (403, 404, 405)):
                self._presence_supported = False
                log.warning("Matrix presence endpoint is unavailable for %s", self.agent)
                return False
            raise
        return True

    async def _request(self, method: str, path: str, *, data: dict | None = None,
                       params: dict | None = None, timeout: int = 40,
                       attempts: int = MATRIX_MAX_ATTEMPTS,
                       budget: float | None = None,
                       stop: Callable[[], bool] | None = None) -> Any:
        """Send one request, retrying transient failures.

        ``attempts`` raises the attempt ceiling for the acknowledgement PUT.
        ``budget`` bounds how long that deeper ceiling may spend before the
        caller (holding the scope lock an emergency stop needs) gets control
        back: it confines the extra attempts to a 429 that identifies itself as a
        limiter breach, and caps every attempt and every wait, so it is a
        wall-clock ceiling, not a sleep allowance.
        ``stop`` reports a cancel or emergency stop already queued on that lock,
        which abandons the retries rather than making the stop wait them out; the
        caller scopes the predicate to the requester authorized to stop it.
        """
        assert self.session is not None
        import aiohttp
        # Retry only methods whose identical URL/body can be replayed safely.
        # Matrix event PUT URLs already contain a transaction ID; never generate
        # another ID here or replay a possibly accepted POST.
        replay_safe = method.upper() in {"GET", "HEAD", "PUT"}
        deadline = None if budget is None else _now() + budget
        payload: Any = None
        for attempt in range(1, attempts + 1):
            remaining = None if deadline is None else deadline - _now()
            if remaining is not None and remaining <= 0:
                log.warning("Matrix %s %s: HTTP 429 retry budget of %.0fs spent after "
                            "%d attempt(s); %s",
                            method, path, budget, attempt - 1, _describe_429(payload))
                raise RuntimeError(f"Matrix {method} failed with HTTP 429")
            if stop is not None and stop():
                log.warning("Matrix %s %s: abandoning attempt %d/%d before sending because a "
                            "cancel or stop is queued on this scope; %s",
                            method, path, attempt, attempts, _describe_429(payload))
                raise StopRequested(f"Matrix {method} abandoned for a queued scope stop")
            async with self.session.request(
                    method, self._url(path), json=data, params=params,
                    timeout=aiohttp.ClientTimeout(
                        total=timeout if remaining is None else min(timeout, remaining))) as response:
                if response.status < 400:
                    return await response.json()
                transient = response.status == 429 or 500 <= response.status < 600
                if budget is not None and response.status == 429:
                    # The 429 body is what says whether this is a limiter breach,
                    # so it is read before the ceiling is chosen, not after.
                    payload = await _json_or_none(response)
                    body_read = True
                else:
                    # Every other response has its body read only if it is going to
                    # be retried or logged.
                    body_read = False
                # A budgeted request spends its deeper attempt budget on a
                # validated limiter signal only: a bare 429 (the front door
                # answering while the backend is down) and a 5xx backend that
                # never answers both keep the default ceiling, so neither holds
                # the scope longer than ordinary traffic does.
                ceiling = attempts
                if budget is not None and not (response.status == 429
                                               and _is_limiter_429(payload, response.headers)):
                    ceiling = min(attempts, MATRIX_MAX_ATTEMPTS)
                if not replay_safe or not transient or attempt >= ceiling:
                    if response.status == 429 and not body_read:
                        payload = await _json_or_none(response)
                    if response.status == 429:
                        log.warning("Matrix %s %s: HTTP 429 after %d attempt(s); %s",
                                    method, path, attempt, _describe_429(payload))
                    raise RuntimeError(f"Matrix {method} failed with HTTP {response.status}")
                if not body_read:
                    payload = await _json_or_none(response)
                delay = _retry_delay(payload, response.headers, attempt)
                if deadline is not None:
                    remaining = deadline - _now()
                    if remaining <= 0:
                        log.warning("Matrix %s %s: HTTP 429 retry budget of %.0fs spent after "
                                    "%d attempt(s); %s",
                                    method, path, budget, attempt, _describe_429(payload))
                        raise RuntimeError(f"Matrix {method} failed with HTTP {response.status}")
                    delay = min(delay, remaining)
                if response.status == 429:
                    log.warning("Matrix %s %s: HTTP 429 on attempt %d/%d, retrying in %.1fs; %s",
                                method, path, attempt, ceiling, delay,
                                _describe_429(payload))
            # Release the response before waiting so retries do not hold a
            # connector slot while other replies or sync requests need it.
            if not await self._backoff(delay, stop):
                log.warning("Matrix %s %s: abandoning attempt %d/%d while waiting to retry "
                            "because a cancel or stop is queued on this scope; %s",
                            method, path, attempt, attempts, _describe_429(payload))
                raise StopRequested(f"Matrix {method} abandoned for a queued scope stop")
        raise RuntimeError(f"Matrix {method} failed with HTTP 429")

    async def _backoff(self, delay: float, stop: Callable[[], bool] | None) -> bool:
        """Wait out a retry delay; False once a queued stop asks to abandon it.

        Only a budgeted acknowledgement passes ``stop``. It is the one request
        that holds the scope lock across its retries, so its wait is the one an
        emergency stop cannot afford to sit out. Every other retry keeps a
        single unsliced sleep: nothing is waiting on those.
        """
        if stop is None:
            await asyncio.sleep(delay)
            return True
        # The total wait is unchanged; it is taken in slices a queued stop is
        # noticed between. Every slice is slept in full: the wait is measured by
        # the slices taken, not by a clock reading, so a coarse clock can neither
        # make the loop spin nor end the wait early.
        slices = max(1, math.ceil(delay / MATRIX_STOP_POLL_SECONDS))
        for _ in range(slices):
            if stop():
                return False
            await asyncio.sleep(delay / slices)
        return True

    def _write_since(self, token: str) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.state_dir.chmod(0o700)
        descriptor, name = tempfile.mkstemp(prefix=".since-", dir=self.state_dir)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w") as output:
                output.write(token + "\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(name, self._since_file)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def _remember(self, event_id: str) -> None:
        self._seen[event_id] = None
        while len(self._seen) > 10_000:
            self._seen.pop(next(iter(self._seen)))
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, name = tempfile.mkstemp(prefix=".seen-", dir=self.state_dir)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w") as output:
                json.dump(list(self._seen), output)
                output.flush()
                os.fsync(output.fileno())
            os.replace(name, self._seen_file)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    async def _history(self, room_id: str, current_id: str,
                       thread_root: str | None = None, *,
                       last_id: str | None = None,
                       summary_saved_ms: int | None = None) -> str:
        room = urllib.parse.quote(room_id, safe="")
        try:
            if thread_root:
                root = urllib.parse.quote(thread_root, safe="")
                path = f"/_matrix/client/v1/rooms/{room}/relations/{root}/m.thread"
            else:
                path = f"/_matrix/client/v3/rooms/{room}/messages"
            data = await self._request("GET", path,
                                       params={"dir": "b", "limit": "100"})
            events = list(reversed(data.get("chunk", [])))
            if not any(event.get("event_id") == current_id for event in events):
                # A busy room can move beyond the current sync event. Matrix's
                # context endpoint anchors the bounded read at that event.
                anchor = urllib.parse.quote(current_id, safe="")
                context = await self._request(
                    "GET", f"/_matrix/client/v3/rooms/{room}/context/{anchor}",
                    params={"limit": "100"})
                events = list(reversed(context.get("events_before", [])))
            events = [event for event in events if _in_scope(event, thread_root)]
            return format_context(events, current_id, last_id=last_id,
                                  summary_saved_ms=summary_saved_ms)
        except Exception:
            log.exception("Could not read Matrix context for a new request")
            return ""

    async def history_page(self, key: SessionKey, before: str | None,
                           limit: int) -> HistoryPage:
        """Read older messages in the caller's configured room or thread."""
        if key.transport != "matrix" or key.agent != self.agent:
            raise ValueError("History scope does not belong to this Matrix adapter")
        thread_root = key.scope.split(":thread:", 1)[1] if ":thread:" in key.scope else None
        room_id = key.scope.split(":thread:", 1)[0].removesuffix(":main")
        if room_id not in self.settings.rooms:
            raise ValueError("Room is not configured for this bridge")
        count = min(max(int(limit), 1), 100)
        room = urllib.parse.quote(room_id, safe="")
        if thread_root:
            root = urllib.parse.quote(thread_root, safe="")
            path = f"/_matrix/client/v1/rooms/{room}/relations/{root}/m.thread"
        else:
            path = f"/_matrix/client/v3/rooms/{room}/messages"
        params = {"dir": "b", "limit": str(count)}
        if before:
            params["from"] = before
        data = await self._request("GET", path, params=params)
        messages: list[HistoryMessage] = []
        for event in data.get("chunk", []):
            if _context_event(event) and _in_scope(event, thread_root):
                content = event["content"]
                messages.append({"id": event.get("event_id", ""),
                                 "sender": event.get("sender", "unknown"),
                                 "timestamp": event_timestamp(event),
                                 "content": ("[image] " if content.get("msgtype") == "m.image" else "")
                                            + content["body"]})
        token = data.get("end")
        return {"messages": messages, "next_before": token if isinstance(token, str) else None,
                "has_more": isinstance(token, str) and bool(token)}

    async def react(self, key: SessionKey, message_id: str, emoji: str) -> None:
        await self._add_reaction(key, message_id, emoji)
        await self.runtime.record_activity(key)

    async def _add_reaction(self, key: SessionKey, message_id: str, emoji: str,
                            *, namespace: str = "") -> dict[str, Any]:
        """React only to an ordinary event in the active configured scope."""
        if key.transport != "matrix" or key.agent != self.agent or not message_id.startswith("$"):
            raise ReactionRejected("matrix_target")
        thread_root = key.scope.split(":thread:", 1)[1] if ":thread:" in key.scope else None
        room_id = key.scope.split(":thread:", 1)[0].removesuffix(":main")
        if room_id not in self.settings.rooms:
            raise ReactionRejected("matrix_room")
        room = urllib.parse.quote(room_id, safe="")
        event = urllib.parse.quote(message_id, safe="")
        target = await self._request("GET", f"/_matrix/client/v3/rooms/{room}/event/{event}")
        if (not isinstance(target, dict) or target.get("event_id") != message_id or
                target.get("room_id", room_id) != room_id or
                not _context_event(target) or not _in_scope(target, thread_root)):
            raise ReactionRejected("matrix_scope")
        transaction = hashlib.sha256(
            (self.agent + "\0" + room_id + "\0" + message_id + "\0" + emoji
             + ("\0" + namespace if namespace else "")).encode()
        ).hexdigest()[:32]
        return await self._request(
            "PUT", f"/_matrix/client/v3/rooms/{room}/send/m.reaction/bridge-{transaction}",
            data={"m.relates_to": {"rel_type": "m.annotation",
                                   "event_id": message_id, "key": emoji}})

    async def turn_status(self, record: Indicator, status: TurnStatus) -> str | None:
        """Apply a durable intent without touching Pi reaction/activity accounting."""
        key, request_id = record.key, record.request_id
        room_id = key.scope.split(":thread:", 1)[0].removesuffix(":main")
        if (key.agent != self.agent or key.transport != "matrix" or
                room_id not in self.settings.rooms):
            raise ReactionRejected("matrix_room")
        namespace = f"turn-status-{record.epoch}"
        running_id = record.running_id
        if status == "running" or (running_id is None and record.pending_attempted):
            # Same phase transaction recovers a lost acknowledgement after a
            # kill between the server accepting the reaction and local fsync.
            data = await self._add_reaction(key, request_id, TURN_EMOJI["running"], namespace=namespace)
            running_id = data["event_id"]
            if not isinstance(running_id, str) or not running_id.startswith("$"):
                raise ValueError("invalid turn reaction receipt")
            record.running_id = running_id
        if status == "running":
            return running_id
        if status != "clear":
            terminal = await self._add_reaction(key, request_id, TURN_EMOJI[status], namespace=namespace)
            if not isinstance(terminal.get("event_id"), str) or not terminal["event_id"].startswith("$"):
                raise ValueError("invalid terminal reaction receipt")
        if running_id is not None:
            room = urllib.parse.quote(room_id, safe="")
            event = urllib.parse.quote(running_id, safe="")
            transaction = hashlib.sha256(running_id.encode()).hexdigest()[:32]
            await self._request(
                "PUT", f"/_matrix/client/v3/rooms/{room}/redact/{event}/turn-{transaction}",
                data={"reason": "Bridge turn ended"})
        return None

    async def _image(self, content: dict[str, Any]) -> RpcImage | None:
        url = content.get("url")
        if not isinstance(url, str) or not url.startswith("mxc://"):
            return None
        server_and_id = url[6:].split("/", 1)
        if len(server_and_id) != 2:
            return None
        server, media_id = (urllib.parse.quote(part, safe="") for part in server_and_id)
        path = f"/_matrix/client/v1/media/download/{server}/{media_id}"
        assert self.session is not None
        import aiohttp
        async with self.session.get(self._url(path), timeout=aiohttp.ClientTimeout(total=30)) as response:
            if response.status != 200:
                return None
            mime = response.headers.get("Content-Type", "image/jpeg").split(";", 1)[0]
            if not mime.startswith("image/"):
                return None
            data = await response.content.read(20_000_001)
            return RpcImage(mime, data) if len(data) <= 20_000_000 else None

    async def _reply_context(self, room_id: str, event: dict[str, Any]) -> dict[str, str] | None:
        relation = event.get("content", {}).get("m.relates_to", {})
        target = relation.get("m.in_reply_to", {}) if isinstance(relation, dict) else {}
        event_id = target.get("event_id") if isinstance(target, dict) else None
        if not isinstance(event_id, str) or not event_id.startswith("$"):
            return None
        room = urllib.parse.quote(room_id, safe="")
        quoted = urllib.parse.quote(event_id, safe="")
        import aiohttp
        try:
            original = await self._request("GET", f"/_matrix/client/v3/rooms/{room}/event/{quoted}")
        except (aiohttp.ClientError, json.JSONDecodeError, OSError, RuntimeError, TimeoutError):
            return {"event_id": event_id}  # Reference known; text unavailable.
        if (not isinstance(original, dict) or original.get("event_id") != event_id or
                original.get("room_id", room_id) != room_id or
                original.get("type") != "m.room.message"):
            return {"event_id": event_id}
        content = original.get("content", {})
        if (not isinstance(content, dict) or content.get("msgtype") not in {"m.text", "m.image"} or
                not isinstance(content.get("body"), str) or
                not isinstance(original.get("sender"), str)):
            return {"event_id": event_id}
        body = content["body"]
        # Matrix's plaintext reply fallback already quotes an earlier post.
        # Strip it rather than quoting a quote (or walking the reply chain).
        nested = content.get("m.relates_to", {})
        if (isinstance(nested, dict) and "m.in_reply_to" in nested and
                body.startswith("> ") and "\n\n" in body):
            body = body.split("\n\n", 1)[1]
        return {"event_id": event_id, "sender": original["sender"],
                "body": body[:1600]}

    async def _forward_interactive(self, room_id: str, event: dict[str, Any]) -> bool:
        if (self.room_leases is None or not self.room_leases.active(room_id) or
                event["content"].get("msgtype") not in {"m.text", "m.image"}):
            return False
        attachment = None
        if event["content"].get("msgtype") == "m.image":
            import aiohttp
            try:
                image = await self._image(event["content"])
                attachment = image.to_json() if image is not None else None
            except (aiohttp.ClientError, json.JSONDecodeError, OSError, RuntimeError, TimeoutError):
                log.warning("Matrix image unavailable for interactive handoff event=%s",
                            event.get("event_id"))
        reply_context = await self._reply_context(room_id, event)
        ping = explicit_ping(event["content"], self.settings.user_id)
        return await self.room_leases.forward(room_id, event, attachment, reply_context, ping)

    async def _handle_event(self, room_id: str, event: dict[str, Any]) -> None:
        scope = matrix_scope(room_id, event)
        async with self.telemetry.queued(self._input_locks.setdefault(scope, asyncio.Lock()), "matrix",
                                        SessionKey(self.agent, "matrix", scope).scope_hash):
            await self._deliver_event(room_id, event)

    async def _deliver_event(self, room_id: str, event: dict[str, Any]) -> None:
        if (event.get("type") != "m.room.message" or
                event.get("sender") == self.settings.user_id):
            return
        event_id = event.get("event_id")
        if not isinstance(event_id, str) or event_id in self._seen:
            return
        content = event.get("content", {})
        if not isinstance(content, dict):
            return
        relation = content.get("m.relates_to")
        meta = content.get(BRIDGE_META)
        if isinstance(meta, dict) and isinstance(meta.get("kind"), str) and meta.get("kind") in SILENT_KINDS:
            # Working indicators are not new user turns. Terminal replies are separate events.
            self._remember(event_id)
            self.telemetry.record("matrix", "filter", "machine_status")
            return
        if isinstance(relation, dict) and relation.get("rel_type") == "m.replace":
            # Interactive Pi coalesces edits while attention is held; without a
            # lease, an edit must never wake bridge Pi as a second user turn.
            await self._forward_interactive(room_id, event)
            self._remember(event_id)
            return
        if (room_id in self.settings.pr_review_rooms and
                event.get("sender") == self.settings.hookshot_user_id):
            if targeted_pr_review(event, room_id, self.settings):
                if self.review_queue is None:
                    raise RuntimeError("PR review room has no durable review queue")
                await self.review_queue.enqueue(event_id, str(content.get("body", "")), room_id)
            self._remember(event_id)
            return
        if (content.get("msgtype") not in {"m.text", "m.image"} and
                not agent_runtime_notice(event, room_id, self.settings)):
            return
        key = SessionKey(self.agent, "matrix", matrix_scope(room_id, event))
        if await self._forward_interactive(room_id, event):
            self._remember(event_id)
            return
        active = await self.runtime.is_awake(key)
        # A lease may have been granted during the awaited resident state check.
        if await self._forward_interactive(room_id, event):
            self._remember(event_id)
            return
        if isinstance(meta, dict):
            hop = meta.get("hop")
            invitations = meta.get("invitations", [])
            legacy = hop is None and meta.get("kind") == "final" and isinstance(invitations, list) and self.settings.user_id in invitations
            if hop is None:
                hop = MAX_HANDOFF_HOPS if legacy else 0
            intentional = (meta.get("kind") == "final" and type(hop) is int and hop > 0
                           and isinstance(invitations, list) and self.settings.user_id in invitations)
            reason = metadata_rejection({**meta, "hop": hop}, awake=active,
                                        addressed=intentional, wake_any=room_id in self.settings.waking_rooms)
            if reason:
                self.telemetry.record("matrix", "filter", reason)
                self._remember(event_id)
                return
        addressed = addressed_to(event, self.settings.user_id,
                                 (self.settings.display_name,) if self.settings.display_name else ())
        mentions = content.get("m.mentions", {})
        user_ids = mentions.get("user_ids") if isinstance(mentions, dict) else None
        if (isinstance(user_ids, list) and user_ids and
                self.settings.user_id not in user_ids and
                not addressed and
                not active and room_id not in self.settings.waking_rooms):
            self._remember(event_id)
            return
        text = str(content.get("body", ""))
        command = (bridge_command(content, self.settings.user_id,
                                   self.settings.display_name)
                   if not isinstance(meta, dict) else None)
        # Debug and stop run before admission: they never touch Pi and cannot
        # wake a sleeping session. Cancel and recover wait for admission below,
        # matching the long-standing bare-token behaviour in waking rooms.
        if command in {"debug", "stop"}:
            sender = str(event.get("sender", ""))
            self._requesters[(key, event_id)] = sender
            try:
                if command == "debug":
                    reply = await debug_status(self.runtime, key)
                else:
                    stopped = await self.runtime.stop_session(key, sender)
                    reply = ("Stopped this conversation's Pi session. Unsaved context was "
                             "discarded; the next wake uses the last saved summary and recent posts."
                             if stopped else "No running session belonging to you here.")
                await self.output(key, "debug", reply, event_id)
            finally:
                self._requesters.pop((key, event_id), None)
                self._remember(event_id)
            return
        can_wake = addressed or room_id in self.settings.waking_rooms
        async def compaction_notice() -> None:
            await self.output(key, "compacting", "Compacting, one moment...", event_id)

        if not await self.runtime.admit(key, can_wake, on_wait=compaction_notice):
            if await self._forward_interactive(room_id, event):
                self._remember(event_id)
                return
            self._remember(event_id)
            return
        # Recheck after admission: the idle sweep may have closed Pi between
        # the initial routing check and this lock-protected admission.
        awake_after_admission = await self.runtime.is_awake(key)
        if isinstance(meta, dict):
            hop_value: int = hop if type(hop) is int else 0
            self._scope_hops[key] = (max(self._scope_hops.get(key, 0), hop_value)
                                     if awake_after_admission else hop_value)
        elif not awake_after_admission:
            self._scope_hops[key] = 0
        text = str(content.get("body", ""))
        sender = str(event.get("sender", ""))
        if command is None and not isinstance(meta, dict):
            # A bare session token with the address rendered only as the full
            # ID elsewhere in the body (or with no mention at all) still
            # reaches the command path after admission.
            command = BRIDGE_COMMANDS.get(
                text.replace(self.settings.user_id, "").strip().lower())
        if command == "cancel":
            self._requesters[(key, event_id)] = sender
            cancelled = await self.runtime.cancel(key, sender)
            await self.output(key, "ack" if cancelled else "error",
                              "Cancellation requested." if cancelled else
                              "No active request from you here.", event_id)
            self._remember(event_id)
            return
        if command == "recover":
            self._requesters[(key, event_id)] = sender
            recovered = await self.runtime.recover(key, sender)
            await self.output(key, "final" if recovered else "error",
                              "Recovered. The next reply starts from the last saved summary; recent unsaved context may be missing."
                              if recovered else "No failed checkpoint belonging to you here.",
                              event_id)
            self._remember(event_id)
            return
        if not awake_after_admission:
            thread_root = (key.scope.split(":thread:", 1)[1]
                           if ":thread:" in key.scope else None)
            last_id, summary_saved_ms = await self.runtime.last_wake(key)
            context = await self._history(room_id, event_id, thread_root,
                                          last_id=last_id,
                                          summary_saved_ms=summary_saved_ms)
            if context:
                text = f"Recent room posts (read before replying):\n{context}\n\nCurrent request:\n{text}"
        images: tuple[RpcImage, ...] = ()
        if content.get("msgtype") == "m.image":
            import aiohttp
            try:
                image = await self._image(content)
                images = (image,) if image else ()
            except (aiohttp.ClientError, json.JSONDecodeError, OSError, RuntimeError, TimeoutError):
                log.warning("Matrix image unavailable event=%s", event_id)
        self._requesters[(key, event_id)] = sender
        try:
            incoming = Incoming(sender, event_id, text, images,
                                optional=not addressed,
                                # Peer bridge traffic expects no reaction.
                                ack_required=not isinstance(meta, dict))
            if self.room_leases is not None and content.get("msgtype") in {"m.text", "m.image"}:
                # Serialize final bridge admission with lease acquisition. A handler
                # may have read history while the interactive Pi claimed attention.
                async with self.room_leases.lock:
                    if self.room_leases.active(room_id):
                        attachment = images[0].to_json() if images else None
                        reply_context = await self._reply_context(room_id, event)
                        ping = explicit_ping(content, self.settings.user_id)
                        if await self.room_leases.forward_locked(room_id, event, attachment, reply_context, ping):
                            self._requesters.pop((key, event_id), None)
                            return
                    await self.runtime.submit(key, incoming)
                    await self.runtime.record_wake(key, event_id)
            else:
                await self.runtime.submit(key, incoming)
                await self.runtime.record_wake(key, event_id)
        except StopRequested:
            # The acknowledgement dropped its retries because a cancel or stop is
            # queued on the scope lock, so this request never started. The stop is
            # the operator's own outcome: there is nothing here to recover from,
            # and no reply that could still need the requester.
            log.info("Matrix request abandoned for a queued scope stop event=%s", event_id)
            self._requesters.pop((key, event_id), None)
        except Exception:
            log.exception("Matrix request could not be submitted event=%s", event_id)
            try:
                await self.output(key, "error",
                                  "I could not start this request. If the checkpoint failed, mention me with `recover` to restart from the last saved summary.",
                                  event_id)
            except Exception:
                log.exception("Matrix request error could not be delivered event=%s", event_id)
                self._requesters.pop((key, event_id), None)
        finally:
            self._remember(event_id)


    async def _room_members(self, room_id: str) -> dict[str, dict[str, Any]] | None:
        """Joined members for this room, or ``None`` when the roster is unavailable."""
        now = time.monotonic()
        cached = self._rosters.get(room_id)
        if cached is not None and cached[0] > now:
            self.telemetry.record("matrix", "roster", "cached")
            return cached[1]
        room = urllib.parse.quote(room_id, safe="")
        try:
            data = await self._request("GET", f"/_matrix/client/v3/rooms/{room}/joined_members")
        except Exception as error:
            self.telemetry.record("matrix", "roster", "unavailable")
            log.warning("Could not read Matrix room members error_class=%s", type(error).__name__)
            return None
        joined = data.get("joined") if isinstance(data, dict) else None
        if not isinstance(joined, dict):
            return None
        members = {user_id: entry for user_id, entry in joined.items()
                   if isinstance(user_id, str) and isinstance(entry, dict)}
        self._rosters[room_id] = (now + ROSTER_TTL_SECONDS, members)
        self.telemetry.record("matrix", "roster", "fetched")
        return members

    async def member_roster(self, key: SessionKey) -> Roster:
        room_id = key.scope.split(":thread:", 1)[0].removesuffix(":main")
        if key.transport != "matrix" or room_id not in self.settings.rooms:
            raise ValueError("Matrix room is not enabled")
        members = await self._room_members(room_id)
        return Roster((Member(user_id, user_id[1:].split(":", 1)[0],
                              tuple(dict.fromkeys(alias for alias in (
                                  user_id[1:].split(":", 1)[0], entry.get("display_name"))
                                  if isinstance(alias, str) and alias)))
                       for user_id, entry in (members or {}).items()),
                      available=members is not None, complete=members is not None)

    async def _render_mentions(self, room_id: str, text: str) -> tuple[str, list[str], bool]:
        if "@" not in text:
            return text, [], False
        roster = await self.member_roster(SessionKey(self.agent, "matrix", room_id + ":main"))
        plan = roster.plan(text, allow_broadcast=room_id in self.settings.broadcast_allowed_room_ids)
        if plan.unknown and room_id in self._rosters:
            # A cached absence may simply be a recently joined or renamed member.
            self._rosters.pop(room_id, None)
            roster = await self.member_roster(SessionKey(self.agent, "matrix", room_id + ":main"))
            plan = roster.plan(text, allow_broadcast=room_id in self.settings.broadcast_allowed_room_ids)
        feedback = roster.feedback(plan)
        if feedback:
            self.telemetry.record("matrix", "mention", "rejected")
            return feedback, [], True
        return replace_mentions(text, plan, lambda _mention, target: target), plan.user_ids, False

    async def _seal_progress(self, key: SessionKey, request_id: str, outcome: str) -> None:
        event_id = self._acks.pop((key, request_id), None)
        header = self._progress_headers.pop((key, request_id), "")
        if event_id is None:
            return
        room_id = key.scope.split(":thread:", 1)[0].removesuffix(":main")
        room = urllib.parse.quote(room_id, safe="")
        event = urllib.parse.quote(event_id, safe="")
        transaction = f"bridge-retire-{os.urandom(8).hex()}"
        history = self._summary_cards.pop(event_id, "")
        if outcome == "continued":
            return  # Handoff/rotation retires tracking, never the visible snapshot.
        try:
            if history:
                body = {"msgtype": "m.text", "body": sealed_progress(header, history, outcome),
                        BRIDGE_META: {"kind": "progress_summary", "sealed": True},
                        "m.mentions": {"user_ids": [], "room": False}}
                await asyncio.wait_for(self._request(
                    "PUT", f"/_matrix/client/v3/rooms/{room}/send/m.room.message/{transaction}",
                    data={**body, "m.new_content": body,
                          "m.relates_to": {"rel_type": "m.replace", "event_id": event_id}}), 2)
            else:
                await asyncio.wait_for(self._request(
                    "PUT", f"/_matrix/client/v3/rooms/{room}/redact/{event}/{transaction}",
                    data={"reason": "Progress card retired"}), 2)
        except Exception:
            log.warning("Could not seal Matrix bridge progress event", exc_info=True)

    async def output(self, key: SessionKey, kind: str, text: str,
                     request_id: str) -> bool | None:
        if key.transport != "matrix":
            return None
        request_error = kind == "request_error"
        if request_error:
            kind = "error"
        scope_room_id = key.scope.split(":thread:", 1)[0].removesuffix(":main")
        broadcast_allowed = scope_room_id in self.settings.broadcast_allowed_room_ids
        if kind != "cleanup" and not broadcast_allowed and has_broadcast_ping(text):
            raise ReplyRejected(BROADCAST_REJECTION)
        if kind == "cleanup_request":
            self._requesters.pop((key, request_id), None)
            return None
        if kind == "cleanup":
            self._requesters.pop((key, request_id), None)
            for target in [target for target in self._acks if target[0] == key]:
                await self._seal_progress(*target, "finished")
            return None
        thread_root = key.scope.split(":thread:", 1)[1] if ":thread:" in key.scope else None
        room_id = key.scope.split(":thread:", 1)[0].removesuffix(":main")
        room = urllib.parse.quote(room_id, safe="")
        requester = self._requesters.get((key, request_id), "")
        prior_hops = self._scope_hops.get(key, 0)
        # Invitations are deliberate handoffs: leading mentions are resolved
        # first so accepted aliases widen the same chain as canonical IDs.
        mentioned: list[str] = []
        if kind == "final":
            text, mentioned, rejected = await self._render_mentions(room_id, text)
            if rejected:
                # Keep room membership validation private to the active Pi
                # session. The runtime asks for a rewrite before any room send.
                raise ReplyRejected(text)
        invitations = (leading_agent_mentions(text)
                       if kind == "final" and prior_hops < MAX_HANDOFF_HOPS else [])
        hop = min(prior_hops + 1, MAX_HANDOFF_HOPS) if kind == "final" else prior_hops
        history = text.partition("\n")[2]
        header = (self._progress_headers.get((key, request_id), "")
                  if kind in {"ack", "progress", "progress_summary"} else "")
        text = f"{header}\n{text}" if header else text
        content: dict[str, Any] = {"msgtype": "m.text", "body": text,
                                   BRIDGE_META: {"kind": kind,
                                                 "hop": hop,
                                                 "invitations": invitations}}
        notify = ([] if kind == "compacting" else list(dict.fromkeys(
            ([requester] if requester else []) + invitations + mentioned)))
        # Only an explicit alerts-room exemption may authorize a native @room
        # ping. Code examples, escaped words, and other platform tokens stay literal.
        room_ping = (kind == "final" and broadcast_allowed and any(
            mention.server is None and mention.localpart.casefold() == "room"
            for mention in find_mentions(text)))
        content["m.mentions"] = {"user_ids": notify, "room": room_ping}
        if kind in {"progress", "progress_summary"}:
            formatted = progress_html(text) if kind == "progress" else None
            if formatted is not None:
                content["format"] = "org.matrix.custom.html"
                content["formatted_body"] = formatted
            ack_id = self._acks.get((key, request_id))
            if ack_id:
                content["m.relates_to"] = {"rel_type": "m.replace", "event_id": ack_id}
                content["m.new_content"] = {"msgtype": "m.text", "body": text,
                                            BRIDGE_META: content[BRIDGE_META],
                                            "m.mentions": content["m.mentions"]}
                if formatted is not None:
                    content["m.new_content"]["format"] = "org.matrix.custom.html"
                    content["m.new_content"]["formatted_body"] = formatted
            else:
                return None
        else:
            relation: dict[str, Any] = {}
            if thread_root:
                relation.update({"rel_type": "m.thread", "event_id": thread_root,
                                 "is_falling_back": True})
            relation["m.in_reply_to"] = {"event_id": request_id}
            content["m.relates_to"] = relation
        transaction = urllib.parse.quote(f"bridge-{self.agent}-{os.urandom(8).hex()}")
        is_ack = kind == "ack"
        # The acknowledgement may outlast the default budget, but its whole
        # retry window is bounded because the scope lock is held across it, and
        # the runtime flags a queued cancel or emergency stop on that lock before
        # it waits for it -- this write gives way instead of delaying the stop.
        data = await self._request("PUT", f"/_matrix/client/v3/rooms/{room}/send/m.room.message/{transaction}",
                                   data=content,
                                   attempts=MATRIX_ACK_ATTEMPTS if is_ack else MATRIX_MAX_ATTEMPTS,
                                   budget=MATRIX_ACK_RETRY_BUDGET_SECONDS if is_ack else None,
                                   # Polled with this write's own requester: a stop
                                   # queued by anyone else was refused and must not
                                   # end the request it was never authorized to stop.
                                   stop=(lambda: self.runtime.stop_requested(key, requester or None))
                                   if is_ack else None)
        if kind == "ack":
            await self._seal_progress(key, request_id, "continued")
            self._acks[(key, request_id)] = data["event_id"]
            if header:
                self._progress_headers[(key, request_id)] = header
        if kind == "progress_summary":
            self._summary_cards[self._acks[(key, request_id)]] = history
        if kind == "steered":
            for target in [target for target in self._acks if target[0] == key]:
                await self._seal_progress(*target, "continued")
            self._acks[(key, request_id)] = data["event_id"]
            self._progress_headers[(key, request_id)] = text
            self._requesters.pop((key, request_id), None)
        if kind in {"final", "cancelled", "error"}:
            for target in [target for target in self._acks if target[0] == key and
                           (not request_error or target[1] == request_id)]:
                await self._seal_progress(*target, "finished" if kind == "final" else kind)
            self._requesters.pop((key, request_id), None)
        # Waiting is not new Pi activity. In particular it must not overwrite
        # the idle checkpoint clock while the sweep is still saving it.
        if kind != "compacting":
            await self.runtime.record_activity(key)
        return (kind == "final" and isinstance(data.get("event_id"), str)
                and data["event_id"].startswith("$"))

    async def _announce_startup(self) -> None:
        room_id = self.settings.startup_room_id
        if room_id is None or self._startup_sent:
            return
        room = urllib.parse.quote(room_id, safe="")
        await self._request(
            "PUT", f"/_matrix/client/v3/rooms/{room}/send/m.room.message/{self._startup_transaction}",
            data={"msgtype": "m.notice", BRIDGE_META: {"kind": "startup"},
                  "body": f"agent-bridge v{__version__} connected — {self.agent} ({self.settings.user_id})",
                  "m.mentions": {"user_ids": [], "room": False}})
        self._startup_sent = True

    async def run(self) -> None:
        import aiohttp  # Imported only when starting the adapter.
        self.session = aiohttp.ClientSession(headers={"Authorization": f"Bearer {self.token}"})
        since = self._since_file.read_text().strip() if self._since_file.exists() else ""
        first = not bool(since)
        try:
            while not self._stopping.is_set():
                try:
                    params = {"timeout": "20000", "filter": json.dumps({
                        "room": {"rooms": list(self.settings.rooms),
                                 "timeline": {"limit": 0 if first else 50,
                                              "types": ["m.room.message"]}}})}
                    if since:
                        params["since"] = since
                    data = await self._request("GET", "/_matrix/client/v3/sync",
                                               params=params, timeout=35)
                    next_batch = data["next_batch"]
                    if not first:
                        rooms = data.get("rooms", {}).get("join", {})
                        grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
                        for room_id in self.settings.rooms:
                            for event in rooms.get(room_id, {}).get("timeline", {}).get("events", []):
                                if isinstance(event, dict):
                                    grouped.setdefault((room_id, matrix_scope(room_id, event)), []).append(event)

                        async def deliver_group(room_id: str, events: list[dict[str, Any]]) -> None:
                            for event in events:
                                try:
                                    await self._handle_event(room_id, event)
                                except Exception:
                                    if targeted_pr_review(event, room_id, self.settings):
                                        raise
                                    log.warning("Skipping Matrix event error scope_hash=%s",
                                                SessionKey(self.agent, "matrix", matrix_scope(room_id, event)).scope_hash)
                                    event_id = event.get("event_id")
                                    if isinstance(event_id, str):
                                        self._remember(event_id)

                        results = await asyncio.gather(*(deliver_group(room_id, events)
                            for (room_id, _), events in grouped.items()), return_exceptions=True)
                        for result in results:
                            if isinstance(result, BaseException):
                                raise result
                    first = False
                    since = next_batch
                    self._write_since(since)
                    try:
                        await self._announce_startup()
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        log.exception("Matrix startup notice failed; retrying after next sync")
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("Matrix sync failed; retrying")
                    await asyncio.sleep(5)
        finally:
            await self.session.close()
            self.session = None

    async def stop(self) -> None:
        self._stopping.set()
