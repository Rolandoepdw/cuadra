"""Turn lifecycle: assemble, interpret, resolve, decide, persist."""

import asyncio
import logging
import time

from cuadra import config
from cuadra.ai import grounding, llm, memory, offers, prompts, tracing
from cuadra.ai.context import build_nlu_messages
from cuadra.ai.tools import base as tools_base
from cuadra.ai.tools import catalog as catalog_tools
from cuadra.ai.tools import sales as sales_tools
from cuadra.domain import catalog, conversations, events, sales as sales_domain, turns

log = logging.getLogger("cuadra.orchestrator")

from cuadra.ai.turn.constants import (
    LOW_CONFIDENCE,
    _DELIVERY_COVERAGE,
    _LOGISTICS_QUESTION,
    _SALE_STAGES,
    _TABLE_LIMIT,
)

from cuadra.ai.turn.composer import (
    _fast_template,
)

from cuadra.ai.turn.frame import (
    build_frame,
    commit,
)

from cuadra.ai.turn.guards import (
    _guard_context,
)

from cuadra.ai.turn.policy import (
    _banal_kind,
    _catalog_request,
    _category_request,
    _decide,
    _is_greeting,
    _stage_for,
    plan_turn,
)

from cuadra.ai.turn.references import (
    _resolve_product,
    product_id_from_text,
    resolve_reference,
)

from cuadra.ai.turn.sales_flow import (
    _banal_outcome,
    _logistics_outcome,
    _maybe_handle_sale,
    _offer_product,
    _offered_alternatives_outcome,
    _post_sale_outcome,
    _sale_guard,
    _zone_coverage_outcome,
)

# L3.0 registers three existing catalog reads as proof of the contract. Nothing
# calls them from the turn yet: `agent.tools.mode=off` keeps the rules in charge.
if not tools_base.registry.has("catalog.search"):
    catalog_tools.register(tools_base.registry)
# L3.2 adds the reads that already back the sale, zone and post-sale routes.
if not tools_base.registry.has("sales.status"):
    sales_tools.register(tools_base.registry)


def _turn_messages(conn, turn: dict) -> list[dict]:
    return conversations.get_messages(conn, turns.message_ids(turn))


def _turn_text(messages: list[dict]) -> str:
    parts = []
    for message in messages:
        if message["direction"] != "INBOUND":
            continue
        body = (message["body"] or "").strip()
        if body:
            parts.append(body)
        elif message["type"] == "image":
            parts.append("[imagen enviada]")
        elif message["type"] == "audio":
            parts.append("[audio]")
    return "\n".join(parts).strip()


def _evidence_availability(products: list[dict]) -> str | None:
    """`out_of_stock` only when every product read is unavailable."""
    if not products:
        return None
    if all(catalog.availability_of(product) != "in_stock" for product in products):
        return "out_of_stock"
    return None


def _catalog_evidence(
    conn,
    *,
    catalog_read: dict | None,
    listing_before,
    listing_after,
    product: dict | None,
) -> list[dict]:
    """Reads that back this turn's reply (doc 14 §8.2).

    Grounding only needs to know which reads happened and whether they were
    empty; the reply is never rewritten from here.
    """
    evidence: list[dict] = []
    if catalog_read is not None:
        products = catalog_read.get("products") or []
        evidence.append(
            {
                "tool": "catalog.search",
                "query": catalog_read.get("query") or "",
                "product_ids": [int(item["id"]) for item in products],
                "empty_reason": catalog_read.get("empty_reason"),
                "availability": _evidence_availability(products),
            }
        )
    if listing_after and listing_after != listing_before:
        ids = [int(value) for value in listing_after.get("product_ids") or []]
        products = []
        for product_id in ids:
            try:
                products.append(catalog.get_product(conn, product_id))
            except catalog.NotFound:
                continue
        evidence.append(
            {
                "tool": "catalog.list_category",
                "query": listing_after.get("query") or "",
                "product_ids": [int(item["id"]) for item in products],
                "empty_reason": None if products else "no_match",
                "availability": _evidence_availability(products),
            }
        )
    if product is not None:
        evidence.append(
            {
                "tool": "catalog.get",
                "product_id": int(product["id"]),
                "product_ids": [int(product["id"])],
                "empty_reason": None,
                "availability": catalog.availability_of(product),
                "stock": catalog.available_units(product),
            }
        )
    return evidence


