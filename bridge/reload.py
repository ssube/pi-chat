"""Re-read a running bridge's private profile without restarting its transports."""

from __future__ import annotations

import logging
from dataclasses import fields
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping

from .config import Profile

log = logging.getLogger("agent_bridge.reload")

# Settings whose new value is read again for the next Matrix event, channel
# message, or Pi session. Everything else describes a transport, a private
# state directory, or a child-process topology that this process already
# opened, so changing it in place would leave two of something running.
LIVE_PROFILE = frozenset({"provider", "model", "image_model", "modes", "default_mode",
                          "pi_extra_args", "pi_pass_env", "pi_env_file",
                          "identity_files"})
LIVE_MATRIX = frozenset({"rooms", "direct_rooms", "hookshot_notice_rooms", "pr_review_rooms",
                         "wake_on_any_room_ids", "display_name", "startup_room_id",
                         "broadcast_allowed_room_ids", "hookshot_user_id"})
LIVE_DISCORD = frozenset({"channel_ids", "wake_on_any_channel_ids",
                          "broadcast_allowed_channel_ids", "startup_channel_id"})


def _changed(before: Any, after: Any, allowed: frozenset[str]) -> tuple[list[str], list[str]]:
    live: list[str] = []
    restart: list[str] = []
    for name in (item.name for item in fields(type(before))):
        if getattr(before, name) != getattr(after, name):
            (live if name in allowed else restart).append(name)
    return live, restart


def _compare(current: Profile, updated: Profile) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return the changed setting names that apply live and those that need a restart."""
    live: list[str] = []
    restart: list[str] = []
    for name in (item.name for item in fields(Profile)):
        if name in {"matrix", "discord"}:
            continue
        if getattr(current, name) != getattr(updated, name):
            (live if name in LIVE_PROFILE else restart).append(name)
    for name, before, after, allowed in (
            ("matrix", current.matrix, updated.matrix, LIVE_MATRIX),
            ("discord", current.discord, updated.discord, LIVE_DISCORD)):
        if (before is None) != (after is None):
            restart.append(name)
            continue
        if before is None or after is None:
            continue
        transport_live, transport_restart = _changed(before, after, allowed)
        live.extend(f"{name}.{item}" for item in transport_live)
        restart.extend(f"{name}.{item}" for item in transport_restart)
    # The review queue and owner notices are created once, when the profile
    # first enables PR review rooms. Adding the first room or removing the last
    # one therefore needs a restart; the room list itself stays live.
    if (bool(current.matrix and current.matrix.pr_review_rooms) !=
            bool(updated.matrix and updated.matrix.pr_review_rooms)):
        restart.append("matrix.pr_review_rooms")
    return (tuple(dict.fromkeys(live)), tuple(dict.fromkeys(restart)))


def restart_required(current: Profile, updated: Profile) -> tuple[str, ...]:
    """Name the changed settings that this process cannot adopt in place."""
    return _compare(current, updated)[1]


def adopt(updated: Profile, *, adapters: Mapping[str, Any] | None = None,
          room_leases: Any = None, review_queue: Any = None,
          runtime: Any = None, activity: Any = None) -> None:
    """Point already-running services at a structurally compatible profile.

    Each assignment replaces one whole frozen settings object, so a reader
    either sees the old profile or the new one and never a half-applied mix.
    """
    if adapters is not None:
        matrix = adapters.get("matrix")
        if matrix is not None and updated.matrix is not None:
            matrix.settings = updated.matrix
        discord = adapters.get("discord")
        if discord is not None and updated.discord is not None:
            discord.settings = updated.discord
    if updated.matrix is not None:
        if room_leases is not None:
            room_leases.rooms = frozenset(updated.matrix.rooms)
        if review_queue is not None:
            review_queue.matrix = updated.matrix
    if runtime is not None:
        runtime.image_captioner = (updated.describe_images
                                   if updated.image_model else None)
        runtime.modes = dict(updated.conversation_modes)
        runtime.default_mode = updated.default_mode
        runtime.progress_summarizer = updated.summarize_progress
    if activity is not None:
        activity.generate = updated.generate_activity


def reload(path: Path, current: Profile, *, adapters: Mapping[str, Any] | None = None,
           room_leases: Any = None, review_queue: Any = None,
           runtime: Any = None, activity: Any = None) -> Profile | None:
    """Adopt a re-read profile, or keep running the previous one.

    A damaged file, an invalid profile, or a change that needs a restart keeps
    the running configuration and logs one safe summary. It never raises into
    the signal handler.
    """
    try:
        updated = Profile.load(path)
        live, restart = _compare(current, updated)
        if restart:
            log.warning("SIGHUP profile reload needs a restart; keeping the "
                        "running profile (changed: %s)", ", ".join(restart))
            return None
        adopt(updated, adapters=adapters, room_leases=room_leases,
              review_queue=review_queue, runtime=runtime, activity=activity)
        log.info("SIGHUP reloaded the bridge profile for %s (%s)", updated.agent,
                 ", ".join(live) if live else "no live changes")
        return updated
    except Exception:
        log.exception("SIGHUP profile reload failed; keeping the running profile")
        return None


async def sighup(path: Path, current: Profile, *,
                 sweep: Callable[[], Awaitable[int]],
                 adapters: Mapping[str, Any] | None = None,
                 room_leases: Any = None, review_queue: Any = None,
                 runtime: Any = None, activity: Any = None) -> Profile:
    """Reload the profile, then checkpoint idle conversations.

    A rejected reload keeps the running profile; the checkpoint is an
    independent operator request and still runs. A failed checkpoint is
    reported without discarding an adopted profile.
    """
    updated = reload(path, current, adapters=adapters, room_leases=room_leases,
                     review_queue=review_queue, runtime=runtime, activity=activity)
    try:
        count = await sweep()
        log.info("SIGHUP checkpointed %d idle conversations", count)
    except Exception:
        log.exception("SIGHUP checkpoint sweep failed")
    return current if updated is None else updated
