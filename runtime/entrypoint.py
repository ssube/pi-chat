"""Non-root startup validation; no owner hooks, shell sourcing or permission repair."""
import os
from pathlib import Path
import stat
import sys

from bridge.config import Profile


SUPERVISOR = "/opt/venv/bin/supervisord"


def private_directory(path):
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    info = path.stat()
    if not path.is_dir() or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise ValueError("private directory ownership or permissions")


def validate(profile_path):
    if os.geteuid() == 0:
        raise ValueError("root execution is not supported")
    info = profile_path.stat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
            or info.st_mode & 0o077):
        raise ValueError("profile must be owner-readable only and owned by the runtime UID")
    profile = Profile.load(profile_path)
    for directory in (Path(os.environ["HOME"]), profile.state_dir, profile.pi_agent_dir):
        private_directory(directory)
    if not profile.work_dir.is_dir() or not os.access(profile.work_dir, os.W_OK):
        raise ValueError("workspace must exist and be writable")
    if not profile.pi_executable.is_file() or not os.access(profile.pi_executable, os.X_OK):
        raise ValueError("Pi executable is unavailable")
    return profile


def main():
    if os.geteuid() == 0:
        raise SystemExit("Runtime refuses root; use a non-root UID and matching mount ownership.")
    command = sys.argv[1:]
    if not command:
        raise SystemExit("A runtime command is required.")
    os.umask(0o077)
    if command[0] == SUPERVISOR:
        try:
            validate(Path(os.environ["AGENT_BRIDGE_PROFILE"]))
        except (OSError, KeyError, ValueError):
            raise SystemExit("Runtime startup refused: check profile, credentials, mount ownership and private permissions.") from None
    os.execvp(command[0], command)


if __name__ == "__main__":
    main()
