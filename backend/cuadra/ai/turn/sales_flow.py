"""Sale state-machine steps and post-sale outcomes."""

import logging

from cuadra.ai import memory, offers, prompts
from cuadra.domain import catalog, conversations, sales as sales_domain

log = logging.getLogger("cuadra.sales_flow")

from cuadra.ai.turn.constants import (
    _DELIVERY_COVERAGE,
    _LIST_REQUEST,
    _LOGISTICS_QUESTION,
    _POST_SALE_HINTS,
    _SALE_STAGES,
    _STOCK_QUESTION,
)

from cuadra.ai.turn.composer import (
    _fast_template,
)

from cuadra.ai.turn.guards import (
    _guard_context,
    _safe_template,
)

from cuadra.ai.turn.policy import (
    _asks_for_alternatives,
    _banal_kind,
    _category_request,
    _confirms,
    _is_decline,
)

from cuadra.ai.turn.render import (
    _available_stock,
    _format_product_list,
    _remember_listing,
    _render_out_of_stock,
    _render_siblings,
)


def _sale_quantity(intent, context: dict, product: dict) -> int:
    """Quantity agreed so far in this sale (F2, prueba #2).

    The NLU only reports a quantity on the turn the customer states it, so the
    rest of the turns of the same sale must reuse the remembered one instead of
    silently falling back to 1. A remembered quantity belongs to a product: when
    the customer switches product the draft starts again at 1.
    """
    if intent.cantidad:
        return int(intent.cantidad)
    remembered = int(context.get("sale_qty") or 0)
    if context.get("sale_qty_product_id") not in (None, product["id"]):
        return 1
    return remembered or 1


def _sale_slots(conn, conversation, facts: dict[str, str]) -> dict:
    try:
        customer = conversations.get_customer(conn, conversation["customer_id"])
    except Exception:  # noqa: BLE001 - a missing customer must not block the reply
        customer = {}
    name = facts.get(memory.FACT_CUSTOMER_NAME) or (customer.get("name") or "").strip()
    return {
        "recipient_name": name or None,
        "delivery_zone": facts.get(memory.FACT_ZONE),
        "delivery_address": facts.get(memory.FACT_ADDRESS),
        "customer_phone": facts.get(memory.FACT_PHONE),
    }


def _missing_slots(slots: dict) -> list[str]:
    labels = []
    if not slots.get("recipient_name"):
        labels.append("tu nombre")
    if not slots.get("delivery_zone"):
        labels.append("la zona de entrega")
    if not slots.get("delivery_address"):
        labels.append("la dirección (calle, número y entrecalles)")
    if not slots.get("customer_phone"):
        labels.append("un teléfono de contacto")
    return labels


def _sale_guard(product: dict, total: float, *, unit: float | None = None) -> dict:
    """Guard context whose only price is the sale total (no internal data).

    ``unit`` also allows the unit price, so the quote can say "350 cada una,
    1050 en total" without tripping the price guardrail.
    """
    safe = {**product, "price": round(total, 2)}
    quoted = [round(float(unit), 2)] if unit is not None else []
    return {"product": safe, "products": [safe], "quoted_prices": quoted}


def _sale_stock_outcome(
    conn, conversation, product: dict, context: dict, requested: int = 1
) -> dict:
    """F14 (prueba #4): say how many units remain instead of "se nos fue".

    Before this, asking for 15 of a product with 1 in stock answered with
    unrelated monitors and tablets. The customer only needs to know the real
    quantity and whether to adjust the order or wait for the warehouse.
    """
    available = _available_stock(product)
    if available <= 0:
        alternatives = catalog.siblings(conn, product)
        guard = _guard_context(product, None, context, extra_products=alternatives)
        reply, generator = _safe_template(
            conn,
            conversation,
            _render_out_of_stock(product, alternatives),
            guard,
            "template",
        )
        return {
            "reply": reply,
            "escalate": False,
            "generator": generator,
            "escalation_question": None,
            "stage": "CLOSE",
        }
    key = "stock_short_one" if available == 1 else "stock_short_many"
    guard = _guard_context(product, None, context)
    reply = prompts.template_variant(
        key, context, producto=product["name"], cantidad=available
    )
    reply, generator = _safe_template(conn, conversation, reply, guard, "template")
    return {
        "reply": reply,
        "escalate": False,
        "generator": generator,
        "escalation_question": None,
        "stage": "CONFIRM",
        "sale_qty": available,
    }


