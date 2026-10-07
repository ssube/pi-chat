"""Durable, agent-owned queue for trusted Hookshot PR review requests."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import signal
import sqlite3
import time
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from .config import MatrixSettings, ReviewSettings
from .observability import _write_private
from .runtime.session import BridgeRuntime


log = logging.getLogger("agent_bridge.review_queue")
COMPLETE_STATES = frozenset({"APPROVED", "REQUEST_CHANGES", "COMMENT"})
OUTPUT_LIMIT = 16 * 1024
OUTPUT_DRAIN_TIMEOUT = 5
FAILURE_RETENTION_DAYS = 30
FAILURE_RETENTION_COUNT = 128
FAILURE_RETENTION_BYTES = 4 * 1024 * 1024
FAILURE_PRUNE_BATCH = 16
FAILURE_PRUNE_INTERVAL = 60
SECRET_KEY = re.compile(
    r"secret|token|password|passwd|api[_-]?key|authorization|credential|cookie|"
    r"private[_-]?key|session[_-]?key|dsn|database[_-]?url|connection[_-]?string", re.I)


class ReviewCliFailure(RuntimeError):
    def __init__(self, reason: str, exit_code: int | None = None,
                 stdout: bytes = b"", stderr: bytes = b"",
                 stdout_truncated: bool = False, stderr_truncated: bool = False):
        super().__init__(reason)
        self.reason = reason
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr
        self.stdout_truncated = stdout_truncated
        self.stderr_truncated = stderr_truncated


async def _bounded_tail(stream: asyncio.StreamReader) -> tuple[bytes, bool]:
    tail = bytearray()
    truncated = False
    while chunk := await stream.read(8192):
        tail.extend(chunk)
        if len(tail) > OUTPUT_LIMIT:
            del tail[:-OUTPUT_LIMIT]
            truncated = True
    if truncated:
        # Never expose the fragment of a secret cut at the capture boundary.
        first_newline = tail.find(b"\n")
        tail = tail[first_newline + 1:] if first_newline >= 0 else bytearray()
    return bytes(tail), truncated


async def _finish_output(stdout_task: asyncio.Task[tuple[bytes, bool]],
                         stderr_task: asyncio.Task[tuple[bytes, bool]]) -> tuple[tuple[bytes, bool],
                                                                                   tuple[bytes, bool]]:
    """Stop waiting for inherited pipes without changing the CLI exit result."""
    tasks = (stdout_task, stderr_task)
    try:
        done, pending = await asyncio.wait(tasks, timeout=OUTPUT_DRAIN_TIMEOUT)
    except asyncio.CancelledError:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    output: list[tuple[bytes, bool]] = []
    for task in tasks:
        if task in done and not task.cancelled() and task.exception() is None:
            output.append(task.result())
        else:
            output.append((b"", True))
    return output[0], output[1]


def _redact_output(data: bytes, truncated: bool) -> str:
    """Apply the shared Pi capture patterns and configured secret values."""
    value = data.decode("utf-8", errors="replace")
    secrets: list[str] = []
    redaction_file = os.environ.get("PI_CAPTURE_REDACTION_FILE")
    if redaction_file:
        configured = json.loads(Path(redaction_file).read_text())
        if not isinstance(configured, dict) or not isinstance(configured.get("secrets"), list) or any(
                not isinstance(item, str) for item in configured["secrets"]):
            raise ValueError("Invalid shared redaction configuration")
        secrets.extend(item for item in configured["secrets"] if len(item) >= 4)
    secrets.extend(value for key, value in os.environ.items()
                   if SECRET_KEY.search(key) and len(value) >= 4)
    for secret in sorted(set(secrets), key=len, reverse=True):
        value = value.replace(secret, "[REDACTED]")
    value = re.sub(r"\b(Bearer|Basic)\s+[A-Za-z0-9._~+/-]+=*", r"\1 [REDACTED]", value, flags=re.I)
    value = re.sub(r"((?:https?|postgres(?:ql)?|redis(?:s)?|amqps?|mysql)://[^\s/@]+:)[^\s/@]+@",
                   r"\1[REDACTED]@", value, flags=re.I)
    value = re.sub(r"([?&](?:token|key|secret|password|api_key|access_token|refresh_token|code)=)[^\s&#]+",
                   r"\1[REDACTED]", value, flags=re.I)
    value = re.sub(r"\b([A-Za-z_][A-Za-z0-9_]*)(\s*=\s*)(?:('[^'\n]*'|\"[^\"\n]*\")|[^\s;|&]+)",
                   lambda match: match[1] + match[2] + "[REDACTED]"
                   if SECRET_KEY.search(match[1]) else match[0], value)
    value = re.sub(r"\b(Authorization|X-Api-Key|Cookie|Set-Cookie)\s*:\s*[^\r\n]+",
                   r"\1: [REDACTED]", value, flags=re.I)
    value = re.sub(r"(--(?:token|secret|password|api-key|access-token|refresh-token)(?:=|\s+))"
                   r"(?:(['\"])[^'\"\n]*\2|[^\s;|&]+)",
                   r"\1[REDACTED]", value, flags=re.I)
    value = re.sub(r"((?:^|\s)(?:--user|-u)(?:=|\s+))"
                   r"(?:(['\"])[^'\"\n]*\2|[^\s;|&]+)",
                   r"\1[REDACTED]", value, flags=re.I | re.M)
    value = re.sub(r"\b(?:gh[pousr]_[A-Za-z0-9_]{12,}|github_pat_[A-Za-z0-9_]{12,}|"
                   r"sk-[A-Za-z0-9_-]{16,}|AKIA[0-9A-Z]{16})\b", "[REDACTED]", value)
    value = re.sub(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----[\s\S]*?"
                   r"-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", "[REDACTED]", value)
    return ("[output truncated; no complete line retained]\n" if truncated and not value
            else "[earlier output omitted]\n" if truncated else "") + value


def review_link(body: str, base_url: str, repositories: tuple[str, ...]) -> tuple[str, int] | None:
    """Accept one URL in the approved Forgejo organization, never free text."""
    pattern = re.compile(re.escape(base_url.rstrip("/")) +
                         r"/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pulls/([1-9][0-9]*)(?![0-9A-Za-z_/?#.-])")
    links = {(match.group(1), int(match.group(2))) for match in pattern.finditer(body)
             if match.group(1) in repositories}
    return next(iter(links)) if len(links) == 1 else None


class ForgejoReviews:
    def __init__(self, token: str, agent: str, base_url: str, repositories: tuple[str, ...]):
        self.token = token
        self.agent = agent
        self.base_url = base_url.rstrip("/")
        self.repositories = repositories

    def _get(self, repo: str, path: str) -> Any:
        if not self.token:
            raise RuntimeError("Agent-owned Forgejo review token is not configured")
        if repo not in self.repositories:
            raise ValueError("Repository is outside the review allowlist")
        url = f"{self.base_url}/api/v1/repos/{repo}{path}"
        request = urllib.request.Request(
            url, headers={"Authorization": f"token {self.token}"})
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.load(response)

    def status(self, repo: str, number: int) -> tuple[str, str | None, int | None, bool]:
        """Return disposition, current head, latest request id, review after request.

        Forgejo exposes review requests in the issue timeline, not /reviews.
        Pagination is bounded; an uninspectable history raises and retries.
        """
        pr = self._get(repo, f"/pulls/{number}")
        if pr.get("state") != "open":
            return "closed", None, None, False
        full_name = repo
        if (pr.get("base", {}).get("repo", {}).get("full_name") != full_name or
                pr.get("base", {}).get("ref") != "main" or
                pr.get("head", {}).get("repo", {}).get("full_name") != full_name or
                pr.get("user", {}).get("login") == self.agent):
            return "ineligible", None, None, False
        head = pr.get("head", {}).get("sha")
        if not isinstance(head, str) or not re.fullmatch(r"[0-9a-f]{40}", head):
            raise ValueError("Forgejo PR has no valid head SHA")
        requested = {item.get("login") for item in pr.get("requested_reviewers", [])
                     if isinstance(item, dict)}
        is_requested = self.agent in requested
        latest: dict[str, Any] | None = None
        for page in range(1, 21):
            batch = self._get(repo, f"/issues/{number}/timeline?page={page}&limit=50")
            if not isinstance(batch, list):
                raise ValueError("Forgejo timeline was not a list")
            for item in batch:
                assignee = item.get("assignee")
                if (item.get("type") == "review_request" and
                        isinstance(assignee, dict) and assignee.get("login") == self.agent and
                        (latest is None or item.get("id", 0) > latest.get("id", 0))):
                    latest = item
            if len(batch) < 50:
                break
        else:
            raise ValueError("Forgejo review-request timeline exceeds the bounded read")
        if latest is None and not is_requested:
            return "unrequested", head, None, False
        if latest is None or type(latest.get("id")) is not int or not latest.get("created_at"):
            raise ValueError("Requested reviewer has no inspectable request event")
        reviewed = False
        for page in range(1, 21):
            batch = self._get(repo, f"/pulls/{number}/reviews?page={page}&limit=50")
            if not isinstance(batch, list):
                raise ValueError("Forgejo reviews were not a list")
            for item in batch:
                if (item.get("user", {}).get("login") == self.agent and
                        item.get("state") in COMPLETE_STATES and
                        item.get("commit_id") == head and
                        not item.get("stale") and not item.get("dismissed") and
                        (item.get("submitted_at") or "") > latest["created_at"]):
                    reviewed = True
            if len(batch) < 50:
                break
        else:
            raise ValueError("Forgejo review history exceeds the bounded read")
        return "requested" if is_requested else "unrequested", head, latest["id"], reviewed

    def approved(self, repo: str, number: int, head: str, request_id: int) -> bool:
        """Read the exact current approval; a completed COMMENT is not approval."""
        request = None
        for page in range(1, 21):
            timeline = self._get(repo, f"/issues/{number}/timeline?page={page}&limit=50")
            if not isinstance(timeline, list):
                raise ValueError("Forgejo timeline was not a list")
            request = next((item for item in timeline if item.get("id") == request_id
                            and item.get("type") == "review_request"), request)
            if len(timeline) < 50:
                break
        if request is None:
            return False
        latest = None
        for page in range(1, 21):
            reviews = self._get(repo, f"/pulls/{number}/reviews?page={page}&limit=50")
            if not isinstance(reviews, list):
                raise ValueError("Forgejo reviews were not a list")
            for item in reviews:
                if (item.get("user", {}).get("login") == self.agent
                        and item.get("state") in COMPLETE_STATES
                        and item.get("commit_id") == head
                        and not item.get("stale") and not item.get("dismissed")
                        and (item.get("submitted_at") or "") > request["created_at"]):
                    if latest is None or (item["submitted_at"], item.get("id") or 0) > (
                            latest["submitted_at"], latest.get("id") or 0):
                        latest = item
            if len(reviews) < 50:
                break
        return latest is not None and latest["state"] == "APPROVED"


class ReviewQueue:
    def __init__(self, agent: str, state_dir: Path, settings: ReviewSettings,
                 runtime: BridgeRuntime, forgejo: ForgejoReviews,
                 matrix: MatrixSettings):
        self.agent = agent
        self.settings = settings
        self.runtime = runtime
        self.forgejo = forgejo
        self.matrix = matrix
        self.wake = asyncio.Event()
        self.react: Callable[[str, str, str], Awaitable[None]] | None = None
        state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        state_dir.chmod(0o700)
        self.db_path = state_dir / "review-queue.sqlite3"
        self.db = sqlite3.connect(self.db_path)
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("""CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY, repo TEXT NOT NULL, number INTEGER NOT NULL,
            head TEXT NOT NULL, request_id INTEGER NOT NULL, event_id TEXT NOT NULL,
            room_id TEXT, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
            not_before REAL NOT NULL DEFAULT 0, created_at REAL NOT NULL,
            UNIQUE(event_id)
        )""")
        self.db.execute("UPDATE jobs SET status='pending' WHERE status='running'")
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(jobs)")}
        for name in ("failure_ref", "failure_sha256", "room_id"):
            if name not in columns:
                self.db.execute(f"ALTER TABLE jobs ADD COLUMN {name} TEXT")
        self.db.execute("CREATE INDEX IF NOT EXISTS jobs_failure_ref ON jobs(failure_ref)")
        self.db.execute("""CREATE TABLE IF NOT EXISTS failure_artifacts (
            path TEXT PRIMARY KEY, created_at REAL NOT NULL, size INTEGER NOT NULL
        )""")
        self.db.execute("CREATE INDEX IF NOT EXISTS failure_artifacts_age "
                        "ON failure_artifacts(created_at)")
        self.db.commit()
        os.chmod(self.db_path, 0o600)
        self.failure_dir = state_dir / "review-failures"
        self._prune_failures()

    async def enqueue(self, event_id: str, body: str, room_id: str | None = None) -> bool:
        target = review_link(body, self.settings.base_url, self.settings.repositories)
        if target is None:
            log.warning("Ignoring targeted Hookshot review without one approved PR URL")
            return False
        repo, number = target
        if room_id is not None and room_id not in self.matrix.pr_review_rooms:
            raise ValueError("Review event room is not configured")
        with self.db:
            self.db.execute("""INSERT OR IGNORE INTO jobs
                (repo, number, head, request_id, event_id, room_id, status, created_at)
                VALUES (?, ?, '', 0, ?, ?, 'pending', ?)""",
                (repo, number, event_id, room_id, time.time()))
        self.wake.set()
        return True

    def _next(self) -> tuple[int, str, int, str, int, int, str, str | None] | None:
        row = self.db.execute("""SELECT id, repo, number, head, request_id, attempts, event_id, room_id
            FROM jobs WHERE status='pending' AND not_before<=?
            ORDER BY created_at, id LIMIT 1""", (time.time(),)).fetchone()
        return row

    def _set(self, job_id: int, status: str, *, attempts: int = 0,
             not_before: float = 0) -> None:
        with self.db:
            self.db.execute("""UPDATE jobs SET status=?, attempts=?, not_before=?
                WHERE id=?""", (status, attempts, not_before, job_id))

    def _prune_failures(self) -> None:
        """Delete a bounded batch of old, unreferenced artifacts.

        The current detail pointer on every job is always protected. The
        ledger supplies age and size without scanning the artifact directory.
        """
        count, total_bytes = self.db.execute("""SELECT COUNT(*), COALESCE(SUM(size), 0)
            FROM failure_artifacts AS a WHERE NOT EXISTS (
                SELECT 1 FROM jobs WHERE failure_ref=a.path)""").fetchone()
        candidates = self.db.execute("""SELECT path, created_at, size
            FROM failure_artifacts AS a WHERE NOT EXISTS (
                SELECT 1 FROM jobs WHERE failure_ref=a.path)
            ORDER BY created_at, path LIMIT ?""", (FAILURE_PRUNE_BATCH,)).fetchall()
        cutoff = time.time() - FAILURE_RETENTION_DAYS * 86400
        for name, created_at, size in candidates:
            if (created_at >= cutoff and count <= FAILURE_RETENTION_COUNT and
                    total_bytes <= FAILURE_RETENTION_BYTES):
                break
            path = Path(name)
            if path.parent != self.failure_dir or path.is_symlink():
                log.warning("Ignoring unsafe review failure artifact ledger entry")
            else:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    log.warning("Could not prune review failure artifact %s", path)
                    break
            with self.db:
                self.db.execute("DELETE FROM failure_artifacts WHERE path=?", (name,))
            count -= 1
            total_bytes -= size

    def _record_failure(self, job_id: int, repo: str, number: int, head: str,
                        request_id: int, attempt: int, error: Exception) -> None:
        failure = error if isinstance(error, ReviewCliFailure) else None
        try:
            detail = {
                "agent": self.agent, "job_id": job_id, "repo": repo, "pr": number,
                "head": head, "request_id": request_id, "attempt": attempt,
                "occurred_at": datetime.now(timezone.utc).isoformat(),
                "reason": failure.reason if failure else type(error).__name__,
                "exit_code": failure.exit_code if failure else None,
                "stdout": _redact_output(failure.stdout, failure.stdout_truncated) if failure else "",
                "stderr": _redact_output(failure.stderr, failure.stderr_truncated) if failure else "",
            }
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            # A broken secret list must never turn raw subprocess output into evidence.
            detail = {
                "agent": self.agent, "job_id": job_id, "repo": repo, "pr": number,
                "head": head, "request_id": request_id, "attempt": attempt,
                "occurred_at": datetime.now(timezone.utc).isoformat(),
                "reason": "redaction_unavailable", "exit_code": failure.exit_code if failure else None,
                "stdout": "", "stderr": "",
            }
        payload = (json.dumps(detail, sort_keys=True, separators=(",", ":")) + "\n").encode()
        path = self.failure_dir / f"job-{job_id}-attempt-{attempt}-{uuid.uuid4().hex}.json"
        _write_private(path, payload)
        digest = hashlib.sha256(payload).hexdigest()
        try:
            with self.db:
                self.db.execute("INSERT INTO failure_artifacts (path, created_at, size) "
                                "VALUES (?, ?, ?)", (str(path), time.time(), len(payload)))
                self.db.execute("UPDATE jobs SET failure_ref=?, failure_sha256=? WHERE id=?",
                                (str(path), digest, job_id))
        except Exception:
            path.unlink(missing_ok=True)
            raise
        self._prune_failures()
        log.error("Review worker failure agent=%s job=%d repo=%s pr=%d head=%s "
                  "request_id=%d attempt=%d reason=%s exit_code=%s detail=%s sha256=%s",
                  self.agent, job_id, repo, number, head, request_id, attempt,
                  detail["reason"], detail["exit_code"], path, digest)

    async def _run_cli(self, repo: str, number: int, room_id: str | None = None) -> bool:
        if room_id is None and len(self.matrix.pr_review_rooms) == 1:
            room_id = self.matrix.pr_review_rooms[0]
        if room_id not in self.matrix.pr_review_rooms:
            raise RuntimeError("Review pointer has no configured PR review room")
        if repo not in self.settings.repositories:
            raise RuntimeError("Repository is outside the review allowlist")
        allowed = {"PATH", "LANG", "LC_ALL", "TZ", "TMPDIR", "HOME", "SSL_CERT_FILE", "SSL_CERT_DIR"}
        allowed.update(self.settings.pass_env)
        env = {key: value for key, value in os.environ.items() if key in allowed}
        env.update(REVIEW_BASE_URL=self.settings.base_url, REVIEW_REVIEWER=self.settings.reviewer,
                   REVIEW_ROOM_ID=room_id)
        process = await asyncio.create_subprocess_exec(
            *self.settings.command, "--repo", repo, "--pr", str(number),
            cwd=self.settings.work_dir, env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            start_new_session=True)
        assert process.stdout is not None and process.stderr is not None
        stdout_task = asyncio.create_task(_bounded_tail(process.stdout))
        stderr_task = asyncio.create_task(_bounded_tail(process.stderr))
        try:
            exit_code = await asyncio.wait_for(process.wait(), timeout=1200)
        except (asyncio.CancelledError, TimeoutError) as interrupted:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except TimeoutError:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await process.wait()
            stdout_result, stderr_result = await _finish_output(stdout_task, stderr_task)
            if isinstance(interrupted, asyncio.CancelledError):
                raise
            stdout, stdout_truncated = stdout_result
            stderr, stderr_truncated = stderr_result
            raise ReviewCliFailure("timeout", process.returncode, stdout, stderr,
                                   stdout_truncated, stderr_truncated)
        (stdout, stdout_truncated), (stderr, stderr_truncated) = await _finish_output(
            stdout_task, stderr_task)
        if exit_code != 0:
            raise ReviewCliFailure("nonzero_exit", exit_code, stdout, stderr,
                                   stdout_truncated, stderr_truncated)
        return True

    async def run(self) -> None:
        """One serial worker; persistence and live Forgejo state survive restarts."""
        next_prune = time.monotonic() + FAILURE_PRUNE_INTERVAL
        while True:
            if time.monotonic() >= next_prune:
                try:
                    self._prune_failures()
                except OSError:
                    log.exception("Could not prune review failure artifacts")
                next_prune = time.monotonic() + FAILURE_PRUNE_INTERVAL
            row = self._next()
            if row is None:
                self.wake.clear()
                try:
                    await asyncio.wait_for(self.wake.wait(), timeout=5)
                except TimeoutError:
                    pass
                continue
            job_id, repo, number, head, request_id, attempts, event_id, room_id = row
            try:
                disposition, current_head, current_request, reviewed = await asyncio.to_thread(
                    self.forgejo.status, repo, number)
                if disposition != "requested" or reviewed:
                    self._set(job_id, "done" if reviewed else "obsolete")
                    continue
                assert current_head is not None and current_request is not None
                if not head:
                    duplicate = self.db.execute("""SELECT id FROM jobs WHERE repo=?
                        AND number=? AND head=? AND request_id=? AND id<>?
                        AND status IN ('pending','running','done') LIMIT 1""",
                        (repo, number, current_head, current_request, job_id)).fetchone()
                    if duplicate:
                        self._set(job_id, "duplicate")
                        continue
                    with self.db:
                        self.db.execute("UPDATE jobs SET head=?, request_id=? WHERE id=?",
                                        (current_head, current_request, job_id))
                    head, request_id = current_head, current_request
                if current_head != head or current_request != request_id:
                    with self.db:
                        self.db.execute("""UPDATE jobs SET head=?, request_id=?, status='pending',
                            attempts=0, not_before=0 WHERE id=?""",
                            (current_head, current_request, job_id))
                    continue
                async with self.runtime.external_job() as reserved:
                    if not reserved:
                        self._set(job_id, "pending", attempts=attempts,
                                  not_before=time.time() + 5)
                        continue
                    self._set(job_id, "running", attempts=attempts)
                    await self._reaction(room_id, event_id, "👀")
                    success = await self._run_cli(repo, number, room_id)
                if not success:
                    raise ReviewCliFailure("nonzero_exit")
                # A successful CLI exit may mean already-reviewed. Live state
                # decides whether this generation is actually complete.
                disposition, current_head, current_request, reviewed = await asyncio.to_thread(
                    self.forgejo.status, repo, number)
                if ((disposition == "requested" and current_head == head and
                     current_request == request_id and reviewed) or
                    (disposition == "unrequested" and current_head == head and reviewed)):
                    self._set(job_id, "done")
                    await self._confirm_approval(repo, number, head, request_id,
                                                 room_id, event_id)
                elif disposition != "requested":
                    self._set(job_id, "obsolete")
                else:
                    raise RuntimeError("Review CLI did not complete the current request")
            except asyncio.CancelledError:
                raise
            except Exception as error:
                delay = min(3600, 15 * 2 ** min(attempts, 8))
                try:
                    self._record_failure(job_id, repo, number, head, request_id,
                                         attempts + 1, error)
                except Exception:
                    log.exception("Could not persist review failure agent=%s job=%d",
                                  self.agent, job_id)
                log.warning("Review queue retry agent=%s job=%d repo=%s pr=%d in %ds",
                            self.agent, job_id, repo, number, delay)
                self._set(job_id, "pending", attempts=attempts + 1,
                          not_before=time.time() + delay)

    async def _confirm_approval(self, repo: str, number: int, head: str,
                                request_id: int, room_id: str | None,
                                event_id: str) -> None:
        try:
            if await asyncio.to_thread(self.forgejo.approved, repo, number, head, request_id):
                latest = await asyncio.to_thread(self.forgejo.status, repo, number)
                if latest[1:] == (head, request_id, True):
                    await self._reaction(room_id, event_id, "✅")
        except Exception:
            # Feedback is optional. Never turn a posted review back into a
            # pending job or rerun its CLI because a confirmation read failed.
            log.exception("Review approval confirmation failed agent=%s", self.agent)

    async def _reaction(self, room_id: str | None, event_id: str, emoji: str) -> None:
        if room_id is None or self.react is None:
            return  # Legacy rows have no trustworthy target room.
        try:
            await self.react(room_id, event_id, emoji)
        except Exception:
            log.exception("Review reaction delivery failed agent=%s", self.agent)

    def close(self) -> None:
        self.db.close()
