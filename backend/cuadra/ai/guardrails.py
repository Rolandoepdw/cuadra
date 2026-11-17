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
    "procesador": re.compile(r"microprocesador|procesador|\bcpu\b", re.IGNORECASE),
    "tarjeta gráfica": re.compile(
        r"\bgpu\b|tarjeta\s+gr[aá]fica|gr[aá]ficos", re.IGNORECASE
    ),
    "memoria RAM": re.compile(r"\bram\b|memoria", re.IGNORECASE),
    "almacenamiento": re.compile(
        r"almacenamiento|\bssd\b|\bhdd\b|disco\s+(?:duro|s[oó]lido)",
        re.IGNORECASE,
    ),
    "pantalla": re.compile(r"pantalla|\bdisplay\b|pulgadas", re.IGNORECASE),
    "batería": re.compile(r"bater[ií]a|autonom[ií]a|\bmah\b", re.IGNORECASE),
    "cámara": re.compile(r"c[aá]mara", re.IGNORECASE),
    "garantía": re.compile(r"garant[ií]a", re.IGNORECASE),
}


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
    """
    products = []
    seen = set()
    for value in (
        context.get("product"),
        *(context.get("products") or []),
        *(context.get("candidates") or []),
        *(context.get("alternatives") or []),
    ):
        if not isinstance(value, dict) or not isinstance(
            value.get("specifications"), dict
        ):
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
        for key, value in product["specifications"].items():
            parts.append(str(key))
            parts.append(str(value))
    return " ".join(parts)


def check_specs(reply: str, context: dict) -> bool:
    """False when the reply asserts a technical spec the catalog does not have."""
    corpus = _specs_corpus(context)
    if corpus is None:
        return True
    for pattern in SPEC_TOPICS.values():
        if not pattern.search(reply or ""):
            continue
        if pattern.search(corpus):
            continue
        # "No tengo ese dato del procesador" no afirma nada: no gasta reintento.
        if _asserts(reply, pattern):
            return False
    return True


def check_format(reply: str) -> bool:
    text = (reply or "").strip()
    if not text:
        return False
    if _CODE_FENCE.search(text):
        return False
    if _MARKDOWN_HEADING.search(text):
        return False
    if _JSON_HINT.search(text):
        return False
    return True


def check_length(reply: str, *, max_chars: int = MAX_REPLY_CHARS) -> bool:
    text = (reply or "").strip()
    if not text or len(text) > max_chars:
        return False
    paragraphs = [p for p in re.split(r"\n\s*\n", text) if p.strip()]
    if len(paragraphs) > MAX_PARAGRAPHS:
        return False
    lines = [line for line in text.splitlines() if line.strip()]
    return len(lines) <= MAX_LINES


def validate_reply(reply: str, context: dict) -> tuple[bool, str]:
    """Return ``(ok, reason)``; ``reason`` is a stable code for the event log."""
    if not check_format(reply):
        return False, "format"
    if not check_length(reply):
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
