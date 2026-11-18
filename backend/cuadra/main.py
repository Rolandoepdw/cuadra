"""cuadra-core entrypoint — single-service runtime.

One process runs: dashboard API, job queue, broadcast scheduler and the
Telegram channel (polling). See cuadra_doc/01_arquitectura.md.
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from cuadra import config, db, instance
from cuadra.ai import assembler, handlers, llm_server, queue, retrieval
from cuadra.api import (
    routes_broadcasts,
    routes_catalog,
    routes_conversations,
    routes_customers,
    routes_dashboard,
    routes_debug,
    routes_finance,
    routes_llm,
    routes_logs,
    routes_settings,
)
from cuadra.channels import telegram as telegram_channel
from cuadra.channels import telegram_user
from cuadra.domain import catalog, events
from cuadra.domain.errors import DomainError, NotFound
from cuadra.scheduler import broadcasts as scheduler_broadcasts

log = logging.getLogger("cuadra")


def _setup_logging() -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)
    file_handler = RotatingFileHandler(
        config.LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    # F9 (prueba #2): el 90 % de cuadra.log era "Running job tick" cada 5 s.
    # Los mensajes de negocio quedaban enterrados. Los errores siguen saliendo.
    for noisy in ("apscheduler", "httpx", "telethon.network"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _warn_suppliers_without_peer() -> None:
    """Surface TCPs that still depend on a mutable `@usuario` (see 10.22)."""
    conn = db.connect()
    try:
        pending = catalog.list_suppliers_missing_peer(conn)
        if not pending:
            return
        log.warning(
            "suppliers without a numeric Telegram id: %s",
            ", ".join(f"#{s['id']} {s['name']}" for s in pending),
        )
        events.log(
            conn,
            "system",
            "suppliers_missing_peer",
            entity="suppliers",
            details={"supplier_ids": [s["id"] for s in pending]},
        )
    except Exception:  # noqa: BLE001 - an alert must never block startup
        log.exception("could not check suppliers without a Telegram peer")
    finally:
        conn.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    config.ensure_dirs()
    _setup_logging()

    # One core per machine: two would both poll Telegram and steal updates.
    lock = instance.acquire()
    log.info("core lock acquired (pid %s, %s)", lock["pid"], lock["file"])

    conn = db.connect()
    try:
        applied = db.apply_migrations(conn)
        if applied:
            log.info("migrations applied: %s", ", ".join(applied))
        if not db.integrity_ok(conn):
            log.critical("DATABASE INTEGRITY FAILURE — check backups (01 §7.1)")
    finally:
        conn.close()

    recovered = queue.requeue_in_progress()
    if recovered:
        log.warning("requeued %s job(s) interrupted by the last shutdown", recovered)
    recovered_turns = assembler.recover_open()
    if recovered_turns:
        log.warning("re-armed %s abandoned customer turn(s)", recovered_turns)
    handlers.register()
    _warn_suppliers_without_peer()
    rearmed = handlers.recover_pending_dispatches()
    if rearmed:
        log.warning("re-armed %s pending sale dispatch(es)", rearmed)
    queue.start()
    # If the embedding model is vendored and some products lack a vector, queue
    # one low-priority reindex. Without the model this is skipped and search
    # stays on FTS5 + fuzzy (never downloads at startup).
    try:
        if retrieval.embeddings_available() and retrieval.needs_reindex():
            queue.enqueue(
                "reindex_all_products", {}, priority=config.PRIORITY_MAINTENANCE
            )
            log.info("queued initial product embedding reindex")
    except Exception:  # noqa: BLE001 - retrieval must never block startup
        log.exception("could not queue the initial product reindex")
    scheduler_broadcasts.start()
    await telegram_channel.start()
    await telegram_user.start()

    # Bring up the local llama.cpp server in the background so the API is
    # available immediately while the model loads (the queue retries meanwhile).
    llm_task = None
    if config.LLM_MANAGE and config.LLM_AUTOSTART:
        llm_task = asyncio.create_task(asyncio.to_thread(llm_server.ensure_running))

    log.info(
        "cuadra-core ready (debug=%s, pin=%s, llm=%s)",
        config.DEBUG,
        "yes" if config.PIN else "no",
        config.LLM_PROVIDER,
    )

    yield

    if llm_task is not None and not llm_task.done():
        llm_task.cancel()
    await telegram_user.stop()
    await telegram_channel.stop()
    scheduler_broadcasts.stop()
    await queue.stop()
    await asyncio.to_thread(llm_server.stop)
    instance.release()
    log.info("cuadra-core stopped")


app = FastAPI(title="Cuadra Core", lifespan=lifespan)


@app.middleware("http")
async def _api_no_store(request, call_next):
    """Las respuestas de /api nunca se cachean en el navegador.

    Antes, cuando una ruta /api todavía no existía, el fallback del SPA
    devolvía index.html con 200 y el navegador lo guardaba en caché: al
    agregar el endpoint el dashboard seguía recibiendo HTML y fallaba con
    "Unexpected token '<'".
    """
    response = await call_next(request)
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


app.include_router(routes_dashboard.router)
app.include_router(routes_conversations.router)
app.include_router(routes_customers.router)
app.include_router(routes_catalog.router)
app.include_router(routes_broadcasts.router)
app.include_router(routes_finance.router)
app.include_router(routes_settings.router)
app.include_router(routes_llm.router)
app.include_router(routes_logs.router)
app.include_router(routes_debug.router)

# Product images: /media/<local_path> (e.g. /media/fotos/product_1/img_1.jpg).
config.DATA_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/media", StaticFiles(directory=config.DATA_DIR), name="media")


@app.get("/", include_in_schema=False)
@app.get("/{path:path}", include_in_schema=False)
async def spa(path: str = ""):
    """Serve the built dashboard (SPA). Unknown paths fall back to index.html.

    /api and /media are matched by their own routes/mounts first, so only
    client-side routes reach here.
    """
    # Un endpoint /api inexistente debe fallar como JSON 404, nunca devolver
    # HTML: si devolvemos index.html el dashboard lo intenta parsear como JSON
    # y el error real ("ruta no encontrada") queda oculto.
    if path == "api" or path.startswith("api/"):
        return JSONResponse({"detail": f"unknown endpoint /{path}"}, status_code=404)
    index = config.WEB_DIR / "index.html"
    if not index.exists():
        return JSONResponse(
            {"detail": "dashboard not built (run build_dashboard.ps1)"},
            status_code=503,
        )
    candidate = (config.WEB_DIR / path).resolve()
    if path and candidate.is_file() and config.WEB_DIR.resolve() in candidate.parents:
        # Vite emite nombres con hash para los assets: se pueden cachear para
        # siempre. El resto (logo) debe revalidar.
        if path.startswith("assets/"):
            headers = {"Cache-Control": "public, max-age=31536000, immutable"}
        else:
            headers = {"Cache-Control": "no-cache"}
        return FileResponse(candidate, headers=headers)
    # index.html siempre debe revalidarse: si el navegador guarda una copia
    # vieja, apunta a assets que el build nuevo ya borró y el dashboard queda en
    # blanco.
    return FileResponse(index, headers={"Cache-Control": "no-cache"})


@app.exception_handler(DomainError)
async def _domain_error_handler(request, exc: DomainError):
    code = 404 if isinstance(exc, NotFound) else 400
    return JSONResponse({"detail": str(exc)}, status_code=code)

