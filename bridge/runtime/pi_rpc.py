"""Async client for Pi's newline-delimited JSON RPC mode.

Adapted from Xuthal Labs' Conclave Swarm Pi RPC client (MIT).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import signal
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, NotRequired, TypeAlias, TypedDict, cast

log = logging.getLogger("agent_bridge.pi_rpc")

JsonScalar: TypeAlias = None | bool | int | float | str
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
JsonObject: TypeAlias = dict[str, JsonValue]


class RpcResponse(TypedDict):
    type: Literal["response"]
    id: str
    success: bool
    data: NotRequired[JsonValue]
    error: NotRequired[JsonValue]


RpcEventHandler: TypeAlias = Callable[[JsonObject], Awaitable[None]]
RpcAcceptedHandler: TypeAlias = Callable[[], Awaitable[None]]
RPC_STREAM_LIMIT_BYTES = 32 * 1024 * 1024
# Current Pi builds return this text when no messages fall outside the
# keep-recent window. There is no structured RPC error code for the case.
COMPACT_TOO_SMALL = "Nothing to compact"
COMPACT_ALREADY_DONE = "Already compacted"
# Control commands are bounded so a wedged process cannot hold a caller
# forever. Prompts and compaction own longer, separately managed deadlines.
CONTROL_COMMAND_TIMEOUT_SECONDS = 120
# Pi can keep a steering message in its own queue for minutes while the current
# turn or a compaction finishes, so a steer confirms delivery rather than
# receipt: once the frame reaches the process the caller is free again. An
# admission reply that is still outstanding after this window is collected
# asynchronously instead of being awaited.
STEER_RECEIPT_GRACE_SECONDS = 2.0
# A dying Pi process can leave arbitrarily sensitive text on stderr: tool
# output, image bytes, credentials. Keep a bounded fingerprint rather than the
# text - a failure vocabulary plus a token skeleton in which every
# unrecognised word becomes a placeholder, so a shape such as
# "error cannot find module <w>" still explains the exit without exposing a
# path, module name or secret.
STDERR_TAIL_LINES = 5
STDERR_TAIL_CHARS = 120
STDERR_DRAIN_GRACE_SECONDS = 1.0
STDERR_BINARY_RATIO = 0.3
STDERR_VOCABULARY = frozenset({
    "abort", "aborted", "again", "already", "at", "attributeerror", "authorization", "bearer",
    "broken", "cannot", "cd", "child", "cjs", "closed", "code", "compaction", "connect",
    "connection", "context", "denied", "directory", "eacces", "eaddrinuse", "eagain",
    "econnrefused", "econnreset", "eexist", "eisdir", "enoent", "enomem", "enospc", "ent",
    "eof", "eperm", "epipe", "error", "esm", "etimedout", "exceeded", "exceeds", "exec", "exit",
    "expected", "expired", "extension", "failed", "failure", "fatal", "file", "find",
    "forbidden", "from", "heap", "host", "import", "importerror", "in", "indexerror",
    "internal", "invalid", "javascript", "js", "json", "keyerror", "killed", "limit", "load",
    "loader", "loading", "malformed", "memory", "missing", "mjs", "module", "modules",
    "nameerror", "network", "node", "not", "npm", "null", "of", "on", "overflow", "package",
    "parse", "parsing", "path", "permission", "pipe", "port", "process", "promise", "provider",
    "quota", "rate", "read", "refused", "rejection", "request", "require", "reset",
    "resolution", "resolve", "resolving", "response", "runtimeerror", "signal", "socket",
    "spawn", "status", "such", "syntaxerror", "terminated", "timed", "timeout", "traceback",
    "ts", "typeerror", "unauthorized", "undefined", "undici", "unexpected", "unhandled",
    "unknown", "unrecognized", "unsupported", "valueerror", "version", "warn", "warning",
    "window", "write", "yarn"
})
_STDERR_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_STDERR_SEGMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|[0-9]+|[^\sA-Za-z0-9_]+")


def stderr_fingerprint(line: bytes) -> str:
    """Reduce one raw stderr line to a bounded, value-free shape.

    Only words from the closed ``STDERR_VOCABULARY`` survive; every other name,
    path, hash or secret becomes ``<w>`` and runs of them collapse, so the
    result describes a failure without carrying a value from it.
    """
    if not line.strip():
        return ""
    text_bytes = sum(1 for byte in line if byte == 9 or 32 <= byte < 127)
    if text_bytes < (1 - STDERR_BINARY_RATIO) * len(line):
        return f"[binary {len(line)} bytes]"
    text = _STDERR_ANSI.sub("", line.decode("utf-8", errors="replace"))
    words: list[str] = []
    for segment in _STDERR_SEGMENT.findall(text):
        if not segment[0].isalnum():
            continue  # Punctuation separates segments; it says nothing itself.
        word = segment.casefold()
        classified = word if word in STDERR_VOCABULARY else "<n>" if word.isdigit() else "<w>"
        if classified != "<w>" or not words or words[-1] != "<w>":
            words.append(classified)
    return " ".join(words)[:STDERR_TAIL_CHARS]


def stderr_digest(shapes: list[str]) -> str:
    """Stable, comparable identity for one exit's retained stderr shapes."""
    if not shapes:
        return "none"
    return hashlib.sha256("\n".join(shapes).encode()).hexdigest()[:16]