def _sale_failure(
    conn, conversation, product: dict, context: dict, exc: Exception, qty: int = 1
) -> dict:
    message = str(exc)
    if "stock" in message:
        return _sale_stock_outcome(conn, conversation, product, context, qty)
    if "zone" in message or "supplier" in message:
        guard = _guard_context(product, None, context)
        reply, generator = _safe_template(
            conn,
            conversation,
            "Esa zona no la cubre el almacén de este producto. ¿Me confirmas otra "
            "zona o te busco una alternativa?",
            guard,
            "template",
        )
        return {
            "reply": reply,
            "escalate": True,
            "generator": generator,
            "escalation_question": "El cliente pide entrega en una zona que el TCP no cubre.",
            "stage": "HANDOFF",
        }
    guard = _guard_context(product, None, context)
    reply, generator = _safe_template(
        conn,
        conversation,
        "No pude registrar el pedido con esos datos. ¿Me los confirmas otra vez?",
        guard,
        "template",
    )
    return {
        "reply": reply,
        "escalate": True,
        "generator": generator,
        "escalation_question": message,
        "stage": "HANDOFF",
    }


def _sale_product(conn, conversation, context: dict) -> dict | None:
    """Product of the sale in progress, when this turn did not resolve one (F1).

    The open sale wins over the context: the context product can be overwritten
    by a later turn, the sale row is the order being built.
    """
    existing = sales_domain.active_sale_for_conversation(conn, conversation["id"])
    product_id = existing.get("product_id") if existing is not None else None
    if not product_id:
        product_id = context.get("product_id")
    if not product_id:
        return None
    try:
        return catalog.get_product(conn, product_id)
    except catalog.NotFound:
        return None


def _delivery_zones_outcome(
    conn, conversation, product: dict, context: dict
) -> dict:
    """Answer a delivery-coverage question that named no zone (prueba #5).

    "¿Puedes entregar en mi zona?" was escalated to the TCP. The real answer is
    in the catalog: the supplier's active delivery zones.
    """
    previous = str(context.get("stage") or "").upper()
    info = catalog.llm_product_json(conn, product)
    zonas = info.get("zonas_entrega") or []
    guard = _guard_context(product, None, context, product_source="text")
    if zonas:
        reply = prompts.template_variant(
            "delivery_zones",
            context,
            producto=product["name"],
            zonas=", ".join(zonas),
        )
    else:
        reply = prompts.template_variant(
            "delivery_zones_unknown", context, producto=product["name"]
        )
    reply, generator = _safe_template(conn, conversation, reply, guard, "template")
    return {
        "reply": reply,
        "escalate": False,
        "generator": generator,
        "escalation_question": None,
        "stage": previous if previous in (*_SALE_STAGES, "DONE") else "QUOTE",
    }


def _zone_coverage_outcome(
    conn, conversation, text: str, product: dict | None, context: dict
) -> dict | None:
    """F9 (prueba #3): reject an uncovered zone in the turn it is mentioned.

    Before this, "Soy de la UCI" was ignored (the zone was inactive) and the bot
    kept quoting the product until the sale failed asking for the address. The
    check uses inactive zones too: mentioning one is enough to answer honestly.

    Prueba #5: una consulta puede nombrar varias zonas ("Marianao o La Lisa") o
    ninguna ("¿puedes entregar en mi zona?"). Se responde por cobertura real en
    vez de escalar o quedarse callado.
    """
    if product is None:
        return None
    zones = catalog.resolve_zones(conn, text or "", active_only=False)
    if not zones:
        if _DELIVERY_COVERAGE.search(text or ""):
            return _delivery_zones_outcome(conn, conversation, product, context)
        return None

    covered: list[str] = []
    uncovered: list[str] = []
    for zone in zones:
        covers = bool(zone.get("active")) and catalog.product_covers_zone(
            conn, product, int(zone["id"])
        )
        (covered if covers else uncovered).append(zone["name"])
    if not uncovered:
        if not _DELIVERY_COVERAGE.search(text or ""):
            # Solo mencionó su zona: no hay nada que rechazar ni que responder
            # aquí; el turno sigue su curso (cotización, venta...).
            return None
        # Preguntó explícitamente por la entrega y la zona está cubierta.
        previous = str(context.get("stage") or "").upper()
        guard = _guard_context(product, None, context, product_source="text")
        reply = prompts.template_variant(
            "delivery_zone_covered",
            context,
            producto=product["name"],
            zonas=" y ".join(covered),
        )
        reply, generator = _safe_template(
            conn, conversation, reply, guard, "template"
        )
        return {
            "reply": reply,
            "escalate": False,
            "generator": generator,
            "escalation_question": None,
            "stage": previous if previous in (*_SALE_STAGES, "DONE") else "QUOTE",
        }

    guard = _guard_context(product, None, context, product_source="text")
    if covered:
        reply = prompts.template_variant(
            "zone_partial_coverage",
            context,
            producto=product["name"],
            cubiertas=" y ".join(covered),
            no_cubiertas=", ".join(uncovered),
        )
    else:
        reply = prompts.template_variant(
            "zone_not_covered", context, producto=product["name"]
        )
    reply, generator = _safe_template(conn, conversation, reply, guard, "template")
    return {
        "reply": reply,
        "escalate": False,
        "generator": generator,
        "escalation_question": None,
        "stage": "CLOSE",
    }


