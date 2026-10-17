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
from typing import Callable, Iterable

# A mention starts at a word boundary so an email address or an already
# rendered link does not match. The localpart charset mirrors the characters
# that appear in real Matrix and Discord handles.
_MENTION = re.compile(r"(?<![\w@./:-])@([\w.=/-]+)(?::((?:[A-Za-z0-9.-]+|\[[A-Fa-f0-9:]+\])(?::[0-9]+)?))?")
_NATIVE = re.compile(r"<@([!&]?[0-9]+)>")
_TRAILING = "."
# A handle written inside code is an example, not an address. Fenced blocks
# are matched first so an inline span inside one is not counted twice.
_FENCED = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE = re.compile(r"`[^`\n]*`")

# A reply that pings more distinct people than this is almost certainly a
# mistake; the extra tokens are left alone rather than notified.
MAX_MENTIONS = 32
BROADCAST_REJECTION = ("Broadcast pings are not allowed in this channel; "
                       "if you wanted to use the word, escape it or add a space.")
_BROADCAST = re.compile(r"(?<![\w@./:-])@(here|everyone|channel|room)(?![\w=/-]|\.[\w]|:[A-Za-z0-9])", re.IGNORECASE)


def _escaped(text: str, start: int) -> bool:
    before = text[:start]
    return (len(before) - len(before.rstrip("\\"))) % 2 == 1


def has_broadcast_ping(text: str) -> bool:
    """Unescaped standalone broadcast tokens, including those in code examples."""
    return any(not _escaped(text, match.start()) for match in _BROADCAST.finditer(text))


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


def find_mentions(text: str, aliases: Iterable[str] = ()) -> list[Mention]:
    """Every candidate ``@handle`` token in a reply, in order."""
    found: list[Mention] = []
    code = _code_spans(text)
    candidates: list[Mention] = []
    for match in _MENTION.finditer(text):
        if _escaped(text, match.start()) or any(start <= match.start() < end for start, end in code):
            continue
        localpart = match.group(1).rstrip(_TRAILING)
        if not localpart:
            continue
        server = match.group(2).rstrip(_TRAILING) if match.group(2) else None
        end = match.start() + 1 + len(localpart)
        if server:
            end += 1 + len(server)
        candidates.append(Mention(match.start(), end, text[match.start():end], localpart, server))
    for match in _NATIVE.finditer(text):
        candidates.append(Mention(match.start(), match.end(), match[0], match[1], None))
    # Roster-known aliases can contain spaces and Unicode. Longest matches win,
    # but an alias must not consume a prefix of an unknown or canonical ID.
    names = sorted(set(name for name in aliases if name), key=lambda name: (-len(name), name))
    if names:
        pattern = re.compile(r"(?<![\w@./:-])@(" + "|".join(map(re.escape, names)) +
                             r")(?![\w=/:\-]|\.[\w])", re.IGNORECASE)
        for match in pattern.finditer(text):
            candidates.append(Mention(match.start(), match.end(), match[0], match[1], None))
    for mention in sorted(candidates, key=lambda item: (item.start, -item.end)):
        if (found and mention.start < found[-1].end) or _escaped(text, mention.start):
            continue
        if any(start <= mention.start < end for start, end in code):
            continue
        found.append(mention)
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


def plan_mentions(text: str, lookup: Callable[[Mention], str | None], *,
                  allow_broadcast: bool = False, aliases: Iterable[str] = ()) -> MentionPlan:
    """Resolve every candidate mention through ``lookup``; never rewrite text."""
    known: list[tuple[Mention, str]] = []
    unknown: list[Mention] = []
    for mention in find_mentions(text, aliases):
        if allow_broadcast and mention.server is None and mention.localpart.casefold() in {
                "here", "everyone", "channel", "room"}:
            continue  # Leave permitted transport-native tokens in the body.
        user_id = lookup(mention)
        if user_id:
            known.append((mention, user_id))
        else:
            unknown.append(mention)
    return MentionPlan(text, tuple(known), tuple(unknown))


@dataclass(frozen=True)
class Member:
    id: str
    name: str
    aliases: tuple[str, ...]
    kind: str = "user"

    def to_json(self) -> dict[str, object]:
        return {"id": self.id, "name": self.name, "aliases": list(self.aliases), "kind": self.kind}


class Roster:
    """One source for enumeration, aliases, canonical resolution and feedback."""

    def __init__(self, members: Iterable[Member], *, complete: bool = True,
                 available: bool = True) -> None:
        self.members = sorted(members, key=lambda member: member.id)
        self.complete = complete
        self.available = available
        self.exact = {member.id: member.id for member in self.members}
        self.aliases: dict[str, set[str]] = {}
        for member in self.members:
            for alias in member.aliases:
                self.aliases.setdefault(alias.casefold(), set()).add(member.id)

    def lookup(self, mention: Mention) -> str | None:
        if mention.raw in self.exact:
            return self.exact[mention.raw]
        candidates = self.aliases.get(mention.localpart.casefold(), set())
        return next(iter(candidates)) if len(candidates) == 1 and not mention.server else None

    def resolve_target(self, value: str) -> str | None:
        """Resolve an explicit argument; unlike prose, its @ is optional."""
        if value in self.exact:
            return self.exact[value]
        if value.startswith("<@"):
            plan = self.plan(value)
            return plan.user_ids[0] if len(plan.known) == 1 and not plan.unknown else None
        candidates = self.aliases.get(value.removeprefix("@").casefold(), set())
        return next(iter(candidates)) if len(candidates) == 1 else None

    def plan(self, text: str, *, allow_broadcast: bool = False) -> MentionPlan:
        return plan_mentions(text, self.lookup, aliases=self.aliases, allow_broadcast=allow_broadcast)

    def feedback(self, plan: MentionPlan) -> str | None:
        if len(plan.known) + len(plan.unknown) > MAX_MENTIONS:
            return f"Reply exceeds the limit of {MAX_MENTIONS} mentions. Reduce the mentions."
        if not plan.unknown:
            return None
        state = "unavailable" if not self.available else "incomplete" if not self.complete else "available"
        canonical = roster_line([f"{member.name} ({member.id})" for member in self.members])
        return ("Reply not posted: unknown or ambiguous mention. "
                f"Membership is {state}. Canonical targets: {canonical or 'none available'}. "
                "Use an exact canonical ID, omit the ping, or retry member lookup.")


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
