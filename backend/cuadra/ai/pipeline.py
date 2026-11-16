"""Incoming-turn pipeline: context -> NLU -> rules -> guardrails -> reply.

The LLM classifies intent (JSON). The *engine* here decides the answer by rules
and templates (doc 01 §6, doc 03 §2-3); free generation is only used for
comparisons, complaints and unforeseen cases, always fed with real catalog JSON
and always validated before it reaches the customer.
"""

import json
import logging
import re
import time

from cuadra import config
from cuadra.ai import guardrails, llm, memory, prompts, retrieval, tracing
from cuadra.ai.context import ContextBuilder, build_nlu_messages
from cuadra.domain import (
    catalog,
    conversations,
    events,
    sales as sales_domain,
    settings,
    turns,
)

log = logging.getLogger("cuadra.pipeline")


# Datos que solo el cliente puede proporcionar; nunca se escalan al TCP.
_DATO_CLIENTE = {
    "zona", "municipio", "dirección", "direccion", "domicilio", "barrio",
    "reparto", "teléfono", "telefono", "tel", "celular", "número",
    "numero", "contacto", "nombre", "destinatario", "a nombre de",
}


def _dato_pertenece_a(dato: str) -> str:
    """Clasifica un dato faltante como del cliente, del proveedor o desconocido.

    Solo los datos del proveedor (stock, garantía, precio, variantes, tiempo de
    entrega) se escalan al TCP. Los datos del cliente (zona, dirección, teléfono,
    nombre) se le piden al cliente, nunca al almacén.
    """
    d = (dato or "").strip().lower()
    if not d:
        return "desconocido"
    if d in _DATO_CLIENTE or any(k in d for k in _DATO_CLIENTE):
        return "cliente"
    # Todo lo que no es del cliente se asume del proveedor (stock, garantía, etc.).
    return "proveedor"


def _build_base_messages(
    conn,
    conversation,
    history: list[dict],
    context: dict,
    *,
    product: dict | None = None,
    candidates: list[dict] | None = None,
    turn_text: str = "",
) -> tuple[list[dict], str]:
    return ContextBuilder(conn, conversation, context=context).build_with_turn(
        history,
        product=product,
        candidates=candidates,
        turn_text=turn_text,
    )


_ID_PREFIXED = re.compile(
    r"\b(?:id|producto|codigo|código)\s*[:#]?\s*(\d{1,9})\b", re.IGNORECASE
)
_ID_BARE = re.compile(r"^\s*#?(\d{1,6})\s*$")


def product_id_from_text(text: str) -> int | None:
    """Return an explicit product id, so the bot never has to guess from a photo."""
    body = (text or "").strip()
    match = _ID_BARE.match(body) or _ID_PREFIXED.search(body)
    return int(match.group(1)) if match else None


def _retrieve_candidates(conn, query: str, *, limit: int = 4) -> list[dict]:
    """Hybrid retrieval (FTS/embeddings/RRF, fuzzy fallback) as real product rows."""
    products: list[dict] = []
    for product_id in retrieval.search_ids(conn, query, top_k=limit):
        try:
            product = catalog.get_product(conn, product_id)
        except catalog.NotFound:
            continue
        if product["status"] in ("ACTIVE", "OUT_OF_STOCK"):
            products.append(product)
    return products


def _lexically_supported(conn, query: str, product: dict) -> bool:
    """The fuzzy scorer must also recognise the product (H-27).

    The embedding model is small: "cargador" scores 0.56 against "Moto
    Eléctrica Cargo", so semantic recall alone would answer about a motorbike.
    Semantic recall is useful to fill gaps, but naming a single product needs
    lexical evidence too. The fuzzy scorer's ratio term still accepts plurals
    and near spellings ("tablet", "neveras").
    """
    try:
        matches = catalog.search_products(
            conn, query, limit=5, statuses=("ACTIVE", "OUT_OF_STOCK")
        )
    except Exception:  # noqa: BLE001 - resolution must never break the reply
        return False
    return any(int(match["id"]) == int(product["id"]) for match in matches)


