"""Bounded operation metrics and privacy-safe lifecycle logging."""
from __future__ import annotations

import logging
import asyncio
import time
from collections import Counter
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from .metric_labels import label_value

log = logging.getLogger("agent_bridge.operations")
BUCKETS = (0.01, 0.1, 1.0, 5.0, 30.0, 120.0, 900.0)


class Telemetry:
    """Event-loop-owned counters; identifiers belong in logs, never labels."""

    def __init__(self) -> None:
        self.counts: Counter[tuple[str, str, str]] = Counter()
        self.durations: dict[tuple[str, str], list[float]] = {}
        self.inflight: Counter[tuple[str, str]] = Counter()

    def record(self, transport: str, operation: str, outcome: str, *,
               seconds: float | None = None, scope_hash: str = "", epoch: int = 0) -> None:
        self.counts[transport, operation, outcome] += 1
        if seconds is not None:
            values = self.durations.setdefault((transport, operation), [0.0] * (len(BUCKETS) + 2))
            for index, bound in enumerate(BUCKETS):
                values[index] += seconds <= bound
            values[-2] += 1
            values[-1] += seconds
        try:
            fields = (transport, operation, outcome, scope_hash, epoch, seconds)
            if outcome in {"error", "failed", "failure"}:
                log.warning("Bridge operation failed transport=%s operation=%s outcome=%s "
                            "scope_hash=%s epoch=%d seconds=%s", *fields)
            elif seconds is not None and seconds >= 1.0:
                log.info("Slow bridge operation transport=%s operation=%s outcome=%s "
                         "scope_hash=%s epoch=%d seconds=%s", *fields)
            else:
                log.debug("Bridge operation transport=%s operation=%s outcome=%s scope_hash=%s "
                          "epoch=%d seconds=%s", *fields)
        except Exception:
            pass  # A broken log sink must not interrupt transport or checkpoint work.

    @asynccontextmanager
    async def queued(self, lock: asyncio.Lock, transport: str, scope_hash: str) -> AsyncIterator[None]:
        started = time.monotonic()
        acquired = False
        self.inflight[transport, "input_queue"] += 1
        try:
            async with lock:
                acquired = True
                self.inflight[transport, "input_queue"] -= 1
                self.record(transport, "input_wait", "acquired", seconds=time.monotonic() - started,
                            scope_hash=scope_hash)
                yield
        finally:
            if not acquired:
                self.inflight[transport, "input_queue"] -= 1

    def render(self, agent: str, *, disconnected: bool = False) -> str:
        label = label_value(agent)
        lines = ["# HELP agent_bridge_operations_total Bridge operations by bounded outcome.",
                 "# TYPE agent_bridge_operations_total counter"]
        for (transport, operation, outcome), count in sorted(self.counts.items()):
            lines.append(f'agent_bridge_operations_total{{agent="{label}",transport="{label_value(transport)}",'
                         f'operation="{label_value(operation)}",outcome="{label_value(outcome)}"}} {count}')
        lines += ["# HELP agent_bridge_operation_seconds Operation duration including queue waits.",
                  "# TYPE agent_bridge_operation_seconds histogram"]
        for (transport, operation), values in sorted(self.durations.items()):
            labels = f'agent="{label}",transport="{label_value(transport)}",operation="{label_value(operation)}"'
            for bound, bucket_count in zip(BUCKETS, values):
                lines.append(f'agent_bridge_operation_seconds_bucket{{{labels},le="{bound}"}} {bucket_count}')
            lines += [f'agent_bridge_operation_seconds_bucket{{{labels},le="+Inf"}} {values[-2]}',
                      f'agent_bridge_operation_seconds_count{{{labels}}} {values[-2]}',
                      f'agent_bridge_operation_seconds_sum{{{labels}}} {values[-1]}']
        lines += ["# HELP agent_bridge_inflight Current operations and queued inputs.",
                  "# TYPE agent_bridge_inflight gauge"]
        for (transport, operation), count in sorted(self.inflight.items()):
            if disconnected:
                count = 0
            lines.append(f'agent_bridge_inflight{{agent="{label}",transport="{label_value(transport)}",'
                         f'operation="{label_value(operation)}"}} {count}')
        return "\n".join(lines) + "\n"
