"""Broadcast scheduler (APScheduler).

Every `CUADRA_BROADCAST_TICK_S` seconds, due campaigns dispatch ONE post to one
eligible group. Spintax, category matching, daily caps and jitter live in
`domain/broadcasts.py`.
"""

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from cuadra import config, db
from cuadra.domain import broadcasts
from cuadra.scheduler import maintenance

log = logging.getLogger("cuadra.scheduler")

_scheduler: AsyncIOScheduler | None = None


def tick() -> None:
    conn = db.connect()
    try:
        for broadcast in broadcasts.due_broadcasts(conn):
            try:
                outcome = broadcasts.schedule_next(conn, broadcast)
                if outcome == "queued":
                    log.info(
                        "broadcast %s: post scheduled", broadcast["id"]
                    )
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