def _find_product_in_text(conn, text: str) -> dict | None:
    """Fuzzy match the whole turn, then each line (consolidated bursts dilute it)."""
    text = (text or "").strip()
    if not text:
        return None
    product = catalog.find_product(conn, text)
    if product is not None:
        return product
    chunks = [chunk.strip() for chunk in re.split(r"[\n.;!?]+", text) if chunk.strip()]
    if len(chunks) < 2:
        return None
    for chunk in sorted(chunks, key=len, reverse=True):
        if len(chunk) < 4:
            continue
        product = catalog.find_product(conn, chunk)
        if product is not None:
            return product
    return None


def _resolve_product(
    conn, intent, context: dict, text: str = ""
) -> tuple[dict | None, str]:
    """Return `(product, source)`; source is id/intent/text/context or ''.

    The turn's own text always wins over the product remembered in the
    context: a customer who names a product must not be answered about the
    previous one (H-11).
    """
    explicit_id = product_id_from_text(text)
    if explicit_id is not None:
        try:
            return catalog.get_product(conn, explicit_id), "id"
        except catalog.NotFound:
            if _ID_PREFIXED.search(text):
                return None, ""
    if intent.producto:
        # Fuzzy first: it is precise for the exact names the NLU usually emits.
        product = catalog.find_product(conn, intent.producto)
        if product is not None:
            return product, "intent"
        # Semantic/lexical fallback for paraphrases the fuzzy matcher misses,
        # but only when the fuzzy scorer also recognises the product (H-27).
        candidates = _retrieve_candidates(conn, intent.producto, limit=2)
        for candidate in candidates:
            if _lexically_supported(conn, intent.producto, candidate):
                return candidate, "intent"
    if text:
        product = _find_product_in_text(conn, text)
        if product is not None:
            return product, "text"
    if intent.producto:
        # H-10: the customer named a product and the NLU echoed that name, but
        # nothing in the catalog matches it. Falling back to the product of the
        # previous turn would answer about a different item ("¿Tienes neveras?"
        # -> Monitor LED 24), so this turn has no product.
        return None, ""
    if context.get("product_id"):
        try:
            return catalog.get_product(conn, context["product_id"]), "context"
        except catalog.NotFound:
            return None, ""
    return None, ""


_COMPARE_SPLIT = re.compile(
    r"\s+(?:con|y|vs\.?|versus|contra|frente\s+a)\s+|[,;/\n]", re.IGNORECASE
)


def _comparison_products(conn, text: str, intent, product: dict | None) -> list[dict]:
    """Every product the customer is comparing (H-07).

    Splitting the phrase and resolving each chunk is precise for the usual
    "A con B" wording; hybrid retrieval is only used to fill gaps, so an
    unrelated candidate never displaces a named product.
    """
    found: dict[int, dict] = {}

    def add(value: dict | None) -> None:
        if value is not None and value["status"] in ("ACTIVE", "OUT_OF_STOCK"):
            found.setdefault(int(value["id"]), value)

    add(product)
    for chunk in _COMPARE_SPLIT.split(text or ""):
        chunk = chunk.strip(" .!?¿¡")
        if len(chunk) >= 3:
            add(catalog.find_product(conn, chunk))
    if len(found) < 2:
        for candidate in _retrieve_candidates(conn, text, limit=4):
            add(candidate)
    return list(found.values())[:4]


def _render_greeting(
    conn, product: dict, conversation: dict, context: dict | None = None
) -> str:
    info = catalog.llm_product_json(conn, product)
    zona, zona_clause = _reply_zone(conn, conversation, info)
    policy = settings.transport_policy(conn)
    # F5 (prueba #2): usa el contexto del turno, no una copia recargada, para que
    # `last_template_greeting_product` llegue a `_update_context` y se guarde.
    if context is None:
        context = conversations.get_context(conn, conversation["id"])
    context["conversation_id"] = conversation["id"]
    return prompts.template_variant(
        "greeting_product",
        context,
        producto=info["nombre"],
        precio=f"{info['precio']:g}",
        moneda=info["moneda"],
        transporte_clause=prompts.TRANSPORT_CLAUSES[policy],
        zona=zona,
        zona_clause=zona_clause,
    )