def _sale_evidence(conn, context: dict, sale: dict | None) -> dict | None:
    """The sale the turn is talking about, or a real `no_sale` read."""
    current = sale
    if current is None and context.get("sale_id"):
        try:
            current = sales_domain.get_sale(conn, int(context["sale_id"]))
        except (sales_domain.NotFound, TypeError, ValueError):
            current = None
    if current is None and context.get("order_code"):
        current = sales_domain.get_sale_by_code(conn, context["order_code"])
    if current is not None:
        return {
            "tool": "sales.status",
            "sale_id": int(current["id"]),
            "order_code": current.get("order_code"),
            "status": current.get("status"),
            "empty_reason": None,
        }
    if context.get("sale_id") or context.get("order_code"):
        return {
            "tool": "sales.status",
            "sale_id": None,
            "status": None,
            "empty_reason": "no_sale",
        }
    return None


def _sales_evidence(
    conn,
    *,
    context: dict,
    text: str,
    product: dict | None,
    stage: str,
    sale: dict | None,
) -> list[dict]:
    """Reads that back a status, coverage or delivery-date claim (doc 14 §8.2).

    This mirrors what the deterministic route used; it never rewrites the reply.
    """
    evidence: list[dict] = []
    sale_item = _sale_evidence(conn, context, sale)
    if sale_item is not None:
        evidence.append(sale_item)
    elif str(stage or "").upper() == "DONE":
        evidence.append(
            {
                "tool": "sales.status",
                "sale_id": None,
                "status": None,
                "empty_reason": "no_sale",
            }
        )

    if product is not None:
        zones = catalog.resolve_zones(conn, text or "", active_only=False)
        if zones:
            for zone in zones:
                covered = bool(zone.get("active")) and catalog.product_covers_zone(
                    conn, product, int(zone["id"])
                )
                evidence.append(
                    {
                        "tool": "zones.check",
                        "product_id": int(product["id"]),
                        "zone_id": int(zone["id"]),
                        "zone_name": zone.get("name"),
                        "covered": covered,
                        "empty_reason": None,
                    }
                )
        elif _DELIVERY_COVERAGE.search(text or ""):
            names = catalog.effective_delivery_zone_names(conn, product)
            evidence.append(
                {
                    "tool": "zones.list",
                    "product_id": int(product["id"]),
                    "zones": names,
                    "empty_reason": None if names else "no_zones",
                }
            )

    if _LOGISTICS_QUESTION.search(text or ""):
        evidence.append(
            {
                "tool": "logistics.eta",
                "product_id": int(product["id"]) if product else None,
                "eta": None,
                "source": None,
                "empty_reason": "no_tcp_eta",
            }
        )
    return evidence


async def _classify(conn, conversation, messages: list[dict], text: str, message_id):
    try:
        return (
            await llm.classify(
                messages, text, conversation_id=conversation["id"]
            ),
            False,
        )
    except llm.LLMInvalidOutput as exc:
        log.warning("NLU degraded to intent=otro: %s", exc)
        events.log(
            conn,
            "llm",
            "nlu_invalid_output",
            entity="conversations",
            entity_id=conversation["id"],
            details={"message_id": message_id},
        )
        return llm.Intent(intencion="otro", confianza=0.0), True


async def _classify_light(
    conn, conversation, messages: list[dict], text: str, message_id
):
    """Lightweight NLU classification using a minimal prompt."""
    try:
        return (
            await llm.classify_light(
                messages, text, conversation_id=conversation["id"]
            ),
            False,
        )
    except llm.LLMInvalidOutput as exc:
        log.warning("NLU light degraded to intent=otro: %s", exc)
        events.log(
            conn,
            "llm",
            "nlu_invalid_output",
            entity="conversations",
            entity_id=conversation["id"],
            details={"message_id": message_id},
        )
        return llm.Intent(intencion="otro", confianza=0.0), True


