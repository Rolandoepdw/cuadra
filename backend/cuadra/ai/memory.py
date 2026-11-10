"""Layered conversation memory (phase 2, doc 01 §4).

Four layers, cheapest first:

1. Working buffer: the last `HISTORY_WINDOW` messages (already in `messages`).
2. Pinned facts: the current value per key in `conversation_facts`.
3. Incremental summary: `conversations.context_json.summary`, advanced by
   message id (`summary_through_message_id`), never by a message count.
4. Customer profile: `customers.preferences_json` plus the last sales.

Facts are deliberately small and deterministic. They are extracted in Python
(no extra LLM call) and are never trimmed from the prompt: if the budget is
tight, history and candidate products shrink first.
"""

import json
import logging
import re
import sqlite3

from cuadra import config
from cuadra.ai import guardrails, llm
from cuadra.domain import catalog, conversations, sales as sales_domain

log = logging.getLogger("cuadra.memory")

FACT_PRODUCT_ID = "product_id"
FACT_PRODUCT_NAME = "product_name"
FACT_ZONE = "zone_name"
FACT_BUDGET = "budget"
FACT_CUSTOMER_NAME = "customer_name"
FACT_PHONE = "customer_phone"
FACT_ADDRESS = "delivery_address"
FACT_OBJECTION = "objection"

FACT_LABELS = {
    FACT_PRODUCT_NAME: "Producto en juego",
    FACT_PRODUCT_ID: "ID del producto",
    FACT_ZONE: "Zona de entrega",
    FACT_BUDGET: "Presupuesto mencionado",
    FACT_CUSTOMER_NAME: "Nombre del cliente",
    FACT_PHONE: "Teléfono",
    FACT_ADDRESS: "Dirección de entrega",
    FACT_OBJECTION: "Objeción",
}

_NAME_RE = re.compile(
    r"\b(?:me llamo|mi nombre es|soy)\s+"
    r"([A-Za-zÁÉÍÓÚÜÑáéíóúüñ]{2,}(?:\s+[A-Za-zÁÉÍÓÚÜÑáéíóúüñ]{2,})?)",
    re.IGNORECASE,
)
_BUDGET_WITH_CURRENCY = re.compile(
    r"\b(?:tengo|dispongo de|cuento con)\b[^\d\n]{0,12}"
    r"(\d+(?:[.,]\d{1,2})?)\s*(usd|cup|\$)",
    re.IGNORECASE,
)
# "50 USD de presupuesto" (amount first) avoids matching a later phone number.
_BUDGET_BEFORE = re.compile(
    r"(\d+(?:[.,]\d{1,2})?)\s*(usd|cup|\$)?\s*(?:de\s+)?"
    r"(?:presupuesto|m[áa]ximo|maximo)\b",
    re.IGNORECASE,
)
# "presupuesto: 50", "puedo gastar 100 USD".
_BUDGET_AFTER = re.compile(
    r"\b(?:presupuesto|puedo gastar|gastar|m[áa]ximo|maximo)\b"
    r"[^\d\n]{0,12}(\d+(?:[.,]\d{1,2})?)\s*(usd|cup|\$)?",
    re.IGNORECASE,
)
# "hasta 100 USD" only counts when a currency is explicit (not "hasta 8 productos").
_BUDGET_UNTIL = re.compile(
    r"\bhasta\b[^\d\n]{0,10}(\d+(?:[.,]\d{1,2})?)\s*(usd|cup|\$)",
    re.IGNORECASE,
)
_PHONE_RE = re.compile(r"(?:\+?\s*53[\s-]?)?(\d(?:[\s-]?\d){5,9})")
_PHONE_HINT = re.compile(
    r"\b(tel[eé]fono|telefono|celular|m[oó]vil|movil|whatsapp|n[uú]mero|numero|"
    r"llamar|contacto)\b",
    re.IGNORECASE,
)
# Delivery address: an explicit label or a street-like phrase. A bare zone
# ("vivo en Marianao") is rejected because it has neither a digit nor a street
# keyword, so it stays a zone fact and never becomes an address.
_ADDRESS_LABEL = re.compile(
    r"\b(?:direcci[oó]n|dirijo a|domicilio|entregar en|entr[eé]gala en|"
    r"vivo en|resido en)\b\s*(?:es|:|-)?\s*(.{6,200})",
    re.IGNORECASE,
)
_ADDRESS_STREET = re.compile(
    r"\b(?:calle|avenida|av\.?|edificio|edif\.?|reparto|carretera|autopista|"
    r"consejo popular|calzada|pasaje)\b[^\n,;.]{2,120}",
    re.IGNORECASE,
)
_ADDRESS_REJECT = {"mi casa", "casa", "aqui", "aquí", "alla", "allá", "por ahi", "por ahí"}
_OBJECTIONS = (
    (re.compile(r"\b(muy caro|caro|costoso|no me alcanza|fuera de presupuesto)\b", re.I), "precio"),
    (re.compile(r"\b(no conf[ií]o|desconf[ií]o|es falso|estafa)\b", re.I), "confianza"),
    (re.compile(r"\b(lo pienso|me lo pienso|despu[eé]s|otro d[ií]a)\b", re.I), "duda"),
)
_NAME_STOPWORDS = {
    "de", "del", "la", "el", "los", "las", "un", "una", "cliente", "yo",
    "mi", "mí", "aqui", "aquí", "nuevo", "nueva",
}


