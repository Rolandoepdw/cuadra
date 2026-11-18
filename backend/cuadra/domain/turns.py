"""Persistent turn assembly (migration 012, phase 1).

A *turn* is one burst of customer messages that must produce one reply. The
unit lives in SQLite, not in RAM, so a power cut during the debounce window
neither loses the burst nor produces two replies.

Lifecycle: ``OPEN -> PROCESSING -> DONE``, or ``CANCELLED``. ``claim`` is an
atomic ``UPDATE ... WHERE status = 'OPEN'`` so a duplicated job is a no-op.
``recover_open`` runs at startup and re-arms turns abandoned by a crash.
"""

import json
import logging
from datetime import datetime, timedelta, timezone

from cuadra import config

log = logging.getLogger("cuadra.turns")

STATUSES = ("OPEN", "PROCESSING", "DONE", "CANCELLED")


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime | None = None) -> str:
    value = value or now()
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def parse_ts(value: datetime | str | None) -> datetime:
    """Parse timestamps from this module and from SQLite defaults.

    SQLite's ``datetime('now')`` is naive UTC; ISO values written by Cuadra
    carry ``+00:00``. Both must compare correctly.
    """
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def get(conn, turn_id: int) -> dict:
    row = conn.execute("SELECT * FROM turns WHERE id = ?", (turn_id,)).fetchone()
    if row is None:
        raise LookupError(f"turn {turn_id} not found")
    return dict(row)


def message_ids(turn: dict) -> list[int]:
    try:
        values = json.loads(turn.get("message_ids_json") or "[]")
    except (TypeError, ValueError):
        values = []
    return [int(value) for value in values if isinstance(value, (int, str))]


def get_open(conn, conversation_id: int) -> dict | None:
    row = conn.execute(
        """SELECT * FROM turns
           WHERE conversation_id = ? AND status = 'OPEN'
           ORDER BY id DESC LIMIT 1""",
        (conversation_id,),
    ).fetchone()
    return dict(row) if row is not None else None


def _insert_open_turn(
    conn,
    conversation_id: int,
    message_id: int,
    due_at: datetime | str,
) -> dict:
    current = now()
    cur = conn.execute(
        """INSERT INTO turns
           (conversation_id, status, first_message_id, last_message_id,
            message_ids_json, due_at, created_at, updated_at)
           VALUES (?, 'OPEN', ?, ?, ?, ?, ?, ?)""",
        (
            conversation_id,
            message_id,
            message_id,
            json.dumps([message_id]),
            iso(parse_ts(due_at)),
            iso(current),
            iso(current),
        ),
    )
    conn.commit()
    return get(conn, cur.lastrowid)


def upsert_open_turn(
    conn,
    conversation_id: int,
    message_id: int,
    *,
    due_at: datetime | str,
    max_wait_s: float | None = None,
) -> dict:
    """Append a message to the conversation's open turn, or create one.

    ``due_at`` is the new debounce deadline. ``max_wait_s`` caps it relative to
    the first message of the turn so a customer who never stops typing still
    gets an answer after the maximum wait.
    """
    current = now()
    proposed = parse_ts(due_at)
    turn = get_open(conn, conversation_id)

    if turn is None:
        if max_wait_s is not None:
            proposed = min(proposed, current + timedelta(seconds=max_wait_s))
        return _insert_open_turn(conn, conversation_id, message_id, proposed)

    ids = message_ids(turn)
    if message_id not in ids:
        ids.append(message_id)

    if max_wait_s is not None:
        first_seen = parse_ts(turn["created_at"])
        proposed = min(proposed, first_seen + timedelta(seconds=max_wait_s))

    cur = conn.execute(
        """UPDATE turns
           SET last_message_id = ?, message_ids_json = ?, due_at = ?,
               updated_at = ?
           WHERE id = ? AND status = 'OPEN'""",
        (message_id, json.dumps(ids), iso(proposed), iso(current), turn["id"]),
    )
    conn.commit()
    if cur.rowcount == 0:
        # The turn was claimed between our read and this update (only possible
        # with more than one worker). This message belongs to a new turn.
        new_due = proposed
        if max_wait_s is not None:
            new_due = min(new_due, current + timedelta(seconds=max_wait_s))
        return _insert_open_turn(conn, conversation_id, message_id, new_due)
    return get(conn, turn["id"])


def set_job(conn, turn_id: int, job_id: int | None) -> None:
    conn.execute(
        "UPDATE turns SET job_id = ?, updated_at = ? WHERE id = ?",
        (job_id, iso(), turn_id),
    )
    conn.commit()


# Columns written by `record_outcome` (migration 020). Keeping the whitelist
# here means callers can pass a pipeline result without leaking new keys into
# SQL.
_OUTCOME_FIELDS = (
    "intent",
    "product_id",
    "stage",
    "generator",
    "escalated",
    "sale_id",
    "reply_message_id",
    "latency_ms",
    "intent_ms",
    "compose_ms",
    "context_tokens",
    "outcome",
    "error",
    "finished_at",
)


