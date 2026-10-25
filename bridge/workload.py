"""Counts-only Pi RPC observation; no session starts, tools or transcript reads."""
from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping

KEY = "agent-bridge/workload/counts/v1"
TASK_STATES = ("pending", "progress", "blocked", "question", "complete")
SUBAGENT_FIELDS = ("active", "capacity_used", "capacity_limit")
SOURCES = ("tasks", "subagents")
MAX_AGE_SECONDS = 75.0  # More than two 30-second bridge push periods.
MAX_COUNT = 2**53 - 1


def _counts(value: object, fields: tuple[str, ...]) -> dict[str, int] | None:
    if not isinstance(value, dict) or set(value) != set(fields):
        return None
    if any(type(value[key]) is not int or not 0 <= value[key] <= MAX_COUNT for key in fields):
        return None
    return {key: value[key] for key in fields}


class WorkloadSnapshot:
    """One managed Pi process's latest observation, with receipt-time freshness."""

    def __init__(self, *, now: Callable[[], float] = time.monotonic) -> None:
        self.now = now
        self.clear()

    def clear(self) -> None:
        self.received: float | None = None
        self.counts: dict[str, dict[str, int] | None] = {source: None for source in SOURCES}

    def consume(self, event: Mapping[str, object]) -> bool:
        if (event.get("type") != "extension_ui_request" or event.get("method") != "setStatus"
                or event.get("statusKey") != KEY):
            return False
        # Consume only the reserved observer status. Malformed replacements
        # invalidate previous data immediately; arbitrary fields never become labels.
        self.clear()
        self.received = self.now()
        raw = event.get("statusText")
        if not isinstance(raw, str) or len(raw) > 2048:
            return True
        try:
            data = json.loads(raw)
        except (ValueError, RecursionError):
            return True
        if (not isinstance(data, dict) or set(data) != {"version", "tasks", "subagents"}
                or type(data["version"]) is not int or data["version"] != 1):
            return True
        self.counts["tasks"] = _counts(data["tasks"], TASK_STATES)
        self.counts["subagents"] = _counts(data["subagents"], SUBAGENT_FIELDS)
        self.received = self.now()
        return True

    def view(self) -> tuple[float | None, dict[str, dict[str, int] | None]]:
        age = None if self.received is None else max(0.0, self.now() - self.received)
        return age, self.counts.copy()


def aggregate(clients: list[object]) -> dict[str, object]:
    """Aggregate only resident clients, omitting totals unless coverage is complete."""
    coverage = {source: {state: 0 for state in ("available", "absent", "unavailable", "stale")} for source in SOURCES}
    totals = {"tasks": dict.fromkeys(TASK_STATES, 0), "subagents": dict.fromkeys(SUBAGENT_FIELDS, 0)}
    oldest: float | None = None
    for client in clients:
        snapshot = getattr(client, "workload_snapshot", None)
        age, counts = snapshot.view() if isinstance(snapshot, WorkloadSnapshot) and getattr(client, "running", False) else (None, {})
        if age is not None:
            oldest = age if oldest is None else max(oldest, age)
        for source in SOURCES:
            values = counts.get(source)
            state = "absent" if age is None else "stale" if age > MAX_AGE_SECONDS else "unavailable" if values is None else "available"
            coverage[source][state] += 1
            if state == "available" and values is not None:
                for key, value in values.items():
                    totals[source][key] += value
    return {"resident_sessions": len(clients), "coverage": coverage, "oldest_age_seconds": oldest,
            "counts": {source: totals[source] if coverage[source]["available"] == len(clients) else None for source in SOURCES}}
