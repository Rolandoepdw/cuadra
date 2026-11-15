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


def _render_greeting(conn, product: dict, conversation: dict) -> str:
    info = catalog.llm_product_json(conn, product)
    zona, zona_clause = _reply_zone(conn, conversation, info)
    policy = settings.transport_policy(conn)
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
_NEGATIVE = re.compile(
    r"\b(no|todav[ií]a no|ahora no|d[eé]jalo|lo pienso|despu[eé]s)\b", re.IGNORECASE
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


def _is_greeting(text: str) -> bool:
    return bool(_GREETING.match((text or "").strip()))


def _is_affirmative(text: str) -> bool:
    body = text or ""
    return bool(_AFFIRMATIVE.search(body)) and not _NEGATIVE.search(body)


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


def _sale_guard(product: dict, total: float) -> dict:
    """Guard context whose only price is the sale total (no internal data)."""
    safe = {**product, "price": round(total, 2)}
    return {"product": safe, "products": [safe], "quoted_prices": []}


def _fast_template(conn, conversation, key: str, guard_context: dict, **values):
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


def _maybe_handle_sale(
    conn, conversation, text: str, intent, product: dict | None, context: dict
) -> dict | None:
    """Advance the sale state machine when a purchase is in play.

    Returns a reply outcome (same shape as `_decide` plus `stage`/`sale`) or
    None when the turn is not part of a sale.
    """
    stage = str(context.get("stage") or "DISCOVERY").upper()
    if product is None:
        return None
    if intent.intencion != "comprar" and stage not in _SALE_STAGES:
        return None

    facts = memory.facts_map(conn, conversation["id"])
    slots = _sale_slots(conn, conversation, facts)
    missing = _missing_slots(slots)
    guard = _sale_guard(product, float(product["price"]))

    # Cantidad solicitada: viene del NLU (ej. "dos tablets" -> 2) o default 1.
    qty = int(intent.cantidad) if intent.cantidad else 1

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
            order_code=existing["order_code"],
        )
        return {
            "reply": reply,
            "escalate": False,
            "generator": generator,
            "escalation_question": None,
            "stage": "DONE",
            "sale": existing,
        }

    if not missing and _is_affirmative(text):
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
            order_code=sale["order_code"],
        )
        return {
            "reply": reply,
            "escalate": False,
            "generator": generator,
            "escalation_question": None,
            "stage": "DONE",
            "sale": sale,
        }

    if missing:
        reply, generator = _fast_template(
            conn,
            conversation,
            "sale_ask_data",
            guard,
            faltan=", ".join(missing),
        )
        return {
            "reply": reply,
            "escalate": False,
            "generator": generator,
            "escalation_question": None,
            "stage": "CAPTURE_DATA",
        }

    if stage == "CONFIRM" and _NEGATIVE.search(text or ""):
        reply, generator = _fast_template(
            conn, conversation, "sale_declined", guard
        )
        return {
            "reply": reply,
            "escalate": False,
            "generator": generator,
            "escalation_question": None,
            "stage": "CLOSE",
        }

    total = round(float(product["price"]) * qty, 2)
    guard = _sale_guard(product, total)
    reply, generator = _fast_template(
        conn,
        conversation,
        "sale_confirm",
        guard,
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
        "qty_solicitada": qty,
    }


def _stage_for(context: dict, intent, product: dict | None, escalate: bool) -> str:
    if escalate:
        return "HANDOFF"
    if product is None:
        return "DISCOVERY"
    if intent.intencion == "queja":
        return "OBJECTION"
    if intent.intencion in ("consulta_precio", "consulta_stock", "comprar", "comparar"):
        return "QUOTE"
    previous = str(context.get("stage") or "").upper()
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
    if product_source in ("", "context") and _is_greeting(text):
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

    if intent.intencion in ("consulta_precio", "consulta_stock", "comprar"):
        if product is None:
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
        reply = _render_greeting(conn, product, conversation)
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
            # No hay datos del proveedor que pedir: no escalar, pedir aclaración.
            guard_context = _guard_context(
                product, None, context, product_source=product_source
            )
            reply, generator = _safe_template(
                conn,
                conversation,
                prompts.TEMPLATES["ask_which_product"],
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
                prompts.TEMPLATES["ask_which_product"],
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
) -> None:
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
    compose_started = time.perf_counter()
    sale_outcome = _maybe_handle_sale(conn, conversation, text, intent, product, context)
    if sale_outcome is not None:
        reply = sale_outcome["reply"]
        escalate = sale_outcome["escalate"]
        generator = sale_outcome["generator"]
        escalation_question = sale_outcome["escalation_question"]
        stage = sale_outcome["stage"]
        sale = sale_outcome.get("sale")
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