def record_outcome(conn, turn_id: int | None, **fields) -> None:
    """Persist what a turn understood and produced (migration 020).

    A missing turn id (the legacy single-message entry point) is a no-op.
    Unknown keys are ignored on purpose.
    """
    if turn_id is None:
        return
    data = {key: value for key, value in fields.items() if key in _OUTCOME_FIELDS}
    if not data:
        return
    data.setdefault("finished_at", iso())
    assignments = ", ".join(f"{key} = ?" for key in data)
    conn.execute(
        f"UPDATE turns SET {assignments}, updated_at = ? WHERE id = ?",
        (*data.values(), iso(), turn_id),
    )
    conn.commit()


def set_reply_message(conn, turn_id: int | None, message_id: int | None) -> None:
    """Link the stored outbound message to its turn (written after the reply)."""
    if turn_id is None or message_id is None:
        return
    conn.execute(
        "UPDATE turns SET reply_message_id = ?, updated_at = ? WHERE id = ?",
        (message_id, iso(), turn_id),
    )
    conn.commit()


def claim(conn, turn_id: int) -> bool:
    """Atomically move ``OPEN -> PROCESSING``. False means someone else owns it."""
    cur = conn.execute(
        """UPDATE turns SET status = 'PROCESSING', updated_at = ?
           WHERE id = ? AND status = 'OPEN'""",
        (iso(), turn_id),
    )
    conn.commit()
    return cur.rowcount == 1


def release(conn, turn_id: int) -> None:
    """Return a failed turn to OPEN so a retry can claim it again."""
    conn.execute(
        """UPDATE turns SET status = 'OPEN', updated_at = ?
           WHERE id = ? AND status = 'PROCESSING'""",
        (iso(), turn_id),
    )
    conn.commit()


def mark_done(conn, turn_id: int) -> None:
    conn.execute(
        """UPDATE turns SET status = 'DONE', updated_at = ?
           WHERE id = ? AND status IN ('OPEN', 'PROCESSING')""",
        (iso(), turn_id),
    )
    conn.commit()


def mark_cancelled(conn, turn_id: int) -> None:
    conn.execute(
        """UPDATE turns SET status = 'CANCELLED', updated_at = ?
           WHERE id = ? AND status IN ('OPEN', 'PROCESSING')""",
        (iso(), turn_id),
    )
    conn.commit()


def _job(conn, job_id: int | None) -> dict | None:
    if job_id is None:
        return None
    row = conn.execute(
        "SELECT id, status, next_retry_at FROM jobs WHERE id = ?", (job_id,)
    ).fetchone()
    return dict(row) if row is not None else None


def recover_open(conn, *, enqueue=None, at: datetime | None = None) -> int:
    """Re-arm turns abandoned by a crash and return how many were scheduled.

    - ``OPEN`` turns with no live job get one at their ``due_at``.
    - ``PROCESSING`` turns whose job is no longer running are reset to OPEN.

    Called after ``queue.requeue_in_progress()`` at startup, when no job is
    legitimately IN_PROGRESS yet.
    """
    if enqueue is None:
        from cuadra.ai import queue as job_queue

        def enqueue(turn_id: int, due_at: str) -> int:
            return job_queue.enqueue(
                "process_turn",
                {"turn_id": turn_id},
                priority=config.PRIORITY_CUSTOMER,
                available_at=due_at,
            )

    current = at or now()
    rows = conn.execute(
        "SELECT * FROM turns WHERE status IN ('OPEN', 'PROCESSING') ORDER BY id"
    ).fetchall()
    scheduled = 0
    for raw in rows:
        turn = dict(raw)
        job = _job(conn, turn.get("job_id"))

        if turn["status"] == "PROCESSING":
            if job is not None and job["status"] == "IN_PROGRESS":
                continue  # genuinely running
            conn.execute(
                """UPDATE turns SET status = 'OPEN', updated_at = ?
                   WHERE id = ? AND status = 'PROCESSING'""",
                (iso(current), turn["id"]),
            )
            conn.commit()
            turn["status"] = "OPEN"

        due_at = parse_ts(turn["due_at"])
        job = _job(conn, turn.get("job_id"))
        if job is not None and job["status"] in ("PENDING", "IN_PROGRESS"):
            if job["status"] == "PENDING":
                retry_at = job.get("next_retry_at")
                if retry_at is None or parse_ts(retry_at) > due_at:
                    conn.execute(
                        """UPDATE jobs SET next_retry_at = ?, updated_at = ?
                           WHERE id = ? AND status = 'PENDING'""",
                        (iso(due_at), iso(current), job["id"]),
                    )
                    conn.commit()
            continue

        job_id = enqueue(turn["id"], iso(due_at))
        set_job(conn, turn["id"], job_id)
        scheduled += 1

    if scheduled:
        log.info("recovered %s abandoned turn(s)", scheduled)
    return scheduled
