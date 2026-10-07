"""Run an agent's enabled Matrix and Discord adapters in one process."""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import signal
from pathlib import Path
from typing import Any

from .adapters.discord import DiscordAdapter
from .adapters.matrix import MatrixAdapter
from .activity import ActivityRejected, ActivityService
from .activity_socket import ActivitySocket
from .config import Profile
from .history_service import HistoryPage, HistoryRejected, HistoryRequest, HistoryService
from .metrics import MetricsService
from .reaction_service import ReactionRejected, ReactionRequest, ReactionService
from .review_queue import ForgejoReviews, ReviewQueue
from .room_lease import RoomLeases
from .runtime.session import BridgeRuntime
from .runtime.state import SessionKey
from .sessions_service import SessionsRequest, SessionsService


log = logging.getLogger("agent_bridge")


async def run(profile: Profile) -> None:
    adapters: dict[str, MatrixAdapter | DiscordAdapter] = {}
    history_tokens: dict[SessionKey, str] = {}
    reaction_tokens: dict[SessionKey, str] = {}
    activity_tokens: dict[SessionKey, str] = {}

    async def output(key: SessionKey, kind: str, text: str, request_id: str) -> None:
        adapter = adapters.get(key.transport)
        if adapter is None:
            raise RuntimeError(f"No {key.transport} adapter for {profile.agent}")
        await adapter.output(key, kind, text, request_id)

    def pi_client(key: SessionKey, session_file: Path):
        history_token = secrets.token_urlsafe(32)
        reaction_token = secrets.token_urlsafe(32)
        activity_token = secrets.token_urlsafe(32)
        history_tokens[key] = history_token
        reaction_tokens[key] = reaction_token
        activity_tokens[key] = activity_token
        return profile.pi_client(key, session_file,
                                 history_capability=history_token,
                                 reaction_capability=reaction_token,
                                 activity_capability=activity_token)

    runtime = BridgeRuntime(profile.state_dir / "sessions", pi_client, output,
                            failure_outbox=profile.observability_outbox,
                            image_captioner=(profile.describe_images
                                             if profile.image_model else None))
    review_queue = None
    if profile.reviews and profile.matrix:
        review_queue = ReviewQueue(profile.agent, profile.state_dir / "reviews", profile.reviews,
                                   runtime, ForgejoReviews(os.environ[profile.reviews.token_env],
                                   profile.reviews.reviewer, profile.reviews.base_url,
                                   profile.reviews.repositories), profile.matrix)
    room_leases = None
    if profile.matrix is not None:
        async def prepare_room(room_id: str) -> bool:
            return await runtime.checkpoint_for_handoff(
                SessionKey(profile.agent, "matrix", f"{room_id}:main"))

        room_leases = RoomLeases(
            profile.state_dir / "bridge-room-lease.sock",
            profile.matrix.rooms,
            profile.matrix.user_id, prepare_room)
        adapters["matrix"] = MatrixAdapter(profile.agent, profile.matrix, runtime,
                                            profile.state_dir / "matrix", review_queue,
                                            room_leases)
    if profile.discord is not None:
        adapters["discord"] = DiscordAdapter(profile.agent, profile.discord, runtime)
    if review_queue is not None:
        async def review_react(room_id: str, event_id: str, emoji: str) -> None:
            await adapters["matrix"].react(SessionKey(profile.agent, "matrix", f"{room_id}:main"), event_id, emoji)
        review_queue.react = review_react
    async def fetch_history(request: HistoryRequest) -> HistoryPage:
        key = SessionKey(profile.agent, request["transport"], request["scope"])
        if history_tokens.get(key) != request["capability"]:
            raise HistoryRejected("capability")
        adapter = adapters.get(key.transport)
        if adapter is None or not await runtime.is_awake(key):
            raise HistoryRejected("awake")
        return await adapter.history_page(key, request["before"], request["limit"])

    history_service = HistoryService(profile.state_dir / "bridge-history.sock",
                                     fetch_history)
    async def react(request: ReactionRequest) -> None:
        key = SessionKey(profile.agent, request["transport"], request["scope"])
        if reaction_tokens.get(key) != request["capability"]:
            raise ReactionRejected("capability")
        adapter = adapters.get(key.transport)
        if adapter is None or not await runtime.is_awake(key):
            raise ReactionRejected("awake")
        await adapter.react(key, request["message_id"], request["emoji"])
        await runtime.record_reaction(key, request["message_id"])

    reaction_service = ReactionService(profile.state_dir / "bridge-reaction.sock", react)
    async def fetch_sessions(request: SessionsRequest) -> dict[str, object]:
        return await runtime.session_summary(detail=request["detail"])

    sessions_service = SessionsService(profile.state_dir / "bridge-sessions.sock",
                                         fetch_sessions)
    activity_service = ActivityService(profile.agent, runtime, tuple(adapters.values()),
                                       profile.state_dir / "activity.json",
                                       profile.generate_activity)
    pushgateway_url = os.environ.get("AGENT_PUSHGATEWAY_URL", "").strip()
    metrics_service = (MetricsService(profile.agent, runtime, pushgateway_url)
                       if pushgateway_url else None)

    async def set_activity(request: dict[str, Any]) -> None:
        if request.get("operator") is True:
            await activity_service.set_status(request["text"], hours=request["hours"])
            return
        transport = request["transport"]
        if transport not in {"matrix", "discord"}:
            raise ActivityRejected("transport")
        scope = request["scope"]
        if not isinstance(scope, str) or not scope:
            raise ActivityRejected("scope")
        key = SessionKey(profile.agent, transport, scope)
        if (activity_tokens.get(key) != request["capability"] or
                not await runtime.is_awake(key)):
            raise ActivityRejected("awake")
        text = request["text"]
        hours = request["hours"]
        if not isinstance(text, str) or type(hours) not in (int, float):
            raise ActivityRejected("fields")
        await activity_service.set_status(text, hours=hours)

    activity_socket = ActivitySocket(profile.state_dir / "bridge-activity.sock",
                                     set_activity)
    await history_service.start()
    try:
        if room_leases is not None:
            await room_leases.start()
        await reaction_service.start()
        await sessions_service.start()
        await activity_socket.start()
    except Exception:
        await activity_socket.close()
        await sessions_service.close()
        await reaction_service.close()
        if room_leases is not None:
            await room_leases.close()
        await history_service.close()
        raise
    stopping = asyncio.Event()
    checkpoint_requested = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stopping.set)
    loop.add_signal_handler(signal.SIGHUP, checkpoint_requested.set)

    async def checkpoint_on_signal() -> None:
        while not stopping.is_set():
            await checkpoint_requested.wait()
            checkpoint_requested.clear()
            if stopping.is_set():
                break
            try:
                count = await runtime.sweep_idle(force=True)
                log.info("SIGHUP checkpointed %d idle conversations", count)
            except Exception:
                log.exception("SIGHUP checkpoint sweep failed")

    async def sweep() -> None:
        while not stopping.is_set():
            await asyncio.sleep(15)
            try:
                count = await runtime.sweep_idle()
                if count:
                    log.info("Checkpointed %d idle conversations", count)
            except Exception:
                log.exception("Idle checkpoint sweep failed")

    async def wait_for_shutdown() -> None:
        await stopping.wait()

    tasks = [asyncio.create_task(adapter.run(), name=f"{name}-adapter")
             for name, adapter in adapters.items()]
    tasks.append(asyncio.create_task(sweep(), name="idle-sweep"))
    tasks.append(asyncio.create_task(checkpoint_on_signal(), name="sighup-checkpoint"))
    tasks.append(asyncio.create_task(activity_service.run(), name="activity-status"))
    if metrics_service is not None:
        tasks.append(asyncio.create_task(metrics_service.run(), name="metrics"))
    if review_queue is not None:
        tasks.append(asyncio.create_task(review_queue.run(), name="pr-review-queue"))
    tasks.append(asyncio.create_task(wait_for_shutdown(), name="shutdown-signal"))
    log.info("Bridge starting for %s with transports %s", profile.agent,
             ",".join(adapters))
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            if task.cancelled():
                raise RuntimeError(f"{task.get_name()} was cancelled unexpectedly")
            failure = task.exception()
            if failure is not None:
                raise failure
            if task.get_name() != "shutdown-signal":
                raise RuntimeError(f"{task.get_name()} ended unexpectedly")
    finally:
        stopping.set()
        await sessions_service.close()
        await reaction_service.close()
        await activity_socket.close()
        await history_service.close()
        if room_leases is not None:
            await room_leases.close()
        await activity_service.shutdown()
        if metrics_service is not None:
            await metrics_service.shutdown()
        await asyncio.gather(*(adapter.stop() for adapter in adapters.values()),
                             return_exceptions=True)
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await runtime.close()
        if review_queue is not None:
            review_queue.close()


def main() -> None:
    logging.basicConfig(level=os.environ.get("AGENT_BRIDGE_LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    path = os.environ.get("AGENT_BRIDGE_PROFILE")
    if not path:
        raise SystemExit("AGENT_BRIDGE_PROFILE must name a private JSON profile")
    asyncio.run(run(Profile.load(Path(path))))


if __name__ == "__main__":
    main()
