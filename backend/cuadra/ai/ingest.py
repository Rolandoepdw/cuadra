"""Ingest an inbound message from any channel into the pipeline.

Stores the message (idempotently by `external_id`) and arms the conversation's
turn:
- text/audio/image from a client -> update turn + `process_turn`
- any from a known TCP          -> `tcp_process` (or `transcribe_audio` first)

Shared by the channel adapters and the debug endpoint so both paths behave
identically. The `source` on the stored message is `customer` or `supplier`.
"""

import logging

from cuadra import config, db
from cuadra.ai import assembler, queue
from cuadra.domain import catalog, conversations, customers, events, turns

log = logging.getLogger("cuadra.ingest")


def ingest_incoming(
    *,
    channel: str,
    identifier: str,
    type: str = "text",
    body: str | None = None,
    file_path: str | None = None,
    customer_name: str | None = None,
    handle: str | None = None,
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
        if supplier is None and channel == "telegram" and handle:
            supplier = _resolve_supplier_by_handle(conn, handle, identifier)

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
        customers.touch(conn, customer["id"])
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


def _resolve_supplier_by_handle(conn, handle: str, identifier: str) -> dict | None:
    """Match an inbound `@usuario` and learn its numeric id the first time.

    Suppliers are registered by handle, so a rename breaks numeric matching until
    the peer is known; a reply from them is a chance to learn it for free. Once a
    peer is stored, a different sender on the same handle is NOT the supplier:
    handles can be released and taken over, and business traffic must never reach
    whoever took them.
    """
    supplier = catalog.find_supplier_by_telegram_handle(conn, handle)
    if supplier is None:
        return None

    actual = str(identifier).strip()
    if actual.startswith("-"):
        # Group/channel chat id: the bot got added to a group, not a TCP writing
        # in private. Learning it would send escalations to the whole group.
        return None

    peer = str(supplier.get("telegram_peer_id") or "").strip()

    if peer and peer != actual:
        log.warning(
            "supplier %s handle %s now writes from %s (expected %s)",
            supplier["id"],
            supplier.get("telegram_id"),
            actual,
            peer,
        )
        already = conn.execute(
            """SELECT 1 FROM events
               WHERE type = 'supplier_peer_mismatch' AND entity = 'suppliers'
                 AND entity_id = ? AND details_json LIKE ?
               LIMIT 1""",
            (supplier["id"], f'%"actual_peer_id": "{actual}"%'),
        ).fetchone()
        if already is None:
            events.log(
                conn,
                "bot",
                "supplier_peer_mismatch",
                entity="suppliers",
                entity_id=supplier["id"],
                details={
                    "telegram_id": supplier.get("telegram_id"),
                    "expected_peer_id": peer,
                    "actual_peer_id": actual,
                },
            )
        return None

    if not peer:
        catalog.set_supplier_telegram_peer(conn, supplier["id"], actual)
        supplier["telegram_peer_id"] = actual
        log.info(
            "learned telegram peer %s for supplier %s (%s)",
            actual,
            supplier["id"],
            supplier.get("telegram_id"),
        )
        events.log(
            conn,
            "bot",
            "supplier_peer_resolved",
            entity="suppliers",
            entity_id=supplier["id"],
            details={
                "telegram_id": supplier.get("telegram_id"),
                "peer_id": actual,
                "source": "inbound",
            },
        )
    return supplier


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
    """Store a TCP message and enqueue its triage (transcribing audio first)."""
    # Supplier replies live in the same conversation bound to that TCP's channel,
    # so the audit trail stays in one place per contact.
    customer = conversations.get_or_create_customer(
        conn, channel, identifier, supplier["name"], kind="supplier"
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
    job_payload = {
        "conversation_id": conversation["id"],
        "message_id": inbound["id"],
        "supplier_id": supplier["id"],
    }
    if type == "audio":
        # `tcp_process` reads the message body, so audio must be transcribed
        # first; `transcribe_audio` then queues the triage.
        job_type = "transcribe_audio"
        job_payload["next_job"] = "tcp_process"
    else:
        job_type = "tcp_process"
    job_id = queue.enqueue(
        job_type,
        job_payload,
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