class PiRpcError(RuntimeError):
    """Pi rejected a command or its RPC process failed."""


class PiRpcTimeout(PiRpcError):
    """Pi made no useful RPC progress during the configured idle interval."""

    def __init__(self, idle_seconds: float) -> None:
        self.idle_seconds = idle_seconds
        super().__init__(f"Pi made no progress for {idle_seconds:g} seconds")


class PiRpcProtocolError(PiRpcError):
    """Pi emitted malformed or unexpected RPC output."""


class PiProviderError(PiRpcError):
    """The model provider ended a turn with an error instead of a reply."""

    def __init__(self, status: int | None) -> None:
        self.status = status
        detail = f"HTTP {status}" if status is not None else "an error"
        super().__init__(f"Model provider returned {detail}")


@dataclass(frozen=True)
class PiRunResult:
    """The assistant text produced by one fully settled Pi run."""

    text: str


@dataclass
class _ActiveRun:
    """Mutable event-collection state for one prompt."""

    on_event: RpcEventHandler
    settled: asyncio.Future[PiRunResult]
    text_deltas: list[str]
    progress: asyncio.Queue[None]
    complete_text: str = ""
    has_complete_message: bool = False
    provider_error: PiProviderError | None = None
    pending_inputs: int = 0
    generation: int = 0
    settlement_pending: bool = False
    awaiting_continuation: bool = False


@dataclass(frozen=True)
class RpcImage:
    """One image attachment accepted by Pi's RPC prompt command."""

    mime_type: str
    data: bytes

    def to_json(self) -> JsonObject:
        return {
            "type": "image",
            "mimeType": self.mime_type,
            "data": base64.b64encode(self.data).decode("ascii"),
        }


def _normalise_json(value: object) -> JsonValue:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, list):
        return [_normalise_json(item) for item in value]
    if isinstance(value, Mapping):
        result: JsonObject = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise PiRpcProtocolError("RPC object contains a non-string key")
            result[key] = _normalise_json(item)
        return result
    raise PiRpcProtocolError(f"RPC value has unsupported type {type(value).__name__}")


def decode_json_object(raw: bytes) -> JsonObject:
    """Decode and validate one JSON object emitted by Pi."""
    try:
        parsed: object = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PiRpcProtocolError("Pi emitted invalid JSON") from exc
    normalised = _normalise_json(parsed)
    if not isinstance(normalised, dict):
        raise PiRpcProtocolError("Pi emitted a JSON value that is not an object")
    return normalised


def parse_rpc_response(event: JsonObject) -> RpcResponse:
    """Reject malformed command replies before resolving a pending request."""
    if (event.get("type") != "response" or
            not isinstance(event.get("id"), str) or
            type(event.get("success")) is not bool):
        raise PiRpcProtocolError("Pi emitted a malformed RPC response")
    return cast(RpcResponse, event)