# H-03: a chat message should not list a dozen municipalities at once.
_MAX_ZONES_IN_REPLY = 3
_DEFERRED_ZONE = "tu municipio"
_DEFERRED_ZONE_CLAUSE = " (te confirmo la zona exacta al cerrar el pedido)"


def _reply_zone(conn, conversation, info: dict) -> tuple[str, str]:
    """Zone phrase for a greeting: the known one, a short list or a promise."""
    zones = [str(zone) for zone in (info.get("zonas_entrega") or []) if zone]
    usual = ""
    try:
        profile = memory.customer_profile(conn, conversation["customer_id"])
        usual = str(profile.get("zona_habitual") or "").strip()
    except Exception:  # noqa: BLE001 - wording must never break the reply
        log.warning("could not read the customer profile for zones", exc_info=True)
    if usual and any(usual.casefold() == zone.casefold() for zone in zones):
        return usual, ""
    if 0 < len(zones) <= _MAX_ZONES_IN_REPLY:
        return ", ".join(zones), ""
    if not zones and info.get("zona_entrega"):
        return str(info["zona_entrega"]), ""
    return _DEFERRED_ZONE, _DEFERRED_ZONE_CLAUSE


def _render_out_of_stock(alternatives: list[dict]) -> str:
    if not alternatives:
        return "Ese se nos fue por ahora. ¿Quieres que te avise cuando vuelva a entrar?"
    first = alternatives[0]
    if len(alternatives) == 1:
        return (
            f"Ese se nos fue por ahora. Tengo {first['name']} en "
            f"{first['price']:g} {first['currency']} que es parecido. ¿Te echo un vistazo?"
        )
    second = alternatives[1]
    return (
        f"Ese se nos fue por ahora. Tengo {first['name']} en {first['price']:g} {first['currency']} "
        f"y {second['name']} en {second['price']:g} {second['currency']}. "
        "¿Te echo un vistazo a alguna?"
    )


def _format_product_list(products: list[dict]) -> str:
    return ", ".join(
        f"{item['name']} ({item['price']:g} {item['currency']})" for item in products
    )


def _render_siblings(
    conn, conversation, product: dict, alternatives: list[dict], context: dict
) -> str:
    """F4: list the other models of the same family, or say there is only one."""
    if not alternatives:
        return prompts.template_variant(
            "siblings_none", context, producto=product["name"]
        )
    first = alternatives[0]
    segunda = ""
    if len(alternatives) > 1:
        second = alternatives[1]
        segunda = f" y {second['name']} en {second['price']:g} {second['currency']}"
    return prompts.template_variant(
        "siblings",
        context,
        producto=product["name"],
        alternativa_1=first["name"],
        precio_1=f"{first['price']:g}",
        moneda_1=first["currency"],
        segunda=segunda,
    )


def _category_listing(
    conn, conversation, intent, context: dict, text: str
) -> tuple[str, bool, str, None] | None:
    """F3: "¿qué laptops tienes?" -> short list with prices, then ask which one.

    Returns None when the customer did not name a category or product family, so
    the caller falls back to "dime cuál producto".
    """
    query = (intent.producto or "").strip()
    words = catalog.category_terms(query) if query else []
    if not words:
        words = catalog.category_terms(text)
    for word in words:
        if len(word) < 4:
            continue
        products = catalog.category_products(conn, word, limit=4)
        if not products:
            continue
        if intent.cantidad:
            # "quiero 3 laptops": la cantidad se conserva para cuando elija una.
            context["sale_qty"] = int(intent.cantidad)
            context["sale_qty_product_id"] = None
        guard_context = _guard_context(None, None, context, extra_products=products)
        reply = prompts.template_variant(
            "catalog_list",
            context,
            categoria=word,
            lista=_format_product_list(products),
        )
        reply, generator = _safe_template(
            conn, conversation, reply, guard_context, "template"
        )
        return reply, False, generator, None
    return None


def _guard_context(
    product: dict | None,
    candidates: list[dict] | None,
    context: dict,
    *,
    extra_products: list[dict] | None = None,
    product_source: str = "",
) -> dict:
    products = []
    for value in (product, *(candidates or []), *(extra_products or [])):
        if value is not None:
            products.append(value)
    return {
        "product": product,
        # H-09: a product inherited from the context must not be reused by the
        # deterministic fallback when this turn did not mention it.
        "product_from_turn": product_source in ("id", "intent", "text"),
        "products": products,
        "quoted_prices": context.get("quoted_prices") or [],
    }


