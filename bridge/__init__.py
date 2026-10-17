"""Shared agent bridge release version."""

import re
import subprocess
from pathlib import Path


def _version_from_git(root: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "describe", "--tags", "--long",
             "--match", "v[0-9]*", "--abbrev=8", "HEAD"],
            check=True, capture_output=True, text=True, timeout=2,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return "unknown"
    match = re.fullmatch(r"v(\d+\.\d+\.\d+)-(\d+)-g([0-9a-f]{8,})", result.stdout.strip())
    if match is None:
        return "unknown"
    version, count, commit = match.groups()
    return version if int(count) == 0 else f"{version}-{int(count)}-{commit}"


__version__ = _version_from_git(Path(__file__).resolve().parent.parent)
