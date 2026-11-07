"""Job handlers registered on the persistent queue."""

import asyncio
import json
import logging
import random
from datetime import datetime, timedelta, timezone

from cuadra import channels, config, db
from cuadra.ai import (
    assembler,
    memory,
    pipeline,
    prompts,
    queue,
    retrieval,
    stt,
    tcp_extractor,
    tcp_triage,
)
from cuadra.domain import (
    broadcasts as broadcasts_domain,
    catalog,
    catalog_mutations,
    conversations,
    escalations,
    events,
    sales as sales_domain,
    turns,
)

log = logging.getLogger("cuadra.handlers")


def register() -> None:
    queue.register_handler("process_turn", process_turn)
    queue.register_handler("generate_reply", generate_reply)
    queue.register_handler("transcribe_audio", transcribe_audio)
    queue.register_handler("send_message", send_message)
    queue.register_handler("send_escalation", send_escalation)
    queue.register_handler("tcp_process", tcp_process)
    # Jobs queued by older versions/restarts keep working after the refactor.
    queue.register_handler("tcp_reply", tcp_process)
    queue.register_handler("dispatch_sale", dispatch_sale)
    queue.register_handler("send_broadcast", send_broadcast)
    queue.register_handler("reindex_product", reindex_product)
    queue.register_handler("reindex_all_products", reindex_all_products)


def _respect_userbot_gap(sender) -> None:
    """Park the job while the userbot is inside its global human-like gap.

    This is Cuadra's own pacing, not a Telegram punishment: it is marked
    ``penalize=False`` so the queue parks the job without consuming attempts
    or rate-limit hits. Kept at the handler (not inside send_text/send_image)
    so a multi-part broadcast checks once and a retry never re-sends the image
    by accident.
    """
    from cuadra.channels import rate_limit, telegram_user

    if sender is not telegram_user:
        return
    wait = rate_limit.local_gap_remaining(
        "userbot", config.USERBOT_MIN_SEND_GAP_S
    )
    if wait > 0:
        # Absolute retry time with a small margin, so the next wake-up lands
        # after the gap instead of exactly on its edge.
        retry_at = datetime.now(timezone.utc) + timedelta(seconds=wait + 0.25)
        raise queue.RateLimited(
            wait,
            f"userbot send gap: wait {wait:.0f}s",
            account=False,
            penalize=False,
            retry_at=retry_at,
        )


def _pending_audio(conn, turn: dict) -> list[dict]:
    """Audio messages in this turn that still have no transcription."""
    ids = turns.message_ids(turn)
    if not ids:
        return []
    placeholders = ", ".join("?" for _ in ids)
    rows = conn.execute(
        f"""SELECT * FROM messages
            WHERE id IN ({placeholders}) AND type = 'audio'
              AND (body IS NULL OR TRIM(body) = '')
            ORDER BY id""",
        tuple(ids),
    ).fetchall()
    return [dict(row) for row in rows]


def _transcription_pending(conn, message_id: int) -> bool:
    row = conn.execute(
        """SELECT 1 FROM jobs
           WHERE type = 'transcribe_audio'
             AND status IN ('PENDING', 'IN_PROGRESS')
             AND json_extract(payload_json, '$.message_id') = ?
           LIMIT 1""",
        (message_id,),
    ).fetchone()
    return row is not None


def _queued_reply(conn, turn_id: int) -> int | None:
    """The outbound message already generated for this turn, if any.

    The `reply_queued` event is written right after the outbound message is
    stored, before the send job exists. It closes the crash window between
    "reply stored" and "reply queued" without ever generating a second reply.
    """
    row = conn.execute(
        """SELECT details_json FROM events
           WHERE type = 'reply_queued' AND entity = 'turns' AND entity_id = ?
           ORDER BY id DESC LIMIT 1""",
        (turn_id,),
    ).fetchone()
    if row is None:
        return None
    try:
        message_id = (json.loads(row["details_json"] or "{}")).get("message_id")
    except (TypeError, ValueError):
        return None
    return int(message_id) if message_id else None


