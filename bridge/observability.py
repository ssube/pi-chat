"""Privacy-bounded bridge failures for the shared Pi observability outbox.

Tool failures belong to the Pi extension. This module records failures that
occur outside a tool call without copying prompts, transcripts, or stderr.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .runtime.pi_rpc import PiProviderError, PiRpcError, PiRpcTimeout
from .runtime.state import SessionKey


def _write_private(path: Path, payload: bytes) -> None:
    """Atomically publish one owner-private JSON file."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=".incident-", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def record_bridge_failure(outbox: Path, key: SessionKey, error: PiRpcError,
                          *, epoch: int, phase: str,
                          request_id: str | None = None,
                          matrix_event_id: str | None = None) -> str:
    """Record safe structured facts about a failed Pi session.

    Pi's arbitrary exception text and stderr may contain secrets. Neither is
    included here; detailed tool evidence is captured by the Pi extension's
    shared redaction filter instead.

    ``request_id`` and ``matrix_event_id`` are stored only in the owner-private
    detail file so the shared outbox record remains privacy-bounded. They may
    be used to correlate bridge failures with Matrix events and provider
    requests without exposing raw room identifiers.
    """
    if key.transport not in {"matrix", "discord"}:
        raise ValueError("Unsupported bridge transport")
    if phase not in {"prompt", "output_revision", "compact"}:
        raise ValueError("Unsupported bridge phase")
    occurrence_id = f"pierr-{uuid.uuid4()}"
    occurred_at = datetime.now(timezone.utc).isoformat()
    is_provider = isinstance(error, PiProviderError)
    kind = "provider" if is_provider else "session"
    error_class = type(error).__name__
    status = error.status if isinstance(error, PiProviderError) else None
    auth_failure = error.auth if isinstance(error, PiProviderError) else None
    summary = f"{error_class} HTTP {status}" if status is not None else error_class
    if auth_failure:
        # The cause, not the provider's diagnostic text: a dead sign-in is
        # actionable and groups separately from an ordinary provider error.
        summary = f"{summary} (provider sign-in rejected)"
    # The scope is hashed by SessionKey. Raw room/thread IDs do not enter the
    # outbox or detail file. Request and Matrix event IDs are kept only in the
    # owner-private detail file for correlation.
    detail = {
        "id": occurrence_id,
        "agent": key.agent,
        "origin": f"bridge_{key.transport}",
        "occurred_at": occurred_at,
        "kind": kind,
        "phase": phase,
        "error_class": error_class,
        "provider_http_status": status,
        "provider_auth_failure": auth_failure,
        "idle_timeout_seconds": error.idle_seconds if isinstance(error, PiRpcTimeout) else None,
        "scope_hash": key.scope_hash,
        "session_epoch": epoch,
        "request_id": request_id,
        "matrix_event_id": matrix_event_id,
        "evidence_limit": "structured bridge facts; no prompt, transcript, or stderr",
    }
    detail_bytes = (json.dumps(detail, sort_keys=True, separators=(",", ":")) + "\n").encode()
    detail_path = outbox.parent / "details" / f"{occurrence_id}.json"
    _write_private(detail_path, detail_bytes)
    signature = f"{kind}:{phase}:{error_class}"
    if status is not None:
        signature += f":http-{status}"
    if auth_failure:
        signature += ":auth"
    entry = {
        "id": occurrence_id,
        "agent": key.agent,
        "origin": f"bridge_{key.transport}",
        "occurred_at": occurred_at,
        "kind": kind,
        "outcome": "error",
        "tool_name": None,
        "signature": signature,
        "detail_ref": str(detail_path),
        "detail_sha256": hashlib.sha256(detail_bytes).hexdigest(),
        "redacted_summary": summary,
    }
    _write_private(outbox / f"{occurrence_id}.json",
                   (json.dumps(entry, sort_keys=True, separators=(",", ":")) + "\n").encode())
    return occurrence_id