def _clean(value, limit: int = 500) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _profile_dict(raw: str | None) -> dict:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


# --------------------------------------------------------------------- facts

def upsert_fact(
    conn,
    conversation_id: int,
    key: str,
    value: str,
    *,
    confidence: float = 1.0,
    source_message_id: int | None = None,
) -> None:
    """Store the current value of one fact (the history lives in events/messages)."""
    key = _clean(key, 60)
    value = _clean(value)
    if not key or not value:
        return
    conn.execute(
        """INSERT INTO conversation_facts
           (conversation_id, fact_key, fact_value, confidence, source_message_id,
            updated_at)
           VALUES (?, ?, ?, ?, ?, datetime('now'))
           ON CONFLICT(conversation_id, fact_key) DO UPDATE SET
               fact_value = excluded.fact_value,
               confidence = excluded.confidence,
               source_message_id = excluded.source_message_id,
               updated_at = datetime('now')""",
        (conversation_id, key, value, max(0.0, min(1.0, confidence)), source_message_id),
    )
    conn.commit()


def list_facts(conn, conversation_id: int) -> list[dict]:
    try:
        rows = conn.execute(
            """SELECT * FROM conversation_facts
               WHERE conversation_id = ? ORDER BY id""",
            (conversation_id,),
        ).fetchall()
    except sqlite3.OperationalError:
        return []  # pre-migration or a test database without phase 2
    return [dict(row) for row in rows]


def facts_map(conn, conversation_id: int) -> dict[str, str]:
    return {fact["fact_key"]: fact["fact_value"] for fact in list_facts(conn, conversation_id)}


def facts_context(conn, conversation_id: int) -> dict[str, str]:
    """Human-labelled facts for the system prompt (fixed, never trimmed)."""
    return {
        FACT_LABELS.get(fact["fact_key"], fact["fact_key"]): fact["fact_value"]
        for fact in list_facts(conn, conversation_id)
    }


def _set_profile_pref(conn, customer_id: int, key: str, value: str) -> None:
    try:
        row = conn.execute(
            "SELECT preferences_json FROM customers WHERE id = ?", (customer_id,)
        ).fetchone()
        if row is None:
            return
        prefs = _profile_dict(row["preferences_json"])
        if prefs.get(key) == value:
            return
        prefs[key] = value
        conn.execute(
            "UPDATE customers SET preferences_json = ? WHERE id = ?",
            (json.dumps(prefs, ensure_ascii=False, separators=(",", ":")), customer_id),
        )
        conn.commit()
    except sqlite3.OperationalError:
        log.debug("customer profile unavailable for customer %s", customer_id)


def _extract_name(text: str) -> str | None:
    match = _NAME_RE.search(text or "")
    if not match:
        return None
    candidate = _clean(match.group(1), 60)
    if not candidate:
        return None
    first = candidate.split()[0].lower()
    if first in _NAME_STOPWORDS:
        return None
    return candidate


def _extract_budget(text: str) -> str | None:
    body = text or ""
    match = None
    for pattern in (
        _BUDGET_WITH_CURRENCY,
        _BUDGET_BEFORE,
        _BUDGET_AFTER,
        _BUDGET_UNTIL,
    ):
        match = pattern.search(body)
        if match:
            break
    if not match:
        return None
    amount = match.group(1).replace(",", ".")
    currency = (match.group(2) or "").strip().upper()
    if currency == "$":
        currency = "USD"
    return f"{amount} {currency}".strip()


