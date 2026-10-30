"""Job handlers registered on the persistent queue."""

import asyncio
import logging
import random
from datetime import datetime

from cuadra import channels, config, db
from cuadra.ai import pipeline, prompts, queue, stt
from cuadra.domain import broadcasts as broadcasts_domain, catalog, conversations, escalations, events

log = logging.getLogger("cuadra.handlers")


def register() -> None:
    queue.register_handler("generate_reply", generate_reply)
    queue.register_handler("transcribe_audio", transcribe_audio)
    queue.register_handler("send_message", send_message)
    queue.register_handler("send_escalation", send_escalation)
    queue.register_handler("tcp_reply", tcp_reply)
    queue.register_handler("send_broadcast", send_broadcast)


def _respect_userbot_gap(sender) -> None:
    """Park the job while the userbot is inside its global human-like gap.

    Kept at the handler (not inside send_text/send_image) so a multi-part
    broadcast checks once and a retry never re-sends the image by accident.
    """
    from cuadra.channels import rate_limit, telegram_user

    if sender is not telegram_user:
        return
    wait = rate_limit.remaining("userbot", config.USERBOT_MIN_SEND_GAP_S)
    if wait > 0:
        raise queue.RateLimited(
            wait, f"userbot send gap: wait {wait:.0f}s", account=False
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
            events.log(
                conn,
                "bot",
                "escalation_created",
                entity="escalations",
                entity_id=escalation_id,
                details={"product_id": result["product_id"], "supplier_id": result["supplier_id"]},
            )
        await pipeline.maybe_summarize(conn, conversation)
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
            details={
                "channel": conversation["channel"],
                "identity": identity,
                "delay_s": round(delay, 2),
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


async def tcp_reply(payload: dict) -> None:
    """A TCP answered: correlate with its oldest pending escalation, enrich the
    product, resolve the escalation and resume the client (Hito 5)."""
    conversation_id = payload["conversation_id"]
    message_id = payload["message_id"]
    supplier_id = payload["supplier_id"]

    conn = db.connect()
    try:
        message = conversations.get_message(conn, message_id)
        answer = (message["body"] or "").strip()
        escalation = escalations.oldest_pending_for_supplier(conn, supplier_id)
        if escalation is None:
            events.log(
                conn,
                "bot",
                "tcp_reply_unmatched",
                entity="suppliers",
                entity_id=supplier_id,
                details={"message_id": message_id},
            )
            log.info("TCP %s replied with no pending escalation", supplier_id)
            return
        escalations.set_answer(conn, escalation["id"], answer)
    finally:
        conn.close()

    # Enrich the product with the answered detail, then tell the client.
    product_name = "el producto"
    if escalation["product_id"]:
        conn = db.connect()
        try:
            product = catalog.get_product(conn, escalation["product_id"])
            product_name = product["name"]
            catalog.enrich_specifications(
                conn, escalation["product_id"], "dato_proveedor", answer
            )
        except catalog.NotFound:
            product = None
        finally:
            conn.close()

    client_conversation_id = escalation["conversation_id"]
    reply = prompts.TEMPLATES["escalation_resolved"].format(
        producto=product_name, respuesta=answer
    )
    conn = db.connect()
    try:
        outbound = conversations.add_message(
            conn, client_conversation_id, "OUTBOUND", "bot", body=reply
        )
        escalations.resolve(conn, escalation["id"])
        events.log(
            conn,
            "bot",
            "escalation_resolved",
            entity="escalations",
            entity_id=escalation["id"],
            details={"supplier_id": supplier_id, "answer": answer},
        )
    finally:
        conn.close()
    queue.enqueue(
        "send_message",
        {"conversation_id": client_conversation_id, "message_id": outbound["id"]},
        priority=config.PRIORITY_CUSTOMER,
    )


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

    if broadcast["status"] == "CANCELLED":
        conn = db.connect()
        try:
            broadcasts_domain.record_result(conn, post_id, "FAILED", "broadcast cancelled")
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
