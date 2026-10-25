"""Private durable, coalesced delivery indicators, independent of progress text."""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import tempfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal, cast

from .runtime.state import SessionKey

TurnStatus = Literal["running", "done", "failed", "clear"]
TURN_EMOJI: dict[TurnStatus, str] = {"running": "⏳", "done": "🤖", "failed": "⚠️"}
MAX_TRANSPORT_RETRY_SECONDS = 30.0  # Same bound as Matrix's validated 429 hint.
log = logging.getLogger("agent_bridge.turn_status")
Target = tuple[SessionKey, str, int]
Bucket = tuple[str, str]  # Transport and physical room/channel, not a request or thread.


@dataclass
class Indicator:
    key: SessionKey
    request_id: str
    epoch: int
    desired: TurnStatus = "running"
    running_id: str | None = None
    pending_attempted: bool = False
    applied: TurnStatus | None = None
    last_attempt: float = 0

    @property
    def target(self) -> Target:
        return self.key, self.request_id, self.epoch


@dataclass(frozen=True)
class Deferred:
    transport: Literal["matrix", "discord"]
    retry_after: float


Emit = Callable[[Indicator, TurnStatus], Awaitable[str | None | Deferred]]


class TurnIndicators:
    """Persist intent before HTTP; only the worker emits, at progress cadence.

    `done` is accepted only from the runtime's explicit final-delivery receipt.
    Restart converts uncertain running intents to failed, never inferred done.
    """

    def __init__(self, path: Path, agent: str, emit: Emit, *, interval: float = 2,
                 now: Callable[[], float] = time.time) -> None:
        self.path, self.agent, self.emit = path, agent, emit
        self.interval, self.now = interval, now
        self.records: dict[Target, Indicator] = {}
        self.waiters: dict[Target, list[tuple[TurnStatus, asyncio.Future[None]]]] = {}
        self.last_scope_attempt: dict[SessionKey, float] = {}
        self.retry_after: dict[Target, float] = {}
        self.failures: dict[Target, int] = {}
        self.transport_not_before: dict[str, float] = {}
        self._stored_transport_not_before: dict[str, float] = {}
        self._inflight: dict[Bucket, asyncio.Task[None]] = {}
        self.draining = False
        self.closed = False
        self.load_error = False
        try:
            if path.is_symlink():
                raise ValueError("unexpected indicator state path")
            data = json.loads(path.read_text()) if path.exists() else {"records": [], "last_updates": []}
            if not isinstance(data, dict) or not isinstance(data.get("records"), list):
                raise ValueError("invalid indicator state")
            schema = data.get("schema", 1)
            if (type(schema) is not int or
                    (schema == 1 and set(data) - {"records", "last_updates"}) or
                    (schema == 2 and set(data) != {"schema", "records", "last_updates", "transport_not_before"}) or
                    schema not in {1, 2}):
                raise ValueError("invalid indicator schema")
            if schema == 2:
                gates = data["transport_not_before"]
                if not isinstance(gates, list):
                    raise ValueError("invalid transport gate")
                seen: set[str] = set()
                for gate in gates:
                    if (not isinstance(gate, dict) or set(gate) != {"transport", "at"} or
                            gate["transport"] not in {"matrix", "discord"} or gate["transport"] in seen or
                            type(gate["at"]) not in {int, float} or not math.isfinite(gate["at"])):
                        raise ValueError("invalid transport gate")
                    seen.add(gate["transport"])
                    current = now()
                    if gate["at"] > current:
                        # This is a future deadline, unlike last_scope_attempt's past stamp.
                        # Bound a skewed clock in memory without shortening the
                        # persisted value during unrelated state writes.
                        self._stored_transport_not_before[gate["transport"]] = gate["at"]
                        self.transport_not_before[gate["transport"]] = min(
                            gate["at"], current + MAX_TRANSPORT_RETRY_SECONDS)
            for cooldown in data.get("last_updates", []):
                key = SessionKey(**cooldown["key"])
                at = cooldown["at"]
                if key.agent != agent or not isinstance(at, (int, float)) or not math.isfinite(at):
                    raise ValueError("invalid indicator cooldown")
                self.last_scope_attempt[key] = min(at, now())
            for row in data["records"]:
                key = SessionKey(**row["key"])
                if (key.agent != agent or key.transport not in {"matrix", "discord"}
                        or not isinstance(key.scope, str) or not key.scope
                        or not isinstance(row["request_id"], str) or not row["request_id"]
                        or type(row["epoch"]) is not int or row["epoch"] < 0
                        or row["desired"] not in {"running", "done", "failed", "clear"}
                        or row.get("applied") not in {None, "running", "done", "failed", "clear"}
                        or type(row.get("pending_attempted", False)) is not bool
                        or (row.get("running_id") is not None and not isinstance(row["running_id"], str))
                        or not isinstance(row.get("last_attempt", 0), (int, float))
                        or not math.isfinite(row.get("last_attempt", 0))):
                    raise ValueError("invalid indicator record")
                record = Indicator(key, row["request_id"], row["epoch"],
                                   cast(TurnStatus, row["desired"]), row.get("running_id"),
                                   row.get("pending_attempted", False), row.get("applied"),
                                   row.get("last_attempt", 0))
                self.records[record.target] = record
        except (OSError, ValueError, TypeError, KeyError):
            # Preserve damaged evidence rather than overwrite it or invent a
            # completion. Cosmetic storage failure must not stop ordinary Pi.
            self.load_error = True
            self.records.clear()
            log.warning("Indicator state unavailable; preserving it without replay")

    def _save(self) -> None:
        # One event loop owns this synchronous, await-free whole-file writer.
        # Do not insert an await between snapshot and atomic replacement at K>1.
        if self.load_error:
            raise OSError("indicator state unavailable")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, name = tempfile.mkstemp(prefix=".turn-status-", dir=self.path.parent)
        try:
            with os.fdopen(descriptor, "w") as output:
                os.fchmod(output.fileno(), 0o600)
                cutoff = self.now() - self.interval
                self.last_scope_attempt = {key: at for key, at in self.last_scope_attempt.items()
                                           if at >= cutoff}
                self.transport_not_before = {transport: at for transport, at in self.transport_not_before.items()
                                             if at > self.now()}
                self._stored_transport_not_before = {
                    transport: self._stored_transport_not_before.get(transport, at)
                    for transport, at in self.transport_not_before.items()}
                json.dump({"schema": 2, "records": [asdict(row) for row in self.records.values()],
                           "last_updates": [{"key": asdict(key), "at": at}
                                            for key, at in self.last_scope_attempt.items()],
                           "transport_not_before": [{"transport": transport, "at": at}
                                                    for transport, at in self._stored_transport_not_before.items()]}, output)
                output.flush()
                os.fsync(output.fileno())
            os.replace(name, self.path)
            parent = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(parent)
            finally:
                os.close(parent)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def recover(self) -> None:
        """Call only after this bridge has acquired its singleton sockets."""
        if self.load_error:
            return
        for record in self.records.values():
            if record.desired == "running":
                record.desired = "failed"
            record.applied = None
        self._save()

    def _resolve(self, target: Target, phase: TurnStatus | None = None) -> None:
        remaining = []
        for requested, future in self.waiters.get(target, []):
            if phase is None or requested == phase:
                if not future.done():
                    future.set_result(None)
            else:
                remaining.append((requested, future))
        if remaining:
            self.waiters[target] = remaining
        else:
            self.waiters.pop(target, None)

    async def update(self, key: SessionKey, request_id: str, status: TurnStatus,
                     epoch: int = 0) -> None:
        if key.agent != self.agent:
            raise ValueError("indicator owner mismatch")
        target = key, request_id, epoch
        same_message = [t for t in self.records if t[:2] == target[:2]]
        if any(t[2] > epoch for t in same_message):
            return  # A stale process epoch cannot repaint a newer intent.
        record = self.records.get(target)
        if status == "running":
            if record is None:
                prior = max((self.records[t] for t in same_message),
                            key=lambda row: row.epoch, default=None)
                record = Indicator(key, request_id, epoch)
                if prior is not None:
                    record.running_id = prior.running_id
                    record.pending_attempted = prior.pending_attempted
                    record.applied = prior.applied if prior.applied == "running" else None
                    record.last_attempt = prior.last_attempt
                for old in same_message:
                    self.records.pop(old)
                    self._resolve(old)
                self.records[target] = record
            elif record.desired != "running":
                return
        elif record is None:
            return  # No corresponding accepted request; never create a terminal.
        record.desired = status
        self.retry_after.pop(target, None)
        self._save()  # Includes the confirmed outcome BEFORE delay or HTTP.
        if self.closed or record.applied == status:
            return
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self.waiters.setdefault(target, []).append((status, future))
        await future

    @staticmethod
    def _bucket(key: SessionKey) -> Bucket:
        if key.transport == "matrix":
            return "matrix", key.scope.split(":thread:", 1)[0].removesuffix(":main")
        return key.transport, key.scope

    def schedule_once(self) -> None:
        if self.draining or self.closed or self.load_error:
            return
        now = self.now()
        for target, record in list(self.records.items()):
            if (record.applied == record.desired or now < self.retry_after.get(target, 0) or
                    now < self.transport_not_before.get(record.key.transport, 0)):
                continue
            bucket = self._bucket(record.key)
            if bucket in self._inflight:
                continue  # One in-flight emit per physical room/channel.
            task = asyncio.create_task(self._flush_bucket(bucket), name=f"turn-indicator-{bucket[0]}")
            self._inflight[bucket] = task

    async def _flush_bucket(self, bucket: Bucket) -> None:
        try:
            for target, record in list(self.records.items()):
                if self._bucket(record.key) == bucket:
                    await self._flush_target(target, record)
        except Exception:
            log.exception("Indicator bucket worker failed; durable intent retained")
        finally:
            self._inflight.pop(bucket, None)

    async def flush_once(self) -> None:
        """Deterministic test/manual pass; production run schedules without waiting."""
        self.schedule_once()
        if self._inflight:
            await asyncio.gather(*list(self._inflight.values()), return_exceptions=True)

    async def _flush_target(self, target: Target, record: Indicator) -> None:
        if self.records.get(target) is not record:
            return
        phase = record.desired
        if (record.applied == phase or self.now() < self.retry_after.get(target, 0) or
                self.now() < self.transport_not_before.get(record.key.transport, 0)):
            return
        # One phase transition per conversation per progress interval,
        # including failed attempts and rapid kill/restart cycles.
        last = self.last_scope_attempt.get(record.key)
        if last is not None and last > self.now():
            last = self.now()
            self.last_scope_attempt[record.key] = last
        if last is not None and self.now() - last < self.interval:
            return
        previous_attempt = record.last_attempt
        previous_scope_attempt = self.last_scope_attempt.get(record.key)
        previous_pending_attempted = record.pending_attempted
        record.last_attempt = self.now()
        self.last_scope_attempt[record.key] = record.last_attempt
        if phase == "running":
            record.pending_attempted = True
        try:
            self._save()
            # Transport clients own Retry-After waits. A caller timeout
            # must not cancel that wait or lose the persisted desired phase.
            receipt = await self.emit(record, phase)
            if isinstance(receipt, Deferred):
                if (receipt.transport != record.key.transport or not math.isfinite(receipt.retry_after) or
                        receipt.retry_after < 0):
                    raise ValueError("invalid indicator deferral")
                until = self.now() + min(receipt.retry_after, MAX_TRANSPORT_RETRY_SECONDS)
                self.transport_not_before[receipt.transport] = max(
                    self.transport_not_before.get(receipt.transport, 0), until)
                self._stored_transport_not_before[receipt.transport] = max(
                    self._stored_transport_not_before.get(receipt.transport, 0), until)
                record.last_attempt = previous_attempt
                record.pending_attempted = previous_pending_attempted
                if previous_scope_attempt is None:
                    self.last_scope_attempt.pop(record.key, None)
                else:
                    self.last_scope_attempt[record.key] = previous_scope_attempt
                self._save()  # Keep the transport gate before releasing the worker.
                return
            if phase == "running":
                record.running_id = receipt
            record.applied = phase
            self.failures.pop(target, None)
            if self.records.get(target) is record:
                if phase != "running" and record.desired == phase:
                    self.records.pop(target)
                self._save()
            self._resolve(target, phase if phase == "running" else None)
        except Exception:
            failures = min(self.failures.get(target, 0) + 1, 8)
            self.failures[target] = failures
            self.retry_after[target] = self.now() + max(self.interval, min(300, 2 ** failures))
            log.warning("Indicator update deferred (%s); durable intent retained", phase)
            # The caller waits only for this attempt. A failed transport must
            # not hold it until a retry succeeds; the durable intent below is
            # replayed by the worker without another caller.
            self._resolve(target, phase if phase == "running" else None)

    async def run(self) -> None:
        while not self.draining:
            self.schedule_once()
            await asyncio.sleep(self.interval)

    def stop_scheduling(self) -> None:
        self.draining = True
        self.closed = True  # Later updates still persist intent, but add no waiters.

    async def drain(self, *, timeout: float = 10.0) -> None:
        self.stop_scheduling()
        workers = list(self._inflight.values())
        if workers:
            _, pending = await asyncio.wait(workers, timeout=timeout)
            for worker in pending:
                worker.cancel()  # Only shutdown stragglers; the saved intent is replayable.
            await asyncio.gather(*workers, return_exceptions=True)
        try:
            self._save()
        except OSError:
            log.warning("Could not persist drained indicator state; prior intent retained")

    def close(self) -> None:
        self.closed = True
        for requests in self.waiters.values():
            for _, future in requests:
                if not future.done():
                    future.set_exception(RuntimeError("indicator worker stopped"))
        self.waiters.clear()
