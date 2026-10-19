"""cuadra-core entrypoint — single-service runtime.

One process runs: dashboard API, job queue, broadcast scheduler and the
Telegram channel (polling). See cuadra_doc/01_arquitectura.md.
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from cuadra import config, db
from cuadra.ai import handlers, llm_server, queue
from cuadra.api import routes_catalog, routes_dashboard, routes_debug, routes_llm
from cuadra.channels import telegram as telegram_channel
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    config.ensure_dirs()
    _setup_logging()

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
    handlers.register()
    queue.start()
    scheduler_broadcasts.start()
    await telegram_channel.start()

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
    await telegram_channel.stop()
    scheduler_broadcasts.stop()
    await queue.stop()
    await asyncio.to_thread(llm_server.stop)
    log.info("cuadra-core stopped")


app = FastAPI(title="Cuadra Core", lifespan=lifespan)
app.include_router(routes_dashboard.router)
app.include_router(routes_catalog.router)
app.include_router(routes_llm.router)
app.include_router(routes_debug.router)


@app.exception_handler(DomainError)
async def _domain_error_handler(request, exc: DomainError):
    code = 404 if isinstance(exc, NotFound) else 400
    return JSONResponse({"detail": str(exc)}, status_code=code)
