"""Dashboard endpoints (Phase 1: health + summary)."""

import json
import time
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

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


# ---------------------------------------------------------------------------
# Trazas de ejecución (execution tracing)
# ---------------------------------------------------------------------------

@router.get("/traces")
async def list_traces(
    conversation_id: Optional[int] = None,
    method: Optional[str] = None,
    ok: Optional[bool] = None,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """Listar trazas de ejecución con filtros opcionales.

    Parámetros de consulta:
      - conversation_id: filtrar por conversación
      - method: filtrar por nombre de método (ej. pipeline.handle_turn)
      - ok: filtrar solo exitosas (true) o fallidas (false)
      - limit: cantidad máxima de resultados (default 50)
      - offset: desplazamiento para paginación
    """
    conn = db.connect()
    try:
        where = "1=1"
        args: list = []
        if conversation_id is not None:
            where += " AND conversation_id = ?"
            args.append(conversation_id)
        if method is not None:
            where += " AND method = ?"
            args.append(method)
        if ok is not None:
            where += " AND ok = ?"
            args.append(1 if ok else 0)

        rows = conn.execute(
            f"""SELECT id, timestamp, method, input_json, output_json, ok,
                       error, error_type, duration_ms, conversation_id, turn_id
                  FROM traces
                 WHERE {where}
              ORDER BY created_at DESC, id DESC
                 LIMIT ? OFFSET ?""",
            (*args, limit, offset),
        ).fetchall()

        total = conn.execute(
            f"SELECT COUNT(*) FROM traces WHERE {where}",
            tuple(args),
        ).fetchone()[0]

        traces = []
        for r in rows:
            try:
                inp = json.loads(r["input_json"]) if r["input_json"] else None
            except (ValueError, TypeError):
                inp = r["input_json"]
            try:
                out = json.loads(r["output_json"]) if r["output_json"] else None
            except (ValueError, TypeError):
                out = r["output_json"]
            traces.append(
                {
                    "id": r["id"],
                    "timestamp": r["timestamp"],
                    "method": r["method"],
                    "input": inp,
                    "output": out,
                    "ok": bool(r["ok"]),
                    "error": r["error"],
                    "error_type": r["error_type"],
                    "duration_ms": r["duration_ms"],
                    "conversation_id": r["conversation_id"],
                    "turn_id": r["turn_id"],
                }
            )
    finally:
        conn.close()

    return {"items": traces, "total": total, "limit": limit, "offset": offset}


@router.get("/traces/{trace_id}")
async def get_trace(trace_id: int) -> dict:
    """Obtener el detalle completo de una traza por su ID."""
    conn = db.connect()
    try:
        row = conn.execute(
            """SELECT id, timestamp, method, input_json, output_json, ok,
                      error, error_type, duration_ms, conversation_id, turn_id
                 FROM traces
                WHERE id = ?""",
            (trace_id,),
        ).fetchone()
        if row is None:
            from fastapi import HTTPException

            raise HTTPException(status_code=404, detail="trace not found")

        try:
            inp = json.loads(row["input_json"]) if row["input_json"] else None
        except (ValueError, TypeError):
            inp = row["input_json"]
        try:
            out = json.loads(row["output_json"]) if row["output_json"] else None
        except (ValueError, TypeError):
            out = row["output_json"]

        return {
            "id": row["id"],
            "timestamp": row["timestamp"],
            "method": row["method"],
            "input": inp,
            "output": out,
            "ok": bool(row["ok"]),
            "error": row["error"],
            "error_type": row["error_type"],
            "duration_ms": row["duration_ms"],
            "conversation_id": row["conversation_id"],
            "turn_id": row["turn_id"],
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Métricas de calidad
# ---------------------------------------------------------------------------

@router.get("/metrics/summary")
async def metrics_summary() -> dict:
    """Métricas de calidad del día (hora de La Habana)."""
    day_start, day_end = clock.day_bounds()
    conn = db.connect()
    try:
        total_traces = _scalar(
            conn,
            "SELECT COUNT(*) FROM traces WHERE created_at >= ? AND created_at < ?",
            (day_start, day_end),
        )
        failed_traces = _scalar(
            conn,
            "SELECT COUNT(*) FROM traces WHERE created_at >= ? AND created_at < ? AND ok = 0",
            (day_start, day_end),
        )
        # F8: el turno completo es la única medida que le importa al negocio.
        # Promediar todas las trazas mezclaba `llm.chat` (segundos) con guardados
        # de venta (milisegundos). El promedio de LLM queda aparte.
        avg_duration_ms = _scalar(
            conn,
            """SELECT AVG(duration_ms) FROM traces
                WHERE created_at >= ? AND created_at < ?
                  AND method = 'pipeline.handle_turn'
                  AND duration_ms IS NOT NULL""",
            (day_start, day_end),
        )
        avg_llm_ms = _scalar(
            conn,
            """SELECT AVG(duration_ms) FROM traces
                WHERE created_at >= ? AND created_at < ?
                  AND method = 'llm.chat' AND duration_ms IS NOT NULL""",
            (day_start, day_end),
        )

        # Ventas atascadas en DISPATCHED más de 30 minutos
        stuck_sales = _scalar(
            conn,
            """SELECT COUNT(*) FROM sales
               WHERE status = 'DISPATCHED'
                 AND dispatched_at IS NOT NULL
                 AND (julianday('now') - julianday(dispatched_at)) * 24 * 60 > 30""",
        )

        # Escalaciones abiertas
        open_escalations = _scalar(
            conn,
            "SELECT COUNT(*) FROM escalations WHERE status IN ('PENDING', 'ANSWERED')",
        )

        # Acks ambiguos hoy
        ambiguous_acks = _scalar(
            conn,
            "SELECT COUNT(*) FROM traces WHERE created_at >= ? AND created_at < ? AND method = 'sales.apply_tcp_ack' AND output_json LIKE '%AMBIGUOUS%'",
            (day_start, day_end),
        )

        # Conversaciones activas
        active_conversations = _scalar(
            conn,
            "SELECT COUNT(*) FROM conversations WHERE status IN ('ACTIVE', 'WAITING_TCP')",
        )
    finally:
        conn.close()

    return {
        "utc": datetime.now(timezone.utc).isoformat(),
        "total_traces_today": total_traces or 0,
        "failed_traces_today": failed_traces or 0,
        "avg_duration_ms": round(avg_duration_ms, 1) if avg_duration_ms is not None else None,
        "avg_llm_ms": round(avg_llm_ms, 1) if avg_llm_ms is not None else None,
        "stuck_sales_dispatched": stuck_sales or 0,
        "open_escalations": open_escalations or 0,
        "ambiguous_acks_today": ambiguous_acks or 0,
        "active_conversations": active_conversations or 0,
    }


@router.get("/metrics/timeline")
async def metrics_timeline(days: int = 7) -> dict:
    """Métricas de calidad agrupadas por día (últimos N días)."""
    conn = db.connect()
    try:
        rows = conn.execute(
            """SELECT date(created_at) AS dia,
                      COUNT(*) AS total_traces,
                      SUM(CASE WHEN ok = 0 THEN 1 ELSE 0 END) AS failed_traces,
                      ROUND(AVG(CASE WHEN method = 'pipeline.handle_turn'
                                     THEN duration_ms END), 1) AS avg_duration_ms
                 FROM traces
                WHERE created_at >= datetime('now', ? || ' days')
                GROUP BY date(created_at)
                ORDER BY dia ASC""",
            (-days,),
        ).fetchall()

        timeline = [
            {
                "dia": r["dia"],
                "total_traces": r["total_traces"],
                "failed_traces": r["failed_traces"],
                "avg_duration_ms": round(r["avg_duration_ms"], 1) if r["avg_duration_ms"] is not None else None,
            }
            for r in rows
        ]
    finally:
        conn.close()

    return {"timeline": timeline}


