"""PII masking for dashboard reads (decision: masked by default).

The database keeps the real phone and address because the bot needs them to
deliver. The dashboard, which is reachable over the LAN, only receives masked
values unless the operator explicitly asks to reveal them (F3, audited).

Masks are deterministic and never reversible; they are a display concern, not
encryption.
"""

import re


def mask_phone(raw: str | None) -> str | None:
    """``+53 55500001`` -> ``+53 5***-***01``. Short numbers are fully hidden."""
    if not raw:
        return None
    digits = re.sub(r"\D", "", str(raw))
    if len(digits) == 10 and digits.startswith("53"):
        digits = digits[2:]
    if len(digits) < 4:
        return "***"
    return f"+53 {digits[0]}***-***{digits[-2:]}"


def mask_address(raw: str | None) -> str | None:
    """Show a short hint (street type + first letters), never the full address."""
    text = str(raw or "").strip()
    if not text:
        return None
    if len(text) <= 8:
        return "***"
    return f"{text[:8]}…"


def mask_identifier(raw: str | None) -> str | None:
    """Keep public ``@handles``; mask numeric chat ids."""
    text = str(raw or "").strip()
    if not text:
        return None
    if text.startswith("@"):
        return text
    digits = re.sub(r"\D", "", text)
    if len(digits) >= 6:
        return f"{digits[:3]}***{digits[-2:]}"
    return text
