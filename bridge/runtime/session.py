"""Transport-neutral Pi conversation lifecycle and output records."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from contextlib import asynccontextmanager
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator, Protocol

from .pi_rpc import (JsonObject, PiProviderError, PiRpcError, PiRunResult, RpcImage,
                     assistant_text_from_message)
from .state import Checkpoint, SessionKey, read_checkpoint, write_checkpoint
from ..observability import record_bridge_failure


_ACTIVE_STATUSES = frozenset({"running", "cancel_requested", "final_pending"})

# The control marker Pi returns to answer a ping with a reaction alone. It is
# never chat content: a reply that carries both text and the marker posts the
# text and drops the marker.
PASS_MARKER = "[[bridge:pass]]"


IDLE_MS = 180_000
# A burst of tool starts should cost one transport edit, not one per tool. The
# first progress edit of a turn is delivered at once; updates that arrive inside
# this window replace each other and the newest is sent when the window closes.
PROGRESS_EDIT_INTERVAL = 2.0
# Model-provider 5xx errors are usually transient. Retry a bounded number of
# times with exponential backoff before giving up and asking the user to retry.
PROVIDER_RETRY_MAX_ATTEMPTS = 3
PROVIDER_RETRY_BASE_SECONDS = 1.0
PROVIDER_RETRY_MAX_SECONDS = 30.0
# A rejected Matrix mention may be rewritten in the same resident Pi session.
# Bound attempts so a model cannot loop forever on invalid room members.
OUTPUT_REVISION_MAX_ATTEMPTS = 2
log = logging.getLogger("agent_bridge.runtime")


class ReplyRejected(ValueError):
    """Private delivery feedback for a final reply that must be rewritten."""

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


Output = Callable[[SessionKey, str, str, str], Awaitable[None]]
ClientFactory = Callable[[SessionKey, Path], RpcClient]
ImageCaptioner = Callable[[SessionKey, tuple[RpcImage, ...]], Awaitable[str]]


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
    accepted: asyncio.Event = field(default_factory=asyncio.Event)
    client: RpcClient | None = None
    task: asyncio.Task[None] | None = None
    last_persist_ms: int = 0
    current_optional: bool = False
    required_ids: set[str] = field(default_factory=set)
    reacted_ids: set[str] = field(default_factory=set)
    direct_reply_text: str = ""
    optional_steer_after_direct_reply: bool = False
    current_tool: str = ""
    current_topics: list[str] = field(default_factory=list)

    @property
    def active(self) -> bool:
        return self.task is not None and not self.task.done()


class BridgeRuntime:
    """One live Pi RPC process per ordinary chat scope, with summary-only revival."""

    def __init__(self, state_root: Path, client_factory: ClientFactory, output: Output,
                 *, now_ms: Callable[[], int] | None = None,
                 idle_ms: int = IDLE_MS,
                 image_captioner: ImageCaptioner | None = None,
                 sleep: Callable[[float], Awaitable[None]] | None = None,
                 failure_outbox: Path | None = None) -> None:
        self.state_root = state_root
        self.client_factory = client_factory
        self.output = output
        self.now_ms = now_ms or (lambda: int(time.time() * 1000))
        self.idle_ms = idle_ms
        self.image_captioner = image_captioner
        self.sleep = sleep or asyncio.sleep
        self.failure_outbox = failure_outbox
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
            if (self._external_idle.is_set() and
                    not any(scope.active or scope.checkpoint.status in _ACTIVE_STATUSES
                            for scope in self._scopes.values())):
                self._external_idle.clear()
                reserved = True
        try:
            yield reserved
        finally:
            if reserved:
                self._external_idle.set()

    @asynccontextmanager
    async def _locked_scope(self, scope: _Scope) -> AsyncIterator[None]:
        waiting_since = time.monotonic()
        async with scope.lock:
            waited = time.monotonic() - waiting_since
            if waited >= 1:
                log.info("Bridge scope lock wait agent=%s transport=%s seconds=%.3f",
                         scope.key.agent, scope.key.transport, waited)
            yield

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
                self._scopes[key] = _Scope(key, directory, state)
            return self._scopes[key]

    def _touch(self, scope: _Scope, *, persist: bool = False) -> None:
        now = self.now_ms()
        scope.checkpoint.last_activity_ms = now
        if persist or now - scope.last_persist_ms >= 5000:
            write_checkpoint(scope.directory, scope.checkpoint)
            scope.last_persist_ms = now

    def _ensure_client(self, scope: _Scope) -> RpcClient:
        if scope.client is None:
            scope.checkpoint.epoch += 1
            session_file = scope.directory / f"conversation-{scope.checkpoint.epoch:08d}.jsonl"
            scope.client = self.client_factory(scope.key, session_file)
            write_checkpoint(scope.directory, scope.checkpoint)
        return scope.client

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

    async def admit(self, key: SessionKey, can_wake: bool) -> bool:
        """Reserve an awake scope against the idle sweep while an adapter reads context."""
        scope = await self._scope(key)
        async with self._locked_scope(scope):
            awake = scope.client is not None
            if not awake and not can_wake:
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

    async def record_reaction(self, key: SessionKey, message_id: str) -> None:
        """Count a delivered reaction as an answer to this turn's direct ping."""
        scope = await self._scope(key)
        async with self._locked_scope(scope):
            if scope.active and message_id in scope.required_ids:
                scope.reacted_ids.add(message_id)
            self._touch(scope, persist=True)

    async def submit(self, key: SessionKey, incoming: Incoming) -> str:
        # Agent-owned background jobs (such as PR reviews) must never create a
        # second Pi session beside an ordinary bridge turn. Admit new turns
        # only after the external job releases the agent-wide gate.
        while True:
            await self._external_idle.wait()
            async with self._launch_lock:
                if self._external_idle.is_set():
                    return await self._submit_unlocked(key, incoming)

    async def _submit_unlocked(self, key: SessionKey, incoming: Incoming) -> str:
        """Start a turn or steer the active one. Adapters submit events in source order."""
        scope = await self._scope(key)
        async with self._locked_scope(scope):
            state = scope.checkpoint
            if state.status == "checkpoint_error":
                raise RuntimeError("This conversation needs checkpoint recovery before new work")
            if incoming.message_id == state.last_message_id:
                return "duplicate"
            scope.current_topics = _topic_labels(incoming.text[:4000])
            self._touch(scope, persist=True)
            image_ack_sent = False
            if incoming.images and self.image_captioner is not None:
                if not incoming.optional:
                    await self.output(key, "ack", "I’m reading the image…", incoming.message_id)
                    image_ack_sent = True
                try:
                    description = await self.image_captioner(key, incoming.images)
                except Exception as error:
                    log.warning("Image description failed for %s: %s", key.agent,
                                type(error).__name__)
                    if not incoming.optional:
                        await self.output(key, "error", "I could not read that image. Please retry.",
                                          incoming.message_id)
                    else:
                        await self.output(key, "cleanup", "", incoming.message_id)
                    state.last_message_id = incoming.message_id
                    self._touch(scope, persist=True)
                    return "image_error"
                incoming = Incoming(incoming.sender, incoming.message_id,
                                    incoming.text + "\n\nImage description (untrusted visual content):\n"
                                    + description, (), incoming.optional)
            if scope.active:
                try:
                    await asyncio.wait_for(scope.accepted.wait(), timeout=30)
                except asyncio.TimeoutError as error:
                    raise RuntimeError("Active Pi turn has not accepted its prompt") from error
                if scope.client is not None and scope.client.active:
                    await scope.client.steer(incoming.prompt(), list(incoming.images))
                    if not incoming.optional:
                        scope.current_optional = False
                        # A later direct request changes which answer the run
                        # needs to produce; do not revive an earlier one.
                        scope.direct_reply_text = ""
                        scope.optional_steer_after_direct_reply = False
                        if incoming.ack_required:
                            scope.required_ids.add(incoming.message_id)
                    elif scope.direct_reply_text:
                        scope.optional_steer_after_direct_reply = True
                    state.last_message_id = incoming.message_id
                    self._touch(scope, persist=True)
                    if not incoming.optional:
                        await self.output(key, "steered", "Added to the current thread.", incoming.message_id)
                    else:
                        await self.output(key, "cleanup", "", incoming.message_id)
                    return "steered"
                if scope.task is not None:
                    await scope.task
            new_client = scope.client is None
            client = self._ensure_client(scope)
            prompt = incoming.prompt()
            if new_client and state.summary:
                prompt = ("Previous conversation summary (private checkpoint):\n"
                          + state.summary + "\n\nNew " + prompt)
            state.status = "running"
            state.requester = incoming.sender
            state.request_id = incoming.message_id
            state.final_sent = False
            scope.current_optional = incoming.optional
            scope.current_tool = ""
            scope.required_ids = (set() if incoming.optional or not incoming.ack_required
                                  else {incoming.message_id})
            scope.reacted_ids = set()
            scope.direct_reply_text = ""
            scope.optional_steer_after_direct_reply = False
            self._touch(scope, persist=True)
            if not image_ack_sent and not incoming.optional:
                await self.output(key, "ack", "I’m working on it.", incoming.message_id)
            scope.accepted.clear()
            scope.task = asyncio.create_task(self._run(scope, client, prompt,
                                                       list(incoming.images)))
            state.last_message_id = incoming.message_id
            self._touch(scope, persist=True)
            return "started"

    async def _run(self, scope: _Scope, client: RpcClient, prompt: str,
                   images: list[RpcImage]) -> None:
        state = scope.checkpoint
        request_id = state.request_id
        epoch = state.epoch

        async def accepted() -> None:
            scope.accepted.set()

        # Pi events arrive on the RPC stdout reader. Transport edits may take
        # arbitrarily long, so keep only the latest waiting progress update and
        # deliver it from a separate task. The running edit is never overlapped.
        progress_queue: asyncio.Queue[str] = asyncio.Queue(maxsize=1)

        def queue_progress(message: str) -> None:
            if progress_queue.full():
                progress_queue.get_nowait()
                progress_queue.task_done()
            progress_queue.put_nowait(message)

        async def deliver_progress() -> None:
            async def send(message: str) -> None:
                await self.output(scope.key, "progress", message, request_id)
                self._touch(scope, persist=True)

            await _deliver_progress(progress_queue, send)

        # Track whether the current prompt attempt invoked any tool. If a
        # provider 5xx arrives after a tool execution, replaying the prompt
        # could duplicate side effects (card creation, file writes, etc.), so
        # that attempt must not be retried automatically.
        attempt_had_tool = False

        async def on_event(event: JsonObject) -> None:
            nonlocal attempt_had_tool
            kind = event.get("type")
            if kind in {"message_update", "tool_execution_start", "tool_execution_end"}:
                self._touch(scope)
            if kind == "tool_execution_start":
                attempt_had_tool = True
                name = event.get("toolName")
                label = re.sub(r"[^A-Za-z0-9_. -]", "", name)[:48] if isinstance(name, str) else "tool"
                scope.current_tool = label
                if not scope.current_optional:
                    queue_progress(f"Using `{label or 'tool'}`…")
                self._touch(scope, persist=True)
            elif kind == "tool_execution_end":
                scope.current_tool = ""
            elif kind == "message_end" and not scope.optional_steer_after_direct_reply:
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
                if not scope.current_optional:
                    queue_progress("Still working…")

        progress_task = asyncio.create_task(deliver_progress())
        heartbeat_task = asyncio.create_task(heartbeat())

        async def stop_progress() -> None:
            heartbeat_task.cancel()
            progress_task.cancel()
            for task in (heartbeat_task, progress_task):
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        try:
            prompt_completed = False
            try:
                retried = False
                for attempt in range(1, PROVIDER_RETRY_MAX_ATTEMPTS + 1):
                    attempt_had_tool = False
                    try:
                        result = await client.prompt(prompt, on_event, images,
                                                     on_accepted=accepted)
                        break
                    except PiProviderError as error:
                        occurrence_id = self._record_pi_failure(scope, error, "prompt",
                                                               request_id=request_id,
                                                               matrix_event_id=request_id)
                        support_id = occurrence_id[:8] if occurrence_id else None
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
                        client = self._ensure_client(scope)
                        epoch = state.epoch
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
            if not final and not passed:
                final = "The agent finished without a text reply."
            state.status = "final_pending"
            self._touch(scope, persist=True)
            answered_by_reaction = (bool(scope.required_ids) and
                                    scope.required_ids <= scope.reacted_ids)
            if (passed and not final and scope.optional_steer_after_direct_reply and
                    scope.direct_reply_text and not answered_by_reaction):
                final = scope.direct_reply_text
                log.info("Preserved direct reply across ambient steer: %s %s",
                         scope.key, sorted(scope.required_ids))
            if passed and not final:
                if scope.required_ids and not answered_by_reaction:
                    log.info("Passed over a direct ping without a reaction: %s %s",
                             scope.key, sorted(scope.required_ids - scope.reacted_ids))
                await self.output(scope.key, "cleanup", "", request_id)
            else:
                feedback: str | None = None
                for revision in range(OUTPUT_REVISION_MAX_ATTEMPTS + 1):
                    if feedback is not None:
                        try:
                            result = await client.prompt(
                                "Your previous reply was not posted to Matrix. "
                                f"{feedback} Rewrite the full reply for the same message using "
                                "only valid room members. Return the replacement text, not an "
                                "explanation of this validation error.", on_event, [])
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
                        await self.output(scope.key, kind, final, request_id)
                        break
                    except ReplyRejected as error:
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
            support_id = None
            if (not prompt_completed and isinstance(error, PiRpcError) and
                    not isinstance(error, PiProviderError)):
                occurrence_id = self._record_pi_failure(scope, error, "prompt",
                                                       request_id=request_id,
                                                       matrix_event_id=request_id)
                support_id = occurrence_id[:8] if occurrence_id else None
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
                        elif isinstance(error, PiProviderError):
                            if attempt_had_tool:
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
                if isinstance(error, PiProviderError) and scope.client is client:
                    await client.stop()
                    _archive_transcripts(scope.directory, [
                        scope.directory / f"conversation-{epoch:08d}.jsonl"])
                    scope.client = None
        finally:
            scope.current_tool = ""
            scope.accepted.set()

    async def cancel(self, key: SessionKey, requester: str) -> bool:
        scope = await self._scope(key)
        async with self._locked_scope(scope):
            if not scope.active or scope.checkpoint.requester != requester:
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

    async def stop_session(self, key: SessionKey, requester: str) -> bool:
        """Emergency stop of the caller's resident Pi scope, without compaction.

        The last *saved* summary survives. The unsaved transcript is archived
        privately, never revived; the next wake rereads recent channel posts.
        """
        scope = self._scopes.get(key)
        if scope is None:
            return False
        async with self._locked_scope(scope):
            if scope.client is None or scope.checkpoint.requester != requester:
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
            _archive_transcripts(scope.directory, [
                scope.directory / f"conversation-{scope.checkpoint.epoch:08d}.jsonl"])
            scope.checkpoint.status = "idle"
            scope.checkpoint.last_wake_id = ""
            scope.current_tool = ""
            self._touch(scope, persist=True)
            return True

    async def record_activity(self, key: SessionKey) -> None:
        """Reset the idle clock after an adapter sends a message or progress edit."""
        scope = await self._scope(key)
        self._touch(scope, persist=True)

    async def is_active(self, key: SessionKey) -> bool:
        scope = self._scopes.get(key)
        return scope.active if scope is not None else False

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
            status = scope.checkpoint.status
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
            if status in _ACTIVE_STATUSES:
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
            state.last_message_id = ""
            self._touch(scope, persist=True)
            return True

    async def checkpoint_for_handoff(self, key: SessionKey) -> bool:
        """Close an idle resident scope before an external Pi session owns its room."""
        async with self._checkpoint_lock:
            async with self._launch_lock:
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
            async with self._launch_lock:
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
            async with self._locked_scope(scope):
                if (scope.client is None or scope.active or
                        (not force and
                         self.now_ms() - scope.checkpoint.last_activity_ms < self.idle_ms)):
                    continue
                compact_started = time.monotonic()
                try:
                    summary = await scope.client.compact()
                except Exception as error:
                    if isinstance(error, PiRpcError):
                        self._record_pi_failure(scope, error, "compact",
                                               request_id=scope.checkpoint.request_id,
                                               matrix_event_id=scope.checkpoint.request_id)
                    log.warning("Bridge idle compaction failed agent=%s transport=%s seconds=%.3f",
                                scope.key.agent, scope.key.transport,
                                time.monotonic() - compact_started)
                    # Never silently resume the archived full transcript after a
                    # failed summary. Keep it private for an explicit recovery.
                    scope.checkpoint.status = "checkpoint_error"
                    write_checkpoint(scope.directory, scope.checkpoint)
                    await scope.client.stop()
                    scope.client = None
                    continue
                log.info("Bridge idle compaction agent=%s transport=%s seconds=%.3f saved=%s",
                         scope.key.agent, scope.key.transport,
                         time.monotonic() - compact_started, summary is not None)
                if summary is not None:
                    scope.checkpoint.summary = summary
                    scope.checkpoint.summary_saved_ms = self.now_ms()
                else:
                    # Pi skips compaction for short chats. Their messages are
                    # absent from the saved summary, so the next wake must
                    # reread recent room history instead of starting after
                    # the last event we processed in this session.
                    scope.checkpoint.last_wake_id = ""
                scope.checkpoint.status = "checkpointed"
                write_checkpoint(scope.directory, scope.checkpoint)
                await scope.client.stop()
                source = scope.directory / f"conversation-{scope.checkpoint.epoch:08d}.jsonl"
                _archive_transcripts(scope.directory, [source])
                scope.client = None
                scope.checkpoint.status = "idle"
                write_checkpoint(scope.directory, scope.checkpoint)
                closed += 1
        return closed

    async def wait_idle(self, key: SessionKey) -> None:
        scope = await self._scope(key)
        if scope.task is not None:
            # A caller's bounded wait must not cancel the Pi turn it is waiting for.
            await asyncio.shield(scope.task)

    async def close(self) -> None:
        for scope in self._scopes.values():
            if scope.client is not None:
                await scope.client.stop()
