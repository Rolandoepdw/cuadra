"""Periodic maintenance jobs (APScheduler).

- Escalation timeouts (Hito 5): a PENDING escalation left unanswered past
  `ESCALATION_TIMEOUT_MIN` gets one retry; if it is still unanswered after
  that, it is marked TIMEOUT so the operator can pick it up (doc 01 §5 —
  never leave the client hanging silently).
- LLM trace retention (phase 0): traces older than `llm.trace_retention_days`
  are deleted so the table does not grow forever.
"""

import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from cuadra import config, db
from cuadra.ai import queue, tracing
from cuadra.domain import conversations, escalations, events, sales, settings

log = logging.getLogger("cuadra.scheduler")


def check_escalation_timeouts() -> None:
    retry_ids: list[int] = []
    conn = db.connect()
    try:
        for escalation in escalations.overdue_pending(conn, config.ESCALATION_TIMEOUT_MIN):
            if escalation["attempts"] < config.ESCALATION_MAX_RETRIES:
                escalations.bump_attempts(conn, escalation["id"])
                # Drop the last escalation_sent event so the resend + its new
                # timestamp reset the timeout window for the next check.
                conn.execute(
                    """DELETE FROM events
                       WHERE id = (SELECT id FROM events
                                   WHERE type = 'escalation_sent'
                                     AND entity = 'escalations' AND entity_id = ?
                                   ORDER BY id DESC LIMIT 1)""",
                    (escalation["id"],),
                )
                conn.commit()
                events.log(
                    conn,
                    "scheduler",
                    "escalation_retry",
                    entity="escalations",
                    entity_id=escalation["id"],
                    details={"attempts": escalation["attempts"] + 1},
                )
                retry_ids.append(escalation["id"])
            else:
                escalations.mark_timeout(conn, escalation["id"])
                events.log(
                    conn,
                    "scheduler",
                    "escalation_timeout",
                    entity="escalations",
                    entity_id=escalation["id"],
                    details={"attempts": escalation["attempts"]},
                )
    finally:
        conn.close()

    for escalation_id in retry_ids:
        queue.enqueue(
            "send_escalation",
            {"escalation_id": escalation_id},
            priority=config.PRIORITY_TCP,
        )


def close_idle_conversations() -> None:
    """Close conversations with no messages for CONVERSATION_IDLE_HOURS.

    The profile and purchase history survive; a returning customer simply gets
    a new conversation on their next message.
    """
    conn = db.connect()
    try:
        closed = conversations.close_idle(conn)
        if closed:
            events.log(
                conn,
                "system",
                "conversations_closed_idle",
                entity="conversations",
                details={
                    "count": closed,
                    "idle_hours": config.CONVERSATION_IDLE_HOURS,
                },
            )
        log.info("closed %s idle conversation(s)", closed)
    except Exception:  # noqa: BLE001 - maintenance must not stop the scheduler
        conn.rollback()
        log.exception("idle conversation closing failed")
    finally:
        conn.close()


def purge_llm_traces() -> None:
    removed = tracing.purge_old()
    if removed:
        log.info("purged %s llm trace rows past retention", removed)


# --------------------------------------------------------------- sale follow-up

FOLLOWUP_WINDOW_START = 9
FOLLOWUP_WINDOW_END = 20


def _next_send_window(now: datetime) -> datetime | None:
    """When to send a customer reminder, or None if it can go right now.

    Reminders only leave 09:00-20:00 America/Havana. A sale that becomes due at
    night waits for the next morning instead of waking the customer up.
    """
    havana = ZoneInfo("America/Havana")
    local = now.astimezone(havana)
    if FOLLOWUP_WINDOW_START <= local.hour < FOLLOWUP_WINDOW_END:
        return None
    start = local.replace(hour=FOLLOWUP_WINDOW_START, minute=0, second=0, microsecond=0)
    if local.hour >= FOLLOWUP_WINDOW_END:
        start += timedelta(days=1)
    return start.astimezone(timezone.utc)


def _followup_job_pending(conn, sale_id: int) -> bool:
    row = conn.execute(
        """SELECT 1 FROM jobs
           WHERE type = 'sale_followup' AND status IN ('PENDING', 'IN_PROGRESS')
             AND json_extract(payload_json, '$.sale_id') = ?
           LIMIT 1""",
        (sale_id,),
    ).fetchone()
    return row is not None


def _followup_due_logged(conn, sale_id: int) -> bool:
    row = conn.execute(
        """SELECT 1 FROM events
           WHERE type = 'sale_followup_due' AND entity = 'sales' AND entity_id = ?
           LIMIT 1""",
        (sale_id,),
    ).fetchone()
    return row is not None


def queue_sale_followups() -> None:
    """Enqueue one customer reminder per due sale (doc 13 §10.24).

    `off` does nothing, `shadow` records `sale_followup_due` once per sale
    without sending, and `on` enqueues a `sale_followup` job. A sale already
    reminded, muted or with a job in flight is skipped, so the 15-minute sweep
    never piles up duplicates.
    """
    try:
        _sweep_sale_followups()
    except Exception:  # noqa: BLE001 - maintenance must not stop the scheduler
        log.exception("sale follow-up sweep failed")


def _sweep_sale_followups() -> None:
    conn = db.connect()
    try:
        mode = settings.sales_followup_mode(conn)
        if mode == "off":
            return
        candidates = sales.followup_candidates(conn)
        # Read everything before writing: `queue.enqueue` commits on its own
        # connection, and upgrading this read snapshot afterwards would hit
        # SQLITE_BUSY_SNAPSHOT.
        pending = {
            sale["id"] for sale in candidates if _followup_job_pending(conn, sale["id"])
        }
        logged = {
            sale["id"] for sale in candidates if _followup_due_logged(conn, sale["id"])
        }
    finally:
        conn.close()

    for sale in candidates:
        if mode == "shadow":
            if sale["id"] in logged:
                continue
            conn = db.connect()
            try:
                events.log(
                    conn,
                    "scheduler",
                    "sale_followup_due",
                    entity="sales",
                    entity_id=sale["id"],
                    details={
                        "order_code": sale["order_code"],
                        "status": sale["status"],
                        "age_hours": sale["age_hours"],
                    },
                )
            finally:
                conn.close()
            continue
        if sale["id"] in pending:
            continue
        queue.enqueue(
            "sale_followup",
            {"sale_id": sale["id"], "trigger": "scheduler"},
            priority=config.PRIORITY_CUSTOMER,
            available_at=_next_send_window(datetime.now(timezone.utc)),
        )
        conn = db.connect()
        try:
            events.log(
                conn,
                "scheduler",
                "sale_followup_queued",
                entity="sales",
                entity_id=sale["id"],
                details={
                    "order_code": sale["order_code"],
                    "status": sale["status"],
                    "age_hours": sale["age_hours"],
                },
            )
        finally:
            conn.close()