async def handle_turn(conn, conversation, turn: dict) -> dict:
    """Run the full pipeline once for a consolidated customer turn (F8).

    One trace per turn, with `duration_ms`: an extra entry trace with
    `output: null` used to double the dashboard counter and leave the average
    latency empty. A failure is traced too, so a crashed turn is never silent.
    """
    started = time.perf_counter()
    turn_messages = _turn_messages(conn, turn)
    trace_input = {
        "turn_id": turn.get("id"),
        "conversation_id": conversation["id"],
        "text": _turn_text(turn_messages) or "[mensaje sin texto]",
        "message_count": len(turn_messages),
    }
    try:
        result = await _run_with_watchdog(conn, conversation, turn)
    except Exception as exc:  # noqa: BLE001 - the trace must survive the failure
        tracing.trace_call(
            "pipeline.handle_turn",
            trace_input,
            None,
            ok=False,
            error=str(exc),
            error_type=type(exc).__name__,
            duration_ms=(time.perf_counter() - started) * 1000,
            conversation_id=conversation["id"],
            turn_id=turn.get("id"),
        )
        try:
            turns.record_outcome(
                conn,
                turn.get("id"),
                outcome="error",
                error=str(exc)[:500],
            )
        except Exception:  # noqa: BLE001 - never mask the original failure
            log.exception("could not record outcome for turn %s", turn.get("id"))
        raise

    intent = result.get("intent") or {}
    tracing.trace_call(
        "pipeline.handle_turn",
        trace_input,
        {
            "reply": result["reply"][:500] if result.get("reply") else None,
            "intent": intent.get("intencion"),
            "confianza": intent.get("confianza"),
            "generator": result.get("generator"),
            "stage": result.get("stage"),
            "sale_id": result.get("sale_id"),
            "sale_status": result.get("sale_status"),
            "escalate": result.get("escalate"),
            "intent_ms": result.get("intent_ms"),
            "compose_ms": result.get("compose_ms"),
        },
        ok=True,
        duration_ms=(time.perf_counter() - started) * 1000,
        conversation_id=conversation["id"],
        turn_id=turn.get("id"),
    )
    try:
        turns.record_outcome(
            conn,
            turn.get("id"),
            intent=intent.get("intencion"),
            product_id=result.get("product_id"),
            stage=result.get("stage"),
            generator=result.get("generator"),
            escalated=1 if result.get("escalate") else 0,
            sale_id=result.get("sale_id"),
            latency_ms=result.get("latency_ms"),
            intent_ms=result.get("intent_ms"),
            compose_ms=result.get("compose_ms"),
            context_tokens=result.get("context_tokens"),
            outcome=result.get("outcome") or "ok",
        )
    except Exception:  # noqa: BLE001 - the reply must not fail for telemetry
        log.exception("could not record outcome for turn %s", turn.get("id"))
    return result


async def _run_with_watchdog(conn, conversation, turn: dict) -> dict:
    """Run the turn with a hang detector, not a latency budget.

    `AGENT_TURN_WATCHDOG_S=0` disables it. A timeout means a bug: the turn is
    finished with a safe reply and is not retried, so the customer never gets
    two answers.
    """
    timeout = float(getattr(config, "AGENT_TURN_WATCHDOG_S", 0) or 0)
    if timeout <= 0:
        return await run_turn(conn, conversation, turn)
    try:
        return await asyncio.wait_for(
            run_turn(conn, conversation, turn), timeout=timeout
        )
    except asyncio.TimeoutError:
        log.error(
            "turn watchdog fired after %.0fs (conversation %s, turn %s)",
            timeout,
            conversation["id"],
            turn.get("id"),
        )
        return _timeout_result(conn, conversation, turn, timeout)


def _timeout_result(conn, conversation, turn: dict, seconds: float) -> dict:
    """One safe answer for a hung turn; the context is not rewritten."""
    try:
        context = conversations.get_context(conn, conversation["id"])
    except Exception:  # noqa: BLE001
        context = {}
    try:
        events.log(
            conn,
            "system",
            "turn_timeout",
            entity="conversations",
            entity_id=conversation["id"],
            details={"seconds": seconds, "turn_id": turn.get("id")},
        )
    except Exception:  # noqa: BLE001 - telemetry must not block the reply
        log.exception("could not log the turn timeout")
    return {
        "turn_id": turn.get("id"),
        "message_id": turn.get("last_message_id"),
        "reply": prompts.TEMPLATES["turn_timeout"],
        "intent": {},
        "degraded": True,
        "product_id": None,
        "product_name": None,
        "supplier_id": None,
        "escalate": False,
        "escalation_question": None,
        "generator": "watchdog",
        "stage": str(context.get("stage") or "DISCOVERY"),
        "sale_id": None,
        "sale_status": None,
        "sale_order_code": None,
        "latency_ms": int(seconds * 1000),
        "intent_ms": 0,
        "compose_ms": 0,
        "context_tokens": 0,
        "outcome": "timeout",
    }


