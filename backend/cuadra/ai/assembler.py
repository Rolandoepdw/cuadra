"""Debounce/deadline policy for customer turns (phase 1).

Every inbound message updates the conversation's open turn and parks the same
`process_turn` job at the new deadline. Nothing is buffered in RAM, so a
restart during the debounce window is recoverable.

`TURN_DEBOUNCE_S` is the silence window; `TURN_MAX_WAIT_S` is the hard cap from
the first message of the turn.
"""

import logging
from datetime import datetime, timedelta

from cuadra import config
from cuadra.ai import queue
from cuadra.domain import turns

log = logging.getLogger("cuadra.assembler")

TURN_JOB = "process_turn"


def _enqueue_turn(turn_id: int, due_at: str) -> int:
    return queue.enqueue(
        TURN_JOB,
        {"turn_id": turn_id},
        priority=config.PRIORITY_CUSTOMER,
        available_at=due_at,
    )


def on_inbound(
    conn,
    conversation_id: int,
    message_id: int,
    *,
    debounce_s: float | None = None,
    max_wait_s: float | None = None,
    at: datetime | None = None,
) -> dict:
    """Attach a message to the open turn and arm/re-arm its job."""
    current = at or turns.now()
    debounce = config.TURN_DEBOUNCE_S if debounce_s is None else debounce_s
    max_wait = config.TURN_MAX_WAIT_S if max_wait_s is None else max_wait_s
    due_at = current + timedelta(seconds=debounce)

    turn = turns.upsert_open_turn(
        conn,
        conversation_id,
        message_id,
        due_at=due_at,
        max_wait_s=max_wait,
    )
    return schedule_turn(conn, turn["id"], available_at=turn["due_at"])


def schedule_turn(
    conn,
    turn_id: int,
    *,
    available_at: datetime | str | None = None,
    force: bool = False,
) -> dict:
    """Ensure exactly one live `process_turn` job points at this turn.

    ``force`` is used by a running handler that woke too early: it cannot
    reschedule its own IN_PROGRESS job, so it enqueues a fresh one.
    """
    turn = turns.get(conn, turn_id)
    if turn["status"] in ("DONE", "CANCELLED"):
        return turn

    due_at = available_at or turn["due_at"]
    job_id = turn.get("job_id")
    if job_id is not None:
        job = conn.execute(
            "SELECT id, status FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
        if job is not None and job["status"] == "PENDING":
            queue.reschedule(job_id, due_at)
            return turns.get(conn, turn_id)
        if job is not None and job["status"] == "IN_PROGRESS" and not force:
            # The running handler owns the turn. It re-reads due_at before
            # claiming and enqueues a follow-up when it ran too early.
            return turn

    if force:
        # Do not stack jobs if a previous follow-up is already waiting.
        pending = conn.execute(
            """SELECT id FROM jobs
               WHERE type = ? AND status = 'PENDING'
                 AND json_extract(payload_json, '$.turn_id') = ?
               ORDER BY id LIMIT 1""",
            (TURN_JOB, turn_id),
        ).fetchone()
        if pending is not None:
            turns.set_job(conn, turn_id, pending["id"])
            return turns.get(conn, turn_id)

    new_job = _enqueue_turn(turn_id, turns.iso(turns.parse_ts(due_at)))
    turns.set_job(conn, turn_id, new_job)
    return turns.get(conn, turn_id)


def recover_open(conn=None) -> int:
    """Startup recovery: re-arm OPEN turns and orphaned PROCESSING turns."""
    own = conn is None
    if own:
        from cuadra import db

        conn = db.connect()
    try:
        return turns.recover_open(conn, enqueue=_enqueue_turn)
    finally:
        if own:
            conn.close()
