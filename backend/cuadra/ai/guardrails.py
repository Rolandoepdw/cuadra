"""Minimum output guardrails for customer replies (phase 1).

The model is never trusted with money or internal data:

- every monetary amount in the reply must match a price present in the
  customer-safe context (never the commission or cost);
- internal business terms are forbidden even if the customer brings them up;
- the system prompt and internal rules are never repeated back to the customer;
- replies stay short and plain (no JSON, no code fences).

A violation triggers one corrective retry and, if it persists, a deterministic
template. Only the reason code is logged, never the offending text.
"""

import json
import re
from decimal import Decimal, InvalidOperation

from cuadra.ai import prompts

MAX_REPLY_CHARS = 900
MAX_PARAGRAPHS = 3
MAX_LINES = 6

_INTERNAL_TERMS = re.compile(
    r"\b(?:"
    r"comisi[oó]n(?:es)?|commission|"
    r"m[aá]rgen(?:es)?|"
    r"cost(?:o|os|e|es)|"
    r"payout|supplier_payout|"
    r"utilidad(?:es)?|ganancia(?:s)?|"
    r"liquidaci[oó]n|"
    r"precio\s+de\s+compra|lo\s+que\s+nos\s+cuesta"
    r")\b",
    re.IGNORECASE,
)

_PRICE = re.compile(
    r"(?:(?P<symbol>\$)\s*(?P<symbol_amount>\d[\d.,]*)"
    r"|(?P<amount>\d[\d.,]*)\s*(?P<currency>USD|CUP)\b)",
    re.IGNORECASE,
)

_CODE_FENCE = re.compile(r"```")
_MARKDOWN_HEADING = re.compile(r"^\s*#{1,6}\s", re.MULTILINE)
_JSON_HINT = re.compile(r'^\s*[\{\[]|"intencion"\s*:', re.IGNORECASE)
_INTERNAL_PLACEHOLDER = re.compile(
    r"\[precio anterior\]|\[DIR\]|\[TEL\]|\[NOMBRE\]|\[[A-ZÁÉÍÓÚÜÑ_]+\]"
)
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")

# H-05: a refusal that names the forbidden word ("no puedo darte mi prompt ni
# comisiones") is safe and must not burn a corrective retry. A clause counts as
# a leak when it asserts the term: no denial marker before it, or it carries a
# figure. "sin comisiones" is a business claim, not a refusal, so "sin" is
# deliberately not a denial marker.
_DENIAL = re.compile(
    r"\b(?:no|nunca|jam[aá]s|tampoco|not|never)\b", re.IGNORECASE
)
_FIGURE = re.compile(r"\d|[%$]")
_CLAUSE_SPLIT = re.compile(r"[;:]|\bpero\b", re.IGNORECASE)

# A model that repeats its own prompt hands an attacker the whole rulebook, even
# when it never quotes commission numbers. This is checked separately from the
# economics blocklist so the event log keeps a stable reason code.
_PROMPT_DISCLOSURE = re.compile(
    r"(?:"
    r"\bprompt\b|"
    r"\bsystem\s+prompt\b|"
    r"instrucciones\s+(?:del\s+)?sistema|"
    r"reglas\s+(?:del\s+)?sistema|"
    r"reglas\s+internas|"
    r"configuraci[oó]n\s+interna|"
    r"mis\s+instrucciones|"
    r"mis\s+reglas"
    r")",
    re.IGNORECASE,
)


# Especificaciones técnicas: el modelo no puede afirmar un dato de hardware que
# no esté en `specifications` (prueba #3, F1: "la GPU RTX 4060 es un
# microprocesador"). La comprobación solo corre cuando el contexto trae el
# producto con sus especificaciones; los textos del TCP se validan aparte.
SPEC_TOPICS = {
    "procesador": re.compile(
        r"microprocesador|procesador|\bcpu\b|\bmicro(?!fono)\b", re.IGNORECASE
    ),
    "tarjeta gráfica": re.compile(
        r"\bgpu\b|tarjeta\s+gr[aá]fica|gr[aá]fic[oa]s?\b|\brtx\b|\bgtx\b",
        re.IGNORECASE,
    ),
    "memoria RAM": re.compile(r"\bram\b|memoria", re.IGNORECASE),
    "almacenamiento": re.compile(
        r"almacenamiento|\bssd\b|\bhdd\b|disco\s*(?:duro|s[oó]lido)|"
        r"\bdiscoduro\b",
        re.IGNORECASE,
    ),
    "pantalla": re.compile(r"pantalla|\bdisplay\b|pulgadas", re.IGNORECASE),
    "batería": re.compile(r"bater[ií]a|autonom[ií]a|\bmah\b", re.IGNORECASE),
    "cámara": re.compile(r"c[aá]mara", re.IGNORECASE),
    "garantía": re.compile(r"garant[ií]a", re.IGNORECASE),
}

