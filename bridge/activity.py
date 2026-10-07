"""Quiet public activity shared by Matrix and Discord."""

from __future__ import annotations

import asyncio
import enum
import json
import logging
import os
import random
import re
import tempfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from bridge.runtime.session import BridgeRuntime


log = logging.getLogger("agent_bridge.activity")
POLL_SECONDS = 15
ROTATE_SECONDS = 3 * 60 * 60
ROTATE_CHANCE = 0.5
HISTORY_LIMIT = 5
_LOCAL_REJECTION_MESSAGES = {
    "transport": "invalid activity transport",
    "scope": "invalid activity scope",
    "awake": "activity is available only for an awake conversation",
    "fields": "invalid activity text or duration",
    "duration": "status duration must be between 0 and 24 hours",
    "status": "status must be short public text without identifiers",
}


class ActivityRejected(ValueError):
    """A local status rejection with a fixed, Pi-safe message."""

    def __init__(self, reason: Literal["transport", "scope", "awake", "fields",
                                       "duration", "status"]) -> None:
        message = _LOCAL_REJECTION_MESSAGES.get(reason)
        if message is None:
            raise ValueError("unknown local activity rejection")
        self.message = message
        super().__init__(message)


class Presence(str, enum.Enum):
    """Presence a bridge publishes to mirror its lifecycle state."""
    AWAKE = "awake"              # online: a turn is running or within the awake window
    CONNECTED = "connected"      # idle: bridge up, every scope asleep
    DISCONNECTED = "disconnected"  # offline: graceful shutdown


@dataclass(frozen=True)
class Activity:
    text: str
    presence: Presence


class ActivityAdapter(Protocol):
    async def publish_activity(self, activity: Activity) -> bool: ...


Generate = Callable[[list[str], list[str]], Awaitable[str]]


def clean_status(value: str) -> str:
    """Only short, single-line public prose; refuse common identifier shapes."""
    text = " ".join(value.strip().strip('"\'`').split())
    if (not text or len(text) > 80 or "\n" in value or
            re.search(r"https?://|www\.|@|#\d|/\S+|\\\S+|\b\d{3,}\b", text, re.I)):
        raise ActivityRejected("status")
    return text


class ActivityService:
    """Occasionally create a new phrase; publish lifecycle changes promptly."""

    def __init__(self, agent: str, runtime: BridgeRuntime,
                 adapters: tuple[ActivityAdapter, ...], state_file: Path,
                 generate: Generate, *, now: Callable[[], float] = time.time,
                 chance: Callable[[], float] = random.random) -> None:
        self.agent = agent
        self.runtime = runtime
        self.adapters = adapters
        self.state_file = state_file
        self.generate = generate
        self.now = now
        self.chance = chance
        self._sent: dict[int, Activity] = {}
        self._retry_after: dict[int, float] = {}
        self._lock = asyncio.Lock()
        self._run_task: asyncio.Task | None = None
        self.history: list[str] = []
        self.last_slot = -1
        self.manual_until = 0.0
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.state_file.read_text())
            if not isinstance(data, dict):
                return
            self.history = [clean_status(value) for value in data.get("history", [])
                            if isinstance(value, str)][-HISTORY_LIMIT:]
            self.last_slot = int(data.get("last_slot", -1))
            self.manual_until = float(data.get("manual_until", 0))
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError):
            log.warning("Ignoring invalid activity state for %s", self.agent)

    def _save(self) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.state_file.parent.chmod(0o700)
        descriptor, name = tempfile.mkstemp(prefix=".activity-", dir=self.state_file.parent)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w") as output:
                json.dump({"history": self.history, "last_slot": self.last_slot,
                           "manual_until": self.manual_until}, output)
                output.flush()
                os.fsync(output.fileno())
            os.replace(name, self.state_file)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    async def set_status(self, value: str, *, hours: float = 3) -> None:
        if not 0 < hours <= 24:
            raise ActivityRejected("duration")
        text = clean_status(value)
        async with self._lock:
            self.history.append(text)
            self.history = self.history[-HISTORY_LIMIT:]
            self.manual_until = self.now() + hours * 3600
            self._save()
        await self.refresh()

    async def refresh(self) -> None:
        generation: tuple[list[str], list[str], int] | None = None
        async with self._lock:
            now = self.now()
            slot = int(now // ROTATE_SECONDS)
            if now >= self.manual_until and slot != self.last_slot:
                self.last_slot = slot
                if not self.history or self.chance() < ROTATE_CHANCE:
                    # The generator receives only public history and fixed topic
                    # labels. Release the lock while Pi works so direct CLI
                    # updates do not queue behind an inference request.
                    generation = (self.history[-HISTORY_LIMIT:],
                                  self.runtime.activity_topics(), slot)
                self._save()
        if generation is not None:
            try:
                # This disposable status Pi is a second process. Keep it out
                # of agent-owned reviews and ordinary active turns as well.
                async with self.runtime.external_job() as reserved:
                    if reserved:
                        proposed = await self.generate(generation[0], generation[1])
                        text = clean_status(proposed)
                        async with self._lock:
                            if (self.last_slot == generation[2] and
                                    self.now() >= self.manual_until and
                                    text not in self.history[-3:]):
                                self.history.append(text)
                                self.history = self.history[-HISTORY_LIMIT:]
                                self._save()
            except Exception:
                log.warning("Could not generate activity for %s", self.agent, exc_info=True)
        async with self._lock:
            now = self.now()
            active, awake, tool = self.runtime.activity_snapshot()
            text = self.history[-1] if self.history else "Listening for a message"
            if active and now >= self.manual_until:
                name = tool.lower()
                if any(word in name for word in ("bash", "shell", "exec", "read", "write", "edit")):
                    text = "Working in the code"
                elif any(word in name for word in ("web", "search", "browse")):
                    text = "Looking something up"
                else:
                    text = "Working on a request"
            presence = Presence.AWAKE if (active or awake) else Presence.CONNECTED
            activity = Activity(text, presence)
            for index, adapter in enumerate(self.adapters):
                if self._sent.get(index) == activity or now < self._retry_after.get(index, 0):
                    continue
                try:
                    if await adapter.publish_activity(activity):
                        self._sent[index] = activity
                except Exception:
                    self._retry_after[index] = now + 300
                    log.warning("Could not update %s activity", type(adapter).__name__, exc_info=True)

    async def run(self) -> None:
        self._run_task = asyncio.current_task()
        try:
            while True:
                await self.refresh()
                await asyncio.sleep(POLL_SECONDS)
        finally:
            self._run_task = None

    async def shutdown(self, *, publish_timeout: float = 5.0) -> None:
        """Stop the poll loop, then publish one final disconnected state.

        The poll task is cancelled and awaited first so a slow in-flight
        ``refresh()`` cannot republish a connected/awake state after the final
        disconnected publish. The final publish is bounded so a wedged adapter
        (e.g. Matrix retry/backoff) cannot hold graceful shutdown indefinitely.
        """
        task = self._run_task
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        activity = Activity(self.history[-1] if self.history else "Listening for a message",
                            Presence.DISCONNECTED)
        for adapter in self.adapters:
            try:
                await asyncio.wait_for(adapter.publish_activity(activity),
                                      timeout=publish_timeout)
            except Exception:
                log.warning("Could not publish disconnected state to %s",
                            type(adapter).__name__, exc_info=True)
