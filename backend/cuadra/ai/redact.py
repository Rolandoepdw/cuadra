"""PII redaction for LLM traces (decision D7).

Traces are useful for evaluation, but they must not become a second copy of the
customer database. This module masks the two fields that actually identify a
person in a chat: phone numbers and delivery addresses. It is intentionally
conservative with numbers so prices survive:

- any 8-digit Cuban number (with optional ``+53`` and separators) is masked;
- 6-7 digit numbers are masked only next to a phone keyword, because a bare
  6-digit number is usually a CUP price;
- emails and address-like phrases are masked too.

The output is deterministic: the same input always produces the same trace.
"""

import json
import re

PHONE = "[TEL]"
ADDRESS = "[DIR]"
EMAIL = "[EMAIL]"

# Cuban numbers are 8 digits. Do not match a slice of a longer number or id.
_PHONE_8 = re.compile(r"(?<![\w])(?:\+?53[\s.-]?)?(?:\d[\s.-]?){7}\d(?![\w])")

# Short numbers are only phones when the text says so ("tel 5555555").
_PHONE_CONTEXT = re.compile(
    r"\b(tel(?:e)?fono|tel|celular|movil|whatsapp|llamar|contacto)\b"
    r"[^\n,;]{0,20}?((?:\+?53[\s.-]?)?(?:\d[\s.-]?){5,7}\d)(?![\w])",
    re.IGNORECASE,
)

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")

_ADDRESS = re.compile(
    r"\b(calle|avenida|av\.?|carretera|calzada|reparto|edificio|apto\.?|"
    r"apartamento|batey|finca|local)\b[^\n,;.]{0,80}",
    re.IGNORECASE,
)

_HOUSE_NUMBER = re.compile(
    r"\bcasa\s+(?:#\s*)?\d+[^\n,;.]{0,60}",
    re.IGNORECASE,
)

TRACE_MAX_CHARS = 8000
_TRUNCATED = "\n...[truncated]"


def text(value: str | None) -> str | None:
    """Return ``value`` with phones, emails and addresses masked."""
    if not value:
        return value
    masked = _EMAIL.sub(EMAIL, value)
    masked = _PHONE_8.sub(PHONE, masked)
    masked = _PHONE_CONTEXT.sub(lambda m: f"{m.group(1)} {PHONE}", masked)
    masked = _ADDRESS.sub(ADDRESS, masked)
    masked = _HOUSE_NUMBER.sub(ADDRESS, masked)
    return masked


def messages(items: list[dict] | None) -> str | None:
    """Serialize chat messages for storage, masking every content string."""
    if not items:
        return None
    safe = []
    for item in items:
        content = item.get("content")
        safe.append(
            {
                "role": item.get("role"),
                "content": text(content) if isinstance(content, str) else content,
            }
        )
    return truncate(json.dumps(safe, ensure_ascii=False))


def truncate(value: str | None, limit: int = TRACE_MAX_CHARS) -> str | None:
    if value is None or len(value) <= limit:
        return value
    return value[:limit] + _TRUNCATED


def completion(value: str | None) -> str | None:
    return truncate(text(value))
