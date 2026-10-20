"""Development-only endpoints (disabled unless CUADRA_DEBUG=1).

POST /api/debug/message — simulate a full inbound-message turn through the queue
POST /api/debug/audio   — transcribe an audio file (optionally run the full turn)
GET  /api/debug/conversation/{id} — inspect stored messages and context
"""

import asyncio
import logging
import uuid
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from pydantic import BaseModel, Field

from cuadra import config, db
from cuadra.ai import ingest, queue, stt
from cuadra.api.auth import require_pin
from cuadra.domain import conversations, escalations

log = logging.getLogger("cuadra.debug")

router = APIRouter(prefix="/api/debug", dependencies=[Depends(require_pin)])


class DebugMessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    identifier: str = Field(default="debug-user", max_length=80)
    channel: Literal["telegram", "whatsapp"] = "telegram"
    customer_name: str | None = None
    wait_s: float = Field(default=60, ge=1, le=180)


def _ensure_debug() -> None:
    if not config.DEBUG:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")


async def _wait_for_reply(job_id: int, conversation_id: int, wait_s: float) -> dict:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + wait_s
    while True:
        conn = db.connect()
        try:
            job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if job is not None and job["status"] == "COMPLETED":
                reply = conn.execute(
                    """SELECT * FROM messages
                       WHERE conversation_id = ? AND direction = 'OUTBOUND'
                       ORDER BY id DESC LIMIT 1""",
                    (conversation_id,),
                ).fetchone()
                return dict(reply) if reply is not None else {}
            if job is not None and job["status"] == "FAILED":
                raise HTTPException(
                    status.HTTP_502_BAD_GATEWAY, f"job failed: {job['last_error']}"
                )
        finally:
            conn.close()
        if loop.time() >= deadline:
            raise HTTPException(
                status.HTTP_504_GATEWAY_TIMEOUT,
                "timed out waiting for the reply — is the local model running?",
            )
        await asyncio.sleep(0.25)


@router.post("/message")
async def debug_message(payload: DebugMessageIn) -> dict:
    _ensure_debug()
    info = ingest.ingest_incoming(
        channel=payload.channel,
        identifier=payload.identifier,
        type="text",
        body=payload.text,
        customer_name=payload.customer_name,
    )
    reply = await _wait_for_reply(info["job_id"], info["conversation_id"], payload.wait_s)
    return {
        "conversation_id": info["conversation_id"],
        "customer_id": info["customer_id"],
        "job_id": info["job_id"],
        "inbound": payload.text,
        "reply": reply.get("body"),
        "reply_message_id": reply.get("id"),
        "latency_ms": reply.get("latency_ms"),
    }


@router.post("/audio")
async def debug_audio(
    file: UploadFile = File(...),
    respond: bool = True,
    identifier: str = "debug-user",
    channel: Literal["telegram", "whatsapp"] = "telegram",
    wait_s: float = 120,
) -> dict:
    _ensure_debug()
    suffix = Path(file.filename or "audio.ogg").suffix or ".ogg"
    name = f"{uuid.uuid4().hex}{suffix}"
    path = config.AUDIOS_DIR / name
    path.write_bytes(await file.read())

    conn = db.connect()
    try:
        initial_prompt = stt.build_initial_prompt(conn)
    finally:
        conn.close()
    result = await stt.transcribe(path, initial_prompt=initial_prompt)
    response: dict = {"audio_path": f"audios/{name}", **result}
    if not respond:
        return response

    conn = db.connect()
    try:
        customer = conversations.get_or_create_customer(conn, channel, identifier)
        conversation = conversations.get_or_create_open_conversation(
            conn, customer["id"], channel
        )
        inbound = conversations.add_message(
            conn,
            conversation["id"],
            "INBOUND",
            "customer",
            type="audio",
            body=result["text"],
            file_path=f"audios/{name}",
        )
        job_id = queue.enqueue(
            "generate_reply",
            {"conversation_id": conversation["id"], "message_id": inbound["id"]},
            priority=config.PRIORITY_CUSTOMER,
        )
        conversation_id = conversation["id"]
    finally:
        conn.close()

    reply = await _wait_for_reply(job_id, conversation_id, wait_s)
    response.update(
        {
            "conversation_id": conversation_id,
            "job_id": job_id,
            "reply": reply.get("body"),
            "reply_latency_ms": reply.get("latency_ms"),
        }
    )
    return response


@router.get("/conversation/{conversation_id}")
def debug_conversation(conversation_id: int) -> dict:
    _ensure_debug()
    conn = db.connect()
    try:
        conversation = conversations.get_conversation(conn, conversation_id)
        context = conversations.get_context(conn, conversation_id)
        messages = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM messages WHERE conversation_id = ? ORDER BY id",
                (conversation_id,),
            )
        ]
    finally:
        conn.close()
    return {"conversation": conversation, "context": context, "messages": messages}


@router.get("/escalations")
def debug_escalations(status: str | None = None, limit: int = 50) -> dict:
    _ensure_debug()
    conn = db.connect()
    try:
        items = escalations.list_escalations(conn, status=status, limit=limit)
    finally:
        conn.close()
    return {"escalations": items}