def _ensure_send_job(conn, conversation_id: int, message_id: int) -> bool:
    """Recover a stored reply whose send job was lost in a crash."""
    sent = conn.execute(
        """SELECT 1 FROM events
           WHERE type = 'message_sent' AND entity = 'messages' AND entity_id = ?""",
        (message_id,),
    ).fetchone()
    if sent is not None:
        return False
    pending = conn.execute(
        """SELECT 1 FROM jobs
           WHERE type = 'send_message' AND status != 'FAILED'
             AND json_extract(payload_json, '$.message_id') = ?
           LIMIT 1""",
        (message_id,),
    ).fetchone()
    if pending is not None:
        return False
    queue.enqueue(
        "send_message",
        {"conversation_id": conversation_id, "message_id": message_id},
        priority=config.PRIORITY_CUSTOMER,
    )
    return True


async def process_turn(payload: dict) -> None:
    """Process one persisted turn: exactly one reply per customer burst.

    Idempotent by construction: the turn is claimed atomically and a job that
    wakes after the turn is DONE is a no-op. If the debounce deadline moved,
    the job is re-armed instead of answering halfway through the burst.
    """
    turn_id = payload["turn_id"]
    conn = db.connect()
    escalation_id = None
    try:
        turn = turns.get(conn, turn_id)
        if turn["status"] == "CANCELLED":
            log.debug("turn %s is CANCELLED, skipping", turn_id)
            return

        existing_reply = _queued_reply(conn, turn_id)
        if existing_reply is not None:
            # A crash after storing the reply must not generate a second one;
            # it only needs the missing `send_message` job (or none at all).
            _ensure_send_job(conn, turn["conversation_id"], existing_reply)
            if turn["status"] != "DONE":
                turns.mark_done(conn, turn_id)
            log.debug("turn %s already replied, skipping", turn_id)
            return
        if turn["status"] == "DONE":
            log.debug("turn %s is DONE, skipping", turn_id)
            return

        if turns.parse_ts(turn["due_at"]) > turns.now():
            # Woke too early (a new message re-armed the turn). An
            # IN_PROGRESS job cannot reschedule itself, so force a follow-up.
            assembler.schedule_turn(
                conn, turn_id, available_at=turn["due_at"], force=True
            )
            return

        pending_audio = _pending_audio(conn, turn)
        if pending_audio:
            for message in pending_audio:
                if _transcription_pending(conn, message["id"]):
                    continue
                queue.enqueue(
                    "transcribe_audio",
                    {
                        "conversation_id": turn["conversation_id"],
                        "message_id": message["id"],
                        "turn_id": turn_id,
                    },
                    priority=config.PRIORITY_CUSTOMER,
                )
            return

        if not turns.claim(conn, turn_id):
            log.debug("turn %s already claimed, skipping", turn_id)
            return

        # A message can join the turn between the deadline check above and the
        # claim. Every inbound re-arms `due_at` into the future, so re-reading
        # it now catches that race; anything later lands in a new turn.
        claimed = turns.get(conn, turn_id)
        if turns.parse_ts(claimed["due_at"]) > turns.now():
            turns.release(conn, turn_id)
            assembler.schedule_turn(
                conn, turn_id, available_at=claimed["due_at"], force=True
            )
            return
        turn = claimed

        conversation = conversations.get_conversation(conn, turn["conversation_id"])
        result = await pipeline.handle_turn(conn, conversation, turn)
        outbound = conversations.add_message(
            conn,
            turn["conversation_id"],
            "OUTBOUND",
            "bot",
            body=result["reply"],
            latency_ms=result["latency_ms"],
            # One outbound message per turn, even after a crash/retry.
            external_id=f"turn:{turn_id}",
        )
        events.log(
            conn,
            "bot",
            "reply_generated",
            entity="conversations",
            entity_id=turn["conversation_id"],
            details={
                "turn_id": turn_id,
                "message_id": result["message_id"],
                "intent": result["intent"]["intencion"],
                "confidence": result["intent"]["confianza"],
                "product_id": result["product_id"],
                "escalate": result["escalate"],
                "generator": result["generator"],
                "degraded": result["degraded"],
                "latency_ms": result["latency_ms"],
                "context_tokens": result["context_tokens"],
                "stage": result.get("stage"),
                "sale_id": result.get("sale_id"),
                "sale_status": result.get("sale_status"),
            },
        )
        events.log(
            conn,
            "bot",
            "reply_queued",
            entity="turns",
            entity_id=turn_id,
            details={
                "message_id": outbound["id"],
                "conversation_id": turn["conversation_id"],
            },
        )
        if result["escalate"] and result["product_id"] and result["supplier_id"]:
            escalation = escalations.create(
                conn,
                conversation_id=turn["conversation_id"],
                product_id=result["product_id"],
                supplier_id=result["supplier_id"],
                question=result["escalation_question"] or result["reply"],
            )
            escalation_id = escalation["id"]
            conversations.set_status(conn, turn["conversation_id"], "WAITING_TCP")
            events.log(
                conn,
                "bot",
                "escalation_created",
                entity="escalations",
                entity_id=escalation_id,
                details={
                    "product_id": result["product_id"],
                    "supplier_id": result["supplier_id"],
                },
            )
        turns.mark_done(conn, turn_id)
        try:
            # After mark_done: the current turn is no longer PROCESSING, so the
            # summary can safely advance over older messages.
            await memory.maybe_summarize(conn, conversation)
        except Exception:  # noqa: BLE001 - summarising must never block delivery
            log.exception("summary failed for conversation %s", turn["conversation_id"])
    except Exception:
        # Never leave the turn stuck in PROCESSING: a queue retry must be able
        # to claim it again. A crash is recovered by turns.recover_open().
        try:
            turns.release(conn, turn_id)
        except Exception:  # noqa: BLE001 - bookkeeping must not mask the error
            log.exception("could not release turn %s", turn_id)
        raise
    finally:
        conn.close()

    if escalation_id is not None:
        queue.enqueue(
            "send_escalation",
            {"escalation_id": escalation_id},
            priority=config.PRIORITY_TCP,
        )
    queue.enqueue(
        "send_message",
        {"conversation_id": turn["conversation_id"], "message_id": outbound["id"]},
        priority=config.PRIORITY_CUSTOMER,
    )
    if result.get("sale_status") == "PENDING_DISPATCH" and result.get("sale_id"):
        queue.enqueue(
            "dispatch_sale",
            {"sale_id": result["sale_id"]},
            priority=config.PRIORITY_TCP,
        )