def _post_sale_outcome(
    conn, conversation, text: str, intent, product: dict | None, context: dict
) -> dict | None:
    """F8 (prueba #3): after the sale, talk about the order, not a new quote.

    DONE used to fall into free composition ("¿Te la traigo en 48h?") and back
    into quoting. Here the answer only uses the stored sale status; prices and
    delivery dates are never invented.
    """
    if str(context.get("stage") or "").upper() != "DONE":
        return None
    order_code = context.get("order_code")
    if not order_code:
        return None
    if intent.intencion == "comprar":
        # A new purchase starts a new flow, not a post-sale reply.
        return None
    if product is not None and context.get("product_id") not in (None, product["id"]):
        return None
    if _LIST_REQUEST.search(text or "") or _STOCK_QUESTION.search(text or ""):
        # "¿Qué laptops tienes?" / "¿cuántas quedan?" are catalog questions, not
        # the order (F14, prueba #4).
        return None
    if (
        intent.intencion in ("consulta_dato", "queja", "comparar")
        and not _POST_SALE_HINTS.search(text or "")
        and not _LOGISTICS_QUESTION.search(text or "")
    ):
        # A real technical/complaint question keeps its normal route (specs and
        # guardrails); only the post-sale conversation is intercepted here.
        return None

    sale = None
    if context.get("sale_id"):
        try:
            sale = sales_domain.get_sale(conn, int(context["sale_id"]))
        except Exception:  # noqa: BLE001 - a missing sale must not block the reply
            sale = None
    if sale is None:
        sale = sales_domain.get_sale_by_code(conn, order_code)
    status = str((sale or {}).get("status") or "")
    if status in ("DELIVERED", "SETTLED"):
        key = "post_sale_delivered"
    elif status == "CONFIRMED":
        key = "post_sale_confirmed"
    elif status == "CANCELLED":
        key = "sale_cancelled"
    elif status == "NEEDS_OPERATOR":
        key = "sale_unavailable"
    else:
        key = "post_sale_pending"
    guard = _guard_context(None, None, context)
    reply = prompts.template_variant(key, context, order_code=order_code)
    reply, generator = _safe_template(conn, conversation, reply, guard, "template")
    return {
        "reply": reply,
        "escalate": False,
        "generator": generator,
        "escalation_question": None,
        "stage": "DONE",
    }


def _banal_outcome(
    conn, conversation, text: str, context: dict
) -> dict | None:
    """F12 (prueba #4): a banal message gets a banal reply, not the order.

    "gracias" used to be classified as a purchase and repeated "Tu pedido
    V-000001 quedó registrado". This handler runs before the post-sale route and
    never changes the stage.
    """
    kind = _banal_kind(text)
    if kind is None:
        return None
    previous = str(context.get("stage") or "").upper()
    if previous in _SALE_STAGES:
        # En plena captura/confirmación, "ok" o "gracias" pueden ser la respuesta
        # al pedido; ahí manda la máquina de venta.
        return None
    key = {
        "thanks": "banal_thanks",
        "sorry": "banal_sorry",
        "ack": "banal_ack",
    }[kind]
    guard = _guard_context(None, None, context)
    reply = prompts.template_variant(key, context)
    reply, generator = _safe_template(conn, conversation, reply, guard, "template")
    return {
        "reply": reply,
        "escalate": False,
        "generator": generator,
        "escalation_question": None,
        "stage": previous or "DISCOVERY",
    }


