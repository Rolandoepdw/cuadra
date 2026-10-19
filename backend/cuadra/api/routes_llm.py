"""Local LLM management endpoints (PIN-protected).

GET  /api/llm/status   — provider, managed/running, current model, port, ctx
GET  /api/llm/models   — GGUF models available in CUADRA_LLM_MODELS_DIR
POST /api/llm/start    — start the managed server (optional {"model": "..."} )
POST /api/llm/stop     — stop it and free VRAM
POST /api/llm/restart  — restart, optionally switching model
"""

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from cuadra.ai import llm_server
from cuadra.api.auth import require_pin

log = logging.getLogger("cuadra.llm_api")

router = APIRouter(prefix="/api/llm", dependencies=[Depends(require_pin)])


class ModelChoice(BaseModel):
    model: str | None = None


def _fail(exc: Exception) -> HTTPException:
    if isinstance(exc, FileNotFoundError):
        return HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    if isinstance(exc, (RuntimeError, TimeoutError)):
        return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc))
    return HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))


async def _run(func, *args):
    try:
        return await asyncio.to_thread(func, *args)
    except Exception as exc:  # noqa: BLE001 — surfaced as a clean HTTP error
        raise _fail(exc) from exc


@router.get("/status")
async def llm_status() -> dict:
    return llm_server.status()


@router.get("/models")
async def llm_models() -> dict:
    models = llm_server.list_models()
    current = llm_server.status().get("model")
    return {"models": models, "current": current}


@router.post("/start")
async def llm_start(choice: ModelChoice | None = None) -> dict:
    return await _run(llm_server.start, choice.model if choice else None)


@router.post("/stop")
async def llm_stop() -> dict:
    return await _run(llm_server.stop)


@router.post("/restart")
async def llm_restart(choice: ModelChoice | None = None) -> dict:
    return await _run(llm_server.restart, choice.model if choice else None)
