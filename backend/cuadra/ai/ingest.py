"""Ingest an inbound message from any channel into the pipeline.

Stores the message and enqueues the right job: `transcribe_audio` for audio,
`generate_reply` otherwise. Shared by the channel adapters and the debug
endpoint so both paths behave identically.
"""

from cuadra import config, db
from cuadra.ai import queue
from cuadra.domain import conversations


def ingest_incoming(
    *,
    channel: str,
    identifier: str,
    type: str = "text",
    body: str | None = None,
    file_path: str | None = None,
    customer_name: str | None = None,
) -> dict:
    conn = db.connect()
    try:
        customer = conversations.get_or_create_customer(
            conn, channel, identifier, customer_name
        )
        conversation = conversations.get_or_create_open_conversation(
            conn, customer["id"], channel
        )
        inbound = conversations.add_message(
            conn,
            conversation["id"],
            "INBOUND",
            "customer",
            type=type,
            body=body,
            file_path=file_path,
        )
        job_type = "transcribe_audio" if type == "audio" else "generate_reply"
        job_id = queue.enqueue(
            job_type,
            {"conversation_id": conversation["id"], "message_id": inbound["id"]},
            priority=config.PRIORITY_CUSTOMER,
        )
        return {
            "customer_id": customer["id"],
            "conversation_id": conversation["id"],
            "message_id": inbound["id"],
            "job_id": job_id,
        }
    finally:
        conn.close()
