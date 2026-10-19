"""cuadra-core entrypoint — single-service runtime.

One process runs: dashboard API, job queue, broadcast scheduler and the
Telegram channel (polling). See cuadra_doc/01_arquitectura.md.
"""

import logging
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from cuadra import config, db
from cuadra.ai import handlers, queue
from cuadra.api import routes_catalog, routes_dashboard, routes_debug
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
    log.info(
        "cuadra-core ready (debug=%s, pin=%s)",
        config.DEBUG,
        "yes" if config.PIN else "no",
    )

    yield

    await telegram_channel.stop()
    scheduler_broadcasts.stop()
    await queue.stop()
    log.info("cuadra-core stopped")


app = FastAPI(title="Cuadra Core", lifespan=lifespan)
app.include_router(routes_dashboard.router)
app.include_router(routes_catalog.router)
app.include_router(routes_debug.router)


@app.exception_handler(DomainError)
async def _domain_error_handler(request, exc: DomainError):
    code = 404 if isinstance(exc, NotFound) else 400
    return JSONResponse({"detail": str(exc)}, status_code=code)
