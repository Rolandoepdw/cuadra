"""Dashboard endpoints (Phase 1: health + stub summary)."""

import time
from datetime import datetime, timezone

from fastapi import APIRouter, Depends

from cuadra import __version__, config, db
from cuadra.api.auth import require_pin
from cuadra.channels import telegram as telegram_channel
from cuadra.ai import llm, llm_server, queue

router = APIRouter(prefix="/api", dependencies=[Depends(require_pin)])

_STARTUP = time.time()


@router.get("/health")
async def health() -> dict:
    conn = db.connect()
    try:
        integrity = "ok" if db.integrity_ok(conn) else "fail"
        jobs = queue.queue_size(conn)
        migrations = [
            r["name"]
            for r in conn.execute("SELECT name FROM schema_migrations ORDER BY name")
        ]
        db_size = config.DB_PATH.stat().st_size if config.DB_PATH.exists() else 0
    finally:
        conn.close()

    server = llm_server.status()
    return {
        "service": "cuadra-core",
        "version": __version__,
        "utc": datetime.now(timezone.utc).isoformat(),
        "uptime_s": int(time.time() - _STARTUP),
        "database": {
            "integrity": integrity,
            "migrations": migrations,
            "size_bytes": db_size,
        },
        "job_queue": jobs,
        "llm": {
            "provider": config.LLM_PROVIDER,
            "status": (await llm.health())["status"],
            "managed": server["managed"],
            "running": server["running"],
            "model": server["model"],
            "url": server["url"],
        },
        "telegram_channel": telegram_channel.mode(),
        "debug": config.DEBUG,
    }
