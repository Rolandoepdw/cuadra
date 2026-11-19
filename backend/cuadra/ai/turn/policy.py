"""Deterministic turn policy: routing, stock and stage decisions."""

import logging
import re
from dataclasses import dataclass, field

from cuadra.ai import guardrails, offers, prompts
from cuadra.domain import catalog, conversations

log = logging.getLogger("cuadra.policy")

from cuadra.ai.turn.constants import (
    _AFFIRMATIVE,    _AFFIRMATIVE_START,
    _ALTERNATIVES,
    _BANAL_ACK,
    _BANAL_SORRY,
    _BANAL_THANKS,
    _BANAL_WORDS,
    _CATEGORY_FILLER,
    _COMPARE_SPLIT,
    _DATO_CLIENTE,
    _DECLINE,
    _GREETING,
    _LIST_REQUEST,
    _NEGATIVE,
    _OPEN_DETAILS,
    _SALE_STAGES,
    _STOCK_QUESTION,
    _TABLE_LIMIT,
)

from cuadra.ai.tools.base import ToolCall

from cuadra.ai.turn.composer import (
    _compose_open,
)

from cuadra.ai.turn.guards import (
    _guard_context,
    _safe_template,
)

from cuadra.ai.turn.references import (
    _retrieve_candidates,
)

from cuadra.ai.turn.render import (
    _available_stock,
    _canonical_topic,
    _category_listing,
    _detail_entries,
    _is_generic_detail_label,
    _mark_greeting,
    _missing_spec_topics,
    _render_catalog_table,
    _render_details,
    _render_out_of_stock,
    _render_product_card,
    _render_siblings,
    _should_greet,
)


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


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        key = catalog._fold(value).strip()
        if key and key not in seen:
            seen.add(key)
            out.append(value)
    return out


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


def _category_request(conn, intent, text: str) -> tuple[str, object] | None:
    """Resolve a message that asks about a product family.

    Returns:
      - ``("list", (term, products))`` when the customer asked for the family
        and did not narrow it to a single product;
      - ``("product", product)`` when a qualifier names exactly one product, or
        when the family itself has only one;
      - ``None`` when the message is not a catalog-family request.

    The customer's own words are tried first: the NLU can echo a concrete
    product in a question that is really about the whole family.
    """
    if offers.answer(text) is not None:
        # Un "si"/"no" suelto responde a la pregunta del turno anterior (la
        # oferta), no pide una familia. El NLU repite el producto de la ficha y
        # sin esto "si" volvía a listar la categoría (prueba #7).
        return None
    if _asks_for_alternatives(text):
        # "¿no tienes otro tipo de laptop?" pide alternativas, no la familia.
        return None
    folded = catalog._fold(text or "")
    words = [w for w in re.split(r"[^a-z0-9]+", folded) if len(w) > 1]
    content = [w for w in words if w not in _CATEGORY_FILLER]
    if not content:
        return None
    terms = [t for t in catalog.category_terms(text) if len(t) >= 4]
    terms += [
        t
        for t in catalog.category_terms(intent.producto or "")
        if len(t) >= 4 and t not in terms
    ]
    for term in terms:
        if term in _CATEGORY_FILLER:
            continue
        products = catalog.family_products(conn, term, limit=_TABLE_LIMIT)
        if not products:
            continue
        singular = term[:-1] if term.endswith("s") else term
        # Solo las palabras que `narrow_products` puede aplicar cuentan como
        # calificador: "si" o "ok" no acotan la familia.
        rest = [
            w for w in content if w not in (term, singular) and len(w) >= 4
        ]
        if rest:
            narrowed = catalog.narrow_products(
                products, " ".join(rest), exclude=term, fallback=False
            )
            if len(narrowed) == 1:
                return "product", narrowed[0]
            return "list", (term, products)
        if len(products) == 1:
            return "product", products[0]
        return "list", (term, products)
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


def _asks_for_open_details(text: str) -> bool:
    return bool(_OPEN_DETAILS.search(text or ""))


def _catalog_request(text: str) -> bool:
    """True when the message asks for products, not for a datum (prueba #8).

    "Qué productos tienes de accesorios para laptop" is a catalog listing even
    when the NLU labels it `consulta_dato`; "¿qué procesador trae la laptop?"
    asks for one specification and must stay on the product-detail path.
    """
    body = text or ""
    if not _LIST_REQUEST.search(body):
        return False
    if _asks_for_open_details(body) or _STOCK_QUESTION.search(body):
        return False
    # Un tema técnico concreto ("procesador", "RAM", "garantía") es una pregunta
    # de detalle, aunque el mensaje empiece con "qué ... tienes".
    return not _asked_detail_topics(body, [])