async def generate_reply(payload: dict) -> None:
    """Inbound message (text or transcribed audio) -> reply stored + queued to send."""
    conversation_id = payload["conversation_id"]
    message_id = payload["message_id"]
    conn = db.connect()
    escalation_id = None
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
                "stage": result.get("stage"),
                "sale_id": result.get("sale_id"),
                "sale_status": result.get("sale_status"),
            },
        )
        if result["escalate"] and result["product_id"] and result["supplier_id"]:
            escalation = escalations.create(
                conn,
                conversation_id=conversation_id,
                product_id=result["product_id"],
                supplier_id=result["supplier_id"],
                question=result["escalation_question"] or result["reply"],
            )
            escalation_id = escalation["id"]
            conversations.set_status(conn, conversation_id, "WAITING_TCP")
            events.log(
                conn,
                "bot",
                "escalation_created",
                entity="escalations",
                entity_id=escalation_id,
                details={"product_id": result["product_id"], "supplier_id": result["supplier_id"]},
            )
        try:
            await memory.maybe_summarize(conn, conversation)
        except Exception:  # noqa: BLE001 - summarising must never block delivery
            log.exception("summary failed for conversation %s", conversation_id)
    finally:
        conn.close()
    if escalation_id is not None:
        queue.enqueue(
            "send_escalation",
            {"escalation_id": escalation_id},
            priority=config.PRIORITY_TCP,
        )
    queue.enqueue(
        "send_message",
        {"conversation_id": conversation_id, "message_id": outbound["id"]},
        priority=config.PRIORITY_CUSTOMER,
    )
    if result.get("sale_status") == "PENDING_DISPATCH" and result.get("sale_id"):
        queue.enqueue(
            "dispatch_sale",
            {"sale_id": result["sale_id"]},
            priority=config.PRIORITY_TCP,
        )


