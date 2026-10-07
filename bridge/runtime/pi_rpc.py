"""Async client for Pi's newline-delimited JSON RPC mode.

Adapted from Xuthal Labs' Conclave Swarm Pi RPC client (MIT).
"""

from __future__ import annotations

import asyncio
import base64
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
                "agent_settled"}:
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
        self._prompt_lock = asyncio.Lock()
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
        return self._active_run is not None

    async def start(self) -> None:
        process = self._process
        if process is not None and process.returncode is None:
            return
        self._stderr_tail.clear()
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
        """Deliver new input during an active turn before Pi's next model call."""
        if not self.active:
            raise PiRpcError("Cannot steer a settled Pi session")
        fields: JsonObject = {"message": message}
        if images:
            fields["images"] = [image.to_json() for image in images]
        await self._send_command("steer", fields)

    async def compact(self) -> str | None:
        """Return a summary, or skip compaction when Pi says the chat is too short."""
        if self.active:
            raise PiRpcError("Cannot compact an active Pi turn")
        await self.start()
        try:
            response = await asyncio.wait_for(self._send_command("compact"),
                                              timeout=min(self._timeout_seconds, 120))
        except PiRpcError as error:
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

    async def _send_command(
        self,
        command: str,
        fields: JsonObject | None = None,
    ) -> RpcResponse:
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
            result = await response
        finally:
            self._pending_responses.pop(request_id, None)
        if result.get("success") is not True:
            error = result.get("error")
            detail = error if isinstance(error, str) else f"{command} rejected"
            raise PiRpcError(f"Pi rejected {command}: {detail}")
        return result

    async def _read_stdout(self, stream: asyncio.StreamReader) -> None:
        try:
            while True:
                raw_line = await stream.readline()
                if not raw_line:
                    detail = "\n".join(self._stderr_tail).strip()
                    if detail:
                        log.error("Pi RPC process exited; stderr tail:\n%s", detail)
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
                if event.get("type") == "message_update":
                    update = event.get("assistantMessageEvent")
                    if isinstance(update, dict) and update.get("type") == "text_delta":
                        delta = update.get("delta")
                        if isinstance(delta, str):
                            active.text_deltas.append(delta)
                elif event.get("type") == "message_end":
                    message = event.get("message")
                    if (isinstance(message, dict) and message.get("role") == "assistant"
                            and message.get("stopReason") == "error"):
                        error_detail = message.get("errorMessage")
                        match = re.match(r"^(\d{3})(?::|\s|$)", error_detail) if isinstance(error_detail, str) else None
                        active.provider_error = PiProviderError(int(match.group(1)) if match else None)
                    if isinstance(message, dict) and message.get("role") == "assistant":
                        active.complete_text = assistant_text_from_message(event)
                        active.has_complete_message = True
                elif (event.get("type") == "agent_settled" or
                      (self._settle_on_agent_end and event.get("type") == "agent_end")) and not active.settled.done():
                    if active.provider_error is not None:
                        active.settled.set_exception(active.provider_error)
                    else:
                        # The completed assistant message is authoritative. Deltas
                        # can include earlier assistant messages before tool calls.
                        text = (active.complete_text if active.has_complete_message
                                else "".join(active.text_deltas)).strip()
                        active.settled.set_result(PiRunResult(text=text))
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

    async def _drain_stderr(
        self,
        stream: asyncio.StreamReader,
    ) -> None:
        while True:
            line = await stream.readline()
            if not line:
                return
            text = line.decode(errors="replace").rstrip()
            self._stderr_tail.append(text)
            log.warning("Pi stderr: %s", text)