def _asked_detail_topics(text: str, labels: list[str]) -> set[str]:
    """Temas técnicos concretos que pidió el cliente (prueba #5).

    Combina las etiquetas del NLU ("procesador", "microprocesador") con el
    léxico del propio mensaje, para responder desde la ficha sin llamar al
    compositor cuando el dato ya está en el catálogo.
    """
    topics: set[str] = set()
    for label in labels:
        topic = _canonical_topic(label)
        if topic in guardrails.SPEC_TOPICS:
            topics.add(topic)
    for topic, pattern in guardrails.SPEC_TOPICS.items():
        if pattern.search(text or ""):
            topics.add(topic)
    return topics


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
    category_request: tuple[str, object] | None = None,
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
        # Una petición de familia ("laptop", "ando buscando una laptop") se lista
        # en tabla; nunca se cotiza ni se vende un producto al azar. Si un
        # calificador deja un solo producto, se responde directo con su ficha.
        category = _category_request(conn, intent, text)
        if category is not None:
            kind, payload = category
            if kind == "list":
                term, products = payload
                return _render_catalog_table(
                    conn,
                    conversation,
                    context,
                    term,
                    products,
                    quantity=intent.cantidad,
                )
            product, product_source = payload, "text"
            if intent.cantidad:
                # "Quiero 3 laptops" con una sola laptop: la cantidad se
                # conserva igual que en el listado en tabla.
                context["sale_qty"] = int(intent.cantidad)
                context["sale_qty_product_id"] = None
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
        reply = _render_product_card(
            conn,
            product,
            conversation,
            buy=intent.intencion == "comprar",
            context=context,
        )
        reply, generator = _safe_template(
            conn, conversation, reply, guard_context, "template", card=True
        )
        return reply, False, generator, None

    if intent.intencion == "consulta_dato":
        if category_request is not None:
            # Prueba #8: "qué productos tienes de accesorios para laptop" pide
            # catálogo aunque el NLU lo clasifique como `consulta_dato`. Se
            # responde con la tabla de la categoría (o con la ficha si el
            # calificador deja un solo producto) y nunca con el compositor, que
            # sin datos afirmaba "no hay accesorios".
            kind, payload = category_request
            if kind == "list":
                term, products = payload
                return _render_catalog_table(
                    conn,
                    conversation,
                    context,
                    term,
                    products,
                    quantity=intent.cantidad,
                )
            product = payload
            guard_context = _guard_context(
                product, None, context, product_source="text"
            )
            reply = _render_product_card(
                conn, product, conversation, buy=False, context=context
            )
            reply, generator = _safe_template(
                conn, conversation, reply, guard_context, "template", card=True
            )
            return reply, False, generator, None

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
        # "propiedades"/"detalles"/"todo" son etiquetas genéricas del NLU, no un
        # dato concreto: no se escalan como si el cliente hubiera preguntado uno.
        datos_proveedor = [
            d
            for d in datos
            if _dato_pertenece_a(d) == "proveedor"
            and not _is_generic_detail_label(d)
        ]
        # F15 (prueba #4): "microprocesador" y "procesador" son el mismo dato.
        datos_proveedor = _dedupe([_canonical_topic(d) for d in datos_proveedor])
        # F1 (prueba #3): un tema técnico que el catálogo no tiene se escala,
        # no se compone. El NLU puede no listarlo, así que se detecta también
        # por léxico antes de llamar al modelo.
        missing = _missing_spec_topics(product, text)
        for topic in missing:
            if topic not in datos_proveedor:
                datos_proveedor.append(topic)

        # Prueba #5: una petición abierta ("qué propiedades tiene", "todo") o un
        # dato concreto que la ficha sí trae se responden determinísticamente
        # desde descripción + especificaciones. Sin compositor: el doble
        # `compose` + fallback metía 6-8 s en cada turno de detalles.
        open_request = _asks_for_open_details(text)
        asked = _asked_detail_topics(text, datos_proveedor)
        if open_request or asked:
            entries = _detail_entries(product)
            if open_request:
                # "todos los que tengas" gana sobre el dato puntual que también
                # mencione ("...además el micro"): se muestra la ficha completa.
                selected = entries
            elif asked:
                selected = [entry for entry in entries if entry[1] & asked] or entries
            else:
                selected = entries
            guard_context = _guard_context(
                product, None, context, product_source=product_source
            )
            reply = _render_details(
                product,
                selected,
                missing,
                open_request=open_request and not missing,
            )
            reply, generator = _safe_template(
                conn, conversation, reply, guard_context, "template"
            )
            if missing:
                # Se responde lo que sí hay y se escala solo lo que falta.
                pregunta = (
                    f"El cliente pregunta sobre {product['name']} y necesita "
                    f"saber: {', '.join(missing)}. ¿Me confirma ese dato para "
                    "pasárselo?"
                )
                return reply, True, generator, pregunta
            return reply, False, generator, None

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


# ------------------------------------------------------------------ TurnPlan
@dataclass(frozen=True)
class TurnPlan:
    """What the policy wants to read and how it intends to answer.

    L3.0 returns a trivial plan: the legacy route still decides and executes.
    The orchestrator already has the extension point, so L3.1+ can fill `reads`
    without touching the turn lifecycle again.
    """

    reads: list[ToolCall] = field(default_factory=list)
    decision: str = "legacy"
    reason: str = ""
    fallback: str | None = None


def plan_turn(frame, intent) -> TurnPlan:
    return TurnPlan(
        reads=[],
        decision="legacy",
        reason="L3.0: la ruta determinista sigue decidiendo",
        fallback=None,
    )