async def transcribe_audio(payload: dict) -> None:
    """Audio inbound -> transcription stored on the message -> reply job."""
    conversation_id = payload["conversation_id"]
    message_id = payload["message_id"]
    conn = db.connect()
    try:
        message = conversations.get_message(conn, message_id)
        if (message["body"] or "").strip():
            # A retry after the transcription was stored must not run STT again.
            log.debug("message %s already transcribed, skipping", message_id)
        else:
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

    next_job = payload.get("next_job")
    if next_job == "tcp_process":
        # TCP audio used to be queued as tcp_reply, which reads `body` before
        # transcription existed. Requeue the real triage job instead.
        supplier_id = payload.get("supplier_id")
        if supplier_id is None:
            raise queue.PermanentJobError("tcp_process job has no supplier_id")
        queue.enqueue(
            "tcp_process",
            {
                "conversation_id": conversation_id,
                "message_id": message_id,
                "supplier_id": supplier_id,
            },
            priority=config.PRIORITY_TCP,
        )
        return

    turn_id = payload.get("turn_id")
    if turn_id is not None:
        # Re-arm the same turn; process_turn answers once all audio is ready.
        conn = db.connect()
        try:
            try:
                turn = turns.get(conn, turn_id)
            except LookupError:
                turn = None
            if turn is not None and turn["status"] not in ("DONE", "CANCELLED"):
                assembler.schedule_turn(conn, turn_id, available_at=turns.iso())
        finally:
            conn.close()
        return

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
    identity = conversation.get("identity") or "bot"
    adapter = channels.get_adapter(conversation["channel"], identity)
    if adapter is None:
        log.warning(
            "no adapter for channel %r identity %r", conversation["channel"], identity
        )
        return

    delay = _human_delay()
    _respect_userbot_gap(adapter)
    await adapter.typing(target)
    await asyncio.sleep(delay)

    if message["type"] == "image" and message["file_path"]:
        await adapter.send_image(
            target, str(config.DATA_DIR / message["file_path"]), caption=message["body"]
        )
        provider_message_id = None
    elif message["type"] == "audio" and message["file_path"]:
        await adapter.send_audio(target, str(config.DATA_DIR / message["file_path"]))
        provider_message_id = None
    else:
        provider_message_id = await adapter.send_text(target, message["body"] or "")

    conn = db.connect()
    try:
        events.log(
            conn,
            "bot",
            "message_sent",
            entity="messages",
            entity_id=message_id,
            details={
                "channel": conversation["channel"],
                "identity": identity,
                "delay_s": round(delay, 2),
                "provider_message_id": provider_message_id,
            },
        )
    finally:
        conn.close()


async def send_escalation(payload: dict) -> None:
    """Write the TCP the client's question, with a human delay (Hito 5).

    Idempotent via `escalation_sent` event: a requeued job never double-asks.
    """
    escalation_id = payload["escalation_id"]
    conn = db.connect()
    try:
        escalation = escalations.get(conn, escalation_id)
        if escalation["status"] != "PENDING":
            log.debug("escalation %s not pending, skipping", escalation_id)
            return
        already = conn.execute(
            """SELECT 1 FROM events
               WHERE type = 'escalation_sent' AND entity = 'escalations' AND entity_id = ?""",
            (escalation_id,),
        ).fetchone()
        if already is not None:
            log.debug("escalation %s already sent, skipping", escalation_id)
            return
        supplier = catalog.get_supplier(conn, escalation["supplier_id"])
        conversation = conversations.get_conversation(conn, escalation["conversation_id"])
        identity = conversation.get("identity") or "bot"
        product = None
        if escalation["product_id"]:
            try:
                product = catalog.get_product(conn, escalation["product_id"])
            except catalog.NotFound:
                product = None
        target = supplier.get("telegram_id")
    finally:
        conn.close()

    if not target:
        log.warning("supplier %s has no telegram_id; cannot escalate", escalation["supplier_id"])
        return
    adapter = channels.get_adapter("telegram", identity)
    if adapter is None:
        return

    text = prompts.TEMPLATES["escalation_question"].format(
        escalation_id=escalation_id,
        producto=(product["name"] if product else "un producto del catálogo"),
        pregunta=escalation["question"],
    )
    _respect_userbot_gap(adapter)
    await adapter.typing(target)
    await asyncio.sleep(_human_delay())
    await adapter.send_text(target, text)

    conn = db.connect()
    try:
        escalations.set_tcp_message_sent(conn, escalation_id, text)
        events.log(
            conn,
            "bot",
            "escalation_sent",
            entity="escalations",
            entity_id=escalation_id,
            details={
                "supplier_id": escalation["supplier_id"],
                "target": target,
                "identity": identity,
            },
        )
    finally:
        conn.close()


