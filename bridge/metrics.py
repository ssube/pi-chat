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
    return "\n".join(lines) + "\n"


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
        body = render_metrics(self.agent, summary, self.now())
        telemetry = getattr(self.runtime, "telemetry", None)
        if isinstance(telemetry, Telemetry):
            body += telemetry.render(self.agent)
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
            body = render_metrics(self.agent, {}, self.now(), disconnected=True)
            telemetry = getattr(self.runtime, "telemetry", None)
            if isinstance(telemetry, Telemetry):
                body += telemetry.render(self.agent, disconnected=True)
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