def compose_phone(raw: str | None) -> str | None:
    """Accept 6-8 digits with optional +53; reject anything else (doc 02 §3.2)."""
    return sales_domain.normalize_phone(raw)


def _extract_phone(text: str) -> str | None:
    body = text or ""
    hinted = _PHONE_HINT.search(body) is not None
    if not hinted and not re.fullmatch(r"\s*\+?[\d\s-]{6,20}\s*", body):
        return None
    if not hinted and re.search(r"\b(id|producto|c[oó]digo)\b", body, re.IGNORECASE):
        return None
    for match in _PHONE_RE.finditer(body):
        phone = compose_phone(match.group(1))
        if phone:
            return phone
    return None


def _extract_objection(text: str) -> str | None:
    for pattern, label in _OBJECTIONS:
        if pattern.search(text or ""):
            return label
    return None


def _looks_like_address(value: str) -> bool:
    candidate = _clean(value, 200).strip(" .,;:-")
    if len(candidate) < 6 or candidate.casefold() in _ADDRESS_REJECT:
        return False
    return bool(re.search(r"\d", candidate) or _ADDRESS_STREET.search(candidate))


def _extract_address(text: str) -> str | None:
    body = text or ""
    match = _ADDRESS_LABEL.search(body)
    if match and _looks_like_address(match.group(1)):
        return _clean(match.group(1), 200).strip(" .,;:-")
    match = _ADDRESS_STREET.search(body)
    if match and _looks_like_address(match.group(0)):
        return _clean(match.group(0), 200).strip(" .,;:-")
    return None


def capture_facts(
    conn,
    conversation: dict,
    text: str,
    *,
    product: dict | None = None,
    source_message_id: int | None = None,
) -> dict[str, str]:
    """Extract deterministic facts from one customer turn.

    No model call: product/zone/budget/name/phone are cheap and must survive a
    long conversation even when the LLM is unavailable.
    """
    conversation_id = conversation["id"]
    customer_id = conversation["customer_id"]
    found: dict[str, str] = {}

    if product is not None:
        values = {
            FACT_PRODUCT_ID: str(product["id"]),
            FACT_PRODUCT_NAME: product["name"],
        }
        for key, value in values.items():
            upsert_fact(
                conn,
                conversation_id,
                key,
                value,
                source_message_id=source_message_id,
            )
            found[key] = value

    zone = catalog.resolve_zone(conn, text)
    if zone is not None:
        upsert_fact(
            conn,
            conversation_id,
            FACT_ZONE,
            zone["name"],
            source_message_id=source_message_id,
        )
        _set_profile_pref(conn, customer_id, "usual_zone", zone["name"])
        found[FACT_ZONE] = zone["name"]

    name = _extract_name(text)
    if name:
        upsert_fact(
            conn,
            conversation_id,
            FACT_CUSTOMER_NAME,
            name,
            confidence=0.8,
            source_message_id=source_message_id,
        )
        _set_profile_pref(conn, customer_id, "name", name)
        conn.execute(
            "UPDATE customers SET name = ? WHERE id = ? AND (name IS NULL OR TRIM(name) = '')",
            (name, customer_id),
        )
        conn.commit()
        found[FACT_CUSTOMER_NAME] = name

    budget = _extract_budget(text)
    if budget:
        upsert_fact(
            conn,
            conversation_id,
            FACT_BUDGET,
            budget,
            confidence=0.7,
            source_message_id=source_message_id,
        )
        found[FACT_BUDGET] = budget

    phone = _extract_phone(text)
    if phone:
        upsert_fact(
            conn,
            conversation_id,
            FACT_PHONE,
            phone,
            confidence=0.8,
            source_message_id=source_message_id,
        )
        _set_profile_pref(conn, customer_id, "phone", phone)
        found[FACT_PHONE] = phone

    address = _extract_address(text)
    if address:
        upsert_fact(
            conn,
            conversation_id,
            FACT_ADDRESS,
            address,
            confidence=0.8,
            source_message_id=source_message_id,
        )
        _set_profile_pref(conn, customer_id, "usual_address", address)
        found[FACT_ADDRESS] = address

    objection = _extract_objection(text)
    if objection:
        upsert_fact(
            conn,
            conversation_id,
            FACT_OBJECTION,
            objection,
            confidence=0.6,
            source_message_id=source_message_id,
        )
        found[FACT_OBJECTION] = objection

    return found


# ------------------------------------------------------------------- profile