def _rejection(command: str, response: RpcResponse) -> PiRpcError | None:
    """Return the rejection one reply carries, or ``None`` when it was accepted."""
    if response.get("success") is True:
        return None
    error = response.get("error")
    detail = error if isinstance(error, str) else f"{command} rejected"
    return PiRpcError(f"Pi rejected {command}: {detail}")


def _text_content(value: JsonValue) -> str:
    if not isinstance(value, list):
        return ""
    parts: list[str] = []
    for item in value:
        if not isinstance(item, dict) or item.get("type") != "text":
            continue
        text = item.get("text")
        if isinstance(text, str):
            parts.append(text)
    return "".join(parts)


def assistant_text_from_message(event: JsonObject) -> str:
    """Extract assistant text from a complete ``message_end`` event."""
    message = event.get("message")
    if not isinstance(message, dict) or message.get("role") != "assistant":
        return ""
    return _text_content(message.get("content"))


def _is_progress_event(event: JsonObject) -> bool:
    """Ignore status/heartbeat traffic that says nothing about turn progress."""
    kind = event.get("type")
    if kind in {"message_start", "message_end", "tool_execution_start",
                "tool_execution_end", "turn_start", "turn_end", "agent_end",
                "agent_settled", "compaction_start", "compaction_end",
                "auto_compaction_start", "auto_compaction_end", "retry_start", "retry_end",
                "summarization_retry_scheduled", "summarization_retry_attempt_start",
                "summarization_retry_finished"}:
        return True
    if kind == "message_update":
        update = event.get("assistantMessageEvent")
        return (isinstance(update, dict) and
                update.get("type") in {"text_delta", "thinking_delta"}
                and isinstance(update.get("delta"), str) and bool(update["delta"]))
    return False