_AFFIRMATIVE = re.compile(
    r"\b(s[ií]|dale|ok|okay|confirmo|confirmado|claro|de acuerdo|perfecto|"
    r"vamos|lo quiero|lo llevo|res[eé]rvamelo|res[eé]rvalo|pa'?lante)\b",
    re.IGNORECASE,
)
_AFFIRMATIVE_START = re.compile(
    r"^[\s¡!¿?.,]*(s[ií]|dale|ok|okay|confirmo|confirmado|claro|de acuerdo|"
    r"perfecto|vamos|lo quiero|lo llevo|res[eé]rvamelo|res[eé]rvalo|pa'?lante)\b",
    re.IGNORECASE,
)
_NEGATIVE = re.compile(
    r"\b(no|todav[ií]a no|ahora no|d[eé]jalo|lo pienso|despu[eé]s)\b", re.IGNORECASE
)
# Prueba #2 (F1): `_NEGATIVE` busca "no" en cualquier parte, así que "quiero 3
# no 1" o "¿no tiene otro modelo?" cancelaban el pedido. Solo una respuesta que
# *empieza* rechazando cancela la venta.
_DECLINE = re.compile(
    r"^\s*(?:no|nop|nel|todav[ií]a no|ahora no|d[eé]jalo|lo pienso|despu[eé]s)\b",
    re.IGNORECASE,
)
_SALE_STAGES = ("CAPTURE_DATA", "CONFIRM")

# H-12: a message that is *only* a greeting must start the conversation again;
# answering it with the product of the previous turn makes the bot look deaf.
_GREETING = re.compile(
    r"^[\s¡!¿?.,]*(?:"
    r"(?:hola|buenas(?:\s+(?:tardes|noches|d[ií]as))?|buenos\s+d[ií]as|"
    r"qu[eé]\s+tal|saludos|hey|hi|hello)"
    r"[\s¡!¿?.,]*)+$",
    re.IGNORECASE,
)

# Below this the NLU is guessing (the unknown-product case often lands here), so
# the engine must not inherit the product of the previous turn (H-09).
LOW_CONFIDENCE = 0.35

# F4 (prueba #2): "¿y no tiene otro tipo de laptop?" pide alternativas, no un
# dato del proveedor.
_ALTERNATIVES = re.compile(
    r"\b(?:otro|otra|otros|otras|otro\s+modelo|modelo|modelos|similar|"
    r"similares|opci[oó]n|opciones|variante|variantes|diferente|diferentes|"
    r"algo\s+m[aá]s)\b",
    re.IGNORECASE,
)

# F3 (prueba #2): el cliente pide explícitamente el catálogo de una categoría
# ("dame un listado de laptops"), aunque el contexto tenga un producto abierto.
_LIST_REQUEST = re.compile(
    r"\b(?:listado|lista|cat[aá]logo|opciones|cu[aá]les|"
    r"qu[eé]\s+\w+\s+tienes)\b",
    re.IGNORECASE,
)


def _is_greeting(text: str) -> bool:
    return bool(_GREETING.match((text or "").strip()))


def _asks_for_alternatives(text: str) -> bool:
    return bool(_ALTERNATIVES.search(text or ""))


def _confirms(text: str, intent=None) -> bool:
    """True when the customer accepts the order, not just mentions it.

    Prueba #2 (m15): "Sí, pero quiero 3 no 1" empieza afirmando y el "no" corrige
    la cantidad, no rechaza el pedido; sin esto la venta no se cerraba nunca.
    """
    body = text or ""
    if bool(_AFFIRMATIVE.search(body)) and not _NEGATIVE.search(body):
        return True
    return bool(
        intent is not None and intent.cantidad and _AFFIRMATIVE_START.match(body)
    )


def _is_decline(text: str) -> bool:
    """True only when the message opens by refusing ("no", "déjalo", ...)."""
    return bool(_DECLINE.match((text or "").strip()))


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


