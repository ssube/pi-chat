"""Push bridge lifecycle metrics to a Prometheus Pushgateway."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Any

from bridge.runtime.session import BridgeRuntime
from bridge.telemetry import Telemetry
from bridge.metric_labels import label_value
from bridge.runtime.state import _STATUSES
from bridge.workload import SOURCES, TASK_STATES, SUBAGENT_FIELDS


log = logging.getLogger("agent_bridge.metrics")
POLL_SECONDS = 30


def _count(value: object) -> int:
    """Narrow a session summary value to an int, defaulting to zero."""
    return value if isinstance(value, int) else 0


def render_metrics(agent: str, summary: dict[str, object], timestamp: float,
                   *, disconnected: bool = False) -> str:
    """Render one Prometheus text snapshot from a session summary.

    Presence is a single lifecycle state rendered as three labelled gauges with
    exactly one set to 1. Scope counts come straight from ``session_summary()``;
    a disconnected snapshot zeroes them out because the scopes are being closed.
    """
    total = _count(summary.get("total_scopes", 0))
    active = _count(summary.get("active", 0))
    awake = _count(summary.get("awake", 0))
    critical = _count(summary.get("active_non_idle", 0))
    agent_label = label_value(agent)
    if disconnected:
        awake_v, connected_v, disconnected_v = 0, 0, 1
        total = active = awake = critical = 0
    elif active or awake:
        awake_v, connected_v, disconnected_v = 1, 0, 0
    else:
        awake_v, connected_v, disconnected_v = 0, 1, 0
    lines = [
        "# HELP agent_bridge_presence Current lifecycle presence (1 when active).",
        "# TYPE agent_bridge_presence gauge",
        f'agent_bridge_presence{{agent="{agent_label}",state="awake"}} {awake_v}',
        f'agent_bridge_presence{{agent="{agent_label}",state="connected"}} {connected_v}',
        f'agent_bridge_presence{{agent="{agent_label}",state="disconnected"}} {disconnected_v}',
        "# HELP agent_bridge_scopes Loaded conversation scopes by lifecycle state.",
        "# TYPE agent_bridge_scopes gauge",
        f'agent_bridge_scopes{{agent="{agent_label}",state="total"}} {total}',
        f'agent_bridge_scopes{{agent="{agent_label}",state="active"}} {active}',
        f'agent_bridge_scopes{{agent="{agent_label}",state="awake"}} {awake}',
        f'agent_bridge_scopes{{agent="{agent_label}",state="critical"}} {critical}',
        "# HELP agent_bridge_last_push_timestamp_seconds Unix time of the last push.",
        "# TYPE agent_bridge_last_push_timestamp_seconds gauge",
        f"agent_bridge_last_push_timestamp_seconds{{agent=\"{agent_label}\"}} {timestamp!r}",
    ]
    # Session lifecycle is bridge-owned and independent of the Pi observer.
    breakdown = summary.get("state_breakdown", {}) if not disconnected else {}
    breakdown = breakdown if isinstance(breakdown, dict) else {}
    states = sorted(_STATUSES | {"compacting", "unknown"})
    session_counts = dict.fromkeys(states, 0)
    for state, value in breakdown.items():
        if type(value) is int and value >= 0:
            session_counts[state if state in session_counts else "unknown"] += value
    lines += ["# HELP agent_bridge_sessions Loaded bridge conversation scopes by mutually exclusive lifecycle state.",
              "# TYPE agent_bridge_sessions gauge"]
    lines += [f'agent_bridge_sessions{{agent="{agent_label}",state="{state}"}} {session_counts[state]}' for state in states]
    lines += render_workload(agent_label, summary.get("workload"), disconnected=disconnected)
    return "\n".join(lines) + "\n"


def render_workload(agent_label: str, workload: object, *, disconnected: bool) -> list[str]:
    """Omit unavailable totals rather than presenting partial observations as zero."""
    data = workload if isinstance(workload, dict) and not disconnected else {}
    coverage = data.get("coverage", {})
    counts = data.get("counts", {})
    lines = ["# HELP agent_bridge_workload_resident_sessions Resident bridge-owned Pi sessions; excludes standalone and archived sessions.",
             "# TYPE agent_bridge_workload_resident_sessions gauge",
             f'agent_bridge_workload_resident_sessions{{agent="{agent_label}"}} {_count(data.get("resident_sessions"))}',
             "# HELP agent_bridge_workload_coverage Exclusive source coverage: absent=no frame, stale=older than TTL, unavailable=source cannot report, available=fresh valid counts.",
             "# TYPE agent_bridge_workload_coverage gauge"]
    lines += ["# HELP agent_bridge_workload_sources_covered Resident sessions with a fresh available source snapshot.",
              "# TYPE agent_bridge_workload_sources_covered gauge",
              "# HELP agent_bridge_workload_sources_resident Resident sessions requiring source coverage.",
              "# TYPE agent_bridge_workload_sources_resident gauge",
              "# HELP agent_bridge_workload_coverage_complete Complete fresh coverage for a source; zero on disconnect or absent bridge observation.",
              "# TYPE agent_bridge_workload_coverage_complete gauge"]
    for source in SOURCES:
        observed = coverage.get(source, {})
        covered = _count(observed.get("available"))
        resident = _count(data.get("resident_sessions"))
        complete = int(bool(data) and covered == resident)
        lines += [f'agent_bridge_workload_sources_covered{{agent="{agent_label}",source="{source}"}} {covered}',
                  f'agent_bridge_workload_sources_resident{{agent="{agent_label}",source="{source}"}} {resident}',
                  f'agent_bridge_workload_coverage_complete{{agent="{agent_label}",source="{source}"}} {complete}']
        for status in ("available", "absent", "unavailable", "stale"):
            lines.append(f'agent_bridge_workload_coverage{{agent="{agent_label}",source="{source}",status="{status}"}} {_count(observed.get(status))}')
    lines += ["# HELP agent_bridge_workload_oldest_age_seconds Oldest resident counts-frame receipt age; not a source event timestamp.",
              "# TYPE agent_bridge_workload_oldest_age_seconds gauge"]
    age = data.get("oldest_age_seconds")
    if isinstance(age, (int, float)):
        lines.append(f'agent_bridge_workload_oldest_age_seconds{{agent="{agent_label}"}} {age}')
    lines += ["# HELP agent_bridge_tasks Tasks by effective state across all resident bridge-owned Pi sessions; omitted without complete fresh coverage.",
              "# TYPE agent_bridge_tasks gauge"]
    tasks = counts.get("tasks")
    if isinstance(tasks, dict):
        lines += [f'agent_bridge_tasks{{agent="{agent_label}",state="{state}"}} {_count(tasks.get(state))}' for state in TASK_STATES]
    subagents = counts.get("subagents")
    descriptions = {
        "active": "Native delegated work outstanding, including pending entries and workflow controllers; NOT running processes or capacity utilization.",
        "capacity_used": "Native top-level async slots used across resident Pi sessions; excludes foreground and nested capacity.",
        "capacity_limit": "Native top-level async slot limits summed across resident Pi sessions; NOT total child concurrency.",
    }
    for field in SUBAGENT_FIELDS:
        name = f"agent_bridge_subagents_{field}"
        lines += [f"# HELP {name} {descriptions[field]} Omitted without complete fresh coverage.", f"# TYPE {name} gauge"]
        if isinstance(subagents, dict):
            lines.append(f'{name}{{agent="{agent_label}"}} {_count(subagents.get(field))}')
    return lines


def compose_metrics(agent: str, summary: dict[str, object], timestamp: float,
                    telemetry: object = None, *, disconnected: bool = False) -> str:
    """One complete group composer for periodic and final replacement PUTs."""
    body = render_metrics(agent, summary, timestamp, disconnected=disconnected)
    if isinstance(telemetry, Telemetry):
        body += telemetry.render(agent, disconnected=disconnected)
    return body


class MetricsService:
    """Periodically push one bounded lifecycle snapshot to the Pushgateway."""

    def __init__(self, agent: str, runtime: BridgeRuntime, pushgateway_url: str,
                 *, job: str = "agent_bridge", poll_seconds: float = POLL_SECONDS,
                 push_timeout: float = 5.0,
                 now: Callable[[], float] = time.time) -> None:
        self.agent = agent
        self.runtime = runtime
        self.pushgateway_url = pushgateway_url.rstrip("/")
        self.job = job
        self.poll_seconds = poll_seconds
        self.push_timeout = push_timeout
        self.now = now
        self._session: Any = None
        self._run_task: asyncio.Task | None = None

    async def _push(self, body: str) -> None:
        import aiohttp  # Optional until metrics are started.
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        url = f"{self.pushgateway_url}/metrics/job/{self.job}/instance/{self.agent}"
        async with asyncio.timeout(self.push_timeout):
            async with self._session.put(url, data=body) as response:
                if response.status >= 400:
                    raise RuntimeError(f"Pushgateway returned HTTP {response.status}")

    async def refresh(self) -> None:
        summary = await self.runtime.session_summary()
        body = compose_metrics(self.agent, summary, self.now(), getattr(self.runtime, "telemetry", None))
        await self._push(body)

    async def shutdown(self) -> None:
        """Stop the poll loop, then publish one final disconnected snapshot.

        The poll task is cancelled and awaited first so an in-flight refresh
        cannot PUT a connected/awake snapshot after the final disconnected one,
        or race the session close below. The final PUT is bounded by
        ``push_timeout`` so a stalled Pushgateway cannot hang shutdown.
        """
        task = self._run_task
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        try:
            body = compose_metrics(self.agent, {}, self.now(), getattr(self.runtime, "telemetry", None), disconnected=True)
            await self._push(body)
        except Exception:
            log.warning("Could not push final disconnected metrics for %s",
                        self.agent, exc_info=True)
        finally:
            if self._session is not None and not self._session.closed:
                await self._session.close()
                self._session = None

    async def run(self) -> None:
        self._run_task = asyncio.current_task()
        try:
            while True:
                try:
                    await self.refresh()
                except Exception:
                    log.warning("Could not push bridge metrics for %s", self.agent, exc_info=True)
                await asyncio.sleep(self.poll_seconds)
        finally:
            self._run_task = None
