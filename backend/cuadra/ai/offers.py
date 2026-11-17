"""Pending offers: the question the bot asked that the customer can accept.

A bare "Si" only means something against the question that preceded it. The
turn that asks an offer stores it in the conversation context; the next turn
consumes it. An offer never survives a second turn, so an old "¿Te reservo?"
cannot be answered three messages later.

Kinds:

- `reserve`: the bot offered to reserve the product (the normal closing
  question). "Si" starts/advances the sale, "No" declines it.
- `alternatives`: the bot offered to show alternatives of an out-of-stock
  product. "Si" lists them.
- `siblings`: the bot offered to show other models of the same family.
  "Si" lists them.
"""

import re

from cuadra.domain import catalog

RESERVE = "reserve"
ALTERNATIVES = "alternatives"
SIBLINGS = "siblings"

# Only a question counts: "Ya te lo reservé" is a confirmation, not an offer.
_RESERVE = re.compile(r"¿[^?]*\bres[eé]rv", re.IGNORECASE)
_ALTERNATIVES = re.compile(r"¿[^?]*echo un vistazo", re.IGNORECASE)
_SIBLINGS = re.compile(
    r"¿[^?]*(enseño|pase el detalle|cotizo|muestro)", re.IGNORECASE
)

_YES = {
    "si", "claro", "dale", "ok", "okay", "vale", "perfecto", "confirmo",
    "confirmado", "vamos", "quiero", "llevo", "reservamela", "reservalo",
    "reservala", "palante", "listo", "acuerdo", "supuesto", "obvio", "seguro",
}
_NO = {
    "no", "nop", "nel", "todavia", "ahora", "dejalo", "pienso", "despues",
    "nada",
}
# Words that add politeness but do not change the answer.
_FILLER = {
    "por", "favor", "gracias", "pues", "entonces", "ya", "de", "lo", "la",
    "el", "me", "mi", "uno", "una", "que", "para", "amigo", "hermano",
    "senor", "senora",
}


def detect(reply: str | None, product_id: int | None = None) -> dict | None:
    """Return the offer the bot just made, if any."""
    text = reply or ""
    for kind, pattern in (
        (RESERVE, _RESERVE),
        (ALTERNATIVES, _ALTERNATIVES),
        (SIBLINGS, _SIBLINGS),
    ):
        if pattern.search(text):
            return {"kind": kind, "product_id": product_id}
    return None


def save(context: dict, offer: dict | None) -> None:
    """Store (or clear) the offer that the next turn may answer."""
    if offer is None:
        context.pop("pending_offer", None)
    else:
        context["pending_offer"] = offer


def consume(context: dict) -> dict | None:
    """Take the offer of the previous turn; it cannot be answered twice."""
    return context.pop("pending_offer", None)


def _words(text: str) -> list[str]:
    folded = catalog._fold(text)
    cleaned = "".join(c if c.isalnum() else " " for c in folded)
    return cleaned.split()


def answer(text: str | None) -> str | None:
    """Classify a *bare* answer to an offer: `"yes"`, `"no"` or None.

    A question or a sentence ("Si, que procesador tiene") is not an answer to
    the previous offer; it is a new turn and must be processed normally.
    """
    body = text or ""
    if "?" in body or "¿" in body:
        return None
    words = [word for word in _words(body) if word not in _FILLER]
    if not words or len(words) > 3:
        return None
    # Every remaining word must be part of the answer. "Vale, cuando me la
    # traes" starts with "vale" but asks a question, so it is not a yes.
    if not all(word in _YES or word in _NO for word in words):
        return None
    if any(word in _NO for word in words):
        return "no"
    return "yes"