class PiRpcClient:
    """Own one long-lived Pi RPC subprocess for one conversation scope."""

    def __init__(
        self,
        command: list[str],
        cwd: Path,
        env: dict[str, str],
        timeout_seconds: float = 900.0,
        settle_on_agent_end: bool = False,
    ) -> None:
        self._command = command
        self._cwd = cwd
        self._env = env
        self._timeout_seconds = timeout_seconds
        self._settle_on_agent_end = settle_on_agent_end
        self._process: asyncio.subprocess.Process | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_tail: deque[str] = deque(maxlen=20)
        self._stderr_lines = 0
        self._stderr_bytes = 0
        self._prompt_lock = asyncio.Lock()
        self._start_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._request_number = 0
        self._pending_responses: dict[str, asyncio.Future[RpcResponse]] = {}
        self._active_run: _ActiveRun | None = None

    @property
    def running(self) -> bool:
        process = self._process
        return process is not None and process.returncode is None

    @property
    def active(self) -> bool:
        return self._active_run is not None and not self._active_run.settled.done()

    async def start(self) -> None:
        async with self._start_lock:
            await self._start()

    async def _start(self) -> None:
        process = self._process
        if process is not None and process.returncode is None:
            return
        self._stderr_tail.clear()
        self._stderr_lines = 0
        self._stderr_bytes = 0
        self._process = await asyncio.create_subprocess_exec(
            *self._command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self._cwd,
            env=self._env,
            start_new_session=True,
            umask=0o077,
            limit=RPC_STREAM_LIMIT_BYTES,
        )
        assert self._process.stderr is not None
        self._stderr_task = asyncio.create_task(
            self._drain_stderr(self._process.stderr)
        )
        assert self._process.stdout is not None
        self._reader_task = asyncio.create_task(
            self._read_stdout(self._process.stdout)
        )

    async def stop(self) -> None:
        async with self._start_lock:
            await self._stop()

    async def _stop(self) -> None:
        process = self._process
        self._process = None
        if process is not None and process.returncode is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await process.wait()
        reader_task = self._reader_task
        self._reader_task = None
        if reader_task is not None and reader_task is not asyncio.current_task():
            if not reader_task.done():
                reader_task.cancel()
            try:
                await reader_task
            except asyncio.CancelledError:
                pass
            except PiRpcError:
                pass
        stderr_task = self._stderr_task
        self._stderr_task = None
        if stderr_task is not None:
            try:
                await stderr_task
            except asyncio.CancelledError:
                pass
        self._fail_pending(PiRpcError("Pi RPC process stopped"))

    async def prompt(
        self,
        message: str,
        on_event: RpcEventHandler,
        images: list[RpcImage] | None = None,
        *,
        on_accepted: RpcAcceptedHandler | None = None,
    ) -> PiRunResult:
        """Send a prompt and return only after Pi emits ``agent_settled``."""
        async with self._prompt_lock:
            try:
                await self.start()
            except OSError as exc:
                raise PiRpcError("Pi RPC process could not start") from exc
            try:
                return await self._prompt_with_idle_timeout(
                    message, on_event, images, on_accepted)
            except asyncio.CancelledError:
                await self.stop()
                raise
            except PiRpcError:
                await self.stop()
                raise
            except Exception as exc:
                await self.stop()
                raise PiRpcError("Pi RPC request failed unexpectedly") from exc

    async def abort(self) -> None:
        """Abort the active Pi operation without terminating the RPC process."""
        await self.start()
        await self._send_command("abort")

    async def steer(self, message: str, images: list[RpcImage] | None = None) -> None:
        """Deliver new input to an active turn without waiting for its receipt.

        Pi may hold a steering message in its own queue for minutes while the
        current turn or a compaction finishes, so delivery is what is awaited
        here: the frame reaches the process and the caller is free again. When
        Pi's admission reply is still outstanding after
        ``STEER_RECEIPT_GRACE_SECONDS`` it is collected when it arrives, because
        the admission is what tells this client that a continuation has taken
        over the run's settlement. That collection outlives a cancelled caller:
        the steer is already with Pi, so dropping its reply would leave a
        continuation Pi still starts without a settlement owner.
        """
        if not self.active:
            raise PiRpcError("Cannot steer a settled Pi session")
        active = self._active_run
        assert active is not None
        generation = active.generation
        active.pending_inputs += 1
        fields: JsonObject = {"message": message, "streamingBehavior": "steer"}
        if images:
            fields["images"] = [image.to_json() for image in images]
        try:
            # A raw steer queues even when Pi has just become idle. Atomic prompt
            # admission instead queues during streaming or starts the next turn,
            # preventing the next input from overtaking an undrained idle steer.
            request_id, response = await self._write_command("prompt", fields)
        except BaseException:
            active.pending_inputs -= 1
            self._settle_active(active)
            raise
        try:
            # Bounded by delivery, not by receipt: Pi may hold the admission for
            # minutes, so the reply future is left intact for a late collection.
            completed, _ = await asyncio.wait({response}, timeout=STEER_RECEIPT_GRACE_SECONDS)
        except BaseException:
            # The caller gave up inside the delivery window, but the frame is
            # already with Pi and it may still admit the steer. Hand the reply to
            # the collector rather than dropping it: a continuation Pi starts
            # afterwards keeps a settlement owner, and a failure the process
            # reports instead is retrieved rather than left unowned.
            log.info("Pi steer caller cancelled; admission receipt still pending")
            response.add_done_callback(
                lambda reply: self._absorb_steer_receipt(
                    request_id, active, generation, reply))
            raise
        if not completed:
            log.info("Pi steer delivered; admission receipt still pending")
            response.add_done_callback(
                lambda reply: self._absorb_steer_receipt(
                    request_id, active, generation, reply))
            return
        self._pending_responses.pop(request_id, None)
        try:
            self._admit_steer(active, generation, response.result())
        finally:
            active.pending_inputs -= 1
            self._settle_active(active)

    @staticmethod
    def _admit_steer(active: _ActiveRun, generation: int, receipt: RpcResponse) -> None:
        """Record one steering admission Pi has acknowledged."""
        rejection = _rejection("prompt", receipt)
        if rejection is not None:
            raise rejection
        data = receipt.get("data")
        disposition = data.get("disposition") if isinstance(data, dict) else None
        if active.generation == generation and (
                disposition == "started" or
                (disposition == "queued" and active.settlement_pending)):
            active.awaiting_continuation = True

    def _absorb_steer_receipt(self, request_id: str, active: _ActiveRun, generation: int,
                              reply: asyncio.Future[RpcResponse]) -> None:
        """Apply a steering admission that arrived after the delivery window.

        The sender has already been told the steer was accepted, so a rejection
        here cannot be returned to it; it is logged as a failure class and the
        run's settlement is released either way.
        """
        self._pending_responses.pop(request_id, None)
        try:
            receipt = reply.result()
        except asyncio.CancelledError:
            receipt = None
        except Exception as failure:
            # The Pi process failed before it answered; the exit was reported by
            # the reader, so the class is enough here.
            log.info("Pi steer admission was never confirmed error_class=%s",
                     type(failure).__name__)
            receipt = None
        if receipt is not None:
            try:
                self._admit_steer(active, generation, receipt)
            except PiRpcError as rejection:
                log.error("Pi rejected a delivered steer error_class=%s",
                          type(rejection).__name__)
        active.pending_inputs -= 1
        self._settle_active(active)

    @staticmethod
    def _settle_active(active: _ActiveRun) -> None:
        if (not active.settlement_pending or active.pending_inputs or
                active.awaiting_continuation or active.settled.done()):
            return
        if active.provider_error is not None:
            active.settled.set_exception(active.provider_error)
        else:
            text = (active.complete_text if active.has_complete_message
                    else "".join(active.text_deltas)).strip()
            active.settled.set_result(PiRunResult(text=text))

    async def compact(self) -> str | None:
        # A tool image or extension continuation may still be running after
        # agent_end. Share the prompt gate until agent_settled completes.
        async with self._prompt_lock:
            try:
                return await self._compact()
            except (PiRpcError, asyncio.CancelledError):
                await self.stop()
                raise

    async def _compact(self) -> str | None:
        """Return a summary, or skip compaction when Pi says the chat is too short."""
        if self.active:
            raise PiRpcError("Cannot compact an active Pi turn")
        await self.start()
        try:
            response = await self._send_command("compact")
        except PiRpcError as error:
            if str(error) == f"Pi rejected compact: {COMPACT_ALREADY_DONE}":
                # Automatic compaction already committed this session. Recover
                # its existing summary rather than discarding it as a short chat.
                response = await self._send_command("get_messages")
                data = response.get("data")
                messages = data.get("messages") if isinstance(data, dict) else None
                if isinstance(messages, list):
                    for message in reversed(messages):
                        if isinstance(message, dict) and message.get("role") == "compactionSummary":
                            summary = message.get("summary")
                            if isinstance(summary, str) and summary.strip():
                                return summary.strip()
                            break
                raise PiRpcProtocolError("Already compacted session has no saved summary") from error
            if COMPACT_TOO_SMALL not in str(error):
                raise
            return None
        except asyncio.TimeoutError as error:
            await self.stop()
            raise PiRpcError("Pi compact timed out; old session file retained") from error
        data = response.get("data")
        summary = data.get("summary") if isinstance(data, dict) else None
        if not isinstance(summary, str) or not summary.strip():
            raise PiRpcProtocolError("Pi compact returned no summary")
        return summary.strip()

    async def get_state(self) -> JsonObject:
        """Return Pi's current RPC session state."""
        await self.start()
        response = await self._send_command("get_state")
        data = response.get("data")
        return data if isinstance(data, dict) else {}

    async def _prompt_started(
        self,
        message: str,
        on_event: RpcEventHandler,
        images: list[RpcImage] | None,
        on_accepted: RpcAcceptedHandler | None,
        progress: asyncio.Queue[None],
    ) -> PiRunResult:
        if self._active_run is not None:
            raise PiRpcError("Pi RPC already has an active prompt")
        settled: asyncio.Future[PiRunResult] = (
            asyncio.get_running_loop().create_future()
        )
        self._active_run = _ActiveRun(on_event, settled, [], progress)
        fields: JsonObject = {"message": message}
        if images:
            fields["images"] = [image.to_json() for image in images]
        try:
            await self._send_command("prompt", fields)
            if on_accepted is not None:
                await on_accepted()
            return await settled
        finally:
            # A malformed command reply can fail _send_command before this
            # future is awaited. Consume its error to avoid an orphaned
            # "Future exception was never retrieved" diagnostic.
            if settled.done():
                if not settled.cancelled():
                    settled.exception()
            else:
                settled.cancel()
            self._active_run = None

    async def _prompt_with_idle_timeout(
        self,
        message: str,
        on_event: RpcEventHandler,
        images: list[RpcImage] | None,
        on_accepted: RpcAcceptedHandler | None,
    ) -> PiRunResult:
        """Wait for completion while resetting the deadline on real Pi progress."""
        progress: asyncio.Queue[None] = asyncio.Queue(maxsize=1)
        prompt_task = asyncio.create_task(
            self._prompt_started(message, on_event, images, on_accepted, progress))
        progress_task: asyncio.Task[None] | None = None
        try:
            while True:
                progress_task = asyncio.create_task(progress.get())
                done, _ = await asyncio.wait(
                    {prompt_task, progress_task}, timeout=self._timeout_seconds,
                    return_when=asyncio.FIRST_COMPLETED)
                if prompt_task in done:
                    return await prompt_task
                if progress_task in done:
                    continue
                raise PiRpcTimeout(self._timeout_seconds)
        finally:
            if progress_task is not None and not progress_task.done():
                progress_task.cancel()
                await asyncio.gather(progress_task, return_exceptions=True)
            if not prompt_task.done():
                prompt_task.cancel()
                await asyncio.gather(prompt_task, return_exceptions=True)

    def _command_deadline(self, command: str) -> float:
        """Silence each command may leave unanswered before it fails.

        Compaction legitimately runs for minutes on a long conversation, so it
        is bounded by the client's configured interval rather than the short
        control deadline. A caller that configures a tighter interval keeps it.
        """
        if command == "compact":
            return self._timeout_seconds
        return min(self._timeout_seconds, CONTROL_COMMAND_TIMEOUT_SECONDS)

    async def _write_command(
        self,
        command: str,
        fields: JsonObject | None = None,
    ) -> tuple[str, asyncio.Future[RpcResponse]]:
        """Write one command frame and return its id and pending reply future."""
        process = self._process
        if process is None or process.stdin is None:
            raise PiRpcError("Pi RPC process is not available")
        self._request_number += 1
        request_id = f"pi-{self._request_number}"
        request: JsonObject = {"id": request_id, "type": command}
        if fields:
            request.update(fields)
        response: asyncio.Future[RpcResponse] = (
            asyncio.get_running_loop().create_future()
        )
        self._pending_responses[request_id] = response
        try:
            async with self._write_lock:
                process.stdin.write((json.dumps(request) + "\n").encode())
                await process.stdin.drain()
        except BaseException:
            # Nothing reached Pi, so no reply can arrive for this request.
            self._pending_responses.pop(request_id, None)
            raise
        return request_id, response

    async def _send_command(
        self,
        command: str,
        fields: JsonObject | None = None,
    ) -> RpcResponse:
        request_id, response = await self._write_command(command, fields)
        try:
            if command == "prompt":
                result = await response  # Productive prompt idle deadline is managed above.
            else:
                deadline = self._command_deadline(command)
                try:
                    result = await asyncio.wait_for(response, deadline)
                except TimeoutError as timeout_error:
                    raise PiRpcTimeout(deadline) from timeout_error
        finally:
            self._pending_responses.pop(request_id, None)
        rejection = _rejection(command, result)
        if rejection is not None:
            raise rejection
        return result

    async def _read_stdout(self, stream: asyncio.StreamReader) -> None:
        try:
            while True:
                raw_line = await stream.readline()
                if not raw_line:
                    if self._process is not None:
                        await self._log_stderr_exit()
                    raise PiRpcError("Pi RPC process exited unexpectedly")
                event = decode_json_object(raw_line)
                if event.get("type") == "response":
                    reply = parse_rpc_response(event)
                    pending = self._pending_responses.get(reply["id"])
                    if pending is not None and not pending.done():
                        pending.set_result(reply)
                    continue
                active = self._active_run
                if active is None:
                    log.debug("Ignored Pi event without active prompt: %s", event.get("type"))
                    continue
                await active.on_event(event)
                if _is_progress_event(event) and active.progress.empty():
                    active.progress.put_nowait(None)
                if event.get("type") == "agent_start":
                    active.generation += 1
                    active.settlement_pending = False
                    active.awaiting_continuation = False
                if event.get("type") == "message_update":
                    update = event.get("assistantMessageEvent")
                    if isinstance(update, dict) and update.get("type") == "text_delta":
                        delta = update.get("delta")
                        if isinstance(delta, str):
                            active.text_deltas.append(delta)
                elif event.get("type") == "message_end":
                    message = event.get("message")
                    if isinstance(message, dict) and message.get("role") == "assistant":
                        # Pi may recover an overflow/provider error within this
                        # same run. Only its latest completed assistant message
                        # decides success at agent_settled.
                        active.provider_error = None
                        if message.get("stopReason") == "error":
                            error_detail = message.get("errorMessage")
                            match = re.match(r"^(\d{3})(?::|\s|$)", error_detail) if isinstance(error_detail, str) else None
                            active.provider_error = PiProviderError(int(match.group(1)) if match else None)
                        active.complete_text = assistant_text_from_message(event)
                        active.has_complete_message = True
                elif (event.get("type") == "agent_settled" or
                      (self._settle_on_agent_end and event.get("type") == "agent_end")) and not active.settled.done():
                    # A concurrent input command may atomically start a new
                    # run after this settlement. Keep the same collector alive
                    # until its admission response and continuation settle.
                    active.settlement_pending = True
                    self._settle_active(active)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error = exc if isinstance(exc, PiRpcError) else PiRpcError(
                "Pi RPC reader failed"
            )
            self._fail_pending(error)
            raise error

    def _fail_pending(self, error: PiRpcError) -> None:
        for pending in self._pending_responses.values():
            if not pending.done():
                pending.set_exception(error)
        active = self._active_run
        if active is not None and not active.settled.done():
            active.settled.set_exception(error)

    async def _log_stderr_exit(self) -> None:
        """Report a bounded stderr fingerprint once the drain has caught up."""
        process = self._process
        if process is None:
            return
        # Pipe EOF can precede the child watcher's returncode update. Give it
        # the existing bounded diagnostic grace; a closed pipe is not a hang.
        if process.returncode is None:
            try:
                await asyncio.wait_for(process.wait(), STDERR_DRAIN_GRACE_SECONDS)
            except asyncio.TimeoutError:
                pass
        drain = self._stderr_task
        if drain is not None and not drain.done() and drain is not asyncio.current_task():
            await asyncio.wait({drain}, timeout=STDERR_DRAIN_GRACE_SECONDS)
        shapes = [shape for shape in self._stderr_tail if shape][-STDERR_TAIL_LINES:]
        log.error("Pi RPC process exited; returncode=%s stderr_lines=%d stderr_bytes=%d "
                  "stderr_digest=%s stderr_shape=%s", process.returncode, self._stderr_lines,
                  self._stderr_bytes, stderr_digest(shapes), " | ".join(shapes) or "none")

    async def _drain_stderr(
        self,
        stream: asyncio.StreamReader,
    ) -> None:
        while True:
            line = await stream.readline()
            if not line:
                return
            self._stderr_lines += 1
            self._stderr_bytes += len(line)
            # Arbitrary stderr can contain tool output, image data or secrets,
            # so only its bounded shape is retained.
            self._stderr_tail.append(stderr_fingerprint(line))
            log.debug("Pi stderr line received bytes=%d", len(line))