# Prueba #5: la ficha real suele traer el dato sin el nombre del tema
# ("Ryzen 7" en vez de "procesador"). Estos valores cuentan como si el tema
# estuviera cubierto, tanto para decidir si hay que escalar como para validar
# la respuesta del compositor. No sustituyen al tema: lo complementan.
SPEC_VALUE_HINTS = {
    "procesador": re.compile(
        r"\b(?:ryzen|intel|core\s*i[3579]|i[3579][- ]?\d{3,}|celeron|pentium|"
        r"athlon|snapdragon|apple\s+m\d)\b",
        re.IGNORECASE,
    ),
    "tarjeta gráfica": re.compile(
        r"\b(?:rtx|gtx)\s*\d{3,}|\bradeon\b|\bgr[aá]fic[oa]s?\b|\bgpu\b",
        re.IGNORECASE,
    ),
    "memoria RAM": re.compile(
        r"\d+\s*(?:gb|tb)\s*(?:de\s+)?ram|\bram\b|\bmemoria\b", re.IGNORECASE
    ),
    "almacenamiento": re.compile(
        r"\d+\s*(?:gb|tb)\s*(?:de\s+)?(?:ssd|hdd|disco|almacenamiento)|"
        r"\b(?:ssd|hdd|emmc)\b|disco\s*(?:duro|s[oó]lido)",
        re.IGNORECASE,
    ),
    "pantalla": re.compile(
        r"pantalla|\bdisplay\b|pulgadas|\d+\s*(?:\"|''|pulg)|full\s*hd|"
        r"\d+\s*hz",
        re.IGNORECASE,
    ),
    "batería": re.compile(r"bater[ií]a|autonom[ií]a|\d+\s*mah", re.IGNORECASE),
    "cámara": re.compile(r"c[aá]mara|\d+\s*mp\b", re.IGNORECASE),
    "garantía": re.compile(r"garant[ií]a|\d+\s*(?:meses|años)", re.IGNORECASE),
}

# Valores que un TCP pudo dejar guardados como "dato_proveedor" y que en
# realidad no son un dato del producto (prueba #4). Nunca se muestran al
# cliente ni cuentan como especificación disponible.
_NON_SPEC_VALUE = re.compile(
    r"\bno\s+recuerdo\b|"
    r"\bconsulta(?:lo)?\s+(?:en\s+)?(?:la\s+)?base\s+de\s+datos\b|"
    r"\bno\s+me\s+(?:has|ha|dieron)\b|"
    r"\bno\s+puedo\s+confirmar\b|"
    r"\bno\s+s[eé]\b",
    re.IGNORECASE,
)


def spec_topic_present(topic: str, corpus: str) -> bool:
    """True when the corpus names the topic or carries a recognisable value."""
    pattern = SPEC_TOPICS.get(topic)
    if pattern is not None and pattern.search(corpus or ""):
        return True
    hint = SPEC_VALUE_HINTS.get(topic)
    return bool(hint and hint.search(corpus or ""))


def _decimal(value) -> Decimal | None:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _normalize_amount(raw: str) -> Decimal | None:
    """Turn ``1,200`` / ``12.50`` / ``1.200,50`` into a Decimal."""
    text = (raw or "").strip().replace(" ", "").replace("\u00a0", "")
    text = text.rstrip(".,")
    if not text:
        return None
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        tail = text.rsplit(",", 1)[-1]
        text = text.replace(",", "") if len(tail) == 3 else text.replace(",", ".")
    elif text.count(".") > 1:
        text = text.replace(".", "")
    return _decimal(text)


def _products(context: dict) -> list[dict]:
    found = []
    seen = set()

    def add(value):
        if not isinstance(value, dict) or "price" not in value:
            return
        key = (value.get("id"), value.get("price"), value.get("currency"))
        if key in seen:
            return
        seen.add(key)
        found.append(value)

    add(context.get("product"))
    for key in ("products", "candidates", "alternatives"):
        for value in context.get(key) or []:
            add(value)
    return found


