"""Ingest an inbound message from any channel into the pipeline.

Stores the message (idempotently by `external_id`) and arms the conversation's
turn:
- text/audio/image from a client -> update turn + `process_turn`
- any from a known TCP          -> `tcp_reply` (correlates with an escalation)

Shared by the channel adapters and the debug endpoint so both paths behave
identically. The `source` on the stored message is `customer` or `supplier`.
"""

import logging

from cuadra import config, db
from cuadra.ai import assembler, queue
from cuadra.domain import catalog, conversations, events, turns

log = logging.getLogger("cuadra.ingest")


def ingest_incoming(
    *,
    channel: str,
    identifier: str,
    type: str = "text",
    body: str | None = None,
    file_path: str | None = None,
    customer_name: str | None = None,
    identity: str | None = None,
    external_id: str | None = None,
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
                identity=identity,
                external_id=external_id,
            )

        customer = conversations.get_or_create_customer(
            conn, channel, identifier, customer_name
        )
        conversation = conversations.get_or_create_open_conversation(
            conn, customer["id"], channel, identity=identity
        )
        inbound, created = conversations.add_message_with_status(
            conn,
            conversation["id"],
            "INBOUND",
            "customer",
            type=type,
            body=body,
            file_path=file_path,
            external_id=external_id,
        )
        if not created:
            # Telegram retried an update we already stored: no new turn/job.
            open_turn = turns.get_open(conn, conversation["id"])
            log.info(
                "duplicate external_id %s for conversation %s ignored",
                external_id,
                conversation["id"],
            )
            return {
                "source": "customer",
                "duplicate": True,
                "customer_id": customer["id"],
                "conversation_id": conversation["id"],
                "message_id": inbound["id"],
                "turn_id": open_turn["id"] if open_turn else None,
                "job_id": open_turn["job_id"] if open_turn else None,
            }

        turn = assembler.on_inbound(conn, conversation["id"], inbound["id"])
        return {
            "source": "customer",
            "duplicate": False,
            "customer_id": customer["id"],
            "conversation_id": conversation["id"],
            "message_id": inbound["id"],
            "turn_id": turn["id"],
            "job_id": turn["job_id"],
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
    identity: str | None = None,
    external_id: str | None = None,
) -> dict:
    """Store a TCP message and enqueue correlation with its oldest escalation."""
    # Supplier replies live in the same conversation bound to that TCP's channel,
    # so the audit trail stays in one place per contact.
    customer = conversations.get_or_create_customer(
        conn, channel, identifier, supplier["name"]
    )
    conversation = conversations.get_or_create_open_conversation(
        conn, customer["id"], channel, identity=identity
    )
    inbound, created = conversations.add_message_with_status(
        conn,
        conversation["id"],
        "INBOUND",
        "tcp",
        type=type,
        body=body,
        file_path=file_path,
        external_id=external_id,
    )
    if not created:
        return {
            "source": "supplier",
            "duplicate": True,
            "supplier_id": supplier["id"],
            "conversation_id": conversation["id"],
            "message_id": inbound["id"],
            "job_id": None,
        }
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
        "duplicate": False,
        "supplier_id": supplier["id"],
        "conversation_id": conversation["id"],
        "message_id": inbound["id"],
        "job_id": job_id,
    }