def _sale_product_name(sale: dict) -> str:
    try:
        context = json.loads(sale.get("context_json") or "{}")
    except (TypeError, ValueError):
        context = {}
    return context.get("product_name") or f"producto {sale['product_id']}"


async def dispatch_sale(payload: dict) -> None:
    """Send a PENDING_DISPATCH sale to its TCP (idempotent by sale status)."""
    sale_id = int(payload["sale_id"])
    conn = db.connect()
    try:
        sale = sales_domain.get_sale(conn, sale_id)
        if sale["status"] != "PENDING_DISPATCH":
            log.debug("sale %s is %s, not dispatching", sale_id, sale["status"])
            return
        supplier = catalog.get_supplier(conn, sale["supplier_id"])
        order_code = sale["order_code"]
        product_name = _sale_product_name(sale)
    finally:
        conn.close()

    target = supplier.get("telegram_id")
    if not target:
        conn = db.connect()
        try:
            sales_domain.mark_needs_operator(
                conn, sale_id, "supplier has no telegram_id for dispatch"
            )
        finally:
            conn.close()
        return

    adapter = channels.get_adapter("telegram", "bot")
    if adapter is None:
        conn = db.connect()
        try:
            sales_domain.mark_needs_operator(
                conn, sale_id, "telegram bot adapter unavailable"
            )
        finally:
            conn.close()
        return

    text = prompts.TEMPLATES["tcp_order"].format(
        order_code=order_code,
        producto=product_name,
        product_id=sale["product_id"],
        cantidad=sale["quantity"],
        total=f"{float(sale['amount']):g}",
        moneda=sale["currency"],
        cliente=sale["recipient_name"],
        zona=sale["delivery_zone"],
        direccion=sale["delivery_address"],
        telefono=sale["customer_phone"],
    )
    try:
        _respect_userbot_gap(adapter)
        await adapter.typing(target)
        await asyncio.sleep(_human_delay())
        tcp_message_id = await adapter.send_text(target, text)
    except Exception as exc:  # noqa: BLE001 - operator takes over on any send error
        log.exception("sale %s dispatch failed", sale_id)
        conn = db.connect()
        try:
            sales_domain.mark_needs_operator(
                conn, sale_id, f"dispatch failed: {str(exc)[:300]}"
            )
        finally:
            conn.close()
        return

    conn = db.connect()
    try:
        sales_domain.mark_dispatched(conn, sale_id, tcp_message_id)
    finally:
        conn.close()


def recover_pending_dispatches() -> int:
    """Re-arm dispatch jobs for sales left PENDING_DISPATCH by a restart."""
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT id FROM sales WHERE status = 'PENDING_DISPATCH' ORDER BY id"
        ).fetchall()
        sale_ids = []
        for row in rows:
            pending = conn.execute(
                """SELECT 1 FROM jobs
                   WHERE type = 'dispatch_sale'
                     AND status IN ('PENDING', 'IN_PROGRESS')
                     AND json_extract(payload_json, '$.sale_id') = ?
                   LIMIT 1""",
                (int(row["id"]),),
            ).fetchone()
            if pending is None:
                sale_ids.append(int(row["id"]))
    finally:
        conn.close()
    for sale_id in sale_ids:
        queue.enqueue(
            "dispatch_sale", {"sale_id": sale_id}, priority=config.PRIORITY_TCP
        )
    return len(sale_ids)