def customer_profile(conn, customer_id: int) -> dict:
    """Profile injected into a new conversation (survives idle closing)."""
    try:
        customer = conversations.get_customer(conn, customer_id)
    except Exception:  # noqa: BLE001 - missing customer must not break a reply
        return {}
    prefs = _profile_dict(customer.get("preferences_json"))
    profile: dict = {}
    name = _clean(prefs.get("name") or customer.get("name"), 80)
    if name:
        profile["nombre"] = name
    if prefs.get("usual_zone"):
        profile["zona_habitual"] = _clean(prefs["usual_zone"], 80)
    if prefs.get("usual_address"):
        profile["direccion_habitual"] = _clean(prefs["usual_address"], 200)
    if prefs.get("phone"):
        profile["telefono"] = _clean(prefs["phone"], 30)
    if prefs.get("notes"):
        profile["notas"] = _clean(prefs["notes"], 300)

    try:
        total = conn.execute(
            """SELECT COUNT(*) FROM sales
               WHERE customer_id = ? AND status NOT IN ('CANCELLED')""",
            (customer_id,),
        ).fetchone()[0]
        rows = conn.execute(
            """SELECT s.amount, s.currency, s.status, s.created_at,
                      p.name AS product_name
               FROM sales s LEFT JOIN products p ON p.id = s.product_id
               WHERE s.customer_id = ?
               ORDER BY s.id DESC LIMIT 3""",
            (customer_id,),
        ).fetchall()
    except sqlite3.OperationalError:
        total, rows = 0, []
    if total:
        profile["compras_anteriores"] = int(total)
    purchases = [
        {
            "producto": row["product_name"],
            "monto": row["amount"],
            "moneda": row["currency"],
            "estado": row["status"],
            "fecha": row["created_at"],
        }
        for row in rows
    ]
    if purchases:
        profile["ultimas_compras"] = purchases
    return profile


def profile_context(conn, customer_id: int) -> dict:
    return customer_profile(conn, customer_id)


# ------------------------------------------------------------------- summary

async def maybe_summarize(conn, conversation, *, llm_client=None) -> bool:
    """Advance the rolling summary by message id, never by a message count.

    Only the delta not covered by `summary_through_message_id` is sent to the
    model; the last `HISTORY_WINDOW` messages stay verbatim. A failure keeps
    the previous summary and returns False (it is retried on the next turn).
    """
    conversation_id = conversation["id"]
    busy = conn.execute(
        """SELECT 1 FROM turns
           WHERE conversation_id = ? AND status = 'PROCESSING' LIMIT 1""",
        (conversation_id,),
    ).fetchone()
    if busy is not None:
        return False  # never summarise a turn that is still being answered

    context = conversations.get_context(conn, conversation_id)
    last_id = int(context.get("summary_through_message_id") or 0)
    rows = conn.execute(
        """SELECT id, direction, author, body, type
           FROM messages
           WHERE conversation_id = ? AND id > ?
           ORDER BY id""",
        (conversation_id, last_id),
    ).fetchall()

    if len(rows) <= config.HISTORY_WINDOW + config.SUMMARY_EVERY:
        return False
    delta = rows[:-config.HISTORY_WINDOW]
    if not delta:
        return False

    client = llm_client or llm
    clean_delta = []
    for row in delta:
        item = dict(row)
        # H-14: never feed internal economics to the summarizer.
        item["body"] = guardrails.redact_internal_terms(item.get("body") or "")
        if item["body"]:
            clean_delta.append(item)
    if not clean_delta:
        # Everything in the delta was internal chatter; skip the model call but
        # advance the marker so the same messages are not reprocessed forever.
        context["summary_through_message_id"] = delta[-1]["id"]
        conversations.set_context(conn, conversation_id, context)
        return False
    try:
        summary = await client.summarize(
            context.get("summary"),
            clean_delta,
            conversation_id=conversation_id,
        )
    except (llm.LLMUnavailable, llm.LLMInvalidOutput):
        log.warning("summary failed for conversation %s; keeping previous", conversation_id)
        return False

    summary = guardrails.redact_internal_terms((summary or "").strip())
    if not summary:
        return False

    context["summary"] = summary
    context["summary_through_message_id"] = delta[-1]["id"]
    context.pop("summary_through", None)  # legacy count, superseded by the id
    conversations.set_context(conn, conversation_id, context)
    log.debug(
        "summary advanced to message %s (+%s messages)",
        delta[-1]["id"],
        len(delta),
    )
    return True
