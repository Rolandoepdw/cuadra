"""Reply renderers: product cards, tables, listings and spec helpers."""

import logging
import re
from datetime import datetime, timezone

from cuadra.ai import guardrails, prompts
from cuadra.domain import catalog, conversations, settings

log = logging.getLogger("cuadra.render")

from cuadra.ai.turn.constants import (
    _GENERIC_DETAIL_LABELS,
    _GREETING_COOLDOWN_S,
    _TABLE_LIMIT,
    _TOPIC_PRIORITY,
)

from cuadra.ai.turn.guards import (
    _guard_context,
    _safe_template,
)


def _product_spec_corpus(product: dict) -> str:
    """Customer-facing text that can back a technical answer (prueba #5).

    La ficha real suele estar en `description` ("ASUS ZenBook / Ryzen 7 /
    16GB RAM / 1TB SSD"), no en `specifications_json`. Sin la descripción el
    bot escalaba datos que ya tenía a mano.
    """
    parts = [str(product.get("name") or ""), str(product.get("description") or "")]
    for key, value in catalog.customer_specifications(product).items():
        parts.append(str(key))
        parts.append(str(value))
    return " ".join(part for part in parts if part)


def _missing_spec_topics(product: dict, text: str) -> list[str]:
    """Temas técnicos que el cliente pregunta y el catálogo no puede responder.

    Prueba #3 (F1): con las especificaciones vacías el compositor inventó que la
    GPU era un microprocesador. Si el dato no está en la ficha (descripción o
    especificaciones), se escala al proveedor; el modelo no compone una
    respuesta técnica a ciegas.
    """
    corpus = _product_spec_corpus(product)
    missing = []
    for topic, pattern in guardrails.SPEC_TOPICS.items():
        if not pattern.search(text or ""):
            continue
        if guardrails.spec_topic_present(topic, corpus):
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


def _available_stock(product: dict) -> int:
    return max(
        0,
        int(product.get("stock") or 0) - int(product.get("stock_reserved") or 0),
    )


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


def _availability_line(product: dict) -> str:
    available = _available_stock(product)
    if available <= 0:
        return "sin unidades disponibles por ahora"
    if available == 1:
        return "1 unidad disponible"
    return f"{available} unidades disponibles"


def _render_product_card(
    conn,
    product: dict,
    conversation: dict,
    *,
    buy: bool = False,
    context: dict | None = None,
) -> str:
    """One message with everything the bot knows about an identified product.

    Replaces the old greeting template, which mixed the price with a delivery
    zone the customer had not asked about yet. The zone is intentionally absent:
    it is only discussed when the customer asks or when the address is captured.
    """
    info = catalog.llm_product_json(conn, product)
    policy = settings.transport_policy(conn)
    lines = [
        prompts.PRODUCT_CARD_TITLE.format(
            producto=info["nombre"],
            precio=f"{info['precio']:g}",
            moneda=info["moneda"],
        ),
        f"ID {info['id']} · {_availability_line(product)}",
    ]
    entries = sorted(_detail_entries(product), key=_entry_priority)
    if entries:
        detail = " · ".join(line for line, _topics in entries[:4])
        if len(detail) > 320:
            detail = detail[:317].rstrip() + "…"
        lines.append(detail)
    lines.append(prompts.TRANSPORT_CLAUSES[policy].strip())
    lines.append(
        prompts.PRODUCT_CARD_CTA_BUY if buy else prompts.PRODUCT_CARD_CTA
    )
    if context is None:
        context = conversations.get_context(conn, conversation["id"])
    return _with_greeting("\n".join(lines), context)


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


def _line_topics(line: str) -> set[str]:
    return {
        topic
        for topic in guardrails.SPEC_TOPICS
        if guardrails.spec_topic_present(topic, line or "")
    }


def _detail_entries(product: dict) -> list[tuple[str, set[str]]]:
    """Customer-facing detail lines from the description and specifications.

    Each entry carries the spec topics it covers, so an open request can show
    everything and a concrete request can select only what was asked.
    """
    entries: list[tuple[str, set[str]]] = []
    seen: set[str] = set()

    for raw in re.split(r"[\n;]+", product.get("description") or ""):
        line = " ".join(raw.split()).strip(" -•\t")
        if not line:
            continue
        key = catalog._fold(line)
        if key in seen:
            continue
        seen.add(key)
        entries.append((line, _line_topics(line)))

    for key, value in catalog.customer_specifications(product).items():
        text = str(value or "").strip()
        if not text:
            continue
        label = str(key).strip()
        line = text if label and catalog._fold(label) in catalog._fold(text) else f"{label}: {text}"
        folded = catalog._fold(line)
        if folded in seen:
            continue
        seen.add(folded)
        covered = {_canonical_topic(label)} if label else set()
        covered |= _line_topics(line)
        entries.append((line, covered))
    return entries


def _entry_priority(entry: tuple[str, set[str]]) -> int:
    topics = entry[1]
    if not topics:
        return len(_TOPIC_PRIORITY) + 1
    return min(
        _TOPIC_PRIORITY.get(topic, len(_TOPIC_PRIORITY)) for topic in topics
    )


