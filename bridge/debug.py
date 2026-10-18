"""Bounded, scope-local diagnostics that do not wake Pi or reveal conversation data."""

from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path

from bridge.modes import mode_label
from bridge.runtime.session import BridgeRuntime
from bridge.runtime.state import SessionKey

STARTED = time.monotonic()
_SOURCE_ROOT = Path(__file__).resolve().parent.parent
_SHA = re.compile(r"[0-9a-f]{40}\Z")


def bridge_commit() -> str:
    """Read the checked-out bridge source revision, not the agent work directory."""
    try:
        result = subprocess.run(
            ["git", "-C", str(_SOURCE_ROOT), "rev-parse", "--verify", "HEAD"],
            capture_output=True, text=True, timeout=2, check=True,
        )
        sha = result.stdout.strip()
        return sha[:12] if _SHA.fullmatch(sha) else "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _duration(seconds: float) -> str:
    total = max(0, int(seconds))
    days, remainder = divmod(total, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{days}d {hours}h {minutes}m {seconds}s" if days else f"{hours}h {minutes}m {seconds}s"


async def debug_status(runtime: BridgeRuntime, key: SessionKey) -> str:
    """Inspect only this scope; a missing/sleeping scope is never loaded or woken."""
    scope = runtime._scopes.get(key)
    if scope is None:
        state = "sleeping"
    elif scope.active:
        state = "awake (working)"
    elif scope.client is not None:
        state = "awake (idle)"
    else:
        state = "sleeping"
    checkpoint = scope.checkpoint.status if scope is not None else "none loaded"
    mode = getattr(scope, "mode", None) if scope is not None else None
    mode_status = ""
    if scope is not None and mode is not None:
        label = mode_label(mode) if mode else "unconfirmed — select a mode before continuing"
        selection = getattr(runtime, "modes", {}).get(mode)
        model = f" · {selection.provider}/{selection.model}" if selection else ""
        pending = " (next wake)" if scope.client is None else ""
        mode_status = f"Mode: {label}{model}{pending}\n"
    names = ", ".join(mode_label(name) for name in sorted(getattr(runtime, "modes", {})))
    configured = f"Configured modes: {names}\n" if names else ""
    return (f"Bridge debug ({key.agent}, {key.transport})\n"
            f"Uptime: {_duration(time.monotonic() - STARTED)}\n"
            f"Status (this conversation): {state}\n"
            f"Checkpoint: {checkpoint}\n"
            f"{mode_status}{configured}"
            f"Bridge commit: {bridge_commit()}\n"
            "Pi status is not queried; this command does not wake a session.")
