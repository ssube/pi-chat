"""Shared machine-message classification; ordinary prose stays conversational."""
from __future__ import annotations

import json
from typing import Any

BRIDGE_META = "org.example.pi_bridge"
SILENT_KINDS = frozenset({"ack", "progress", "progress_summary", "steered", "status", "startup", "cleanup", "cleanup_request", "compacting", "mode"})
TERMINAL_KINDS = frozenset({"final", "error", "cancelled"})
MAX_HANDOFF_HOPS = 3
DISCORD_META_PREFIX = "agent-bridge:v1:"


def discord_metadata(message: Any) -> dict[str, Any] | None:
    if not getattr(message.author, "bot", False):
        return None
    for embed in getattr(message, "embeds", ()):
        footer = getattr(getattr(embed, "footer", None), "text", None)
        if not isinstance(footer, str) or not footer.startswith(DISCORD_META_PREFIX):
            continue
        try:
            data = json.loads(footer[len(DISCORD_META_PREFIX):])
        except (ValueError, TypeError):
            continue
        if isinstance(data, dict):
            return data
    return None


def metadata_rejection(meta: object, *, awake: bool, addressed: bool,
                       wake_any: bool) -> str | None:
    if not isinstance(meta, dict):
        return None
    kind = meta.get("kind")
    if isinstance(kind, str) and kind in SILENT_KINDS:
        return "machine_status"
    if not isinstance(kind, str) or kind not in TERMINAL_KINDS:
        return "protocol"
    hop = meta.get("hop", 0)
    if type(hop) is not int or not 0 <= hop <= MAX_HANDOFF_HOPS:
        return "invalid_hop"
    if kind != "final":
        return None if awake else "terminal_idle"
    if hop >= MAX_HANDOFF_HOPS and not addressed:
        return "hop_limit"
    return None if addressed or awake or wake_any else "unaddressed"
