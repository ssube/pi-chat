"""Private, atomic checkpoint state for one bridge conversation."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, cast


@dataclass(frozen=True)
class SessionKey:
    agent: str
    transport: str
    scope: str

    @property
    def scope_hash(self) -> str:
        return hashlib.sha256(self.scope.encode("utf-8")).hexdigest()[:24]

    def directory(self, root: Path) -> Path:
        return root / self.agent / self.transport / self.scope_hash


@dataclass
class Checkpoint:
    epoch: int = 0
    summary: str = ""
    last_activity_ms: int = 0
    status: str = "idle"
    requester: str = ""
    request_id: str = ""
    last_message_id: str = ""
    # Highest Discord message handled, or an explicit catch-up baseline. This
    # lives beside last_message_id so a crash after admission cannot replay work.
    last_observed_message_id: str = ""
    discord_recovery_gap: str = ""
    final_sent: bool = False
    summary_saved_ms: int = 0
    last_wake_id: str = ""


_STATUSES = frozenset({"idle", "running", "cancel_requested", "final_pending",
                       "interrupted", "delivery_error", "error",
                       "checkpoint_error", "checkpointed"})


def _checkpoint_field(value: Any, name: str, expected: type) -> Any:
    if type(value) is not expected:
        raise ValueError(f"Bridge checkpoint {name} must be {expected.__name__}")
    if expected is int and cast(int, value) < 0:
        raise ValueError(f"Bridge checkpoint {name} must be nonnegative")
    if name == "status" and value not in _STATUSES:
        raise ValueError("Bridge checkpoint status is unknown")
    if name == "last_observed_message_id" and value and not cast(str, value).isdecimal():
        raise ValueError("Bridge checkpoint Discord observation must be a message ID")
    if name == "discord_recovery_gap" and len(cast(str, value)) > 200:
        raise ValueError("Bridge checkpoint Discord gap marker is too long")
    return value


def read_checkpoint(directory: Path) -> Checkpoint:
    path = directory / "state.json"
    if not path.exists():
        return Checkpoint()
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise ValueError("Bridge checkpoint is not an object")
    defaults = Checkpoint()
    values: dict[str, Any] = {}
    for name in Checkpoint.__dataclass_fields__:
        default = getattr(defaults, name)
        values[name] = _checkpoint_field(data.get(name, default), name, type(default))
    return Checkpoint(**values)


def write_checkpoint(directory: Path, checkpoint: Checkpoint) -> None:
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    encoded = (json.dumps(asdict(checkpoint), ensure_ascii=False, sort_keys=True) + "\n").encode()
    descriptor, name = tempfile.mkstemp(prefix=".state-", suffix=".tmp", dir=directory)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, directory / "state.json")
        parent = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    finally:
        if os.path.exists(name):
            os.unlink(name)
