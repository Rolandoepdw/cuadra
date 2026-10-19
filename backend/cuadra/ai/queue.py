"""Persistent job queue (table `jobs`).

Hito 1: worker with per-type dispatch. With no handlers registered, jobs are
marked FAILED (permanent). Real handlers are registered in Hito 3.
"""

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Awaitable, Callable

from cuadra import db

log = logging.getLogger("cuadra.queue")

Handler = Callable[[dict], Awaitable[None]]

_handlers: dict[str, Handler] = {}
_task: asyncio.Task | None = None
_stop = asyncio.Event()

POLL_INTERVAL_S = 1.0


def register_handler(job_type: str, handler: Handler) -> None:
    _handlers[job_type] = handler


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


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


def _finish(conn, job_id: int, state: str, error: str | None = None) -> None:
    conn.execute(
        """UPDATE jobs
           SET status = ?, last_error = COALESCE(?, last_error), updated_at = ?
           WHERE id = ?""",
        (state, error, _now(), job_id),
    )
    conn.execute(
        """INSERT INTO events (source, type, entity, entity_id, details_json)
           VALUES ('system', ?, 'jobs', ?, ?)""",
        (
            "job_completed" if state == "COMPLETED" else "job_failed",
            job_id,
            json.dumps({"error": error} if error else {}, ensure_ascii=False),
        ),
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
            )
        finally:
            conn.close()
        return
    try:
        payload = json.loads(job["payload_json"])
        await handler(payload)
        conn = db.connect()
        try:
            _finish(conn, job["id"], "COMPLETED")
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 — the worker never dies without marking the job
        log.exception("job %s failed", job["id"])
        conn = db.connect()
        try:
            transient = isinstance(exc, (TimeoutError, ConnectionError, OSError))
            if transient and job["attempts"] + 1 < job["max_attempts"]:
                # Hito 3: real exponential backoff
                conn.execute(
                    """UPDATE jobs
                       SET status = 'PENDING', attempts = attempts + 1,
                           next_retry_at = ?, last_error = ?, updated_at = ?
                       WHERE id = ?""",
                    (_now(), str(exc), _now(), job["id"]),
                )
                conn.commit()
            else:
                _finish(conn, job["id"], "FAILED", str(exc))
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