def _logistics_outcome(
    conn, conversation, text: str, product: dict | None, context: dict
) -> dict | None:
    """F6 (prueba #3): no prometer una fecha antes de que exista el pedido.

    La fecha de entrega depende de la zona, la dirección y el almacén, así que
    escalarla cuando aún no hay pedido es una promesa que el TCP no puede
    cumplir. Solo una venta ya despachada justifica consultar logística; en el
    resto de etapas se responde que se coordina al confirmar.
    """
    if not _LOGISTICS_QUESTION.search(text or ""):
        return None

    sale = None
    if context.get("sale_id"):
        try:
            sale = sales_domain.get_sale(conn, int(context["sale_id"]))
        except Exception:  # noqa: BLE001 - a missing sale must not block the reply
            sale = None
    if sale is None:
        sale = sales_domain.active_sale_for_conversation(conn, conversation["id"])
    if str((sale or {}).get("status") or "") in (
        "DISPATCHED",
        "CONFIRMED",
        "DELIVERED",
        "SETTLED",
    ):
        # El pedido ya existe: `_post_sale_outcome` responde con su estado real.
        return None

    previous = str(context.get("stage") or "").upper()
    guard = _guard_context(product, None, context)
    reply = prompts.template_variant("delivery_coordinated", context)
    reply, generator = _safe_template(conn, conversation, reply, guard, "template")
    return {
        "reply": reply,
        "escalate": False,
        "generator": generator,
        "escalation_question": None,
        "stage": previous if previous in (*_SALE_STAGES, "DONE") else "QUOTE",
    }


def _offer_product(conn, offer: dict | None) -> dict | None:
    """Product of the previous turn's offer, when it still exists."""
    if not offer or not offer.get("product_id"):
        return None
    try:
        return catalog.get_product(conn, int(offer["product_id"]))
    except (catalog.NotFound, TypeError, ValueError):
        return None


def _offered_alternatives_outcome(
    conn, conversation, product: dict, kind: str, context: dict
) -> dict:
    """F5: a "Si" to "¿te enseño alguno?" actually shows the alternatives."""
    if kind == offers.SIBLINGS:
        alternatives = catalog.siblings(conn, product)
        reply = _render_siblings(conn, conversation, product, alternatives, context)
    else:
        # F14 (prueba #4): las alternativas de un producto agotado son de su
        # misma familia (otras laptops), no un monitor o una tablet.
        alternatives = catalog.siblings(conn, product)
        if alternatives:
            _remember_listing(
                context, alternatives, query=product["name"], kind="alternatives"
            )
            reply = f"Tengo {_format_product_list(alternatives)}. ¿Cuál te interesa?"
        else:
            reply = (
                f"De {product['name']} no tengo otro modelo parecido por ahora."
            )
    guard = _guard_context(
        product, None, context, extra_products=alternatives, product_source="offer"
    )
    reply, generator = _safe_template(conn, conversation, reply, guard, "template")
    return {
        "reply": reply,
        "escalate": False,
        "generator": generator,
        "escalation_question": None,
        "stage": "QUOTE",
    }