def _render_details(
    product: dict,
    entries: list[tuple[str, set[str]]],
    missing: list[str],
    *,
    open_request: bool,
) -> str:
    """Deterministic ficha answer (prueba #5).

    Nunca inventa: solo enumera lo que está en la descripción o en las
    especificaciones. Un tema concreto que falte se anuncia como pendiente y se
    escala al almacén. La frase de lo que falta empieza por "No tengo el dato
    de…" para que el guardrail de specs no la lea como una afirmación.
    """
    lines = [line[:140] for line, _topics in entries][:6]
    if lines:
        summary = " · ".join(lines)
        if len(summary) > 700:
            summary = summary[:697].rstrip() + "…"
        reply = f"Esto es lo que tengo de {product['name']}: {summary}."
    else:
        reply = f"Todavía no tengo la ficha completa de {product['name']}."
    if missing:
        reply += (
            f" No tengo el dato de {', '.join(missing)}; consulto al almacén y "
            "te confirmo. ¿Te interesa que te reserve uno?"
        )
    elif open_request:
        reply += " Si quieres, te confirmo algún dato puntual con el almacén."
    return reply


def _table_cell(text: str) -> str:
    """Plain text safe inside a Telegram/WhatsApp monospace block."""
    return re.sub(r"[`*_\[\]\\]", "", str(text or "")).strip()


def _ellipsis(text: str, width: int) -> str:
    text = _table_cell(text)
    if len(text) <= width:
        return text
    return text[: max(0, width - 1)].rstrip() + "…"


def _product_highlights(product: dict) -> str:
    """Most useful specs for a listing row, in a mobile-friendly order."""
    entries = sorted(_detail_entries(product), key=_entry_priority)
    # La disponibilidad va primero: la columna se recorta a ~30 caracteres y
    # "agotado"/"última unidad" no puede perderse en el recorte.
    parts = []
    available = _available_stock(product)
    if available <= 0:
        parts.append("agotado")
    elif available == 1:
        parts.append("última unidad")
    parts.extend(line for line, _topics in entries)
    return ", ".join(parts)


def _format_product_table(products: list[dict]) -> str:
    """Aligned monospace table (works in WhatsApp and Telegram).

    Triple backticks are the common denominator: WhatsApp renders them as a
    monospace block natively and Telegram does too when the channel sends with
    `parse_mode=Markdown`.
    """
    rows = []
    for product in products:
        rows.append(
            (
                str(product["id"]),
                _table_cell(product["name"]),
                f"{float(product['price']):g} {product['currency']}",
                _table_cell(_product_highlights(product)),
            )
        )
    if not rows:
        return ""
    id_w = max(2, max(len(row[0]) for row in rows))
    name_w = min(20, max(8, max(len(row[1]) for row in rows)))
    price_w = max(6, max(len(row[2]) for row in rows))
    char_w = 30
    header = (
        f"{'ID':<{id_w}}  {'Producto':<{name_w}}  {'Precio':<{price_w}}  "
        f"{'Características':<{char_w}}"
    )
    separator = (
        f"{'-' * id_w}  {'-' * name_w}  {'-' * price_w}  {'-' * char_w}"
    )
    lines = [header, separator]
    for product_id, name, price, chars in rows:
        lines.append(
            f"{product_id:<{id_w}}  {_ellipsis(name, name_w):<{name_w}}  "
            f"{price:<{price_w}}  {_ellipsis(chars, char_w):<{char_w}}"
        )
    return "```\n" + "\n".join(lines).rstrip() + "\n```"


def _remember_listing(
    context: dict,
    products: list[dict],
    *,
    query: str,
    kind: str,
) -> None:
    """Persist the listing so "esos" and "el segundo" survive the turn."""
    from cuadra.ai.turn.frame import Listing

    if not products:
        return
    listing = Listing(
        product_ids=[int(product["id"]) for product in products],
        labels=[str(product.get("name") or "") for product in products],
        query=query or "",
        kind=kind,
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    context["last_listing"] = listing.as_dict()


def _render_catalog_table(
    conn,
    conversation,
    context: dict,
    category: str,
    products: list[dict],
    *,
    quantity: int | None = None,
) -> tuple[str, bool, str, None]:
    if quantity:
        # "quiero 3 laptops": la cantidad se conserva para cuando elija una.
        context["sale_qty"] = int(quantity)
        context["sale_qty_product_id"] = None
    _remember_listing(context, products, query=category, kind="table")
    guard_context = _guard_context(
        None, None, context, extra_products=products
    )
    reply = prompts.template_variant(
        "catalog_table",
        context,
        categoria=category,
        tabla=_format_product_table(products),
    )
    reply, generator = _safe_template(
        conn, conversation, reply, guard_context, "template", table=True
    )
    return reply, False, generator, None


def _render_siblings(
    conn, conversation, product: dict, alternatives: list[dict], context: dict
) -> str:
    """F4: list the other models of the same family, or say there is only one."""
    if not alternatives:
        return prompts.template_variant(
            "siblings_none", context, producto=product["name"]
        )
    _remember_listing(
        context, alternatives, query=product["name"], kind="siblings"
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
    """F3: "¿qué laptops tienes?" -> short table, then ask which one.

    Prueba #5: el listado va en tabla monoespaciada (ID, producto, precio,
    características) para que el cliente pueda responder con el ID. Se limita a
    `_TABLE_LIMIT` filas para no saturar el móvil.

    Returns None when the customer did not name a category or product family, so
    the caller falls back to "dime cuál producto".
    """
    # El texto del cliente manda: el NLU puede repetir un producto concreto
    # ("Monitor LED 24") en una pregunta que en realidad pide la categoría
    # ("¿qué laptops tienes?").
    query = (text or "").strip() or (intent.producto or "").strip()
    if not query:
        return None
    found = catalog.search_catalog(conn, query, limit=_TABLE_LIMIT)
    products = found.get("products") or []
    if not products:
        return None
    return _render_catalog_table(
        conn,
        conversation,
        context,
        found.get("match_term") or query,
        products,
        quantity=intent.cantidad,
    )


def _is_generic_detail_label(value: str) -> bool:
    folded = catalog._fold(value or "").strip()
    return folded in _GENERIC_DETAIL_LABELS
