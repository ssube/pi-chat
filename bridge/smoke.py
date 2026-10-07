"""One private, safe Pi RPC gate using an owner's actual bridge profile."""

from __future__ import annotations

import argparse
import asyncio
import tempfile
from pathlib import Path

from .config import Profile
from .runtime.state import SessionKey


def _marker_matches(text: str) -> bool:
    return text.strip().strip("`'\"“”").rstrip(".!?").strip() == "BRIDGE_RPC_OK"


async def smoke(profile: Profile, *, timeout_seconds: float = 300) -> None:
    profile.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="bridge-pi-smoke-", dir=profile.state_dir) as directory:
        private_dir = Path(directory)
        private_dir.chmod(0o700)
        key = SessionKey(profile.agent, "matrix" if profile.matrix else "discord", "pi-smoke")
        client = profile.pi_client(key, private_dir / "session.jsonl",
                                   safe_mode=True, timeout_seconds=timeout_seconds)

        async def on_event(event: dict) -> None:
            return None

        try:
            result = await client.prompt("Reply with exactly BRIDGE_RPC_OK and nothing else.", on_event)
            if not _marker_matches(result.text):
                raise RuntimeError(
                    f"Pi RPC completed, but the smoke marker was not returned "
                    f"(reply length {len(result.text)} characters)"
                )
            summary = await client.compact()
            if summary is not None and not summary.strip():
                raise RuntimeError("Pi RPC compaction returned no summary")
        finally:
            await client.stop()
    if summary is None:
        print("Pi RPC smoke passed: turn completed, compaction skipped (session too small), process stopped")
    else:
        print("Pi RPC smoke passed: turn completed, compaction returned a summary, process stopped")


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify one owner's Pi RPC profile")
    parser.add_argument("--profile", required=True, type=Path)
    parser.add_argument("--timeout", type=float, default=300,
                        help="maximum seconds for the prompt (default: 300)")
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    asyncio.run(smoke(Profile.load(args.profile), timeout_seconds=args.timeout))


if __name__ == "__main__":
    main()