_SALE_ACK_TEMPLATE = {
    "CONFIRMED": "sale_confirmed",
    "DELIVERED": "sale_delivered",
    "NEEDS_OPERATOR": "sale_unavailable",
}


def _notify_sale_ack(conversation_id: int, order_code: str, action: str) -> None:
    key = _SALE_ACK_TEMPLATE.get(action)
    if key is None:
        return
    text = prompts.TEMPLATES[key].format(order_code=order_code)
    conn = db.connect()
    try:
        outbound = conversations.add_message(
            conn, conversation_id, "OUTBOUND", "bot", body=text
        )
    finally:
        conn.close()
    queue.enqueue(
        "send_message",
        {"conversation_id": conversation_id, "message_id": outbound["id"]},
        priority=config.PRIORITY_CUSTOMER,
    )


def _tcp_already_processed(conn, message_id: int) -> bool:
    row = conn.execute(
        """SELECT 1 FROM events
           WHERE type = 'tcp_processed' AND entity = 'messages' AND entity_id = ?
           LIMIT 1""",
        (message_id,),
    ).fetchone()
    return row is not None


def _mark_tcp_processed(
    conn,
    *,
    message_id: int,
    supplier_id: int,
    kind: str,
    details: dict | None = None,
) -> None:
    events.log(
        conn,
        "bot",
        "tcp_processed",
        entity="messages",
        entity_id=message_id,
        details={"supplier_id": supplier_id, "kind": kind, **(details or {})},
    )


