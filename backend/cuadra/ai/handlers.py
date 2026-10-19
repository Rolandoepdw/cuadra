"""Job handlers registered on the persistent queue."""

import asyncio
import logging
import random
from datetime import datetime

from cuadra import channels, config, db
from cuadra.ai import pipeline, queue, stt
from cuadra.domain import conversations, events

log = logging.getLogger("cuadra.handlers")


def register() -> None:
    queue.register_handler("generate_reply", generate_reply)
    queue.register_handler("transcribe_audio", transcribe_audio)
    queue.register_handler("send_message", send_message)


async def generate_reply(payload: dict) -> None:
    """Inbound message (text or transcribed audio) -> reply stored + queued to send."""
    conversation_id = payload["conversation_id"]
    message_id = payload["message_id"]
    conn = db.connect()
    try:
        conversation = conversations.get_conversation(conn, conversation_id)
        inbound = conversations.get_message(conn, message_id)
        result = await pipeline.handle_incoming(conn, conversation, inbound)
        outbound = conversations.add_message(
            conn,
            conversation_id,
            "OUTBOUND",
            "bot",
            body=result["reply"],
            latency_ms=result["latency_ms"],
        )
        events.log(
            conn,
            "bot",
            "reply_generated",
            entity="conversations",
            entity_id=conversation_id,
            details={
                "message_id": message_id,
                "intent": result["intent"]["intencion"],
                "confidence": result["intent"]["confianza"],
                "product_id": result["product_id"],
                "escalate": result["escalate"],
                "generator": result["generator"],
                "degraded": result["degraded"],
                "latency_ms": result["latency_ms"],
                "context_tokens": result["context_tokens"],
            },
        )
        await pipeline.maybe_summarize(conn, conversation)
    finally:
        conn.close()
    queue.enqueue(
        "send_message",
        {"conversation_id": conversation_id, "message_id": outbound["id"]},
        priority=config.PRIORITY_CUSTOMER,
    )


async def transcribe_audio(payload: dict) -> None:
    """Audio inbound -> transcription stored on the message -> reply job."""
    conversation_id = payload["conversation_id"]
    message_id = payload["message_id"]
    conn = db.connect()
    try:
        message = conversations.get_message(conn, message_id)
        abs_path = config.DATA_DIR / message["file_path"]
        initial_prompt = stt.build_initial_prompt(conn)
        result = await stt.transcribe(abs_path, initial_prompt=initial_prompt)
        conversations.set_message_body(conn, message_id, result["text"])
        events.log(
            conn,
            "stt",
            "audio_transcribed",
            entity="messages",
            entity_id=message_id,
            details=result,
        )
    finally:
        conn.close()
    queue.enqueue(
        "generate_reply",
        {"conversation_id": conversation_id, "message_id": message_id},
        priority=config.PRIORITY_CUSTOMER,
    )


def _human_delay() -> float:
    """Seconds to wait before sending (never below 1s; slower out of hours)."""
    in_hours = True
    try:
        from zoneinfo import ZoneInfo

        hour = datetime.now(ZoneInfo("America/Havana")).hour
        in_hours = config.BUSINESS_START_HOUR <= hour < config.BUSINESS_END_HOUR
    except Exception:  # noqa: BLE001 — no tz data => always in hours
        pass
    lo, hi = (
        (config.TYPING_DELAY_MIN, config.TYPING_DELAY_MAX)
        if in_hours
        else (config.OUT_OF_HOURS_DELAY_MIN, config.OUT_OF_HOURS_DELAY_MAX)
    )
    return max(1.0, random.uniform(lo, hi))


async def send_message(payload: dict) -> None:
    """Deliver a stored OUTBOUND message through its channel, human-like.

    Idempotent: a `message_sent` event marks delivery, so a requeued job
    (crash between send and completion) never double-sends.
    """
    conversation_id = payload["conversation_id"]
    message_id = payload["message_id"]

    conn = db.connect()
    try:
        sent = conn.execute(
            """SELECT 1 FROM events
               WHERE type = 'message_sent' AND entity = 'messages' AND entity_id = ?""",
            (message_id,),
        ).fetchone()
        if sent is not None:
            log.debug("message %s already sent, skipping", message_id)
            return
        message = conversations.get_message(conn, message_id)
        conversation = conversations.get_conversation(conn, conversation_id)
        target = conversations.get_channel_identifier(
            conn, conversation["customer_id"], conversation["channel"]
        )
    finally:
        conn.close()

    if target is None:
        log.warning("no channel identifier for conversation %s", conversation_id)
        return
    adapter = channels.get_adapter(conversation["channel"])
    if adapter is None:
        log.warning("no adapter for channel %r", conversation["channel"])
        return

    delay = _human_delay()
    await adapter.typing(target)
    await asyncio.sleep(delay)

    if message["type"] == "image" and message["file_path"]:
        await adapter.send_image(
            target, str(config.DATA_DIR / message["file_path"]), caption=message["body"]
        )
    elif message["type"] == "audio" and message["file_path"]:
        await adapter.send_audio(target, str(config.DATA_DIR / message["file_path"]))
    else:
        await adapter.send_text(target, message["body"] or "")

    conn = db.connect()
    try:
        events.log(
            conn,
            "bot",
            "message_sent",
            entity="messages",
            entity_id=message_id,
            details={"channel": conversation["channel"], "delay_s": round(delay, 2)},
        )
    finally:
        conn.close()
