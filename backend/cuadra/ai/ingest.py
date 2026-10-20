"""Ingest an inbound message from any channel into the pipeline.

Stores the message and enqueues the right job:
- audio from a client      -> `transcribe_audio`
- text from a client       -> `generate_reply`
- any from a known TCP     -> `tcp_reply` (correlates with a pending escalation)

Shared by the channel adapters and the debug endpoint so both paths behave
identically. The `source` on the stored message is `customer` or `supplier`.
"""

import logging

from cuadra import config, db
from cuadra.ai import queue
from cuadra.domain import catalog, conversations, events

log = logging.getLogger("cuadra.ingest")


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
        # A known supplier writing on its Telegram id is a TCP reply, not a client.
        supplier = (
            catalog.find_supplier_by_telegram_id(conn, identifier)
            if channel == "telegram"
            else None
        )

        if supplier is not None:
            return _ingest_supplier(
                conn,
                channel=channel,
                identifier=identifier,
                supplier=supplier,
                type=type,
                body=body,
                file_path=file_path,
            )

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
            "source": "customer",
            "customer_id": customer["id"],
            "conversation_id": conversation["id"],
            "message_id": inbound["id"],
            "job_id": job_id,
        }
    finally:
        conn.close()


def _ingest_supplier(
    conn,
    *,
    channel: str,
    identifier: str,
    supplier: dict,
    type: str,
    body: str | None,
    file_path: str | None,
) -> dict:
    """Store a TCP message and enqueue correlation with its oldest escalation."""
    # Supplier replies live in the same conversation bound to that TCP's channel,
    # so the audit trail stays in one place per contact.
    customer = conversations.get_or_create_customer(
        conn, channel, identifier, supplier["name"]
    )
    conversation = conversations.get_or_create_open_conversation(
        conn, customer["id"], channel
    )
    inbound = conversations.add_message(
        conn,
        conversation["id"],
        "INBOUND",
        "tcp",
        type=type,
        body=body,
        file_path=file_path,
    )
    job_id = queue.enqueue(
        "tcp_reply",
        {"conversation_id": conversation["id"], "message_id": inbound["id"], "supplier_id": supplier["id"]},
        priority=config.PRIORITY_TCP,
    )
    events.log(
        conn,
        "bot",
        "tcp_message_received",
        entity="suppliers",
        entity_id=supplier["id"],
        details={"message_id": inbound["id"], "conversation_id": conversation["id"]},
    )
    return {
        "source": "supplier",
        "supplier_id": supplier["id"],
        "conversation_id": conversation["id"],
        "message_id": inbound["id"],
        "job_id": job_id,
    }
