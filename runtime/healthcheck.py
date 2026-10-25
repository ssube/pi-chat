"""Probe only local bridge startup; never query a provider or transport account."""
import json
import os
from pathlib import Path
import socket
import stat
import sys


def probe(profile_path):
    profile = json.loads(profile_path.read_text())
    directory = Path(profile["state_dir"])
    if not directory.is_absolute():
        return False
    path = directory / "bridge-health.sock"
    for target, kind in ((directory, stat.S_ISDIR), (path, stat.S_ISSOCK)):
        info = target.stat()
        if not kind(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            return False
    for endpoint in ("live", "ready"):
        with socket.socket(socket.AF_UNIX) as connection:
            connection.settimeout(2)
            connection.connect(str(path))
            connection.sendall(f"GET /health/{endpoint} HTTP/1.1\r\nHost: localhost\r\n\r\n".encode())
            response = b""
            while b"\r\n" not in response and len(response) < 128:
                chunk = connection.recv(128 - len(response))
                if not chunk:
                    break
                response += chunk
            if response.split(b"\r\n", 1)[0] != b"HTTP/1.1 200 OK":
                return False
    return True


def main():
    try:
        healthy = probe(Path(os.environ["AGENT_BRIDGE_PROFILE"]))
    except (OSError, ValueError, KeyError, TypeError):
        healthy = False
    if not healthy:
        print("Local bridge startup is unavailable or pending.", file=sys.stderr)
    return 0 if healthy else 1


if __name__ == "__main__":
    raise SystemExit(main())