async def tcp_process(payload: dict) -> None:
    """Triage a supplier message: sale ack, escalation answer or catalog update.

    Absorbs the phase-3 `tcp_reply`: `[E-###]`/`[V-######]` are honoured,
    audio arrives already transcribed, and a retried job never applies twice.
    """
    conversation_id = payload["conversation_id"]
    message_id = payload["message_id"]
    supplier_id = payload["supplier_id"]

    conn = db.connect()
    sends: list[tuple[int, int]] = []
    notify_sale = None
    try:
        if _tcp_already_processed(conn, message_id):
            log.debug("TCP message %s already processed, skipping", message_id)
            return

        message = conversations.get_message(conn, message_id)
        answer = (message["body"] or "").strip()
        if not answer:
            events.log(
                conn,
                "bot",
                "tcp_empty_message",
                entity="suppliers",
                entity_id=supplier_id,
                details={"message_id": message_id},
            )
            _mark_tcp_processed(
                conn, message_id=message_id, supplier_id=supplier_id, kind="EMPTY"
            )
            return

        escalation_ids, order_codes = tcp_triage.parse_case_ids(answer)
        # An explicit escalation reference always wins over sale-ack keywords.
        if not escalation_ids:
            sale, action = sales_domain.apply_tcp_ack(conn, supplier_id, answer)
            if sale is not None and action is not None:
                events.log(
                    conn,
                    "bot",
                    "tcp_sale_ack",
                    entity="sales",
                    entity_id=sale["id"],
                    details={
                        "action": action,
                        "order_code": sale["order_code"],
                        "message_id": message_id,
                        "order_codes": order_codes,
                    },
                )
                notify_sale = (
                    sale["conversation_id"],
                    sale["order_code"],
                    action,
                )
                _mark_tcp_processed(
                    conn,
                    message_id=message_id,
                    supplier_id=supplier_id,
                    kind="SALE_ACK",
                    details={"action": action, "sale_id": sale["id"]},
                )
                return

        correlation = tcp_triage.correlate(conn, supplier_id=supplier_id, text=answer)
        if correlation.reason.startswith("case_not_"):
            events.log(
                conn,
                "bot",
                correlation.reason,
                entity="suppliers",
                entity_id=supplier_id,
                details={
                    "message_id": message_id,
                    "escalation_ids": escalation_ids,
                    "status": (correlation.escalation or {}).get("status"),
                },
            )
            _mark_tcp_processed(
                conn,
                message_id=message_id,
                supplier_id=supplier_id,
                kind=correlation.reason.upper(),
            )
            return
        if not correlation.found:
            events.log(
                conn,
                "bot",
                "tcp_reply_unmatched",
                entity="suppliers",
                entity_id=supplier_id,
                details={
                    "message_id": message_id,
                    "triage": tcp_triage.classify(answer),
                    "method": correlation.method,
                    "reason": correlation.reason,
                    "escalation_ids": escalation_ids,
                },
            )
            _mark_tcp_processed(
                conn,
                message_id=message_id,
                supplier_id=supplier_id,
                kind="UNMATCHED",
                details={"triage": tcp_triage.classify(answer)},
            )
            return

        escalation = correlation.escalation
        product_id = correlation.product_id
        if product_id is None:
            events.log(
                conn,
                "bot",
                "tcp_reply_unmatched",
                entity="suppliers",
                entity_id=supplier_id,
                details={"message_id": message_id, "reason": "correlation_without_product"},
            )
            _mark_tcp_processed(
                conn, message_id=message_id, supplier_id=supplier_id, kind="UNMATCHED"
            )
            return

        product = catalog.get_product(conn, product_id)
        if escalation is not None and escalation["status"] in escalations.OPEN_STATUSES:
            escalations.set_answer(conn, escalation["id"], answer)

        extraction = await tcp_extractor.extract(
            product,
            answer,
            conversation_id=conversation_id,
            job_id=payload.get("job_id"),
        )

        mutation = None
        if extraction is not None and extraction.has_catalog_update:
            mutation = catalog_mutations.apply_tcp_update(
                conn,
                product_id=product_id,
                supplier_id=supplier_id,
                escalation_id=int(escalation["id"]) if escalation else None,
                source_message_id=message_id,
                source_text=answer,
                extraction=extraction,
                correlation_method=correlation.method,
                ambiguous=correlation.ambiguous,
            )

        if mutation is not None:
            if mutation["status"] == "APPLIED":
                sends = tcp_triage.resume_escalations(
                    conn,
                    product_id=product_id,
                    supplier_id=supplier_id,
                    answer=answer,
                    summary=extraction.customer_answer_summary,
                )
            # PENDING_REVIEW: the escalation stays ANSWERED until the operator
            # approves or rejects it; the audit panel already flagged it.
        elif escalation is not None:
            # Model unavailable/invalid (or no catalog data): keep the legacy
            # answer flow so the client is never left waiting.
            clean_answer = tcp_triage.strip_case_ids(answer)
            catalog.enrich_specifications(
                conn, product_id, "dato_proveedor", clean_answer
            )
            retrieval.queue_reindex(product_id)
            sends = tcp_triage.resume_escalations(
                conn,
                product_id=product_id,
                supplier_id=supplier_id,
                answer=clean_answer,
                summary=(
                    extraction.customer_answer_summary
                    if extraction is not None
                    else clean_answer
                ),
            )

        _mark_tcp_processed(
            conn,
            message_id=message_id,
            supplier_id=supplier_id,
            kind=tcp_triage.classify(answer),
            details={
                "method": correlation.method,
                "ambiguous": correlation.ambiguous,
                "product_id": product_id,
                "mutation_id": mutation["id"] if mutation else None,
                "mutation_status": mutation["status"] if mutation else None,
            },
        )
    finally:
        conn.close()

    if notify_sale is not None:
        client_conversation_id, order_code, action = notify_sale
        if client_conversation_id:
            _notify_sale_ack(client_conversation_id, order_code, action)
        return

    for client_conversation_id, outbound_id in sends:
        queue.enqueue(
            "send_message",
            {
                "conversation_id": client_conversation_id,
                "message_id": outbound_id,
            },
            priority=config.PRIORITY_CUSTOMER,
        )


# Backwards-compatible name for callers that imported the phase-3 handler.
tcp_reply = tcp_process


def _index_one(product_id: int) -> bool:
    conn = db.connect()
    try:
        return retrieval.index_product(conn, product_id)
    finally:
        conn.close()


def _index_all() -> dict:
    conn = db.connect()
    try:
        return retrieval.reindex_all(conn)
    finally:
        conn.close()


