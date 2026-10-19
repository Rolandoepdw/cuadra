"""Broadcast scheduler (APScheduler). Hito 1: start/stop only.

Spintax, category matching and jitter logic live in domain/broadcasts.py (Hito 7).
"""

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

log = logging.getLogger("cuadra.scheduler")

_scheduler: AsyncIOScheduler | None = None


def start() -> None:
    global _scheduler
    _scheduler = AsyncIOScheduler(timezone="America/Havana")
    _scheduler.start()
    # Hito 7: _scheduler.add_job(broadcasts.tick, "interval", seconds=60)
    log.info("scheduler started")


def stop() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        log.info("scheduler stopped")
