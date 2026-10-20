"""Local LLM management endpoints (PIN-protected).

GET  /api/llm/status   — provider, managed/running, current model, port, ctx, VRAM
GET  /api/llm/models   — GGUF models available in CUADRA_LLM_MODELS_DIR
POST /api/llm/start    — start the managed server (optional {"model": "..."} )
POST /api/llm/stop     — stop it and free VRAM
POST /api/llm/restart  — restart, optionally switching model
POST /api/llm/model    — change the active model in place (restarts if running)
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


class CtxChoice(BaseModel):
    ctx: int | None = None


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


@router.post("/model")
async def llm_set_model(choice: ModelChoice) -> dict:
    """Switch the active model, persisted in settings.

    If the managed server is running it restarts with the new model; otherwise
    it just records the choice (applied on the next start).
    """
    if not choice.model:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "model is required")

    def _switch() -> dict:
        llm_server.resolve_model(choice.model)  # validate it exists (raises)
        if llm_server.is_running():
            return llm_server.restart(choice.model)
        llm_server.set_active_model(choice.model)
        return llm_server.status()

    return await _run(_switch)


@router.post("/ctx")
async def llm_set_ctx(choice: CtxChoice) -> dict:
    """Set the context window (tokens), persisted in settings.

    Send ``{"ctx": null}`` (or omit it) to reset to the config default.
    Validates against the active model's trained maximum, if known. Restarts the
    managed server if running so the new context takes effect.
    """
    ctx = choice.ctx
    if ctx is None:
        ctx = llm_server.config_default_ctx()

    max_ctx = llm_server.model_context_max()
    if ctx < 512:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "ctx must be >= 512")
    if max_ctx and ctx > max_ctx:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"ctx exceeds the model's max ({max_ctx} tokens)",
        )

    def _apply() -> dict:
        llm_server.set_active_ctx(ctx)
        if llm_server.is_running():
            return llm_server.restart()
        return llm_server.status()

    return await _run(_apply)
