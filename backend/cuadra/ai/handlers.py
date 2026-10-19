"""Job handlers registered on the persistent queue."""

import logging

from cuadra import config, db
from cuadra.ai import pipeline, queue, stt
from cuadra.domain import conversations, events

log = logging.getLogger("cuadra.handlers")


def register() -> None:
    queue.register_handler("generate_reply", generate_reply)
    queue.register_handler("transcribe_audio", transcribe_audio)


async def generate_reply(payload: dict) -> None:
    """Inbound message (text or transcribed audio) -> reply stored in DB.

    Hito 4 sends the stored reply through the channel (with human delays).
    """
    conversation_id = payload["conversation_id"]
    message_id = payload["message_id"]
    conn = db.connect()
    try:
        conversation = conversations.get_conversation(conn, conversation_id)
        inbound = conversations.get_message(conn, message_id)
        result = await pipeline.handle_incoming(conn, conversation, inbound)
        conversations.add_message(
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
