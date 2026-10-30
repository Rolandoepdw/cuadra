"""Persistent job queue (table `jobs`).

Survives power cuts: PENDING jobs are resumed at startup and jobs left
IN_PROGRESS are requeued with attempts+1 (doc 01 §5). Transient failures get
exponential backoff; permanent failures are marked FAILED with an audit event.
"""

import asyncio
import json
import logging
import random
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable

from cuadra import config, db
from cuadra.domain import events

log = logging.getLogger("cuadra.queue")

Handler = Callable[[dict], Awaitable[None]]

_handlers: dict[str, Handler] = {}
_task: asyncio.Task | None = None
_stop = asyncio.Event()

POLL_INTERVAL_S = 1.0


class PermanentJobError(Exception):
    """Raised by a handler when a failure will never succeed by retrying
    (e.g. Telegram 'chat not found'). The job is marked FAILED immediately."""


class RateLimited(TimeoutError):
    """Telegram asked us to wait before the next action.

    `retry_after` is Telegram's own FloodWait seconds. When `account` is true
    the cooldown applies to the whole sender (every group), not just this job.
    Subclasses TimeoutError so existing handlers keep treating it as transient.
    """

    def __init__(self, retry_after: float, message: str, *, account: bool = True):
        super().__init__(message)
        self.retry_after = max(1.0, float(retry_after or 0))
        self.account = bool(account)


