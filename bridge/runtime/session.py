"""Transport-neutral Pi conversation lifecycle and output records."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from contextlib import asynccontextmanager
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import AsyncIterator, Protocol

from bridge.modes import ModelSelection, ModeSwitchError, mode_label

from .pi_rpc import (JsonObject, PiProviderError, PiRpcError, PiRpcTimeout, PiRunResult, RpcImage,
                     assistant_text_from_message)
from .state import Checkpoint, SessionKey, read_checkpoint, write_checkpoint
from ..observability import record_bridge_failure
from ..telemetry import Telemetry
from ..turn_status import TurnStatus
from ..workload import aggregate


MODE_SWITCH_TIMEOUT_SECONDS = 5

_ACTIVE_STATUSES = frozenset({"running", "cancel_requested", "final_pending"})

# The control marker Pi returns to answer a ping with a reaction alone. It is
# never chat content: a reply that carries both text and the marker posts the
# text and drops the marker, unless the turn already spoke for itself through a
# chat tool. In that case the marker is authoritative and the trailing text is
# narration about the turn, not a reply worth posting.
PASS_MARKER = "[[bridge:pass]]"


IDLE_MS = 180_000
# A burst of tool starts should cost one transport edit, not one per tool. The
# first progress edit of a turn is delivered at once; updates that arrive inside
# this window replace each other and the newest is sent when the window closes.
# A live turn spends roughly one write per interval on progress edits, and the
# homeserver budget is finite and shared with the acknowledgement and the final
# reply. Synapse's stock `rc_message` sustains 0.2 writes/s per identity, so a
# two-second cadence (0.5/s) never lets the bucket refill and every later write
# in the turn sees 429. Five seconds is exactly the stock sustained rate and
# leaves the acknowledgement and the reply room behind it.
PROGRESS_EDIT_INTERVAL = 5.0
PROGRESS_SUMMARY_INTERVAL = 120.0
PROGRESS_SUMMARY_TIMEOUT = 30.0
PROGRESS_SUMMARY_GRACE = 2.0
PROGRESS_SUMMARY_MAX_CALLS = 12
# Leave room for a live tool status inside Discord's message limit.
PROGRESS_SUMMARY_CARD_LIMIT = 1700
# Model-provider 5xx errors are usually transient. Retry a bounded number of
# times with exponential backoff before giving up and asking the user to retry.
PROVIDER_RETRY_MAX_ATTEMPTS = 3
PROVIDER_RETRY_BASE_SECONDS = 1.0
PROVIDER_RETRY_MAX_SECONDS = 30.0


def auth_failure_text(error: PiProviderError) -> str:
    """Say that the mode's model sign-in died, not that the request failed.

    A rejected OAuth credential never reaches the model, so retrying is
    pointless until the credential is renewed. Name the configured provider
    only; Pi's diagnostic text stays private.
    """
    provider = f"the sign-in for {error.provider}" if error.provider else \
        "the sign-in for this mode's model provider"
    return (f"The {provider} is no longer valid, so the request never reached the model. "
            "Re-authorize that provider's credential in Pi, then retry.")


# A rejected final reply (an unresolved handle or a broadcast ping) may be
# rewritten in the same resident Pi session. Bound attempts so a model cannot
# loop forever on invalid mention targets.
OUTPUT_REVISION_MAX_ATTEMPTS = 2
log = logging.getLogger("agent_bridge.runtime")


class ReplyRejected(ValueError):
    """Private delivery feedback for a final reply that must be rewritten."""


class StopRequested(RuntimeError):
    """A control write gave up its retries because a stop or cancel is queued.

    The runtime flags the scope *before* it queues on the scope lock, so a write
    that is retrying inside that scope can release the lock instead of making an
    emergency stop, or an explicit cancel, wait out a rate-limited homeserver.
    """


_PUBLIC_TOPICS = (
    ("code", ("code", "python", "pull request", "git", "test", "bug")),
    ("infrastructure", ("deploy", "cluster", "container", "kubernetes", "runtime")),
    ("research", ("research", "search", "documentation", "investigat")),
    ("writing", ("write", "draft", "document", "notes")),
    ("planning", ("plan", "agenda", "meeting", "scope")),
    ("creative work", ("image", "art", "model", "design")),
)


def split_pass(text: str) -> tuple[str, bool]:
    """Return the reply without standalone pass markers, and whether one was there."""
    lines = text.splitlines()
    kept = [line for line in lines if line.strip() != PASS_MARKER]
    return "\n".join(kept).strip(), len(kept) != len(lines)


def _topic_labels(text: str) -> list[str]:
    lowered = text.lower()
    return [label for label, clues in _PUBLIC_TOPICS
            if any(clue in lowered for clue in clues)]


async def _deliver_progress(queue: asyncio.Queue[str],
                            send: Callable[[str], Awaitable[None]]) -> None:
    """Deliver queued progress updates, coalescing bursts into one edit.

    Every ``get()`` is balanced by exactly one ``task_done()`` — including when
    the task is cancelled mid-coalesce-wait — so a future ``join()`` never hangs
    on a leaked unfinished task.
    """
    last_sent: float | None = None
    while True:
        message = await queue.get()
        try:
            # Coalesce a burst: while the throttle window is open, keep
            # taking newer updates and drop the ones they supersede.
            while last_sent is not None:
                delay = last_sent + PROGRESS_EDIT_INTERVAL - time.monotonic()
                if delay <= 0:
                    break
                try:
                    incoming = await asyncio.wait_for(queue.get(), delay)
                except asyncio.TimeoutError:
                    break
                queue.task_done()
                message = incoming
            try:
                await send(message)
                last_sent = time.monotonic()
            except Exception:
                # A failed progress edit must not cancel the model turn.
                pass
        finally:
            queue.task_done()


def _archive_transcripts(directory: Path, files: list[Path]) -> None:
    if not files:
        return
    archive = directory / "archive"
    archive.mkdir(mode=0o700, exist_ok=True)
    archive.chmod(0o700)
    for source in files:
        if source.exists():
            source.replace(archive / source.name)


class RpcClient(Protocol):
    @property
    def active(self) -> bool: ...

    async def prompt(self, message: str, on_event: Callable[[JsonObject], Awaitable[None]],
                     images: list[RpcImage] | None = None, *,
                     on_accepted: Callable[[], Awaitable[None]] | None = None) -> PiRunResult: ...

    async def steer(self, message: str, images: list[RpcImage] | None = None) -> None: ...

    async def abort(self) -> None: ...

    async def compact(self) -> str | None: ...

    async def stop(self) -> None: ...

    async def set_model(self, provider: str, model: str) -> None: ...


# Only an explicit True confirms a complete final post. Legacy/no-op sinks
# may return None; returning normally alone is not a delivery receipt.
Output = Callable[[SessionKey, str, str, str], Awaitable[bool | None]]
ClientFactory = Callable[[SessionKey, Path], RpcClient]
ImageCaptioner = Callable[[SessionKey, tuple[RpcImage, ...]], Awaitable[str]]
ProgressSummarizer = Callable[[SessionKey, list[str]], Awaitable[str]]


@dataclass(frozen=True)
class Incoming:
    sender: str
    message_id: str
    text: str
    images: tuple[RpcImage, ...] = ()
    optional: bool = False
    # Another bridge's message wakes this agent but expects no reaction: peer
    # traffic acknowledges itself in its own turn. A ping from a person keeps
    # the encouragement in the prompt and nothing more.
    ack_required: bool = True
    mode: str | None = None

    def prompt(self) -> str:
        prompt = f"Message from {self.sender} (event {self.message_id}):\n{self.text}"
        if self.optional:
            prompt += ("\n\nThis message arrived while you were awake without a direct ping. "
                       "Use the conversation context to decide whether to answer. "
                       "If no reply is useful, return exactly [[bridge:pass]] and nothing else.")
        elif self.ack_required:
            prompt += ("\n\nA reaction on this message acknowledges the ping when you have "
                       "nothing to add. That is polite and encouraged, but it is not required; "
                       "reply, react, or return exactly [[bridge:pass]] when no answer is useful.")
        return prompt


@dataclass
class _Scope:
    key: SessionKey
    directory: Path
    checkpoint: Checkpoint
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    input_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    accepted: asyncio.Event = field(default_factory=asyncio.Event)
    client: RpcClient | None = None
    mode: str = "fast"
    mode_switch: asyncio.Task[None] | None = None
    task: asyncio.Task[None] | None = None
    last_persist_ms: int = 0
    current_optional: bool = False
    required_ids: set[str] = field(default_factory=set)
    reacted_ids: set[str] = field(default_factory=set)
    status_ids: dict[str, int] = field(default_factory=dict)
    steer_epoch: int | None = None
    # True once the agent posted a message itself in this conversation during
    # the current turn, which makes a later pass marker authoritative over any
    # text returned beside it.
    self_spoken: bool = False
    direct_reply_text: str = ""
    optional_steer_after_direct_reply: bool = False
    current_tool: str = ""
    progress_request_id: str = ""
    progress_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    progress_context: deque[str] = field(default_factory=lambda: deque(maxlen=3))
    progress_request_text: str = ""
    progress_summaries: list[str] = field(default_factory=list)
    progress_status: str = "Still working…"
    progress_dirty: bool = False
    current_topics: list[str] = field(default_factory=list)
    compacting: bool = False
    compaction_finished: asyncio.Event = field(default_factory=asyncio.Event)
    # Set before a cancel or emergency stop queues on `lock`, so a rate-limited
    # control write holding that lock can abandon its retries and release it.
    # Keyed by the requester the stop was authorized for, so the signal reaches
    # only that requester's own in-flight write, and counted rather than flagged
    # because two stops can overlap on one scope.
    stop_requests: dict[str, int] = field(default_factory=dict)

    @property
    def active(self) -> bool:
        return self.task is not None and not self.task.done()


class BridgeRuntime:
    """One live Pi RPC process per ordinary chat scope, with summary-only revival."""

    def __init__(self, state_root: Path, client_factory: ClientFactory, output: Output,
                 *, now_ms: Callable[[], int] | None = None,
                 idle_ms: int = IDLE_MS,
                 image_captioner: ImageCaptioner | None = None,
                 progress_summarizer: ProgressSummarizer | None = None,
                 turn_status: Callable[[SessionKey, str, TurnStatus, int], Awaitable[None]] | None = None,
                 sleep: Callable[[float], Awaitable[None]] | None = None,
                 failure_outbox: Path | None = None,
                 modes: dict[str, ModelSelection] | None = None,
                 default_mode: str = "fast") -> None:
        self.modes = dict(modes or {})
        self.default_mode = default_mode
        if self.modes and default_mode not in self.modes:
            raise ValueError("default_mode must name a configured mode")
        self.state_root = state_root
        self.client_factory = client_factory
        self.output = output
        self.turn_status = turn_status
        self.now_ms = now_ms or (lambda: int(time.time() * 1000))
        self.idle_ms = idle_ms
        self.image_captioner = image_captioner
        self.progress_summarizer = progress_summarizer
        self.sleep = sleep or asyncio.sleep
        self.failure_outbox = failure_outbox
        self.telemetry = Telemetry()
        self._operations = 0
        self._operation_tasks: set[asyncio.Task] = set()
        self._closing = False
        self._scopes: dict[SessionKey, _Scope] = {}
        self._manager_lock = asyncio.Lock()
        self._checkpoint_lock = asyncio.Lock()
        self._launch_lock = asyncio.Lock()
        self._external_idle = asyncio.Event()
        self._external_idle.set()

    @asynccontextmanager
    async def external_job(self) -> AsyncIterator[bool]:
        """Reserve this agent only when no bridge Pi turn is running.

        New submissions wait while the reservation is held. The worker retries
        later when an existing turn is still active; it never steers that turn.
        """
        reserved = False
        async with self._launch_lock:
            if (not self._closing and not self._operations and self._external_idle.is_set() and
                    not any(scope.active or scope.checkpoint.status in _ACTIVE_STATUSES
                            for scope in self._scopes.values())):
                self._external_idle.clear()
                reserved = True
        try:
            yield reserved
        finally:
            if reserved:
                self._external_idle.set()

    async def _wait_for_compaction(self, scope: _Scope,
                                   on_wait: Callable[[], Awaitable[None]] | None = None) -> None:
        if scope.compacting and on_wait is not None:
            try:
                await on_wait()
            except Exception:
                # A notice failure must not lose input or cancel compaction.
                log.warning("Could not send compaction wait notice agent=%s transport=%s",
                            scope.key.agent, scope.key.transport)
        while scope.compacting:
            # Waiting on an event cannot cancel the Pi turn or idle sweep.
            await scope.compaction_finished.wait()

    @asynccontextmanager
    async def _locked_scope(self, scope: _Scope, *, wait_compaction: bool = False,
                            on_wait: Callable[[], Awaitable[None]] | None = None) -> AsyncIterator[None]:
        waiting_since = time.monotonic()
        while True:
            if wait_compaction:
                await self._wait_for_compaction(scope, on_wait)
            await scope.lock.acquire()
            if not wait_compaction or not scope.compacting:
                break
            # A new compaction began while we acquired the lock. Release it
            # before waiting so cancel/emergency stop remain able to proceed.
            scope.lock.release()
        try:
            waited = time.monotonic() - waiting_since
            self.telemetry.record(scope.key.transport, "lock_wait", "acquired", seconds=waited,
                                  scope_hash=scope.key.scope_hash, epoch=scope.checkpoint.epoch)
            if waited >= 1:
                log.info("Bridge scope lock wait agent=%s transport=%s seconds=%.3f",
                         scope.key.agent, scope.key.transport, waited)
            yield
        finally:
            scope.lock.release()

    async def _scope(self, key: SessionKey) -> _Scope:
        async with self._manager_lock:
            if key not in self._scopes:
                directory = key.directory(self.state_root)
                try:
                    state = read_checkpoint(directory)
                except (ValueError, TypeError, OSError):
                    log.exception("Damaged bridge checkpoint in %s", directory)
                    archive = directory / "archive"
                    archive.mkdir(mode=0o700, exist_ok=True)
                    archive.chmod(0o700)
                    checkpoint_file = directory / "state.json"
                    if checkpoint_file.exists():
                        checkpoint_file.replace(archive / f"corrupt-state-{time.time_ns()}.json")
                    _archive_transcripts(directory, list(directory.glob("conversation-*.jsonl")))
                    state = Checkpoint(status="checkpoint_error")
                    write_checkpoint(directory, state)
                if state.status in {"running", "cancel_requested", "final_pending"}:
                    # A restarted bridge never trusts an old process or its transcript.
                    _archive_transcripts(directory, [
                        directory / f"conversation-{state.epoch:08d}.jsonl"])
                    state.status = "interrupted"
                    state.final_sent = False
                    write_checkpoint(directory, state)
                self._scopes[key] = _Scope(key, directory, state, mode=self.default_mode)
            return self._scopes[key]

    def _touch(self, scope: _Scope, *, persist: bool = False) -> None:
        now = self.now_ms()
        scope.checkpoint.last_activity_ms = now
        if persist or now - scope.last_persist_ms >= 5000:
            write_checkpoint(scope.directory, scope.checkpoint)
            scope.last_persist_ms = now

    async def _reset_mode(self, scope: _Scope) -> None:
        """Retire an uncertain/removed selection without leaving the room blocked."""
        client, scope.client = scope.client, None
        scope.mode = self.default_mode
        if client is not None:
            await client.stop()
        _archive_transcripts(scope.directory, [
            scope.directory / f"conversation-{scope.checkpoint.epoch:08d}.jsonl"])

    def _mode_failure_notice(self, failed_mode: str, operation: str) -> str:
        detail = ("The configured default itself is unavailable; check its configuration "
                  "or choose another configured mode." if failed_mode == self.default_mode else
                  "The session has reset to its configured default; the next request will use it.")
        return f"Pi could not {operation}; no request was submitted. {detail}"

    async def _ensure_client(self, scope: _Scope) -> RpcClient:
        if self.modes and scope.mode not in self.modes:
            await self._reset_mode(scope)
        if scope.client is None:
            scope.checkpoint.epoch += 1
            session_file = scope.directory / f"conversation-{scope.checkpoint.epoch:08d}.jsonl"
            scope.client = self.client_factory(scope.key, session_file)
            write_checkpoint(scope.directory, scope.checkpoint)
            # Apply at the sole creation point, including provider-error retries.
            # This validates the configured selection before any prompt runs.
            if self.modes:
                failed_mode = scope.mode
                try:
                    await self._apply_model(scope, failed_mode)
                except (PiRpcError, OSError, asyncio.CancelledError) as error:
                    await self._reset_mode(scope)
                    if isinstance(error, (PiRpcError, OSError)):
                        raise ModeSwitchError(self._mode_failure_notice(failed_mode, "start the selected mode")) from error
                    raise
        return scope.client

    async def _apply_model(self, scope: _Scope, name: str) -> None:
        """A bounded control operation that queued stops can interrupt under lock."""
        assert scope.client is not None
        selection = self.modes[name]
        task = asyncio.create_task(scope.client.set_model(selection.provider, selection.model))
        scope.mode_switch = task
        try:
            await asyncio.wait_for(task, MODE_SWITCH_TIMEOUT_SECONDS)
        except TimeoutError as error:
            raise PiRpcTimeout(MODE_SWITCH_TIMEOUT_SECONDS) from error
        except asyncio.CancelledError:
            if self.stop_requested(scope.key, scope.checkpoint.requester):
                raise StopRequested("Mode selection interrupted by a queued stop") from None
            raise
        finally:
            scope.mode_switch = None

    async def _select_mode(self, scope: _Scope, name: str, requester: str = "", *,
                           take_ownership: bool = False) -> str:
        # Caller owns input_lock and the non-compacting scope lock. Do not
        # interrupt a tool, alter another request's next call, or replay work.
        if name not in self.modes:
            raise ModeSwitchError(f"Mode '{name}' is not configured for this bridge.")
        if scope.active:
            raise ModeSwitchError("A turn is running here. Let it finish or cancel it before switching modes; this request was not submitted.")
        if scope.checkpoint.status == "checkpoint_error":
            raise ModeSwitchError("Recover this conversation's failed checkpoint before switching modes.")
        selection = self.modes[name]
        # Only a mode-bearing turn owns the pending switch; a bare control
        # must leave the previous turn's requester alone.
        if take_ownership:
            scope.checkpoint.requester = requester
        previous = scope.mode
        # Never reuse an uncertain client. Failed attempts reset to the owner's
        # configured default, which the next client validates before prompting.
        scope.mode = name
        if scope.client is not None and previous != name:
            try:
                await self._apply_model(scope, name)
            except StopRequested:
                await self._reset_mode(scope)
                raise
            except (PiRpcError, OSError, asyncio.CancelledError) as error:
                await self._reset_mode(scope)
                if isinstance(error, (PiRpcError, OSError)):
                    raise ModeSwitchError(self._mode_failure_notice(name, "confirm the mode switch")) from error
                raise
        self.telemetry.record(scope.key.transport, "mode", name, scope_hash=scope.key.scope_hash)
        self._touch(scope, persist=True)
        pending = " · next wake" if scope.client is None else ""
        return f"{mode_label(name)} mode · {selection.provider}/{selection.model}{pending}"

    async def change_mode(self, key: SessionKey, name: str, requester: str = "") -> str:
        """Bare command: remember the next selection without starting inference."""
        async with self._operation_slot():
            scope = await self._scope(key)
            async with scope.input_lock, self._locked_scope(scope, wait_compaction=True):
                return await self._select_mode(scope, name, requester)

    def _record_pi_failure(self, scope: _Scope, error: PiRpcError, phase: str,
                            *, request_id: str | None = None,
                            matrix_event_id: str | None = None) -> str | None:
        if self.failure_outbox is None:
            return None
        try:
            return record_bridge_failure(self.failure_outbox, scope.key, error,
                                         epoch=scope.checkpoint.epoch, phase=phase,
                                         request_id=request_id,
                                         matrix_event_id=matrix_event_id)
        except (OSError, ValueError):
            # Observability must never turn a recoverable model failure into a
            # new bridge failure or interrupt chat delivery.
            log.warning("Could not record Pi failure agent=%s transport=%s",
                        scope.key.agent, scope.key.transport)
            return None

    async def is_awake(self, key: SessionKey) -> bool:
        scope = self._scopes.get(key)
        # The idle sweep closes the live process after the three-minute window.
        # Until it does, this scope still has its conversation in memory.
        return bool(scope and scope.client is not None)

    async def needs_context(self, key: SessionKey) -> bool:
        """A removed selection will retire Pi even while the scope appears awake."""
        scope = await self._scope(key)
        return scope.client is None or bool(self.modes and scope.mode not in self.modes)

    async def admit(self, key: SessionKey, can_wake: bool, *,
                    on_wait: Callable[[], Awaitable[None]] | None = None) -> bool:
        """Wait for checkpointing before admitting and reading the new context."""
        scope = await self._scope(key)
        was_awake = scope.client is not None
        async with self._locked_scope(scope, wait_compaction=True, on_wait=on_wait):
            awake = scope.client is not None
            # A follow-up received while awake remains eligible even if the
            # compaction we waited for has now closed that process.
            if not awake and not can_wake and not was_awake:
                return False
            if awake:
                self._touch(scope, persist=True)
            return True

    async def last_wake(self, key: SessionKey) -> tuple[str, int]:
        scope = await self._scope(key)
        return scope.checkpoint.last_wake_id, scope.checkpoint.summary_saved_ms

    async def record_wake(self, key: SessionKey, event_id: str) -> None:
        scope = await self._scope(key)
        async with self._locked_scope(scope):
            scope.checkpoint.last_wake_id = event_id
            write_checkpoint(scope.directory, scope.checkpoint)

    async def discord_recovery_state(self, key: SessionKey) -> tuple[str, str, str]:
        """Observed watermark, last accepted ID, and status for one Discord scope."""
        if key.transport != "discord":
            raise ValueError("Discord recovery requires a Discord scope")
        scope = await self._scope(key)
        state = scope.checkpoint
        accepted = state.last_message_id if state.last_message_id.isdecimal() else ""
        return state.last_observed_message_id, accepted, state.status

    async def record_discord_observed(self, key: SessionKey, message_id: str) -> None:
        """Persist only after the input was handled; never turn observation into activity."""
        if key.transport != "discord" or not message_id.isdecimal():
            raise ValueError("Discord observation requires a numeric Discord message ID")
        scope = await self._scope(key)
        async with self._locked_scope(scope):
            previous = scope.checkpoint.last_observed_message_id
            if not previous or int(message_id) > int(previous):
                scope.checkpoint.last_observed_message_id = message_id
                write_checkpoint(scope.directory, scope.checkpoint)

    async def record_discord_gap(self, key: SessionKey, reason: str,
                                 boundary: int, skipped: str) -> bool:
        """Durably name a bounded catch-up skip; True only for a new incident."""
        if (key.transport != "discord" or
                reason not in {"age", "count", "first_enable", "fresh_install", "permission", "not_found",
                               "transient", "limit", "discovery", "thread_discovery", "error",
                               "hold_expired", "thread_hold_expired"}
                or boundary < 0):
            raise ValueError("Invalid Discord recovery gap")
        prefix = ("baseline_reset" if reason in {"age", "count", "first_enable", "fresh_install",
                                                 "hold_expired", "thread_hold_expired"}
                  else "recovery_paused")
        marker = f"{prefix} reason={reason} boundary={boundary} skipped={skipped}"
        if len(marker) > 200:
            raise ValueError("Discord recovery gap marker is too long")
        scope = await self._scope(key)
        async with self._locked_scope(scope):
            prior = scope.checkpoint.discord_recovery_gap
            if prior == marker:
                return False
            scope.checkpoint.discord_recovery_gap = marker
            write_checkpoint(scope.directory, scope.checkpoint)
            if prefix == "recovery_paused":
                return not prior.startswith("recovery_paused ")
            return not prior.startswith(f"{prefix} reason={reason} ")

    async def clear_discord_gap(self, key: SessionKey) -> None:
        scope = await self._scope(key)
        async with self._locked_scope(scope):
            if scope.checkpoint.discord_recovery_gap:
                scope.checkpoint.discord_recovery_gap = ""
                write_checkpoint(scope.directory, scope.checkpoint)

    async def record_reaction(self, key: SessionKey, message_id: str) -> None:
        """Count a delivered reaction as an answer to this turn's direct ping."""
        scope = await self._scope(key)
        async with self._locked_scope(scope):
            if scope.active and message_id in scope.required_ids:
                scope.reacted_ids.add(message_id)
            self._touch(scope, persist=True)

    async def record_self_message(self, key: SessionKey) -> None:
        """Note that the agent posted a message itself during the current turn.

        A turn that answers through a chat tool and then returns text beside
        ``[[bridge:pass]]`` is narrating its own work, not talking to the room,
        so the marker decides and the narration is dropped.
        """
        scope = self._scopes.get(key)
        if scope is None or not scope.active:
            return
        async with self._locked_scope(scope):
            if scope.active:
                scope.self_spoken = True
            self._touch(scope, persist=True)

    async def submit(self, key: SessionKey, incoming: Incoming) -> str:
        # Agent-owned background jobs (such as PR reviews) must never create a
        # second Pi session beside an ordinary bridge turn. Admit new turns
        # only after the external job releases the agent-wide gate.
        started = time.monotonic()
        self.telemetry.inflight[key.transport, "input"] += 1
        try:
            async with self._operation_slot():
                outcome = await self._submit_unlocked(key, incoming)
                self.telemetry.record(key.transport, "submit", outcome, seconds=time.monotonic() - started,
                                      scope_hash=key.scope_hash)
                return outcome
        except (Exception, asyncio.CancelledError) as error:
            self.telemetry.record(key.transport, "submit", "cancelled" if isinstance(error, asyncio.CancelledError) else "error",
                                  seconds=time.monotonic() - started, scope_hash=key.scope_hash)
            raise
        finally:
            self.telemetry.inflight[key.transport, "input"] -= 1

    @asynccontextmanager
    async def _operation_slot(self) -> AsyncIterator[None]:
        # Only admission is global. Slow work holds a reservation count, not
        # the launch lock, so unrelated scopes proceed but external jobs wait.
        while True:
            if self._closing:
                raise RuntimeError("Bridge is closing")
            await self._external_idle.wait()
            async with self._launch_lock:
                if self._external_idle.is_set():
                    if self._closing:
                        raise RuntimeError("Bridge is closing")
                    self._operations += 1
                    break
        task = asyncio.current_task()
        if task is not None:
            self._operation_tasks.add(task)
        try:
            yield
        finally:
            self._operations -= 1
            if task is not None:
                self._operation_tasks.discard(task)

    async def _submit_unlocked(self, key: SessionKey, incoming: Incoming) -> str:
        scope = await self._scope(key)
        async with scope.input_lock:
            try:
                return await self._submit_in_scope(scope, incoming)
            except StopRequested:
                # An acknowledgement gave way to a queued cancel or stop and
                # released the scope lock on its way out. Resolve the indicator it
                # had already claimed here, with the lock the stop was waiting for
                # out of the picture, and with the same terminal a stopped run
                # records for itself; the write itself resolves nothing under the
                # lock. A request that never started owns no other state.
                if incoming.ack_required:
                    await self._report_turn_status(key, incoming.message_id, "failed",
                                                   scope.checkpoint.epoch)
                raise

    async def _submit_in_scope(self, scope: _Scope, incoming: Incoming) -> str:
        key = scope.key

        async def compaction_notice() -> None:
            await self.output(key, "compacting", "Compacting, one moment...", incoming.message_id)

        await self._wait_for_compaction(scope, compaction_notice)
        state = scope.checkpoint
        if state.status == "checkpoint_error":
            raise RuntimeError("This conversation needs checkpoint recovery before new work")
        if incoming.message_id == state.last_message_id:
            return "duplicate"
        if self.modes and scope.mode not in self.modes:
            # A live reload may remove a sleeping or resident selection. Finish
            # existing work, then use the configured default for the next turn.
            if scope.active and scope.task is not None:
                try:
                    await asyncio.shield(scope.task)
                except asyncio.CancelledError:
                    current = asyncio.current_task()
                    if current is not None and current.cancelling():
                        raise  # Only the old turn's cancellation is recoverable here.
            async with self._locked_scope(scope, wait_compaction=True):
                if scope.mode not in self.modes:
                    await self._reset_mode(scope)
        mode_notice = ""
        if incoming.mode is not None:
            if incoming.optional:
                raise ModeSwitchError("Mode selection requires an explicit mode command.")
            async with self._locked_scope(scope, wait_compaction=True, on_wait=compaction_notice):
                mode_notice = await self._select_mode(scope, incoming.mode, incoming.sender,
                                                      take_ownership=True)
        scope.current_topics = _topic_labels(incoming.text[:4000])
        image_prepared_here = bool(incoming.images) and self.image_captioner is not None
        if image_prepared_here:
            # Caption inference is owned work too. Persist admission before
            # cosmetic waits or inference, without consuming unadmitted waits.
            state.last_message_id = incoming.message_id
        self._touch(scope, persist=True)
        image_ack_sent = False
        image_status_epoch = state.epoch
        if incoming.images and self.image_captioner is not None:
            if not incoming.optional:
                if incoming.ack_required:
                    await self._report_turn_status(key, incoming.message_id, "running", image_status_epoch)
                try:
                    await self.output(key, "ack", (mode_notice + "\n" if mode_notice else "") +
                                      "I’m reading the image…", incoming.message_id)
                except StopRequested:
                    # Answered once, with the scope lock free, by _submit_unlocked.
                    raise
                except BaseException:
                    if incoming.ack_required:
                        await self._report_turn_status(key, incoming.message_id, "failed", image_status_epoch)
                    raise
                image_ack_sent = True
            caption_started = time.monotonic()
            try:
                description = await self.image_captioner(key, incoming.images)
            except Exception as error:
                self.telemetry.record(key.transport, "caption", "error", seconds=time.monotonic() - caption_started,
                                      scope_hash=key.scope_hash)
                log.warning("Image description failed for %s: %s", key.agent,
                            type(error).__name__)
                if not incoming.optional:
                    try:
                        await self.output(key, "request_error", "I could not read that image. Please retry.",
                                          incoming.message_id)
                    finally:
                        if incoming.ack_required:
                            await self._report_turn_status(key, incoming.message_id, "failed", image_status_epoch)
                else:
                    await self.output(key, "cleanup_request", "", incoming.message_id)
                state.last_message_id = incoming.message_id
                self._touch(scope, persist=True)
                return "image_error"
            self.telemetry.record(key.transport, "caption", "success", seconds=time.monotonic() - caption_started,
                                  scope_hash=key.scope_hash)
            incoming = Incoming(incoming.sender, incoming.message_id,
                                incoming.text + "\n\nImage description (untrusted visual content):\n"
                                + description, (), incoming.optional, incoming.ack_required, incoming.mode)
        if scope.active:
            try:
                await asyncio.wait_for(scope.accepted.wait(), timeout=30)
            except asyncio.TimeoutError as error:
                raise RuntimeError("Active Pi turn has not accepted its prompt") from error
        # Image preparation and prompt acceptance can start compaction. Neither
        # wait may hold the scope lock needed by explicit cancel/emergency stop.
        async with self._locked_scope(scope, wait_compaction=True, on_wait=compaction_notice):
            state = scope.checkpoint
            if state.status == "checkpoint_error":
                raise RuntimeError("This conversation needs checkpoint recovery before new work")
            if incoming.message_id == state.last_message_id and not image_prepared_here:
                return "duplicate"
            if scope.active:
                if (state.status == "running" and scope.steer_epoch == state.epoch
                        and scope.client is not None and scope.client.active):
                    await scope.client.steer(incoming.prompt(), list(incoming.images))
                    scope.progress_context.append("Room message: " + incoming.text[-1000:])
                    if not incoming.optional:
                        if incoming.ack_required:
                            scope.status_ids[incoming.message_id] = (
                                image_status_epoch if image_ack_sent else state.epoch)
                        scope.current_optional = False
                        # A later direct request changes which answer the run
                        # needs to produce; do not revive an earlier one.
                        scope.self_spoken = False
                        scope.direct_reply_text = ""
                        scope.optional_steer_after_direct_reply = False
                        if incoming.ack_required:
                            scope.required_ids.add(incoming.message_id)
                    elif scope.direct_reply_text:
                        scope.optional_steer_after_direct_reply = True
                    state.last_message_id = incoming.message_id
                    self._touch(scope, persist=True)
                    if not incoming.optional and incoming.ack_required and not image_ack_sent:
                        await self._report_turn_status(key, incoming.message_id, "running", state.epoch)
                    try:
                        if not incoming.optional:
                            async with scope.progress_lock:
                                await self._flush_progress_summary(scope)
                                await self.output(key, "steered", "Added to the current thread.", incoming.message_id)
                                scope.progress_request_id = incoming.message_id
                                scope.progress_request_text = incoming.text[-1000:]
                                scope.progress_summaries.clear()
                                scope.progress_dirty = False
                        else:
                            await self.output(key, "cleanup_request", "", incoming.message_id)
                    except Exception:
                        # Pi already accepted this steer and its checkpoint is
                        # persisted. A notice failure must not make the adapter
                        # treat the accepted request as failed or replay it.
                        log.exception("Steer accepted but delivery notice failed for %s", key.agent)
                    return "steered"
                if scope.task is not None:
                    await scope.task
            new_client = scope.client is None
            if new_client:
                state.requester = incoming.sender
            previous_epoch = state.epoch
            client = await self._ensure_client(scope)
            # A reload during awaited preparation can replace a resident client.
            # Prefix the saved summary based on actual creation, not an old snapshot.
            new_client = state.epoch != previous_epoch
            prompt = incoming.prompt()
            if new_client and state.summary:
                prompt = ("Previous conversation summary (private checkpoint):\n"
                          + state.summary + "\n\nNew " + prompt)
            state.status = "running"
            state.requester = incoming.sender
            state.request_id = incoming.message_id
            state.last_message_id = incoming.message_id
            state.final_sent = False
            scope.current_optional = incoming.optional
            scope.current_tool = ""
            scope.progress_request_id = incoming.message_id
            scope.progress_context.clear()
            scope.progress_request_text = incoming.text[-1000:]
            scope.progress_summaries.clear()
            scope.progress_status = "Still working…"
            scope.progress_dirty = False
            scope.required_ids = (set() if incoming.optional or not incoming.ack_required
                                  else {incoming.message_id})
            scope.reacted_ids = set()
            scope.status_ids = ({} if incoming.optional or not incoming.ack_required else
                                {incoming.message_id: image_status_epoch if image_ack_sent else state.epoch})
            scope.self_spoken = False
            scope.direct_reply_text = ""
            scope.optional_steer_after_direct_reply = False
            self._touch(scope, persist=True)
            if not image_ack_sent and not incoming.optional:
                if incoming.ack_required:
                    await self._report_turn_status(key, incoming.message_id, "running", state.epoch)
                try:
                    await self.output(key, "ack", (mode_notice.removesuffix(" · next wake") + "\n" if mode_notice else "") +
                                      "I’m working on it.", incoming.message_id)
                except StopRequested:
                    # Answered once, with the scope lock free, by _submit_unlocked:
                    # this write released the lock a queued stop was waiting for,
                    # and must not take it again to report itself.
                    raise
                except BaseException:
                    if incoming.ack_required:
                        await self._report_turn_status(key, incoming.message_id, "failed", state.epoch)
                    raise
            scope.accepted.clear()
            scope.task = asyncio.create_task(self._run(scope, client, prompt,
                                                       list(incoming.images)))
            self._touch(scope, persist=True)
            return "started"

    async def _flush_progress_summary(self, scope: _Scope) -> None:
        """Caller holds progress_lock; flush pending history before a card handoff."""
        if scope.progress_dirty:
            text = "\n".join([scope.progress_status, *scope.progress_summaries])
            try:
                await asyncio.wait_for(
                    self.output(scope.key, "progress_summary", text, scope.progress_request_id),
                    PROGRESS_SUMMARY_GRACE)
                scope.progress_dirty = False
            except Exception:
                log.warning("Could not flush progress history for %s", scope.key.agent)

    async def _report_turn_status(self, key: SessionKey, request_id: str,
                                  status: TurnStatus, epoch: int) -> None:
        if self.turn_status is None:
            return
        try:
            timeout = 5 if status == "running" else 15
            await asyncio.wait_for(self.turn_status(key, request_id, status, epoch), timeout)
        except Exception:
            # Cosmetic failures must not fail/replay a prompt or its delivery.
            log.warning("Could not update bridge turn indicator (%s)", status)

    async def _run(self, scope: _Scope, client: RpcClient, prompt: str,
                   images: list[RpcImage]) -> None:
        state = scope.checkpoint
        request_id = state.request_id
        epoch = state.epoch
        status_ids = scope.status_ids  # Steers extend this turn's accepted-epoch map.
        terminal_status: TurnStatus = "failed"

        async def accepted() -> None:
            scope.accepted.set()

        # Pi events arrive on the RPC stdout reader. Transport edits may take
        # arbitrarily long, so keep only the latest waiting progress update and
        # deliver it from a separate task. The running edit is never overlapped.
        progress_queue: asyncio.Queue[str] = asyncio.Queue(maxsize=1)
        summary_lines = scope.progress_summaries
        recent_tools: deque[str] = deque(maxlen=8)
        active_tools = 0
        closing = False
        summary_inflight = False

        def queue_progress(message: str) -> None:
            scope.progress_status = message
            if progress_queue.full():
                progress_queue.get_nowait()
                progress_queue.task_done()
            progress_queue.put_nowait(message)

        async def deliver_progress() -> None:
            async def send(message: str) -> None:
                async with scope.progress_lock:
                    text = "\n".join([message, *summary_lines])
                    kind = "progress_summary" if summary_lines else "progress"
                    if scope.progress_dirty and len(text) > PROGRESS_SUMMARY_CARD_LIMIT:
                        # Rotate only for a new summary, never copy old history
                        # because a status label grew. The card limit leaves headroom.
                        # The adapter seals the old card before registering its replacement.
                        await self.output(scope.key, "ack", message, scope.progress_request_id)
                        summary_lines[:] = summary_lines[-1:]
                        text = "\n".join([message, *summary_lines])
                    await self.output(scope.key, kind, text, scope.progress_request_id)
                    scope.progress_dirty = False
                    self._touch(scope, persist=True)

            await _deliver_progress(progress_queue, send)

        # Track whether the current prompt attempt invoked any tool. If a
        # provider 5xx arrives after a tool execution, replaying the prompt
        # could duplicate side effects (card creation, file writes, etc.), so
        # that attempt must not be retried automatically.
        attempt_had_tool = False
        auto_compact_started: float | None = None

        async def on_event(event: JsonObject) -> None:
            nonlocal attempt_had_tool, auto_compact_started, active_tools
            kind = event.get("type")
            if kind in {"compaction_start", "auto_compaction_start"}:
                auto_compact_started = time.monotonic()
                scope.compaction_finished.clear()
                scope.compacting = True
                if not scope.current_optional:
                    if scope.progress_status != "Compacting, one moment...":
                        # Keep the active card's previous status when compaction takes over.
                        stamp = datetime.fromtimestamp(self.now_ms() / 1000, timezone.utc)
                        summary_lines.append(f"[{stamp:%H:%M UTC}] {scope.progress_status}")
                        scope.progress_dirty = True
                    queue_progress("Compacting, one moment...")
            elif kind in {"compaction_end", "auto_compaction_end"}:
                elapsed = time.monotonic() - auto_compact_started if auto_compact_started is not None else None
                outcome = "cancelled" if event.get("aborted") else "error" if event.get("errorMessage") else "completed"
                self.telemetry.record(scope.key.transport, "auto_compact", outcome, seconds=elapsed,
                                      scope_hash=scope.key.scope_hash, epoch=epoch)
                auto_compact_started = None
                scope.compacting = False
                scope.compaction_finished.set()
            if kind in {"message_update", "tool_execution_start", "tool_execution_end",
                        "compaction_start", "compaction_end", "auto_compaction_start", "auto_compaction_end"}:
                self._touch(scope)
            if kind == "tool_execution_start":
                attempt_had_tool = True
                active_tools += 1
                name = event.get("toolName")
                label = re.sub(r"[^A-Za-z0-9_. -]", "", name)[:48] if isinstance(name, str) else "tool"
                scope.current_tool = label
                recent_tools.append(label)
                if not scope.current_optional:
                    queue_progress(f"Using `{label or 'tool'}`…")
                self._touch(scope, persist=True)
            elif kind == "tool_execution_end":
                active_tools = max(0, active_tools - 1)
                scope.current_tool = ""
            if kind == "message_end":
                visible_text = assistant_text_from_message(event)
                if visible_text:
                    scope.progress_context.append("Agent message: " + visible_text[:1000])
            if kind == "message_end" and not scope.optional_steer_after_direct_reply:
                # Pi can finish the direct answer just before an ambient room
                # post is steered into the same run. Retain that complete answer:
                # Pi's final message may then be only [[bridge:pass]] for the
                # ambient post, which would otherwise erase the direct reply.
                direct_text, _ = split_pass(assistant_text_from_message(event))
                if direct_text and scope.required_ids:
                    scope.direct_reply_text = direct_text

        async def heartbeat() -> None:
            while True:
                await asyncio.sleep(30)
                if state.status not in {"running", "cancel_requested"}:
                    return
                if not scope.current_optional and not scope.compacting:
                    queue_progress("Still working…")

        async def summarize_progress() -> None:
            nonlocal summary_inflight
            next_summary = time.monotonic() + PROGRESS_SUMMARY_INTERVAL
            previous_context: list[str] | None = None
            calls = 0
            while not closing and calls < PROGRESS_SUMMARY_MAX_CALLS:
                await asyncio.sleep(max(0, next_summary - time.monotonic()))
                next_summary = time.monotonic() + PROGRESS_SUMMARY_INTERVAL
                if state.status != "running" or closing:
                    return
                if (scope.current_optional or scope.compacting or active_tools or
                        self.progress_summarizer is None):
                    continue
                target = scope.progress_request_id
                context = ["Current request: " + scope.progress_request_text,
                           *scope.progress_context,
                           "Recent tools (names only): " + ", ".join(recent_tools)]
                if context == previous_context:
                    continue
                previous_context = context
                calls += 1
                summary_inflight = True
                try:
                    summary = await asyncio.wait_for(
                        self.progress_summarizer(scope.key, context), PROGRESS_SUMMARY_TIMEOUT)
                    # A summary is plain text, never a mention or a command.
                    summary = " ".join(summary.split())[:180]
                    if not summary or "@" in summary or "[[bridge:" in summary:
                        continue
                    if (state.status != "running" or scope.current_optional or active_tools or
                            scope.progress_request_id != target):
                        continue
                    async with scope.progress_lock:
                        if scope.progress_request_id != target or active_tools:
                            continue
                        stamp = datetime.fromtimestamp(self.now_ms() / 1000, timezone.utc)
                        summary_lines.append(f"[{stamp:%H:%M UTC}] {summary}")
                        scope.progress_dirty = True
                        queue_progress(scope.progress_status)
                except Exception as error:
                    log.warning("Progress summary failed agent=%s error_class=%s",
                                scope.key.agent, type(error).__name__)
                finally:
                    summary_inflight = False

        run_started = time.monotonic()
        self.telemetry.inflight[scope.key.transport, "turn"] += 1
        progress_task = asyncio.create_task(deliver_progress())
        heartbeat_task = asyncio.create_task(heartbeat())
        summary_task = (asyncio.create_task(summarize_progress())
                        if self.progress_summarizer is not None else None)

        async def stop_progress() -> None:
            nonlocal closing
            closing = True
            heartbeat_task.cancel()
            if summary_task is not None and summary_inflight:
                try:
                    await asyncio.wait_for(asyncio.shield(summary_task), PROGRESS_SUMMARY_GRACE)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    pass
            tasks = [heartbeat_task, progress_task]
            if summary_task is not None:
                tasks.append(summary_task)
            for task in tasks:
                task.cancel()
            for task in tasks:
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            async with scope.progress_lock:
                await self._flush_progress_summary(scope)

        try:
            prompt_completed = False
            try:
                retried = False
                for attempt in range(1, PROVIDER_RETRY_MAX_ATTEMPTS + 1):
                    attempt_had_tool = False
                    try:
                        scope.steer_epoch = epoch
                        try:
                            result = await client.prompt(prompt, on_event, images,
                                                         on_accepted=accepted)
                        finally:
                            # Close admission immediately, without cutting off
                            # an in-flight summary's existing terminal grace.
                            if scope.steer_epoch == epoch:
                                scope.steer_epoch = None
                        break
                    except PiProviderError as error:
                        self.telemetry.record(scope.key.transport, "provider", "error", scope_hash=scope.key.scope_hash, epoch=epoch)
                        occurrence_id = self._record_pi_failure(scope, error, "prompt",
                                                               request_id=request_id,
                                                               matrix_event_id=request_id)
                        support_id = occurrence_id if occurrence_id else None
                        if (error.status is None or error.status < 500 or
                                attempt_had_tool or
                                attempt >= PROVIDER_RETRY_MAX_ATTEMPTS):
                            if support_id:
                                log.warning(
                                    "Bridge model provider error agent=%s transport=%s "
                                    "request_id=%s attempt=%d/%d status=%s support_id=%s; giving up",
                                    scope.key.agent, scope.key.transport, request_id,
                                    attempt, PROVIDER_RETRY_MAX_ATTEMPTS,
                                    error.status if error.status is not None else "none",
                                    support_id)
                            raise
                        retried = True
                        self.telemetry.record(scope.key.transport, "provider", "retry", scope_hash=scope.key.scope_hash, epoch=epoch)
                        log.warning(
                            "Bridge model provider error agent=%s transport=%s "
                            "request_id=%s attempt=%d/%d status=%d; retrying",
                            scope.key.agent, scope.key.transport, request_id,
                            attempt, PROVIDER_RETRY_MAX_ATTEMPTS, error.status)
                        # Discard the failed process and its transcript; a fresh
                        # process may see a different backend.
                        await client.stop()
                        _archive_transcripts(scope.directory, [
                            scope.directory / f"conversation-{epoch:08d}.jsonl"])
                        scope.client = None
                        # Own the next creation attempt even if validation fails.
                        # Keep stale-epoch suppression for genuinely newer work.
                        epoch = state.epoch + 1
                        client = await self._ensure_client(scope)
                        delay = min(
                            PROVIDER_RETRY_BASE_SECONDS * (2 ** (attempt - 1)),
                            PROVIDER_RETRY_MAX_SECONDS,
                        )
                        await self.sleep(delay)
                        if state.status == "cancel_requested":
                            # The user cancelled while we were waiting to retry.
                            # Do not start another attempt; clean up the fresh
                            # client and let the normal cancellation path run.
                            await client.stop()
                            _archive_transcripts(scope.directory, [
                                scope.directory / f"conversation-{epoch:08d}.jsonl"])
                            scope.client = None
                            raise PiRpcError("Request cancelled during provider retry backoff")
                prompt_completed = True
            finally:
                # No progress edit may overlap the final or error record.
                await stop_progress()
            if state.epoch != epoch:
                return  # Stale output from a previous process epoch.
            final = "Request cancelled." if state.status == "cancel_requested" else result.text.strip()
            kind = "cancelled" if state.status == "cancel_requested" else "final"
            passed = False
            if kind == "final":
                final, passed = split_pass(final)
                if passed and final and scope.self_spoken:
                    log.info("Dropped narration beside a pass marker after a tool-sent "
                             "reply: agent=%s transport=%s lines=%d",
                             scope.key.agent, scope.key.transport, len(final.splitlines()))
                    final = ""
                    scope.direct_reply_text = ""
            if not final and not passed:
                final = "The agent finished without a text reply."
            state.status = "final_pending"
            self._touch(scope, persist=True)
            answered_by_reaction = (bool(scope.required_ids) and
                                    scope.required_ids <= scope.reacted_ids)
            if (passed and not final and scope.optional_steer_after_direct_reply and
                    scope.direct_reply_text and not answered_by_reaction and not scope.self_spoken):
                final = scope.direct_reply_text
                log.info("Preserved direct reply across ambient steer: %s %s",
                         scope.key, sorted(scope.required_ids))
            if passed and not final:
                if scope.required_ids and not answered_by_reaction and not scope.self_spoken:
                    log.info("Passed over a direct ping without a reaction: %s %s",
                             scope.key, sorted(scope.required_ids - scope.reacted_ids))
                await self.output(scope.key, "cleanup", "", request_id)
                terminal_status = "clear"  # A deliberate pass posted no text.
            else:
                feedback: str | None = None
                for revision in range(OUTPUT_REVISION_MAX_ATTEMPTS + 1):
                    if feedback is not None:
                        try:
                            result = await client.prompt(
                                "Your previous reply was not posted to the conversation. "
                                f"{feedback} Rewrite the full reply for the same message using "
                                "only people who are actually present. Return the replacement "
                                "text, not an explanation of this validation error.", on_event, [])
                        except PiRpcError as error:
                            occurrence_id = self._record_pi_failure(
                                scope, error, "output_revision", request_id=request_id,
                                matrix_event_id=request_id)
                            log.warning("Bridge Pi revision failure agent=%s transport=%s "
                                        "request_id=%s error_class=%s support_id=%s",
                                        scope.key.agent, scope.key.transport, request_id,
                                        type(error).__name__, occurrence_id)
                            raise
                        if state.epoch != epoch:
                            return
                        final, _ = split_pass(result.text.strip())
                        if not final:
                            feedback = "The replacement was empty. Write the full reply."
                            continue
                    try:
                        delivered = await self.output(scope.key, kind, final, request_id)
                        if kind == "final" and delivered is True:
                            terminal_status = "done"
                        break
                    except ReplyRejected as error:
                        self.telemetry.record(scope.key.transport, "revision", "rejected",
                                              scope_hash=scope.key.scope_hash, epoch=epoch)
                        if kind != "final" or revision == OUTPUT_REVISION_MAX_ATTEMPTS:
                            log.warning("Bridge reply remained undeliverable after %d revisions: %s",
                                        revision, scope.key)
                            raise
                        feedback = str(error)
                else:
                    raise ReplyRejected("No deliverable replacement reply after bounded revisions")
            state.final_sent = True
            state.status = "idle"
            self._touch(scope, persist=True)
        except Exception as error:
            terminal_status = "failed"
            support_id = None
            if (not prompt_completed and isinstance(error, PiRpcError) and
                    not isinstance(error, PiProviderError)):
                occurrence_id = self._record_pi_failure(scope, error, "prompt",
                                                       request_id=request_id,
                                                       matrix_event_id=request_id)
                support_id = occurrence_id if occurrence_id else None
                log.warning(
                    "Bridge Pi RPC failure agent=%s transport=%s "
                    "request_id=%s phase=prompt error_class=%s support_id=%s",
                    scope.key.agent, scope.key.transport, request_id,
                    type(error).__name__, support_id)
            if state.epoch == epoch:
                if state.status == "final_pending":
                    # Delivery may have succeeded before its acknowledgement failed.
                    # Never send a second final-shaped record in that case.
                    log.warning("Bridge final delivery failed for %s: %s",
                                scope.key, type(error).__name__)
                    state.status = "delivery_error"
                    self._touch(scope, persist=True)
                    try:
                        await self.output(scope.key, "cleanup", "", request_id)
                    except Exception:
                        pass
                else:
                    kind = "cancelled" if state.status == "cancel_requested" else "error"
                    state.status = "error"
                    self._touch(scope, persist=True)
                    try:
                        if kind == "cancelled":
                            text = "Request cancelled."
                        elif isinstance(error, ModeSwitchError):
                            text = str(error)
                        elif isinstance(error, PiProviderError):
                            if error.auth:
                                text = auth_failure_text(error)
                            elif attempt_had_tool:
                                text = f"{error}. A tool may have run; inspect its result before retrying."
                            elif retried:
                                text = f"{error} after {PROVIDER_RETRY_MAX_ATTEMPTS} attempts. Please retry."
                            else:
                                text = f"{error}. Please retry."
                        else:
                            if support_id:
                                text = f"The request failed ({type(error).__name__}). Support ID: `{support_id}`."
                            else:
                                text = f"The request failed ({type(error).__name__})."
                        await self.output(scope.key, kind, text, request_id)
                    except Exception:
                        pass
                if isinstance(error, PiRpcError) and scope.client is client:
                    await client.stop()
                    _archive_transcripts(scope.directory, [
                        scope.directory / f"conversation-{epoch:08d}.jsonl"])
                    scope.client = None
        finally:
            # Persist all terminal intents together; a rate-limited room must
            # not multiply the bounded tracked flush budget by its steer count.
            await asyncio.gather(*(self._report_turn_status(scope.key, status_id, terminal_status,
                                                          accepted_epoch)
                                   for status_id, accepted_epoch in tuple(status_ids.items())))
            self.telemetry.inflight[scope.key.transport, "turn"] -= 1
            self.telemetry.record(scope.key.transport, "turn", state.status,
                                  seconds=time.monotonic() - run_started, scope_hash=scope.key.scope_hash, epoch=epoch)
            # An RPC exit or timeout may arrive without compaction_end. Release
            # waiting inputs only after the run has recorded its terminal state.
            scope.compacting = False
            scope.compaction_finished.set()
            scope.current_tool = ""
            scope.accepted.set()

    async def cancel(self, key: SessionKey, requester: str) -> bool:
        scope = await self._scope(key)
        switching = scope.mode_switch is not None
        if (not scope.active and not switching) or scope.checkpoint.requester != requester:
            # Denied before the cooperative-stop flag is raised: a caller whose
            # request this is not must not make that request abandon the control
            # write it is retrying. Rechecked under the lock below.
            return False
        # Flagged before the lock is queued on: a control write retrying inside the
        # scope reads it and releases that lock instead of delaying this cancel.
        self._queue_stop(scope, requester)
        try:
            async with self._locked_scope(scope):
                if scope.checkpoint.requester != requester:
                    return False
                if switching and not scope.active:
                    if scope.client is not None:
                        await scope.client.stop()
                        scope.client = None
                    scope.mode = self.default_mode
                    self._touch(scope, persist=True)
                    return True
                if not scope.active:
                    return False
                try:
                    await asyncio.wait_for(scope.accepted.wait(), timeout=30)
                except asyncio.TimeoutError:
                    return False
                scope.checkpoint.status = "cancel_requested"
                self._touch(scope, persist=True)
                assert scope.client is not None
                await scope.client.abort()
                return True
        finally:
            self._release_stop(scope, requester)

    async def stop_session(self, key: SessionKey, requester: str) -> bool:
        """Emergency stop of the caller's resident Pi scope, without compaction.

        The last *saved* summary survives. The unsaved transcript is archived
        privately, never revived; the next wake rereads recent channel posts.
        """
        scope = self._scopes.get(key)
        if scope is None:
            return False
        if scope.checkpoint.requester != requester:
            # Denied before the cooperative-stop flag is raised: an unauthorized
            # caller must not abort the running answer it was refused. Rechecked
            # under the lock below.
            return False
        # Flagged before the lock is queued on, so a rate-limited control write
        # holding it gives way rather than making this stop wait it out.
        stop_key = requester
        self._queue_stop(scope, stop_key)
        try:
            async with self._locked_scope(scope):
                if scope.client is None or (scope.checkpoint.requester != requester):
                    return False
                task = scope.task
                if task is not None and not task.done():
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
                client = scope.client
                if client is not None:
                    await client.stop()
                scope.client = None
                scope.mode = self.default_mode
                _archive_transcripts(scope.directory, [
                    scope.directory / f"conversation-{scope.checkpoint.epoch:08d}.jsonl"])
                scope.checkpoint.status = "idle"
                scope.checkpoint.last_wake_id = ""
                scope.current_tool = ""
                self._touch(scope, persist=True)
                return True
        finally:
            self._release_stop(scope, stop_key)

    @staticmethod
    def _queue_stop(scope: _Scope, stop_key: str) -> None:
        scope.stop_requests[stop_key] = scope.stop_requests.get(stop_key, 0) + 1
        if (scope.mode_switch is not None and
                (not stop_key or stop_key == scope.checkpoint.requester)):
            scope.mode_switch.cancel()

    @staticmethod
    def _release_stop(scope: _Scope, stop_key: str) -> None:
        remaining = scope.stop_requests.get(stop_key, 0) - 1
        if remaining > 0:
            scope.stop_requests[stop_key] = remaining
        else:
            scope.stop_requests.pop(stop_key, None)

    async def record_activity(self, key: SessionKey) -> None:
        """Reset the idle clock after an adapter sends a message or progress edit."""
        scope = await self._scope(key)
        self._touch(scope, persist=True)

    async def is_active(self, key: SessionKey) -> bool:
        scope = self._scopes.get(key)
        return scope.active if scope is not None else False

    def stop_requested(self, key: SessionKey, requester: str | None = None) -> bool:
        """True once a stop authorized for this request is queued on the scope lock.

        Sync and allocation-free on purpose: the transport polls it between
        retry attempts and inside their backoff waits. ``requester`` names the
        author of the write asking, so a queued stop can only interrupt the
        request it was authorized for; omitting it asks whether any authorized
        stop is pending on the scope.
        """
        scope = self._scopes.get(key)
        if scope is None:
            return False
        pending = scope.stop_requests
        if requester is None:
            return any(pending.values())
        return pending.get(requester, 0) > 0

    async def session_summary(self, *, detail: str = "summary") -> dict[str, object]:
        """Return a snapshot of loaded scopes for deploy safety checks.

        A scoped source deploy waits for ``active`` (running) and
        ``active_non_idle`` (finalizing/cancelling) to reach zero. ``awake``
        is advisory: restarting a resident idle scope discards unsummarized
        context since its last checkpoint.
        """
        total_scopes = len(self._scopes)
        state_breakdown: dict[str, int] = {}
        per_agent: dict[str, int] = {}
        per_transport: dict[str, int] = {}
        awake_per_agent: dict[str, int] = {}
        awake_per_transport: dict[str, int] = {}
        critical_sessions: list[dict[str, object]] = []
        active = 0
        active_non_idle = 0
        awake = 0
        for scope in self._scopes.values():
            status = "compacting" if scope.compacting else scope.checkpoint.status
            state_breakdown[status] = state_breakdown.get(status, 0) + 1
            per_agent[scope.key.agent] = per_agent.get(scope.key.agent, 0) + 1
            per_transport[scope.key.transport] = per_transport.get(scope.key.transport, 0) + 1
            is_awake = scope.client is not None
            if is_awake:
                awake += 1
                awake_per_agent[scope.key.agent] = awake_per_agent.get(scope.key.agent, 0) + 1
                awake_per_transport[scope.key.transport] = awake_per_transport.get(scope.key.transport, 0) + 1
            if scope.active:
                active += 1
            if status in _ACTIVE_STATUSES or scope.compacting:
                active_non_idle += 1
                session: dict[str, object] = {
                    "agent": scope.key.agent,
                    "transport": scope.key.transport,
                    "status": status,
                    "epoch": scope.checkpoint.epoch,
                    "last_activity_ms": scope.checkpoint.last_activity_ms,
                    "awake": is_awake,
                }
                if detail == "full":
                    session["scope_hash"] = scope.key.scope_hash
                    session["requester"] = scope.checkpoint.requester
                    session["request_id"] = scope.checkpoint.request_id
                    session["final_sent"] = scope.checkpoint.final_sent
                critical_sessions.append(session)
        return {
            "ok": True,
            "total_scopes": total_scopes,
            "awake": awake,
            "active": active,
            "active_non_idle": active_non_idle,
            "state_breakdown": state_breakdown,
            "per_agent": per_agent,
            "per_transport": per_transport,
            "awake_per_agent": awake_per_agent,
            "awake_per_transport": awake_per_transport,
            "critical_sessions": critical_sessions,
            "workload": aggregate([scope.client for scope in self._scopes.values() if scope.client is not None]),
        }
    def activity_snapshot(self) -> tuple[bool, bool, str]:
        """Public, bounded lifecycle state; never exposes requests or tool arguments."""
        active = [scope for scope in self._scopes.values() if scope.active]
        return (bool(active), any(scope.client is not None for scope in self._scopes.values()),
                active[0].current_tool if active else "")

    def activity_topics(self) -> list[str]:
        """Classify saved and active session context into public-safe labels.

        This deliberately returns only a fixed vocabulary. A private summary,
        requester, room name, path, or tool argument can never reach the status
        generator through this method.
        """
        recent = sorted(self._scopes.values(),
                        key=lambda scope: scope.checkpoint.last_activity_ms,
                        reverse=True)[:5]
        topics = []
        for scope in recent:
            topics.extend(scope.current_topics)
            topics.extend(_topic_labels(scope.checkpoint.summary[:2000]))
        if any(scope.active for scope in recent):
            topics.insert(0, "active conversation")
        return list(dict.fromkeys(topics))[:4] or ["general assistance"]

    async def recover(self, key: SessionKey, requester: str) -> bool:
        """Explicitly restart from the last sound summary after failed compaction."""
        scope = await self._scope(key)
        async with self._locked_scope(scope):
            state = scope.checkpoint
            if (state.status != "checkpoint_error" or scope.active or
                    (state.requester and state.requester != requester)):
                return False
            source = scope.directory / f"conversation-{state.epoch:08d}.jsonl"
            _archive_transcripts(scope.directory, [source])
            state.status = "idle"
            scope.mode = self.default_mode
            state.last_message_id = ""
            self._touch(scope, persist=True)
            return True

    async def checkpoint_for_handoff(self, key: SessionKey) -> bool:
        """Close an idle resident scope before an external Pi session owns its room."""
        async with self._checkpoint_lock:
            async with self._operation_slot():
                if not self._external_idle.is_set():
                    return False
                room = key.scope.removesuffix(":main")
                scopes = [scope for scope in self._scopes.values()
                          if scope.key.agent == key.agent and
                          scope.key.transport == key.transport and
                          (scope.key == key or scope.key.scope.startswith(room + ":thread:"))]
                for scope in scopes:
                    async with self._locked_scope(scope):
                        if scope.active or scope.checkpoint.status == "checkpoint_error":
                            return False
                await self._sweep_scopes(force=True, only={scope.key for scope in scopes})
                return all(scope.client is None and
                           scope.checkpoint.status != "checkpoint_error" for scope in scopes)

    async def sweep_idle(self, *, force: bool = False) -> int:
        """Checkpoint resident idle scopes; force bypasses only the idle timer.

        A signal-triggered sweep never aborts or compacts an active turn. The
        scope lock preserves the same ordering as an ordinary idle sweep.
        """
        async with self._checkpoint_lock:
            if not self._external_idle.is_set():
                return 0
            async with self._operation_slot():
                # Compaction invokes Pi too. It cannot run beside an external
                # review, and a review waits for an in-progress compaction.
                if not self._external_idle.is_set():
                    return 0
                return await self._sweep_scopes(force=force)

    async def _sweep_scopes(self, *, force: bool,
                            only: set[SessionKey] | None = None) -> int:
        closed = 0
        for scope in list(self._scopes.values()):
            if only is not None and scope.key not in only:
                continue
            async with scope.input_lock, self._locked_scope(scope):
                if (scope.client is None or scope.active or
                        (not force and
                         self.now_ms() - scope.checkpoint.last_activity_ms < self.idle_ms)):
                    continue
                if self.modes and scope.mode not in self.modes:
                    await self._reset_mode(scope)
                    closed += 1
                    continue
                scope.compaction_finished.clear()
                scope.compacting = True
                compact_started = time.monotonic()
                client = scope.client
                self.telemetry.inflight[scope.key.transport, "compact"] += 1
                outcome = "error"
                stopped = False
                try:
                    summary = await client.compact()
                    staged = replace(scope.checkpoint, status="checkpointed")
                    if summary is not None:
                        staged.summary = summary
                        staged.summary_saved_ms = self.now_ms()
                    else:
                        staged.last_wake_id = ""
                    # Commit the summary before stopping or archiving the source.
                    write_checkpoint(scope.directory, staged)
                    scope.checkpoint = staged
                    await client.stop()
                    stopped = True
                    source = scope.directory / f"conversation-{staged.epoch:08d}.jsonl"
                    _archive_transcripts(scope.directory, [source])
                    staged.status = "idle"
                    write_checkpoint(scope.directory, staged)
                    outcome = "saved" if summary is not None else "skipped"
                except (Exception, asyncio.CancelledError) as error:
                    if isinstance(error, PiRpcError):
                        self._record_pi_failure(scope, error, "compact",
                                               request_id=scope.checkpoint.request_id,
                                               matrix_event_id=scope.checkpoint.request_id)
                    scope.checkpoint.status = "checkpoint_error"
                    try:
                        write_checkpoint(scope.directory, scope.checkpoint)
                    except OSError:
                        log.warning("Bridge checkpoint persistence failed scope_hash=%s", scope.key.scope_hash)
                    log.warning("Bridge idle compaction failed scope_hash=%s error_class=%s",
                                scope.key.scope_hash, type(error).__name__)
                    if isinstance(error, asyncio.CancelledError):
                        outcome = "cancelled"
                        raise
                    continue
                finally:
                    try:
                        if not stopped:
                            await client.stop()
                    finally:
                        scope.client = None
                        scope.mode = self.default_mode
                        scope.compacting = False
                        scope.compaction_finished.set()
                        self.telemetry.inflight[scope.key.transport, "compact"] -= 1
                        self.telemetry.record(scope.key.transport, "compact", outcome,
                                              seconds=time.monotonic() - compact_started,
                                              scope_hash=scope.key.scope_hash, epoch=scope.checkpoint.epoch)
                closed += 1
        return closed

    async def wait_idle(self, key: SessionKey) -> None:
        scope = await self._scope(key)
        if scope.task is not None:
            # A caller's bounded wait must not cancel the Pi turn it is waiting for.
            await asyncio.shield(scope.task)

    async def close(self) -> None:
        self._closing = True
        self._external_idle.set()
        tasks = [scope.task for scope in self._scopes.values()
                 if scope.task is not None and not scope.task.done()]
        tasks.extend(task for task in self._operation_tasks if task is not asyncio.current_task())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for scope in self._scopes.values():
            if scope.client is not None:
                await scope.client.stop()
                scope.client = None
