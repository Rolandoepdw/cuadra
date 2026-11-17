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
from datetime import datetime, timezone
from typing import Callable

from cuadra import config
from cuadra.ai import guardrails, llm, memory, offers, prompts, retrieval, tracing
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


def _missing_spec_topics(product: dict, text: str) -> list[str]:
    """Temas técnicos que el cliente pregunta y el catálogo no puede responder.

    Prueba #3 (F1): con las especificaciones vacías el compositor inventó que la
    GPU era un microprocesador. Si el dato no está en `specifications`, se
    escala al proveedor; el modelo no compone una respuesta técnica a ciegas.
    """
    specs = product.get("specifications") or {}
    corpus = " ".join(f"{key} {value}" for key, value in specs.items())
    missing = []
    for topic, pattern in guardrails.SPEC_TOPICS.items():
        if not pattern.search(text or ""):
            continue
        if pattern.search(corpus):
            continue
        missing.append(topic)
    return missing


def _canonical_topic(dato: str) -> str:
    """Normalize a missing-data label to the catalog's spec topic (F15).

    The NLU can report "microprocesador" and "procesador" for the same
    question; the escalation must ask the supplier once, not twice.
    """
    value = (dato or "").strip()
    for topic, pattern in guardrails.SPEC_TOPICS.items():
        if pattern.search(value):
            return topic
    return value


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        key = catalog._fold(value).strip()
        if key and key not in seen:
            seen.add(key)
            out.append(value)
    return out


def _available_stock(product: dict) -> int:
    return max(
        0,
        int(product.get("stock") or 0) - int(product.get("stock_reserved") or 0),
    )


# F13 (prueba #4): no saludar en cada respuesta. El saludo se reserva para el
# inicio de la conversación o para cuando el cliente vuelve tras un hueco.
_GREETING_COOLDOWN_S = 600


def _mark_greeting(context: dict) -> None:
    context["last_greeting_at"] = datetime.now(timezone.utc).isoformat()


def _should_greet(context: dict) -> bool:
    last = context.get("last_greeting_at")
    if not last:
        return True
    try:
        when = datetime.fromisoformat(str(last))
    except (TypeError, ValueError):
        return True
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - when).total_seconds() >= _GREETING_COOLDOWN_S


def _with_greeting(text: str, context: dict) -> str:
    """Prefix a greeting only when the conversation has not greeted recently."""
    if not _should_greet(context):
        return text
    _mark_greeting(context)
    return f"¡Buenas! {text}"


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
    reply = prompts.template_variant(
        "greeting_product",
        context,
        producto=info["nombre"],
        precio=f"{info['precio']:g}",
        moneda=info["moneda"],
        transporte_clause=prompts.TRANSPORT_CLAUSES[policy],
        zona=zona,
        zona_clause=zona_clause,
    )
    return _with_greeting(reply, context)


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