def _fast_template(
    conn, conversation, key: str, guard_context: dict, *, context=None, **values
):
    """Render a template with anti-repetition variants.

    ``context`` is the turn's context dict: without it the variant choice cannot
    be remembered (F5, prueba #2), so callers inside a turn must pass it.
    """
    if context is not None:
        text = prompts.template_variant(key, context, **values)
    else:
        text = prompts.TEMPLATES[key].format(**values)
    return _safe_template(conn, conversation, text, guard_context, "template")


def _sale_failure(
    conn, conversation, product: dict, context: dict, exc: Exception
) -> dict:
    message = str(exc)
    if "stock" in message:
        alternatives = catalog.alternatives(conn, product)
        guard = _guard_context(product, alternatives, context)
        reply, generator = _safe_template(
            conn, conversation, _render_out_of_stock(alternatives), guard, "template"
        )
        return {
            "reply": reply,
            "escalate": False,
            "generator": generator,
            "escalation_question": None,
            "stage": "CLOSE",
        }
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
                return _sale_failure(conn, conversation, product, context, exc)
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
            return _sale_failure(conn, conversation, product, context, exc)

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


def _stage_for(context: dict, intent, product: dict | None, escalate: bool) -> str:
    previous = str(context.get("stage") or "").upper()
    if escalate:
        return "HANDOFF"
    if product is None:
        # F1: un turno sin producto no reinicia una venta en curso. CAPTURE_DATA
        # y CONFIRM describen el pedido, no el último mensaje.
        return previous if previous in _SALE_STAGES else "DISCOVERY"
    if intent.intencion == "queja":
        return "OBJECTION"
    # Una pregunta sobre el mismo producto no saca el pedido de CONFIRM /
    # CAPTURE_DATA: el estado describe la venta, no el último mensaje (F1/F4).
    if (
        previous in _SALE_STAGES
        and intent.intencion != "comprar"
        and context.get("product_id") == product["id"]
    ):
        return previous
    if intent.intencion in ("consulta_precio", "consulta_stock", "comprar", "comparar"):
        return "QUOTE"
    if previous in ("CONFIRM", "CAPTURE_DATA", "DONE", "HANDOFF"):
        return previous
    return "PRODUCT_MATCH"


def _log_guardrail(conn, conversation, reason: str, stage: str) -> None:
    events.log(
        conn,
        "llm",
        "guardrail_violation",
        entity="conversations",
        entity_id=conversation["id"],
        details={"reason": reason, "stage": stage},
    )
    log.warning(
        "guardrail violation (%s/%s) in conversation %s",
        stage,
        reason,
        conversation["id"],
    )


def _safe_template(conn, conversation, reply: str, guard_context: dict, generator: str):
    ok, reason = guardrails.validate_reply(reply, guard_context)
    if ok:
        return reply, generator
    _log_guardrail(conn, conversation, reason, "template")
    return guardrails.fallback_reply(guard_context), "fallback"


