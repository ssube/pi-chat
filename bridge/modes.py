"""Configured conversation modes; no model calls or automatic escalation."""

from __future__ import annotations

import re
from collections.abc import Collection
from dataclasses import dataclass
from typing import Any

_RESERVED = {"debug", "stop", "cancel", "recover", "mode",
             "bridge_debug", "bridge_stop", "bridge_cancel", "bridge_recover"}
_NAME = re.compile(r"[a-z][a-z0-9_-]{0,31}\Z")


@dataclass(frozen=True)
class ModelSelection:
    provider: str
    model: str


def read_modes(value: Any, legacy_model: str, default: str, legacy_provider: str = "test") -> dict[str, ModelSelection]:
    if value is None:
        value = {"fast": {"provider": legacy_provider, "model": legacy_model}}
    if not isinstance(value, dict) or not value:
        raise ValueError("modes must be a nonempty object")
    modes = {}
    for name, selection in value.items():
        if not isinstance(name, str) or not _NAME.fullmatch(name) or name in _RESERVED:
            raise ValueError("Mode names must be lowercase command names, not reserved bridge commands")
        if not isinstance(selection, dict) or set(selection) != {"provider", "model"}:
            raise ValueError("Each mode requires only provider and model")
        if any(not isinstance(item, str) or not item.strip() or
               any(character.isspace() or ord(character) < 32 for character in item)
               for item in selection.values()):
            raise ValueError("Mode provider and model must be nonempty identifiers without whitespace")
        modes[name] = ModelSelection(**selection)
    if default not in modes:
        raise ValueError("default_mode must name a configured mode")
    return modes


def mode_label(name: str) -> str:
    icon = {"smart": "🧠", "fast": "🐇"}.get(name, "⚙️")
    return f"{icon} {name}"


def mode_command(text: str, names: Collection[str]) -> tuple[str, str] | None:
    """Only a leading command controls mode; quoted/prose mentions never do."""
    match = re.fullmatch(r"\s*[!/]([a-z][a-z0-9_-]{0,31})(?:\s+([\s\S]*))?", text, re.IGNORECASE)
    if match is None:
        return None
    name = match[1].lower()
    if name not in {*names, "fast", "smart"}:
        return None
    return name, (match[2] or "").strip()


class ModeSwitchError(RuntimeError):
    """Safe, user-facing mode refusal, not a raw provider diagnostic."""
