"""Periodic maintenance jobs (APScheduler).

- Escalation timeouts (Hito 5): a PENDING escalation left unanswered past
  `ESCALATION_TIMEOUT_MIN` gets one retry; if it is still unanswered after
  that, it is marked TIMEOUT so the operator can pick it up (doc 01 §5 —
  never leave the client hanging silently).
- LLM trace retention (phase 0): traces older than `llm.trace_retention_days`
  are deleted so the table does not grow forever.
"""

import logging

from cuadra import config, db
from cuadra.ai import queue, tracing
from cuadra.domain import conversations, escalations, events

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
