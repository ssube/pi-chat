"""Presentation shared by the Matrix and Discord progress-card lifecycles."""
from datetime import datetime, timezone


def sealed_progress(header: str, history: str, outcome: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%H:%M UTC")
    return "\n".join(part for part in (header, f"Work log — {outcome} {stamp}", history) if part)