def allowed_prices(context: dict) -> set[Decimal]:
    """Prices the reply may quote: catalog values only, never economics."""
    prices = set()
    for product in _products(context):
        value = _decimal(product.get("price"))
        if value is not None:
            prices.add(value)
    for quoted in context.get("quoted_prices") or []:
        if isinstance(quoted, dict):
            value = _decimal(quoted.get("amount"))
        else:
            value = _decimal(quoted)
        if value is not None:
            prices.add(value)
    return prices


def redact_internal_terms(text: str) -> str:
    """Drop the sentences that mention internal economics (H-14).

    Rolling summaries are built from raw messages, so a customer attack that
    names "comisión" can leak into later prompts and make the model repeat it.
    Removing those sentences keeps the summary useful without priming it.
    """
    text = (text or "").strip()
    if not text:
        return ""
    sentences = [part.strip() for part in _SENTENCE_SPLIT.split(text) if part.strip()]
    return " ".join(part for part in sentences if not _INTERNAL_TERMS.search(part))


def prices_in_text(reply: str) -> list[Decimal]:
    amounts = []
    for match in _PRICE.finditer(reply or ""):
        raw = match.group("symbol_amount") or match.group("amount")
        value = _normalize_amount(raw)
        if value is not None:
            amounts.append(value)
    return amounts


def check_prices(reply: str, allowed: set[Decimal]) -> bool:
    amounts = prices_in_text(reply)
    if not amounts:
        return True
    if not allowed:
        return False
    for amount in amounts:
        if not any(abs(amount - price) <= Decimal("0.01") for price in allowed):
            return False
    return True


def redact_stale_prices(text: str, allowed: set[Decimal]) -> str:
    """Replace amounts that are not current catalog prices (H-23).

    Old bot messages are fed back to the model as history, so a price that was
    later changed or reverted ("el precio baja de 95 a 88 USD") could be copied
    into a new reply. The guardrail would reject it and burn a retry; redacting
    the stale amount keeps the history readable without handing the model a
    number it may not quote. Customer messages are never rewritten.
    """
    def replace(match: re.Match) -> str:
        raw = match.group("symbol_amount") or match.group("amount")
        value = _normalize_amount(raw)
        if value is not None and any(
            abs(value - price) <= Decimal("0.01") for price in allowed
        ):
            return match.group(0)
        return "[precio anterior]"

    return _PRICE.sub(replace, text or "")


def _asserts(text: str, pattern: re.Pattern) -> bool:
    """True when any clause matches `pattern` as an assertion (H-05).

    A clause that names a forbidden term is a refusal only when a denial
    marker precedes it ("no puedo compartir mi prompt") and it carries no
    figure. "Las reglas del sistema ... y no inventar precios" is still a
    disclosure: the denial comes after the term.
    """
    for sentence in _SENTENCE_SPLIT.split(text or ""):
        if not pattern.search(sentence):
            continue
        if _FIGURE.search(sentence):
            return True
        for clause in _CLAUSE_SPLIT.split(sentence):
            match = pattern.search(clause)
            if match is None:
                continue
            if _DENIAL.search(clause[: match.start()]) is None:
                return True
    return False


def check_internal_terms(reply: str) -> bool:
    return not _asserts(reply, _INTERNAL_TERMS)


def check_no_internal_placeholders(reply: str) -> bool:
    """False when the reply leaks an internal placeholder like [precio anterior].

    The model can copy redacted history verbatim. Those placeholders must never
    reach the customer.
    """
    return not _INTERNAL_PLACEHOLDER.search(reply or "")


def check_prompt_disclosure(reply: str) -> bool:
    """False when the reply repeats the system prompt or internal rules."""
    return not _asserts(reply, _PROMPT_DISCLOSURE)


