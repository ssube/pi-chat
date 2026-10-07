"""Resolve ``@`` mentions in an agent reply into real transport pings.

The bridge only rewrites a token it can resolve to a real account in the
current room or channel. An unresolved token is reported back to the caller
so the reply can be rejected instead of posting a name that reads like a ping
but notifies nobody.

This module is deliberately transport-neutral: the adapters supply the
lookup, the replacement, and the roster used for the rejection message.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

# A mention starts at a word boundary so an email address or an already
# rendered link does not match. The localpart charset mirrors the characters
# that appear in real Matrix and Discord handles.
_MENTION = re.compile(r"(?<![\w@./:-])@([A-Za-z0-9._=/-]+)(?::([A-Za-z0-9.-]+))?")
_TRAILING = "."
# A handle written inside code is an example, not an address. Fenced blocks
# are matched first so an inline span inside one is not counted twice.
_FENCED = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE = re.compile(r"`[^`\n]*`")

# A reply that pings more distinct people than this is almost certainly a
# mistake; the extra tokens are left alone rather than notified.
MAX_MENTIONS = 32


@dataclass(frozen=True)
class Mention:
    start: int
    end: int
    raw: str
    localpart: str
    server: str | None

    @property
    def user_id(self) -> str | None:
        """The full ID when the reply already spelled one out, else ``None``."""
        return f"@{self.localpart}:{self.server}" if self.server else None


def _code_spans(text: str) -> list[tuple[int, int]]:
    spans = [(match.start(), match.end()) for match in _FENCED.finditer(text)]
    spans.extend((match.start(), match.end()) for match in _INLINE_CODE.finditer(text)
                 if not any(start <= match.start() < end for start, end in spans))
    return spans


def find_mentions(text: str) -> list[Mention]:
    """Every candidate ``@handle`` token in a reply, in order."""
    found: list[Mention] = []
    code = _code_spans(text)
    for match in _MENTION.finditer(text):
        if any(start <= match.start() < end for start, end in code):
            continue
        localpart = match.group(1).rstrip(_TRAILING)
        if not localpart:
            continue
        server = match.group(2).rstrip(_TRAILING) if match.group(2) else None
        end = match.start() + 1 + len(localpart)
        if server:
            end += 1 + len(server)
        found.append(Mention(match.start(), end, text[match.start():end],
                             localpart, server))
        if len(found) >= MAX_MENTIONS:
            break
    return found


@dataclass(frozen=True)
class MentionPlan:
    """A reply split into resolvable and unknown mentions."""

    text: str
    known: tuple[tuple[Mention, str], ...]
    unknown: tuple[Mention, ...]

    @property
    def user_ids(self) -> list[str]:
        return list(dict.fromkeys(user_id for _, user_id in self.known))

    @property
    def unknown_raw(self) -> list[str]:
        return [mention.raw for mention in self.unknown]


def plan_mentions(text: str, lookup: Callable[[Mention], str | None]) -> MentionPlan:
    """Resolve every candidate mention through ``lookup``; never rewrite text."""
    known: list[tuple[Mention, str]] = []
    unknown: list[Mention] = []
    for mention in find_mentions(text):
        user_id = lookup(mention)
        if user_id:
            known.append((mention, user_id))
        else:
            unknown.append(mention)
    return MentionPlan(text, tuple(known), tuple(unknown))


def replace_mentions(text: str, plan: MentionPlan,
                     render: Callable[[Mention, str], str]) -> str:
    """Return ``text`` with each resolvable mention replaced by ``render``."""
    if not plan.known:
        return text
    pieces: list[str] = []
    cursor = 0
    for mention, user_id in sorted(plan.known, key=lambda item: item[0].start):
        pieces.append(text[cursor:mention.start])
        pieces.append(render(mention, user_id))
        cursor = mention.end
    pieces.append(text[cursor:])
    return "".join(pieces)


def roster_line(names: list[str], limit: int = 25) -> str:
    """A bounded, human-readable member list for a rejected mention."""
    unique = list(dict.fromkeys(name for name in names if name))
    shown = unique[:limit]
    rendered = ", ".join(shown)
    if len(unique) > limit:
        rendered += f", and {len(unique) - limit} more"
    return rendered