def _render_out_of_stock(product: dict, alternatives: list[dict]) -> str:
    name = (product or {}).get("name") or "ese producto"
    if not alternatives:
        return (
            f"Por ahora no me queda ninguna unidad de {name}. "
            "¿Te aviso cuando entre o te busco otra opción?"
        )
    first = alternatives[0]
    if len(alternatives) == 1:
        return (
            f"Por ahora no me queda ninguna unidad de {name}. Tengo "
            f"{first['name']} en {first['price']:g} {first['currency']}. "
            "¿Te interesa?"
        )
    second = alternatives[1]
    return (
        f"Por ahora no me queda ninguna unidad de {name}. Tengo "
        f"{first['name']} en {first['price']:g} {first['currency']} y "
        f"{second['name']} en {second['price']:g} {second['currency']}. "
        "¿Te interesa alguna?"
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


def _single_category_product(conn, intent, text: str) -> dict | None:
    """Resolve "laptops gaming" to the one product its qualifier names (F13).

    `find_product` cannot match the plural category plus a qualifier, so the
    stock question fell back to listing every laptop. Here the category is
    listed and the remaining words narrow it; only an unambiguous result wins.
    """
    query = (intent.producto or "").strip() or (text or "")
    for word in catalog.category_terms(query):
        if len(word) < 4:
            continue
        products = catalog.category_products(conn, word, limit=6)
        if not products:
            continue
        narrowed = catalog.narrow_products(products, query, exclude=word)
        return narrowed[0] if len(narrowed) == 1 else None
    return None


def _stock_outcome(
    conn, conversation, product: dict, context: dict, product_source: str
) -> tuple[str, bool, str, None]:
    """F14 (prueba #4): answer how many units are actually available."""
    available = _available_stock(product)
    if available <= 0:
        alternatives = catalog.siblings(conn, product)
        guard = _guard_context(
            product,
            None,
            context,
            extra_products=alternatives,
            product_source=product_source,
        )
        reply, generator = _safe_template(
            conn,
            conversation,
            _render_out_of_stock(product, alternatives),
            guard,
            "template",
        )
        return reply, False, generator, None
    guard = _guard_context(product, None, context, product_source=product_source)
    info = catalog.llm_product_json(conn, product)
    if available == 1:
        reply = prompts.template_variant(
            "stock_available_one",
            context,
            producto=info["nombre"],
            precio=f"{info['precio']:g}",
            moneda=info["moneda"],
        )
    else:
        reply = prompts.template_variant(
            "stock_available",
            context,
            producto=info["nombre"],
            cantidad=available,
            precio=f"{info['precio']:g}",
            moneda=info["moneda"],
        )
    reply, generator = _safe_template(conn, conversation, reply, guard, "template")
    return reply, False, generator, None


def _guard_product(product: dict | None) -> dict | None:
    """Expose parsed `specifications` to the guardrails.

    `get_product` parses `specifications_json`, but the rows returned by
    `search_products`/`find_product` only carry the raw column. Without this
    normalization `guardrails.check_specs` saw no specifications at all and let
    the composer invent them (prueba #4, "16 GB de RAM y 1 TB de SSD").
    """
    if product is None or isinstance(product.get("specifications"), dict):
        return product
    raw = product.get("specifications_json")
    if raw is None:
        return product
    try:
        parsed = json.loads(raw or "{}")
    except (TypeError, ValueError):
        parsed = {}
    normalized = dict(product)
    normalized["specifications"] = parsed if isinstance(parsed, dict) else {}
    return normalized


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
        value = _guard_product(value)
        if value is not None:
            products.append(value)
    product = _guard_product(product)
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
    r"(?:hola|buen[ao]s?(?:\s+(?:tardes|noches|d[ií]as))?|"
    r"bue(?:ns|nas|as)(?:\s+(?:tardes|noches|d[ií]as))?|"
    r"buenos\s+d[ií]as|qu[eé]\s+tal|saludos|hey|hi|hello)"
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
    r"algo\s+m[aá]s|ning[uú]n\s+(?:tipo|modelo|otro)|no\s+sea|"
    r"m[aá]s\s+(?:tipo|modelo|opci[oó]n))\b",
    re.IGNORECASE,
)

# F3 (prueba #2): el cliente pide explícitamente el catálogo de una categoría
# ("dame un listado de laptops"), aunque el contexto tenga un producto abierto.
_LIST_REQUEST = re.compile(
    r"\b(?:listado|lista|cat[aá]logo|opciones|ofertas?|cu[aá]les|"
    r"mu[eé]strame|ens[eé]ñame|"
    r"qu[eé]\s+(?:\w+\s+){0,3}(?:tienes|hay|venden|manejas|manejan))\b",
    re.IGNORECASE,
)

# F14 (prueba #4): preguntas de cantidad. "cuánto cuesta" no cuenta como stock.
_STOCK_QUESTION = re.compile(
    r"\b(?:cu[aá]nt[ao]s?\s+(?:unidades|te\s+quedan|quedan|tienes|hay|"
    r"puedo|me\s+puedes|disponibles)|cantidad|stock|disponibles?|"
    r"te\s+quedan?|quedan?\s+unidades)\b",
    re.IGNORECASE,
)