async def run_turn(conn, conversation, turn: dict) -> dict:
    """Build the reply for one consolidated turn (traced by `handle_turn`)."""
    started = time.perf_counter()
    assemble_started = time.perf_counter()
    intent_ms = 0
    compose_ms = 0
    turn_messages = _turn_messages(conn, turn)
    turn_ids = {message["id"] for message in turn_messages}
    last_message_id = (
        turn_messages[-1]["id"] if turn_messages else turn.get("last_message_id")
    )
    text = _turn_text(turn_messages) or "[mensaje sin texto]"

    context = conversations.get_context(conn, conversation["id"])
    history = [
        message
        for message in conversations.recent_messages(
            conn,
            conversation["id"],
            config.HISTORY_WINDOW + len(turn_ids) + 4,
        )
        if message["id"] not in turn_ids
    ]

    current_product, current_source = _resolve_product(
        conn, llm.Intent(intencion="otro"), context, text
    )
    frame = build_frame(
        conn,
        conversation,
        turn,
        turn_messages=turn_messages,
        text=text,
        context=context,
        history=history,
        focus_product=current_product,
        focus_source=current_source,
    )
    # Deterministic facts are written before the prompt is built so a zone,
    # budget or name mentioned now survives even if the summary is not due yet.
    memory.capture_facts(
        conn,
        conversation,
        text,
        product=current_product,
        source_message_id=last_message_id,
    )
    # NLU ligero: prompt mínimo (~600 tokens) para clasificar intención.
    nlu_messages = build_nlu_messages(history, text)

    tracing.trace_call(
        "orchestrator.assemble",
        {"turn_id": turn.get("id"), "text": text},
        {
            "stage": frame.stage,
            "has_listing": frame.last_listing is not None,
            "has_pending_offer": frame.pending_offer is not None,
            "active_sale_id": (frame.active_sale or {}).get("id"),
            "history_messages": len(history),
        },
        ok=True,
        duration_ms=(time.perf_counter() - assemble_started) * 1000,
        conversation_id=conversation["id"],
        turn_id=turn.get("id"),
    )

    # F12/F13 (prueba #4): un saludo o un mensaje banal no necesita el modelo.
    # Evita que "gracias" se clasifique como compra y ahorra la latencia del NLU.
    fast_intent = None
    if str(context.get("stage") or "").upper() not in _SALE_STAGES:
        if _banal_kind(text) is not None or _is_greeting(text):
            fast_intent = llm.Intent(intencion="otro", confianza=1.0)

    intent_started = time.perf_counter()
    if fast_intent is not None:
        intent, degraded = fast_intent, False
    else:
        intent, degraded = await _classify_light(
            conn, conversation, nlu_messages, text, last_message_id
        )
    intent_ms = int((time.perf_counter() - intent_started) * 1000)

    # Trace: resultado del NLU
    tracing.trace_call(
        "pipeline._classify_light",
        {
            "text": text,
            "message_count": len(nlu_messages),
            "history_messages": len(history),
            "history_window": config.HISTORY_WINDOW,
            "fast_path": fast_intent is not None,
        },
        {
            "intencion": intent.intencion,
            "producto": intent.producto,
            "datos_faltantes": intent.datos_faltantes,
            "confianza": intent.confianza,
            "cantidad": intent.cantidad,
            "degraded": degraded,
        },
        ok=True,
        conversation_id=conversation["id"],
        turn_id=turn.get("id"),
    )
    frame.intent = intent
    tracing.trace_call(
        "orchestrator.interpret",
        {"text": text, "fast_path": fast_intent is not None},
        {
            "intencion": intent.intencion,
            "producto": intent.producto,
            "confianza": intent.confianza,
            "degraded": degraded,
        },
        ok=True,
        duration_ms=intent_ms,
        conversation_id=conversation["id"],
        turn_id=turn.get("id"),
    )

    resolve_started = time.perf_counter()
    product, product_source = _resolve_product(conn, intent, context, text)
    previous_stage = str(context.get("stage") or "").upper()
    # Prueba #8: una petición de catálogo ("qué productos tienes de accesorios")
    # no es una pregunta sobre el producto del turno anterior ni sobre una venta
    # ya entregada. Se resuelve con la categoría real y no hereda el producto.
    catalog_read = (
        catalog.search_catalog(conn, text, limit=_TABLE_LIMIT)
        if _catalog_request(text)
        else None
    )
    category_request = (
        _category_request(conn, intent, text, found=catalog_read)
        if catalog_read is not None
        else None
    )
    if category_request is not None:
        kind, payload = category_request
        if kind == "product":
            product, product_source = payload, "text"
        elif previous_stage not in _SALE_STAGES:
            # Un listado no selecciona producto: no queda nada "en juego".
            product, product_source = None, ""
    # H-09: a low-confidence read must not inherit the previous product. An
    # unknown product ("¿Tienes neveras?") would otherwise be answered or
    # escalated about the monitor of the previous turn. An in-progress sale is
    # exempt: there the context product is the one being purchased.
    stage_hint = previous_stage
    if (
        product_source == "context"
        and intent.confianza < LOW_CONFIDENCE
        and stage_hint not in _SALE_STAGES
    ):
        product, product_source = None, ""
    if product is not None and product_id_from_text(text) is not None:
        if intent.intencion == "otro":
            intent.intencion = "consulta_precio"
            intent.confianza = max(intent.confianza, 0.9)
    memory.capture_facts(
        conn,
        conversation,
        text,
        product=product,
        source_message_id=last_message_id,
    )

    # Trace: producto resuelto
    tracing.trace_call(
        "pipeline._resolve_product",
        {
            "intent": intent.intencion,
            "producto_solicitado": intent.producto,
            "confianza": intent.confianza,
            "product_source_in": product_source,
            "context_stage": str(context.get("stage") or ""),
        },
        {
            "product_id": product["id"] if product else None,
            "product_name": product["name"] if product else None,
            "product_source_out": product_source,
        },
        ok=True,
        conversation_id=conversation["id"],
        turn_id=turn.get("id"),
    )

    frame.focus_product = product
    frame.focus_source = product_source
    reference = resolve_reference(text, frame)
    if reference is not None:
        frame.references.append(reference)
        # L3.1: "el segundo", "esos", "el mismo" and "el de antes" win over the
        # product remembered from the previous turn. An explicit id or a named
        # product still wins over the reference.
        override = reference.kind == "ordinal" or product_source in ("", "context")
        if (
            reference.product_id
            and override
            and previous_stage not in _SALE_STAGES
        ):
            try:
                referenced = catalog.get_product(conn, reference.product_id)
            except catalog.NotFound:
                referenced = None
            if referenced is not None:
                product = referenced
                product_source = "reference"
                frame.focus_product = product
                frame.focus_source = product_source
                memory.capture_facts(
                    conn,
                    conversation,
                    text,
                    product=product,
                    source_message_id=last_message_id,
                )
    tracing.trace_call(
        "orchestrator.resolve",
        {"text": text, "stage": previous_stage},
        {
            "product_id": product["id"] if product else None,
            "product_source": product_source,
            "category_request": category_request[0] if category_request else None,
            "reference_kind": reference.kind if reference else None,
            "reference_product_ids": list(reference.product_ids) if reference else [],
            "needs_clarification": bool(reference and reference.needs_clarification),
        },
        ok=True,
        duration_ms=(time.perf_counter() - resolve_started) * 1000,
        conversation_id=conversation["id"],
        turn_id=turn.get("id"),
    )

    plan = plan_turn(frame, intent)
    tracing.trace_call(
        "orchestrator.plan",
        {"intencion": intent.intencion, "stage": previous_stage},
        {"decision": plan.decision, "reads": len(plan.reads), "reason": plan.reason},
        ok=True,
        conversation_id=conversation["id"],
        turn_id=turn.get("id"),
    )

    decide_started = time.perf_counter()
    listing_before = context.get("last_listing")
    sale = None
    sale_qty = None
    clear_product = False
    compose_started = time.perf_counter()
    # F5 (prueba #3): un "Si" suelto se resuelve contra la pregunta del turno
    # anterior, no contra el NLU. La oferta se consume aquí y no sobrevive a
    # este turno, así que nunca se puede responder dos veces.
    offer = offers.consume(context)
    offer_answer = offers.answer(text) if offer is not None else None
    offer_product = _offer_product(conn, offer)
    sale_outcome = None
    if (
        offer_answer == "no"
        and offer is not None
        and offer["kind"] == offers.RESERVE
        and offer_product is not None
    ):
        product = offer_product
        guard = _sale_guard(product, float(product["price"]))
        reply, generator = _fast_template(
            conn, conversation, "sale_declined", guard, context=context
        )
        sale_outcome = {
            "reply": reply,
            "escalate": False,
            "generator": generator,
            "escalation_question": None,
            "stage": "CLOSE",
        }
    elif offer_answer == "yes" and offer is not None:
        if offer["kind"] == offers.RESERVE and offer_product is not None:
            # "Si" a "¿te reservo?" es una intención de compra del producto
            # ofrecido, aunque el NLU solo haya visto un "Si".
            product = offer_product
            product_source = "offer"
            intent.intencion = "comprar"
            intent.confianza = max(float(intent.confianza or 0), 0.9)
        elif (
            offer["kind"] in (offers.ALTERNATIVES, offers.SIBLINGS)
            and offer_product is not None
        ):
            product = offer_product
            product_source = "offer"
            sale_outcome = _offered_alternatives_outcome(
                conn, conversation, product, offer["kind"], context
            )
    if sale_outcome is None and offer_answer is None:
        # F12 (prueba #4): "gracias"/"disculpa"/"ok" reciben una respuesta banal
        # antes de la ruta post-venta; nunca repiten el pedido.
        sale_outcome = _banal_outcome(conn, conversation, text, context)
    if sale_outcome is None:
        # F8 (prueba #3): después de la venta se habla del pedido, no de una
        # ficha nueva. Va antes de logística y zona: el estado real manda.
        sale_outcome = _post_sale_outcome(
            conn, conversation, text, intent, product, context
        )
    if sale_outcome is None:
        # F9 (prueba #3): una zona sin cobertura se rechaza en el turno en que
        # se menciona, antes de pedir dirección y teléfono.
        sale_outcome = _zone_coverage_outcome(
            conn, conversation, text, product, context
        )
    if sale_outcome is None:
        # F6 (prueba #3): la fecha de entrega no se escala antes del pedido.
        sale_outcome = _logistics_outcome(
            conn, conversation, text, product, context
        )
    if sale_outcome is None:
        sale_outcome = _maybe_handle_sale(
            conn, conversation, text, intent, product, context
        )
    if sale_outcome is not None:
        reply = sale_outcome["reply"]
        escalate = sale_outcome["escalate"]
        generator = sale_outcome["generator"]
        escalation_question = sale_outcome["escalation_question"]
        stage = sale_outcome["stage"]
        sale = sale_outcome.get("sale")
        sale_qty = sale_outcome.get("sale_qty")
    else:
        reply, escalate, generator, escalation_question = await _decide(
            conn,
            conversation,
            {"body": text, "id": last_message_id},
            intent,
            product,
            context,
            history,
            last_text=text,
            product_source=product_source,
            category_request=category_request,
            reference=reference,
        )
        stage = _stage_for(context, intent, product, escalate)
        # Prueba #8: tras una venta entregada, un listado de catálogo abre una
        # consulta nueva: vuelve a DISCOVERY y suelta el producto de la venta,
        # conservando sale_id/order_code para el post-venta.
        if category_request is not None:
            if previous_stage == "DONE":
                stage = "DISCOVERY"
            if category_request[0] == "list" and previous_stage not in _SALE_STAGES:
                clear_product = True
    compose_ms = int((time.perf_counter() - compose_started) * 1000)
    listing_after = context.get("last_listing")
    frame.evidence = _catalog_evidence(
        conn,
        catalog_read=catalog_read,
        listing_before=listing_before,
        listing_after=listing_after,
        product=product,
    ) + _sales_evidence(
        conn,
        context=context,
        text=text,
        product=product,
        stage=stage,
        sale=sale,
    )

    tracing.trace_call(
        "orchestrator.decide",
        {
            "intencion": intent.intencion,
            "product_id": product["id"] if product else None,
            "category_request": category_request[0] if category_request else None,
        },
        {
            "generator": generator,
            "stage": stage,
            "escalate": escalate,
            "sale_id": sale["id"] if sale else None,
        },
        ok=True,
        duration_ms=(time.perf_counter() - decide_started) * 1000,
        conversation_id=conversation["id"],
        turn_id=turn.get("id"),
    )

    tracing.trace_call(
        "orchestrator.compose",
        {"intencion": intent.intencion, "stage": stage},
        {
            "generator": generator,
            "chars": len(reply or ""),
            "escalation_question": bool(escalation_question),
        },
        ok=True,
        duration_ms=compose_ms,
        conversation_id=conversation["id"],
        turn_id=turn.get("id"),
    )

    # Grounding in `shadow`: record violations, never rewrite the reply in L3.0.
    if str(getattr(config, "AGENT_GROUNDING_MODE", "off")).lower() in ("shadow", "on"):
        guard_context = _guard_context(
            product,
            context.get("candidates") or [],
            context,
            extra_products=context.get("products") or [],
            product_source=product_source,
        )
        report = grounding.validate_claims(reply, frame, guard_context)
        if not report.ok:
            try:
                events.log(
                    conn,
                    "system",
                    "grounding_violation",
                    entity="conversations",
                    entity_id=conversation["id"],
                    details={
                        "mode": config.AGENT_GROUNDING_MODE,
                        "stage": stage,
                        **report.as_dict(),
                    },
                )
            except Exception:  # noqa: BLE001 - telemetry must not block the reply
                log.exception("could not log the grounding report")

    supplier_id = product["supplier_id"] if (escalate and product) else None

    # La oferta de este turno queda disponible solo para el siguiente.
    turn_listing = (
        listing_after if listing_after and listing_after != listing_before else {}
    )
    next_offer = offers.detect(
        reply,
        product["id"] if product else None,
        product_ids=turn_listing.get("product_ids"),
        query=turn_listing.get("query") or "",
    )
    offers.save(context, next_offer)
    frame.pending_offer = next_offer

    persist_started = time.perf_counter()
    commit(
        frame,
        intent=intent,
        product=product,
        generator=generator,
        escalate=escalate,
        stage=stage,
        sale=sale,
        sale_qty=sale_qty,
        clear_product=clear_product,
    )
    tracing.trace_call(
        "orchestrator.persist",
        {"stage": stage, "sale_id": sale["id"] if sale else None},
        {
            "product_id": product["id"] if product else None,
            "pending_offer": (next_offer or {}).get("kind"),
        },
        ok=True,
        duration_ms=(time.perf_counter() - persist_started) * 1000,
        conversation_id=conversation["id"],
        turn_id=turn.get("id"),
    )
    return {
        "turn_id": turn.get("id"),
        "message_id": last_message_id,
        "reply": reply,
        "intent": intent.model_dump(),
        "degraded": degraded,
        "product_id": product["id"] if product else None,
        "product_name": product["name"] if product else None,
        "supplier_id": supplier_id,
        "escalate": escalate,
        "escalation_question": escalation_question,
        "generator": generator,
        "stage": stage,
        "sale_id": sale["id"] if sale else None,
        "sale_status": sale["status"] if sale else None,
        "sale_order_code": sale["order_code"] if sale else None,
        "latency_ms": int((time.perf_counter() - started) * 1000),
        "intent_ms": intent_ms,
        "compose_ms": compose_ms,
        "context_tokens": llm.estimate_tokens(
            [*nlu_messages, {"role": "user", "content": text}]
        ),
    }


# L3.0 compatibility: tests and callers still import `pipeline._run_turn`.
_run_turn = run_turn