async def reindex_product(payload: dict) -> None:
    """Recompute one product embedding after a catalog mutation.

    Runs in a worker thread: ONNX inference must not block the event loop (and
    therefore customer replies). Missing model/library is a no-op by design.
    """
    product_id = payload.get("product_id")
    if product_id is None:
        raise queue.PermanentJobError("reindex_product job has no product_id")
    changed = await asyncio.to_thread(_index_one, int(product_id))
    log.debug("reindex_product %s changed=%s", product_id, changed)


async def reindex_all_products(payload: dict) -> None:
    """Low-priority full reindex; safe to enqueue when the model is missing."""
    stats = await asyncio.to_thread(_index_all)
    if stats["available"]:
        log.info(
            "reindex_all_products: %s indexed, %s skipped",
            stats["indexed"],
            stats["skipped"],
        )
    else:
        log.info("reindex_all_products skipped: embeddings unavailable")


async def send_broadcast(payload: dict) -> None:
    """Publish one queued campaign post in one group."""
    post_id = payload.get("post_id")
    if post_id is None:
        raise queue.PermanentJobError("broadcast job has no post_id")
    broadcast_id = payload["broadcast_id"]
    group_id = payload["group_id"]

    conn = db.connect()
    try:
        post = conn.execute(
            "SELECT * FROM broadcast_posts WHERE id = ?", (post_id,)
        ).fetchone()
        if post is None:
            raise queue.PermanentJobError(f"broadcast post {post_id} not found")
        if post["status"] in ("SENT", "FAILED"):
            log.debug("broadcast post %s is already %s; skipping", post_id, post["status"])
            return
        broadcast = broadcasts_domain.get_broadcast(conn, broadcast_id)
        group = broadcasts_domain.get_group(conn, group_id)
        text = post["sent_text"] or ""
        image_path = post["image_path"]
    finally:
        conn.close()

    if not broadcast.get("enabled", 1):
        conn = db.connect()
        try:
            broadcasts_domain.record_result(conn, post_id, "FAILED", "broadcast disabled")
        finally:
            conn.close()
        return
    if group["status"] != "ACTIVE":
        conn = db.connect()
        try:
            broadcasts_domain.record_result(
                conn, post_id, "FAILED", f"group is {group['status']}"
            )
        finally:
            conn.close()
        return

    sender = channels.get_sender(group["sender"])
    if sender is None:
        raise queue.PermanentJobError(f"unknown sender '{group['sender']}'")

    _respect_userbot_gap(sender)

    abs_image = None
    if image_path:
        candidate = config.DATA_DIR / image_path
        if candidate.is_file():
            abs_image = str(candidate)
        else:
            log.warning(
                "broadcast %s: image %s missing; sending text only", broadcast_id, image_path
            )

    try:
        if abs_image and len(text) <= 1024:
            await sender.send_image(group["target"], abs_image, caption=text)
        elif abs_image:
            await sender.send_image(group["target"], abs_image, caption=None)
            await sender.send_text(group["target"], text)
        else:
            await sender.send_text(group["target"], text)
    except Exception as exc:  # noqa: BLE001 - classified below
        permanent = isinstance(exc, queue.PermanentJobError)
        rate_limited = isinstance(exc, queue.RateLimited)
        conn = db.connect()
        try:
            if permanent:
                broadcasts_domain.record_result(conn, post_id, "FAILED", str(exc)[:500])
            elif rate_limited:
                # Telegram asked us to wait: keep the post QUEUED and do not
                # burn its attempts. The queue will retry after the cooldown.
                conn.execute(
                    "UPDATE broadcast_posts SET error = ? WHERE id = ?",
                    (str(exc)[:500], post_id),
                )
                conn.commit()
            else:
                conn.execute(
                    """UPDATE broadcast_posts
                       SET attempts = attempts + 1, error = ? WHERE id = ?""",
                    (str(exc)[:500], post_id),
                )
                conn.commit()
        finally:
            conn.close()
        if permanent or rate_limited:
            raise
        if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
            raise
        raise queue.PermanentJobError(str(exc)) from exc

    conn = db.connect()
    try:
        broadcasts_domain.record_result(conn, post_id, "SENT")
    finally:
        conn.close()