def _maybe_handle_sale(
    conn, conversation, text: str, intent, product: dict | None, context: dict
) -> dict | None:
    """Advance the sale state machine when a purchase is in play.

    Returns a reply outcome (same shape as `_decide` plus `stage`/`sale`) or
    None when the turn is not part of a sale.
    """
    stage = str(context.get("stage") or "DISCOVERY").upper()
    in_sale = stage in _SALE_STAGES
    if intent.intencion != "comprar" and not in_sale:
        return None
    # F13/F14 (prueba #4): una pregunta de catálogo o de cantidad no es una
    # orden de compra, aunque el NLU la etiquete como "comprar".
    asks_about_catalog = (
        _STOCK_QUESTION.search(text or "")
        or _LIST_REQUEST.search(text or "")
        or _asks_for_alternatives(text or "")
    )
    if (
        asks_about_catalog
        and not intent.cantidad
        and not _confirms(text, intent)
        and not _is_decline(text)
    ):
        return None
    # "laptop" / "ando buscando una laptop" es una consulta de catálogo, no una
    # orden de compra de un producto al azar. `_decide` la responde con la tabla.
    category = _category_request(conn, intent, text)
    if category is not None and category[0] == "list":
        return None
    if product is None:
        if intent.producto:
            # H-10: el cliente nombró un producto que no está en el catálogo; no
            # se responde sobre el de la venta anterior.
            return None
        # F1: el NLU no repitió el producto ("null"), pero la venta sigue abierta.
        product = _sale_product(conn, conversation, context)
        if product is None:
            return None
    if in_sale and intent.intencion != "comprar" and stage == "CONFIRM":
        # Una pregunta abierta sobre la confirmación ("¿no tiene otro modelo?")
        # no cancela ni re-confirma el pedido: la responde `_decide` y la venta
        # sigue intacta (F4). Una corrección de cantidad sí entra al flujo (F2).
        correcting = bool(intent.cantidad)
        if not _confirms(text, intent) and not _is_decline(text) and not correcting:
            return None

    facts = memory.facts_map(conn, conversation["id"])
    slots = _sale_slots(conn, conversation, facts)
    missing = _missing_slots(slots)
    guard = _sale_guard(product, float(product["price"]))

    # Cantidad solicitada: viene del NLU (ej. "dos tablets" -> 2), si no, la que
    # ya se acordó en esta venta, y solo entonces 1.
    qty = _sale_quantity(intent, context, product)

    existing = sales_domain.active_sale_for_conversation(conn, conversation["id"])
    if existing is not None and int(existing.get("product_id", -1)) == int(product["id"]):
        # Ya hay una venta abierta de este producto: permitir actualizar cantidad.
        if intent.intencion == "comprar" and qty != int(existing["quantity"]):
            try:
                sales_domain.update_sale_quantity(conn, existing["id"], qty)
            except sales_domain.Invalid as exc:
                return _sale_failure(conn, conversation, product, context, exc, qty)
            existing = sales_domain.get_sale(conn, existing["id"])
        guard = _sale_guard(product, float(existing["amount"]))
        reply, generator = _fast_template(
            conn,
            conversation,
            "sale_created",
            guard,
            context=context,
            order_code=existing["order_code"],
        )
        return {
            "reply": reply,
            "escalate": False,
            "generator": generator,
            "escalation_question": None,
            "stage": "DONE",
            "sale": existing,
            "sale_qty": qty,
        }

    # F14 (prueba #4): no confirmar ni pedir datos para una cantidad que el
    # almacén no tiene. Se responde cuántas quedan y se conserva esa cantidad.
    if qty > _available_stock(product):
        return _sale_stock_outcome(conn, conversation, product, context, qty)

    if not missing and _confirms(text, intent):
        try:
            sale = sales_domain.create_sale(
                conn,
                conversation_id=conversation["id"],
                customer_id=conversation["customer_id"],
                product_id=product["id"],
                quantity=qty,
                recipient_name=slots["recipient_name"],
                delivery_zone=slots["delivery_zone"],
                delivery_address=slots["delivery_address"],
                customer_phone=slots["customer_phone"],
            )
        except sales_domain.Invalid as exc:
            return _sale_failure(conn, conversation, product, context, exc, qty)

        guard = _sale_guard(product, float(sale["amount"]))
        reply, generator = _fast_template(
            conn,
            conversation,
            "sale_created",
            guard,
            context=context,
            order_code=sale["order_code"],
        )
        return {
            "reply": reply,
            "escalate": False,
            "generator": generator,
            "escalation_question": None,
            "stage": "DONE",
            "sale": sale,
            "sale_qty": qty,
        }

    if missing:
        # F3b (prueba #2): cotizar antes de pedir datos. El cliente preguntó
        # precio y recibía "me falta la zona, la dirección y un teléfono" sin
        # ver un solo número.
        total = round(float(product["price"]) * qty, 2)
        guard = _sale_guard(product, total, unit=float(product["price"]))
        reply, generator = _fast_template(
            conn,
            conversation,
            "sale_ask_data",
            guard,
            context=context,
            producto=product["name"],
            cantidad=qty,
            total=f"{total:g}",
            moneda=product["currency"],
            faltan=", ".join(missing),
        )
        return {
            "reply": reply,
            "escalate": False,
            "generator": generator,
            "escalation_question": None,
            "stage": "CAPTURE_DATA",
            "sale_qty": qty,
        }

    if stage == "CONFIRM" and _is_decline(text) and not intent.cantidad:
        reply, generator = _fast_template(
            conn, conversation, "sale_declined", guard, context=context
        )
        return {
            "reply": reply,
            "escalate": False,
            "generator": generator,
            "escalation_question": None,
            "stage": "CLOSE",
        }

    total = round(float(product["price"]) * qty, 2)
    guard = _sale_guard(product, total, unit=float(product["price"]))
    reply, generator = _fast_template(
        conn,
        conversation,
        "sale_confirm",
        guard,
        context=context,
        producto=product["name"],
        cantidad=qty,
        total=f"{total:g}",
        moneda=product["currency"],
        cliente=slots["recipient_name"],
        direccion=slots["delivery_address"],
        zona=slots["delivery_zone"],
    )
    return {
        "reply": reply,
        "escalate": False,
        "generator": generator,
        "escalation_question": None,
        "stage": "CONFIRM",
        "sale_qty": qty,
    }