# F12 (prueba #4): mensajes banales. Solo cuentan cuando TODO el mensaje es
# banal; "gracias, ¿y el precio?" sigue siendo una consulta.
_BANAL_WORDS = {
    "gracias", "grax", "muchas", "mil", "agradezco", "agradecido",
    "agradecida", "disculpa", "disculpas", "perdon", "perdone", "disculpe",
    "lamento", "siento", "lo", "ok", "okay", "okey", "vale", "de", "acuerdo",
    "perfecto", "listo", "entendido", "genial", "excelente", "bien", "bueno",
    "buena", "entonces", "pues", "ya", "claro", "no", "hay", "problema",
    "tranquilo", "tranquila", "nada", "igual", "saludos", "cordiales", "que",
    "tengas", "buen", "dia", "dias", "tarde", "tardes", "noche", "noches",
    "por", "todo", "muy", "amable", "atento", "atenta",
}
_BANAL_THANKS = {"gracias", "grax", "agradezco", "agradecido", "agradecida", "amable"}
_BANAL_SORRY = {
    "disculpa", "disculpas", "perdon", "perdone", "disculpe", "lamento", "siento",
}
_BANAL_ACK = {
    "ok", "okay", "okey", "vale", "acuerdo", "perfecto", "listo", "entendido",
    "genial", "excelente", "bien", "tranquilo", "tranquila", "nada", "problema",
}

# F6 (prueba #3): preguntas de logística. La fecha depende del pedido, así que
# antes de una venta despachada no se escalan ni se promete un plazo.
_LOGISTICS_QUESTION = re.compile(
    r"\b(?:fecha\s+de\s+entrega|d[ií]a\s+de\s+entrega|plazo\s+de\s+entrega|"
    r"tiempo\s+de\s+entrega|cu[aá]ndo\s+(?:llega|llegan|me\s+la\s+traes|"
    r"me\s+lo\s+traes|la\s+traes|lo\s+traes|me\s+llega|lo\s+recibo)|"
    r"cu[aá]nto\s+tarda|demora|hacen\s+env[ií]o|tienen\s+env[ií]o|"
    r"me\s+la\s+traes|me\s+lo\s+traes)\b",
    re.IGNORECASE,
)

# F8 (prueba #3): mensajes que en la etapa DONE se responden con el estado del
# pedido. Una pregunta técnica real sigue su flujo normal.
_POST_SALE_HINTS = re.compile(
    r"\b(?:gracias|vale|ok|okay|perfecto|listo|de\s+acuerdo|pedido|orden|"
    r"entreg\w*|lleg\w*|demora|tarda|cu[aá]ndo|d[oó]nde)\b",
    re.IGNORECASE,
)


def _is_greeting(text: str) -> bool:
    return bool(_GREETING.match((text or "").strip()))


def _banal_kind(text: str) -> str | None:
    """Return ``thanks``/``sorry``/``ack`` only when the whole message is banal.

    F12 (prueba #4): "gracias" was classified as a purchase and repeated the
    order confirmation. A banal message gets a banal reply; a message that also
    asks something ("gracias, ¿y el precio?") keeps its normal route.
    """
    body = (text or "").strip()
    if not body:
        return None
    cleaned = "".join(c if c.isalnum() else " " for c in catalog._fold(body))
    words = cleaned.split()
    if not words or len(words) > 8:
        return None
    if any(word not in _BANAL_WORDS for word in words):
        return None
    if any(word in _BANAL_THANKS for word in words):
        return "thanks"
    if any(word in _BANAL_SORRY for word in words):
        return "sorry"
    if any(word in _BANAL_ACK for word in words):
        return "ack"
    return None


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