def register_handler(job_type: str, handler: Handler) -> None:
    _handlers[job_type] = handler


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _retry_at(attempts: int) -> str:
    delay = min(config.QUEUE_BACKOFF_MAX_S, config.QUEUE_BACKOFF_BASE_S * (2**attempts))
    return (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()


def _rate_limit_retry_at(exc: RateLimited) -> tuple[str, float]:
    """Park a rate-limited job for Telegram's wait plus a little jitter."""
    wait = min(exc.retry_after, config.QUEUE_RATE_LIMIT_MAX_WAIT_S)
    jitter = min(config.QUEUE_RATE_LIMIT_JITTER_S, max(0.0, wait * 0.1))
    delay = max(1.0, wait + random.uniform(0.0, jitter))
    return (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat(), delay


def _mark_post_failed(job: dict, error: str) -> None:
    """When a broadcast job dies, do not leave its post stuck in QUEUED."""
    if job.get("type") != "send_broadcast":
        return
    try:
        payload = json.loads(job["payload_json"])
        post_id = payload.get("post_id")
        if post_id is None:
            return
        from cuadra.domain import broadcasts

        conn = db.connect()
        try:
            broadcasts.record_result(conn, int(post_id), "FAILED", str(error)[:500])
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 - bookkeeping must never kill the worker
        log.exception("could not mark post for failed job %s", job.get("id"))


def enqueue(job_type: str, payload: dict, *, priority: int = 100) -> int:
    """Append a job. Lower priority number runs first (clients < TCP < broadcasts)."""
    conn = db.connect()
    try:
        cur = conn.execute(
            "INSERT INTO jobs (type, payload_json, priority) VALUES (?, ?, ?)",
            (job_type, json.dumps(payload, ensure_ascii=False), priority),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def requeue_in_progress() -> int:
    """Startup recovery: jobs interrupted mid-run go back to PENDING."""
    conn = db.connect()
    try:
        cur = conn.execute(
            """UPDATE jobs
               SET status = 'PENDING', attempts = attempts + 1,
                   last_error = COALESCE(last_error, 'requeued after restart'),
                   updated_at = ?
               WHERE status = 'IN_PROGRESS'""",
            (_now(),),
        )
        recovered = cur.rowcount
        if recovered:
            events.log(
                conn,
                "system",
                "jobs_requeued",
                entity="jobs",
                details={"count": recovered},
            )
        conn.commit()
        return recovered
    finally:
        conn.close()


def _claim(conn) -> dict | None:
    """Take the next ready PENDING job (priority first, then FIFO)."""
    row = conn.execute(
        """SELECT * FROM jobs
           WHERE status = 'PENDING'
             AND (next_retry_at IS NULL OR next_retry_at <= ?)
           ORDER BY priority, id
           LIMIT 1""",
        (_now(),),
    ).fetchone()
    if row is None:
        return None
    conn.execute(
        "UPDATE jobs SET status = 'IN_PROGRESS', updated_at = ? WHERE id = ?",
        (_now(), row["id"]),
    )
    conn.commit()
    return dict(row)


def _finish(
    conn,
    job_id: int,
    state: str,
    error: str | None = None,
    *,
    job_type: str | None = None,
) -> None:
    conn.execute(
        """UPDATE jobs
           SET status = ?, last_error = COALESCE(?, last_error), updated_at = ?
           WHERE id = ?""",
        (state, error, _now(), job_id),
    )
    details = {"job_type": job_type} if job_type else {}
    if error:
        details["error"] = error
    events.log(
        conn,
        "system",
        "job_completed" if state == "COMPLETED" else "job_failed",
        entity="jobs",
        entity_id=job_id,
        details=details,
    )
    conn.commit()


async def _process(job: dict) -> None:
    handler = _handlers.get(job["type"])
    if handler is None:
        conn = db.connect()
        try:
            _finish(
                conn,
                job["id"],
                "FAILED",
                f"no handler registered for job type '{job['type']}'",
                job_type=job["type"],
            )
        finally:
            conn.close()
        return
    try:
        payload = json.loads(job["payload_json"])
        await handler(payload)
        conn = db.connect()
        try:
            _finish(conn, job["id"], "COMPLETED", job_type=job["type"])
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 — the worker never dies without marking the job
        if isinstance(exc, RateLimited):
            log.warning("job %s rate limited: %s", job["id"], exc)
        else:
            log.exception("job %s failed", job["id"])
        conn = db.connect()
        try:
            if isinstance(exc, RateLimited):
                hits = int(job.get("rate_limit_hits") or 0) + 1
                if hits < config.QUEUE_RATE_LIMIT_MAX_HITS:
                    retry_at, delay = _rate_limit_retry_at(exc)
                    conn.execute(
                        """UPDATE jobs
                           SET status = 'PENDING', rate_limit_hits = ?,
                               next_retry_at = ?, last_error = ?, updated_at = ?
                           WHERE id = ?""",
                        (hits, retry_at, str(exc), _now(), job["id"]),
                    )
                    events.log(
                        conn,
                        "system",
                        "job_rate_limited",
                        entity="jobs",
                        entity_id=job["id"],
                        details={
                            "job_type": job["type"],
                            "hit": hits,
                            "max_hits": config.QUEUE_RATE_LIMIT_MAX_HITS,
                            "wait_s": round(delay),
                            "retry_at": retry_at,
                        },
                    )
                    conn.commit()
                    log.warning(
                        "job %s rate limit hit %s/%s; retry in %.0fs",
                        job["id"],
                        hits,
                        config.QUEUE_RATE_LIMIT_MAX_HITS,
                        delay,
                    )
                else:
                    _finish(
                        conn, job["id"], "FAILED", str(exc), job_type=job["type"]
                    )
                    _mark_post_failed(job, str(exc))
                return
            transient = isinstance(exc, (TimeoutError, ConnectionError, OSError)) and not isinstance(
                exc, PermanentJobError
            )
            if transient and job["attempts"] + 1 < job["max_attempts"]:
                conn.execute(
                    """UPDATE jobs
                       SET status = 'PENDING', attempts = attempts + 1,
                           next_retry_at = ?, last_error = ?, updated_at = ?
                       WHERE id = ?""",
                    (_retry_at(job["attempts"] + 1), str(exc), _now(), job["id"]),
                )
                conn.commit()
                log.warning(
                    "job %s transient failure, retry scheduled: %s", job["id"], exc
                )
            else:
                _finish(
                    conn, job["id"], "FAILED", str(exc), job_type=job["type"]
                )
                if not isinstance(exc, PermanentJobError):
                    _mark_post_failed(job, str(exc))
        finally:
            conn.close()


async def _loop() -> None:
    log.info("job queue worker started")
    while not _stop.is_set():
        conn = db.connect()
        try:
            job = _claim(conn)
        finally:
            conn.close()
        if job is None:
            try:
                await asyncio.wait_for(_stop.wait(), timeout=POLL_INTERVAL_S)
            except TimeoutError:
                pass
            continue
        await _process(job)
    log.info("job queue worker stopped")


def start() -> None:
    global _task
    _stop.clear()
    _task = asyncio.get_running_loop().create_task(_loop())


async def stop() -> None:
    global _task
    if _task is not None:
        _stop.set()
        await _task
        _task = None


def queue_size(conn=None) -> int:
    own = conn is None
    if own:
        conn = db.connect()
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE status IN ('PENDING','IN_PROGRESS')"
        ).fetchone()[0]
    finally:
        if own:
            conn.close()