async def _decide(
    conn,
    conversation,
    inbound: dict,
    intent,
    product: dict | None,
    context: dict,
    history: list[dict],
    *,
    last_text: str | None = None,
    product_source: str = "",
) -> tuple[str, bool, str, str | None]:
    """Returns (reply, escalate, generator, escalation_question)."""
    text = last_text if last_text is not None else (inbound["body"] or "")

    # H-12: a bare greeting starts over; answering it with the product remembered
    # from the previous turn ("Hola, buenas" -> Pan con Croqueta) looks deaf.
    # F7 (prueba #2): un saludo suelto recibe un saludo, no la plantilla de producto.
    if product_source in ("", "context") and _is_greeting(text):
        guard_context = _guard_context(None, None, context)
        reply = prompts.template_variant("greeting", context)
        reply, generator = _safe_template(
            conn,
            conversation,
            reply,
            guard_context,
            "template",
        )
        return reply, False, generator, None

    if intent.intencion in ("consulta_precio", "consulta_stock", "comprar"):
        if product is None:
            listing = _category_listing(conn, conversation, intent, context, text)
            if listing is not None:
                return listing
            guard_context = _guard_context(None, None, context)
            reply = prompts.template_variant("ask_which_product", context)
            reply, generator = _safe_template(
                conn,
                conversation,
                reply,
                guard_context,
                "template",
            )
            return reply, False, generator, None
        if product_source == "context" and _LIST_REQUEST.search(text or ""):
            # F3: "dame un listado de laptops" no se responde con el producto que
            # quedó abierto de un turno anterior.
            listing = _category_listing(conn, conversation, intent, context, text)
            if listing is not None:
                return listing
        if product["status"] != "ACTIVE" or (product.get("stock") or 0) <= 0:
            alternatives = catalog.alternatives(conn, product)
            guard_context = _guard_context(
                product,
                None,
                context,
                extra_products=alternatives,
                product_source=product_source,
            )
            reply, generator = _safe_template(
                conn,
                conversation,
                _render_out_of_stock(alternatives),
                guard_context,
                "template",
            )
            return reply, False, generator, None
        guard_context = _guard_context(
            product, None, context, product_source=product_source
        )
        reply = _render_greeting(conn, product, conversation, context)
        if intent.intencion == "comprar":
            canonical = catalog.llm_product_json(conn, product)
            reply += f" ¿Te reservo {canonical['nombre']} y coordinamos la entrega?"
        reply, generator = _safe_template(
            conn, conversation, reply, guard_context, "template"
        )
        return reply, False, generator, None

    if intent.intencion == "consulta_dato":
        datos = list(intent.datos_faltantes) if intent.datos_faltantes else []
        if product is None:
            # Nothing in the catalog matches what the customer asked about
            # (H-09): never escalate a question about an unknown product.
            guard_context = _guard_context(None, None, context)
            reply = prompts.template_variant("ask_which_product", context)
            reply, generator = _safe_template(
                conn,
                conversation,
                reply,
                guard_context,
                "template",
            )
            return reply, False, generator, None

        # Clasificar qué datos faltan: los del cliente se le piden a él;
        # solo los del proveedor se escalan al TCP.
        datos_cliente = [d for d in datos if _dato_pertenece_a(d) == "cliente"]
        datos_proveedor = [d for d in datos if _dato_pertenece_a(d) == "proveedor"]

        if datos_cliente and not datos_proveedor:
            # Solo faltan datos que el cliente conoce: no escalar, preguntarle.
            guard_context = _guard_context(
                product, None, context, product_source=product_source
            )
            faltante = " y ".join(datos_cliente)
            reply = prompts.template_variant("ask_client_data", context, dato=faltante)
            reply, generator = _safe_template(
                conn,
                conversation,
                reply,
                guard_context,
                "template",
            )
            return reply, False, generator, None

        if not datos_proveedor:
            # F4 (prueba #2): si el cliente pide otro modelo, listar alternativas
            # reales del mismo tipo en vez de preguntar "¿cuál producto?".
            if _asks_for_alternatives(text):
                alternatives = catalog.siblings(conn, product)
                guard_context = _guard_context(
                    product,
                    None,
                    context,
                    extra_products=alternatives,
                    product_source=product_source,
                )
                reply = _render_siblings(
                    conn, conversation, product, alternatives, context
                )
                reply, generator = _safe_template(
                    conn,
                    conversation,
                    reply,
                    guard_context,
                    "template",
                )
                return reply, False, generator, None
            # No hay datos del proveedor que pedir: preguntar por ese producto,
            # no por "cuál producto" cuando ya sabemos cuál es.
            guard_context = _guard_context(
                product, None, context, product_source=product_source
            )
            reply = prompts.template_variant(
                "ask_product_data", context, producto=product["name"]
            )
            reply, generator = _safe_template(
                conn,
                conversation,
                reply,
                guard_context,
                "template",
            )
            return reply, False, generator, None

        # Quedan datos del proveedor: escalar con una pregunta real, no el
        # nombre crudo del slot.
        guard_context = _guard_context(
            product, None, context, product_source=product_source
        )
        pregunta = (
            f"El cliente pregunta sobre {product['name']} y necesita saber: "
            + ", ".join(datos_proveedor)
            + ". ¿Me confirma ese dato para pasárselo?"
        )
        reply, generator = _safe_template(
            conn,
            conversation,
            prompts.TEMPLATES["missing_data_escalate"],
            guard_context,
            "template",
        )
        return reply, True, generator, pregunta

    # Ruta de queja determinista: léxico de insulto/molestia antes del LLM.
    _QUEJA_LEXICON = (
        "tonto", "estúpido", "estupido", "no sirve", "no entiendes", "feo",
        "basura", "inútil", "inutil", "molesto", "estafa", "fraude", "robo",
        "quiero hablar con un humano", "atención al cliente", "reclamo",
        "queja", "estás cansado", "estás lenta", "estás lento",
    )
    text_lower = (text or "").lower()
    is_queja = intent.intencion == "queja" or any(
        w in text_lower for w in _QUEJA_LEXICON
    )
    if is_queja:
        guard_context = _guard_context(product, None, context, product_source=product_source)
        # Contador de quejas consecutivas en contexto.
        qcount = int(context.get("queja_count", 0)) + 1
        context["queja_count"] = qcount
        conversations.set_context(conn, conversation["id"], context)
        extra_queja = (
            "El cliente está molesto o insultando. Disculpa brevemente, no te pongas a la defensiva, "
            "y vuelve al asunto comercial con empatía. Máximo 2 párrafos cortos."
        )
        messages, prompt_text = _build_base_messages(
            conn, conversation, history, context, product=product, turn_text=text
        )
        reply = await llm.compose(
            messages, prompt_text, extra=extra_queja, conversation_id=conversation["id"]
        )
        ok, reason = guardrails.validate_reply(reply, guard_context)
        if not ok:
            reply = guardrails.fallback_reply(guard_context)
            generator = "fallback"
        else:
            generator = "llm"
        return reply, False, generator, None

    # Contador de 'otro' consecutivos: rescate del bucle.
    if intent.intencion == "otro":
        otro_count = int(context.get("otro_count", 0)) + 1
        context["otro_count"] = otro_count
        conversations.set_context(conn, conversation["id"], context)
        if otro_count >= 3:
            # Derivar a operador: no seguir dando vueltas.
            guard_context = _guard_context(None, None, context)
            reply, generator = _safe_template(
                conn,
                conversation,
                prompts.template_variant("ask_which_product", context),
                guard_context,
                "template",
            )
            return reply, False, generator, None
    else:
        # Resetear el contador si el intent cambia de 'otro'.
        if context.get("otro_count", 0) > 0:
            context["otro_count"] = 0
            conversations.set_context(conn, conversation["id"], context)

    # comparar / queja / otro / low confidence -> careful free generation
    candidates = None
    if intent.intencion == "comparar":
        candidates = _comparison_products(conn, text, intent, product)
    messages, prompt_text = _build_base_messages(
        conn,
        conversation,
        history,
        context,
        product=product,
        candidates=candidates,
        turn_text=text,
    )
    extra = ""
    if candidates:
        extra = "El cliente compara productos: usa solo los PRODUCTOS CANDIDATOS que tienes."
    guard_context = _guard_context(
        product, candidates, context, product_source=product_source
    )

    generator = "llm"
    reply = await llm.compose(
        messages, prompt_text, extra=extra, conversation_id=conversation["id"]
    )
    ok, reason = guardrails.validate_reply(reply, guard_context)
    if not ok:
        _log_guardrail(conn, conversation, reason, "retry")
        correction = "\n".join(
            part for part in (extra, prompts.GUARDRAIL_CORRECTION) if part
        )
        reply = await llm.compose(
            messages,
            prompt_text,
            extra=correction,
            conversation_id=conversation["id"],
        )
        ok, reason = guardrails.validate_reply(reply, guard_context)
        if not ok:
            _log_guardrail(conn, conversation, reason, "fallback")
            reply = guardrails.fallback_reply(guard_context)
            generator = "fallback"
    return reply, False, generator, None


