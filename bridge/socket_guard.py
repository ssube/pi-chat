"""Refuse to replace a live or unexpected private Unix socket."""

from __future__ import annotations

import os
import socket
import stat
from pathlib import Path


def remove_stale_socket(path: Path, service: str) -> None:
    if not (path.exists() or path.is_socket()):
        return
    info = path.lstat()
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.geteuid():
        raise RuntimeError(f"refusing to replace an unexpected {service} socket")
    probe = socket.socket(socket.AF_UNIX)
    try:
        if probe.connect_ex(str(path)) == 0:
            raise RuntimeError(f"another {service} service owns this socket")
    finally:
        probe.close()
    path.unlink()