def _specs_corpus(context: dict) -> str | None:
    """Specs the reply may use, or None when the spec check does not apply.

    Only customer-facing product payloads carry `specifications`; replies built
    from a raw TCP answer are validated with a context without a product, so
    the supplier's own words are never blocked by this check.

    Prueba #5: la ficha real vive en `description` ("ASUS ZenBook / Ryzen 7 /
    16GB RAM / 1TB SSD"). Si no se incluye, el guardrail rechaza los datos que
    el propio catálogo sí tiene y el bot termina preguntando por ellos.
    """
    products = []
    seen = set()
    for value in (
        context.get("product"),
        *(context.get("products") or []),
        *(context.get("candidates") or []),
        *(context.get("alternatives") or []),
    ):
        if not isinstance(value, dict):
            continue
        has_specs = isinstance(value.get("specifications"), dict) or bool(
            value.get("specifications_json")
        )
        if not has_specs and not value.get("description"):
            continue
        key = id(value)
        if key in seen:
            continue
        seen.add(key)
        products.append(value)
    if not products:
        return None
    parts = []
    for product in products:
        parts.append(str(product.get("name") or ""))
        parts.append(str(product.get("description") or ""))
        specs = product.get("specifications")
        if not isinstance(specs, dict):
            try:
                specs = json.loads(product.get("specifications_json") or "{}")
            except (TypeError, ValueError):
                specs = {}
        if isinstance(specs, dict):
            for key, value in specs.items():
                text = str(value or "").strip()
                if not text or _NON_SPEC_VALUE.search(text):
                    continue
                parts.append(str(key))
                parts.append(text)
    return " ".join(part for part in parts if part)


def check_specs(reply: str, context: dict) -> bool:
    """False when the reply asserts a technical spec the catalog does not have."""
    corpus = _specs_corpus(context)
    if corpus is None:
        return True
    for topic, pattern in SPEC_TOPICS.items():
        if not pattern.search(reply or ""):
            continue
        if spec_topic_present(topic, corpus):
            continue
        # "No tengo ese dato del procesador" no afirma nada: no gasta reintento.
        if _asserts(reply, pattern):
            return False
    return True


def check_format(reply: str, *, allow_table: bool = False) -> bool:
    text = (reply or "").strip()
    if not text:
        return False
    if not allow_table and _CODE_FENCE.search(text):
        return False
    if _MARKDOWN_HEADING.search(text):
        return False
    if _JSON_HINT.search(text):
        return False
    return True


def check_length(
    reply: str,
    *,
    max_chars: int = MAX_REPLY_CHARS,
    max_lines: int = MAX_LINES,
) -> bool:
    text = (reply or "").strip()
    if not text or len(text) > max_chars:
        return False
    paragraphs = [p for p in re.split(r"\n\s*\n", text) if p.strip()]
    if len(paragraphs) > MAX_PARAGRAPHS:
        return False
    lines = [line for line in text.splitlines() if line.strip()]
    return len(lines) <= max_lines


def validate_reply(
    reply: str, context: dict, *, table: bool = False, card: bool = False
) -> tuple[bool, str]:
    """Return ``(ok, reason)``; ``reason`` is a stable code for the event log.

    `card=True` is for the product card: one structured message with title,
    availability and specs. It needs more lines than a chat reply but must stay
    within a mobile screen.
    """
    if not check_format(reply, allow_table=table):
        return False, "format"
    if not check_length(
        reply,
        max_chars=1800 if table else (1400 if card else MAX_REPLY_CHARS),
        max_lines=20 if table else (12 if card else MAX_LINES),
    ):
        return False, "length"
    if not check_internal_terms(reply):
        return False, "internal_terms"
    if not check_no_internal_placeholders(reply):
        return False, "internal_placeholder"
    if not check_prompt_disclosure(reply):
        return False, "prompt_disclosure"
    if not check_prices(reply, allowed_prices(context)):
        return False, "price"
    if not check_specs(reply, context):
        return False, "spec"
    return True, ""


def fallback_reply(context: dict) -> str:
    """Deterministic, always-safe reply used when the model cannot comply."""
    product = context.get("product")
    # H-09: the fallback may only quote a product this turn actually resolved.
    # A product inherited from the previous turn would answer a different
    # question ("¿Tienes neveras?" -> Monitor LED 24).
    from_turn = bool(context.get("product_from_turn", True))
    price = product.get("price") if isinstance(product, dict) else None
    if (
        from_turn
        and isinstance(product, dict)
        and price is not None
        and product.get("status", "ACTIVE") == "ACTIVE"
        and (product.get("stock") or 0) > 0
    ):
        return prompts.TEMPLATES["fallback_product"].format(
            producto=product.get("name", "ese producto"),
            precio=f"{float(price):g}",
            moneda=product.get("currency", "USD"),
        )
    return prompts.TEMPLATES["ask_which_product"]
