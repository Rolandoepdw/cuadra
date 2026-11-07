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


def check_internal_terms(reply: str) -> bool:
    return _INTERNAL_TERMS.search(reply or "") is None


def check_prompt_disclosure(reply: str) -> bool:
    """False when the reply repeats the system prompt or internal rules."""
    return _PROMPT_DISCLOSURE.search(reply or "") is None


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
    if not check_prompt_disclosure(reply):
        return False, "prompt_disclosure"
    if not check_prices(reply, allowed_prices(context)):
        return False, "price"
    return True, ""


def fallback_reply(context: dict) -> str:
    """Deterministic, always-safe reply used when the model cannot comply."""
    product = context.get("product")
    price = product.get("price") if isinstance(product, dict) else None
    if (
        isinstance(product, dict)
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