def _update_context(
    conn,
    conversation,
    intent,
    product,
    generator: str,
    escalate: bool,
    *,
    stage: str | None = None,
    sale: dict | None = None,
    sale_qty: int | None = None,
    context: dict | None = None,
) -> None:
    # F5 (prueba #2): guardar el mismo dict que mutó el turno. Recargarlo desde
    # la BD borraba `last_template_*` y las variantes nunca rotaban.
    if context is None:
        context = conversations.get_context(conn, conversation["id"])
    context["conversation_id"] = conversation["id"]
    if product is not None:
        context["product_id"] = product["id"]
    context["last_intent"] = intent.intencion
    context["last_generator"] = generator
    context["pending_escalation"] = bool(escalate)
    if stage:
        context["stage"] = stage
    if sale is not None:
        context["sale_id"] = sale["id"]
        context["order_code"] = sale["order_code"]
        # Una venta nueva cierra el bucle de 'otro' y de quejas.
        context["otro_count"] = 0
        context["queja_count"] = 0
        # La cantidad acordada ya vive en la venta: no debe reaparecer en otra.
        context.pop("sale_qty", None)
        context.pop("sale_qty_product_id", None)
    elif sale_qty is not None:
        context["sale_qty"] = int(sale_qty)
        context["sale_qty_product_id"] = product["id"] if product is not None else None
    conversations.set_context(conn, conversation["id"], context)


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
    """Run the full pipeline once for a consolidated customer turn."""
    started = time.perf_counter()
    intent_ms = 0
    compose_ms = 0
    turn_messages = _turn_messages(conn, turn)
    turn_ids = {message["id"] for message in turn_messages}
    last_message_id = (
        turn_messages[-1]["id"] if turn_messages else turn.get("last_message_id")
    )
    text = _turn_text(turn_messages) or "[mensaje sin texto]"

    # Trace: entrada del turno
    tracing.trace_call(
        "pipeline.handle_turn",
        {
            "turn_id": turn.get("id"),
            "conversation_id": conversation["id"],
            "text": text,
            "message_count": len(turn_messages),
        },
        None,  # output se llena al final
        ok=True,
        conversation_id=conversation["id"],
        turn_id=turn.get("id"),
    )

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

    current_product, _current_source = _resolve_product(
        conn, llm.Intent(intencion="otro"), context, text
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

    intent_started = time.perf_counter()
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

    product, product_source = _resolve_product(conn, intent, context, text)
    # H-09: a low-confidence read must not inherit the previous product. An
    # unknown product ("¿Tienes neveras?") would otherwise be answered or
    # escalated about the monitor of the previous turn. An in-progress sale is
    # exempt: there the context product is the one being purchased.
    stage_hint = str(context.get("stage") or "").upper()
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

    sale = None
    sale_qty = None
    compose_started = time.perf_counter()
    sale_outcome = _maybe_handle_sale(conn, conversation, text, intent, product, context)
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
        )
        stage = _stage_for(context, intent, product, escalate)
    compose_ms = int((time.perf_counter() - compose_started) * 1000)

    supplier_id = product["supplier_id"] if (escalate and product) else None

    _update_context(
        conn,
        conversation,
        intent,
        product,
        generator,
        escalate,
        stage=stage,
        sale=sale,
        sale_qty=sale_qty,
        context=context,
    )
    # Trace: salida completa del turno
    tracing.trace_call(
        "pipeline.handle_turn",
        {
            "turn_id": turn.get("id"),
            "conversation_id": conversation["id"],
            "text": text,
            "message_count": len(turn_messages),
        },
        {
            "reply": reply[:500] if reply else None,
            "intent": intent.intencion,
            "confianza": intent.confianza,
            "generator": generator,
            "stage": stage,
            "sale_id": sale["id"] if sale else None,
            "sale_status": sale["status"] if sale else None,
            "escalate": escalate,
            "latency_ms": int((time.perf_counter() - started) * 1000),
            "intent_ms": intent_ms,
            "compose_ms": compose_ms,
        },
        ok=True,
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


async def handle_incoming(conn, conversation, inbound: dict) -> dict:
    """Backwards-compatible single-message entry point (legacy jobs/debug)."""
    turn = {
        "id": None,
        "conversation_id": conversation["id"],
        "last_message_id": inbound["id"],
        "message_ids_json": json.dumps([inbound["id"]]),
    }
    return await handle_turn(conn, conversation, turn)


async def maybe_summarize(conn, conversation) -> bool:
    """Backwards-compatible wrapper around the phase 2 memory module."""
    return await memory.maybe_summarize(conn, conversation)
