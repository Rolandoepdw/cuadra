"""Dashboard endpoints (Phase 1: health + summary)."""

import json
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Depends

from cuadra import __version__, config, db
from cuadra.api.auth import require_pin
from cuadra.channels import rate_limit, telegram as telegram_channel
from cuadra.channels import telegram_user
from cuadra.ai import llm, llm_server, queue
from cuadra.domain import clock

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
        "pacing": rate_limit.snapshot("userbot", config.USERBOT_MIN_SEND_GAP_S),
        "llm": {
            "provider": config.LLM_PROVIDER,
            "status": (await llm.health())["status"],
            "managed": server["managed"],
            "running": server["running"],
            "model": server["model"],
            "url": server["url"],
            "gpu": server["gpu"],
            "ctx": server["ctx"],
            "ctx_default": server["ctx_default"],
            "ctx_max": server["ctx_max"],
            "memory": server["memory"],
            "process_ram_mib": server["process_ram_mib"],
        },
        "telegram_channel": telegram_channel.mode(),
        "telegram_user": telegram_user.status(),
        "debug": config.DEBUG,
    }


def _scalar(conn, sql: str, args: tuple = ()) -> float | int | None:
    row = conn.execute(sql, args).fetchone()
    return row[0] if row and row[0] is not None else None


def _avg_stt_ms(conn) -> float | None:
    """Average whisper latency from today's audio_transcribed events.

    STT latency lives in the event details (elapsed_s), not on the message.
    """
    day_start, day_end = clock.day_bounds()
    rows = conn.execute(
        "SELECT details_json FROM events "
        "WHERE source = 'stt' AND type = 'audio_transcribed' "
        "AND created_at >= ? AND created_at < ?",
        (day_start, day_end),
    ).fetchall()
    samples = []
    for row in rows:
        try:
            elapsed = json.loads(row["details_json"]).get("elapsed_s")
        except (ValueError, TypeError):
            continue
        if elapsed is not None:
            samples.append(float(elapsed) * 1000)
    return round(sum(samples) / len(samples), 1) if samples else None


@router.get("/summary")
async def summary() -> dict:
    """Headline metrics for the General panel (today, Havana business day)."""
    day_start, day_end = clock.day_bounds()
    conn = db.connect()
    try:
        messages_today = _scalar(
            conn,
            "SELECT COUNT(*) FROM messages "
            "WHERE created_at >= ? AND created_at < ?",
            (day_start, day_end),
        )
        messages_in = _scalar(
            conn,
            "SELECT COUNT(*) FROM messages "
            "WHERE created_at >= ? AND created_at < ? AND direction = 'INBOUND'",
            (day_start, day_end),
        )
        active_conversations = _scalar(
            conn,
            "SELECT COUNT(*) FROM conversations WHERE status IN ('ACTIVE', 'WAITING_TCP')",
        )
        escalations_open = _scalar(
            conn,
            "SELECT COUNT(*) FROM escalations WHERE status IN ('PENDING', 'ANSWERED')",
        )
        avg_llm_ms = _scalar(
            conn,
            "SELECT AVG(latency_ms) FROM messages "
            "WHERE author = 'bot' AND direction = 'OUTBOUND' "
            "AND created_at >= ? AND created_at < ? AND latency_ms IS NOT NULL",
            (day_start, day_end),
        )
        customers_total = _scalar(conn, "SELECT COUNT(*) FROM customers")
        products_active = _scalar(
            conn, "SELECT COUNT(*) FROM products WHERE status = 'ACTIVE'"
        )
        stt_ms = _avg_stt_ms(conn)
        queue_size = queue.queue_size(conn)
    finally:
        conn.close()

    return {
        "utc": datetime.now(timezone.utc).isoformat(),
        "messages_today": messages_today or 0,
        "messages_inbound_today": messages_in or 0,
        "active_conversations": active_conversations or 0,
        "escalations_open": escalations_open or 0,
        "customers_total": customers_total or 0,
        "products_active": products_active or 0,
        "avg_llm_ms": round(avg_llm_ms, 1) if avg_llm_ms is not None else None,
        "avg_stt_ms": stt_ms,
        "queue_size": queue_size,
    }

