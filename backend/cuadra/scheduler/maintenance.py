"""Periodic maintenance jobs (APScheduler): escalation timeouts (Hito 5).

A PENDING escalation left unanswered past `ESCALATION_TIMEOUT_MIN` gets one
retry; if it is still unanswered after that, it is marked TIMEOUT so the
operator can pick it up (doc 01 §5 — never leave the client hanging silently).
"""

import logging

from cuadra import config, db
from cuadra.ai import queue
from cuadra.domain import escalations, events

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