def _zone_coverage_outcome(
    conn, conversation, text: str, product: dict | None, context: dict
) -> dict | None:
    """F9 (prueba #3): reject an uncovered zone in the turn it is mentioned.

    Before this, "Soy de la UCI" was ignored (the zone was inactive) and the bot
    kept quoting the product until the sale failed asking for the address. The
    check uses inactive zones too: mentioning one is enough to answer honestly.
    """
    if product is None:
        return None
    zone = catalog.resolve_zone(conn, text or "", active_only=False)
    if zone is None:
        return None
    covered = bool(zone.get("active")) and sales_domain.supplier_covers_zone(
        conn, product["supplier_id"], int(zone["id"])
    )
    if covered:
        return None
    guard = _guard_context(product, None, context, product_source="text")
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


def _stage_for(context: dict, intent, product: dict | None, escalate: bool) -> str:
    previous = str(context.get("stage") or "").upper()
    if escalate:
        return "HANDOFF"
    if product is None:
        # F1: un turno sin producto no reinicia una venta en curso. CAPTURE_DATA
        # y CONFIRM describen el pedido, no el último mensaje.
        return previous if previous in (*_SALE_STAGES, "DONE") else "DISCOVERY"
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


async def _compose_open(
    conn,
    conversation,
    history: list[dict],
    context: dict,
    product: dict | None,
    text: str,
    *,
    extra: str = "",
    candidates: list[dict] | None = None,
    guard_context: dict | None = None,
    fallback: Callable[[], tuple[str, str]] | None = None,
) -> tuple[str, str]:
    """Compose a free-form reply for an open question (F10, prueba #2).

    Plantillas para los pasos transaccionales; el compositor solo para lo que
    una plantilla no puede contestar (un detalle del producto, una queja, una
    comparación, un tema abierto). Los guardrails corren en cada intento y, si
    el modelo no está disponible o insiste en violarlos, responde el fallback
    determinista en vez de dejar al cliente sin respuesta.
    """
    messages, prompt_text = _build_base_messages(
        conn,
        conversation,
        history,
        context,
        product=product,
        candidates=candidates,
        turn_text=text,
    )
    guard_context = guard_context or _guard_context(product, candidates, context)

    def _fallback() -> tuple[str, str]:
        # Se construye solo si hace falta: una plantilla de reserva no debe
        # consumir una variante ni dejar rastro en el contexto del turno.
        if fallback is not None:
            return fallback()
        return guardrails.fallback_reply(guard_context), "fallback"

    try:
        reply = await llm.compose(
            messages, prompt_text, extra=extra, conversation_id=conversation["id"]
        )
    except llm.LLMUnavailable:
        log.warning("compose unavailable; using deterministic reply")
        return _fallback()

    ok, reason = guardrails.validate_reply(reply, guard_context)
    if not ok:
        _log_guardrail(conn, conversation, reason, "retry")
        correction = "\n".join(
            part for part in (extra, prompts.GUARDRAIL_CORRECTION) if part
        )
        try:
            reply = await llm.compose(
                messages,
                prompt_text,
                extra=correction,
                conversation_id=conversation["id"],
            )
        except llm.LLMUnavailable:
            log.warning("compose unavailable on retry; using deterministic reply")
            return _fallback()
        ok, reason = guardrails.validate_reply(reply, guard_context)
        if not ok:
            _log_guardrail(conn, conversation, reason, "fallback")
            return _fallback()
    return reply, "llm"


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
    # F13 (prueba #4): dentro del cooldown no se repite el saludo; se pregunta en
    # qué se puede ayudar.
    if product_source in ("", "context") and _is_greeting(text):
        guard_context = _guard_context(None, None, context)
        if _should_greet(context):
            reply = prompts.template_variant("greeting", context)
            _mark_greeting(context)
        else:
            reply = prompts.template_variant("greeting_again", context)
        reply, generator = _safe_template(
            conn,
            conversation,
            reply,
            guard_context,
            "template",
        )
        return reply, False, generator, None

    if intent.intencion in ("consulta_precio", "consulta_stock", "comprar"):
        # F13 (prueba #4): "laptops gaming" no es un nombre exacto; la categoría
        # más el calificador resuelven el producto antes de responder stock.
        if product is None and _STOCK_QUESTION.search(text or ""):
            candidate = _single_category_product(conn, intent, text)
            if candidate is not None:
                product, product_source = candidate, "text"
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
        # F14 (prueba #4): una pregunta de cantidad se responde con el stock real.
        if _STOCK_QUESTION.search(text or ""):
            return _stock_outcome(
                conn, conversation, product, context, product_source
            )
        # F4 (prueba #2): "¿no tiene otro tipo de laptop?" pide alternativas.
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
                conn, conversation, reply, guard_context, "template"
            )
            return reply, False, generator, None
        if product_source != "id" and _LIST_REQUEST.search(text or ""):
            # F3: "dame un listado de laptops" no se responde con el producto que
            # quedó abierto de un turno anterior.
            listing = _category_listing(conn, conversation, intent, context, text)
            if listing is not None:
                return listing
        if product["status"] != "ACTIVE" or _available_stock(product) <= 0:
            alternatives = catalog.siblings(conn, product)
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
                _render_out_of_stock(product, alternatives),
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
        # F15 (prueba #4): "microprocesador" y "procesador" son el mismo dato.
        datos_proveedor = _dedupe([_canonical_topic(d) for d in datos_proveedor])
        # F1 (prueba #3): un tema técnico que el catálogo no tiene se escala,
        # no se compone. El NLU puede no listarlo, así que se detecta también
        # por léxico antes de llamar al modelo.
        for topic in _missing_spec_topics(product, text):
            if topic not in datos_proveedor:
                datos_proveedor.append(topic)

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
            # F10 (prueba #2): el cliente ya preguntó un detalle concreto; la
            # plantilla "¿qué dato necesitas?" repite la pregunta y suena a
            # robot. Componer con los datos reales del producto y caer en la
            # plantilla solo si el modelo falla o viola los guardrails.
            guard_context = _guard_context(
                product, None, context, product_source=product_source
            )
            reply, generator = await _compose_open(
                conn,
                conversation,
                history,
                context,
                product,
                text,
                extra=(
                    "El cliente pregunta un detalle del producto. Respóndele con "
                    "la descripción y las especificaciones del PRODUCTO EN JUEGO. "
                    "Si el dato no está ahí, dilo y ofrece verificarlo con el "
                    "almacén; nunca lo inventes. No repitas el precio si no lo pidió."
                ),
                guard_context=guard_context,
                fallback=lambda: _safe_template(
                    conn,
                    conversation,
                    prompts.template_variant(
                        "ask_product_data", context, producto=product["name"]
                    ),
                    guard_context,
                    "template",
                ),
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
        reply, generator = await _compose_open(
            conn,
            conversation,
            history,
            context,
            product,
            text,
            extra=extra_queja,
            guard_context=guard_context,
        )
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

    # comparar / otro / low confidence -> careful free generation (F10)
    candidates = None
    if intent.intencion == "comparar":
        candidates = _comparison_products(conn, text, intent, product)
    extra = ""
    if candidates:
        extra = "El cliente compara productos: usa solo los PRODUCTOS CANDIDATOS que tienes."
    guard_context = _guard_context(
        product, candidates, context, product_source=product_source
    )
    reply, generator = await _compose_open(
        conn,
        conversation,
        history,
        context,
        product,
        text,
        extra=extra,
        candidates=candidates,
        guard_context=guard_context,
    )
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
        result = await _run_turn(conn, conversation, turn)
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
    return result


async def _run_turn(conn, conversation, turn: dict) -> dict:
    """Build the reply for one consolidated turn (traced by `handle_turn`)."""
    started = time.perf_counter()
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
        )
        stage = _stage_for(context, intent, product, escalate)
    compose_ms = int((time.perf_counter() - compose_started) * 1000)

    supplier_id = product["supplier_id"] if (escalate and product) else None

    # La oferta de este turno queda disponible solo para el siguiente.
    offers.save(context, offers.detect(reply, product["id"] if product else None))

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
