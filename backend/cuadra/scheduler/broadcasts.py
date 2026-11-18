"""Broadcast scheduler (APScheduler).

Every `CUADRA_BROADCAST_TICK_S` seconds, due campaigns dispatch ONE post to one
eligible group. Spintax, category matching, daily caps and jitter live in
`domain/broadcasts.py`.
"""

import logging
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from cuadra import config, db
from cuadra.ai import queue
from cuadra.channels import rate_limit
from cuadra.domain import broadcasts, settings
from cuadra.scheduler import maintenance

log = logging.getLogger("cuadra.scheduler")

_scheduler: AsyncIOScheduler | None = None


def _admission_open(conn) -> tuple[bool, str]:
    """Ask the sender governor whether a new broadcast may be created.

    The scheduler is the producer: creating a post while the userbot is still
    inside its pacing window only piles up jobs that will be parked (and, in
    the old design, wrongly counted as rate-limit failures). MVP policy: at
    most one broadcast in flight at a time.
    """
    wait = rate_limit.remaining("userbot", config.USERBOT_MIN_SEND_GAP_S)
    if wait > 0:
        return False, f"userbot busy ({wait:.0f}s)"
    if queue.has_pending("send_broadcast", conn):
        return False, "broadcast already in flight"
    return True, ""


def tick() -> None:
    conn = db.connect()
    try:
        if not settings.broadcast_publishing_enabled(conn):
            log.debug("scheduler tick skipped: publishing paused")
            return
        allowed, reason = _admission_open(conn)
        if not allowed:
            log.debug("scheduler tick skipped: %s", reason)
            return
        for broadcast in broadcasts.due_broadcasts(conn):
            try:
                outcome = broadcasts.schedule_next(conn, broadcast)
                if outcome == "queued":
                    log.info(
                        "broadcast %s: post scheduled", broadcast["id"]
                    )
                    # One post per tick: keep the global gap meaningful and
                    # avoid a stampede across campaigns.
                    break
            except Exception:  # noqa: BLE001 - one bad campaign must not stop the tick
                conn.rollback()
                log.exception("broadcast %s scheduling failed", broadcast["id"])
    finally:
        conn.close()


def start() -> None:
    global _scheduler
    _scheduler = AsyncIOScheduler(timezone="America/Havana")
    # Escalation timeouts: check every couple of minutes (Hito 5).
    _scheduler.add_job(maintenance.check_escalation_timeouts, "interval", seconds=120)
    # Customer signals are deterministic and cheap; never inside a turn.
    _scheduler.add_job(
        maintenance.scan_customer_signals,
        "interval",
        minutes=5,
        id="customer_signal_scan",
        max_instances=1,
        coalesce=True,
        next_run_time=datetime.now(timezone.utc) + timedelta(minutes=1),
    )
    # Privacy: raw messages past `customers.message_retention_days` are purged
    # once a day. Profile, sales and commissions stay.
    _scheduler.add_job(
        maintenance.purge_old_messages,
        "interval",
        hours=24,
        id="message_retention_purge",
        max_instances=1,
        coalesce=True,
        next_run_time=datetime.now(timezone.utc) + timedelta(minutes=10),
    )
    # LLM traces are pruned once a day (default retention: 30 days).
    _scheduler.add_job(
        maintenance.purge_llm_traces,
        "interval",
        hours=24,
        id="llm_trace_purge",
        max_instances=1,
        coalesce=True,
    )
    # Idle conversations (phase 2). Run shortly after boot and then daily, so a
    # laptop that restarts every day still closes abandoned conversations.
    _scheduler.add_job(
        maintenance.close_idle_conversations,
        "interval",
        hours=24,
        id="conversation_idle_close",
        max_instances=1,
        coalesce=True,
        next_run_time=datetime.now(timezone.utc) + timedelta(minutes=2),
    )
    # Customer reminders for stale sales (doc 13 §10.24). Off unless the
    # operator enables `sales.followup.mode`; `shadow` only records candidates.
    _scheduler.add_job(
        maintenance.queue_sale_followups,
        "interval",
        minutes=15,
        id="sale_followup_sweep",
        max_instances=1,
        coalesce=True,
        next_run_time=datetime.now(timezone.utc) + timedelta(minutes=3),
    )
    _scheduler.add_job(
        tick,
        "interval",
        seconds=max(5, config.BROADCAST_TICK_S),
        id="broadcast_tick",
        max_instances=1,
        coalesce=True,
    )
    _scheduler.start()
    log.info("scheduler started (broadcast tick %ss)", config.BROADCAST_TICK_S)


def stop() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        log.info("scheduler stopped")
