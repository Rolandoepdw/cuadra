"""Conversational references: product resolution and last listing."""

import logging
import re
import unicodedata
from dataclasses import dataclass

from cuadra.ai import retrieval
from cuadra.domain import catalog

log = logging.getLogger("cuadra.references")

from cuadra.ai.turn.constants import (
    _ID_BARE,
    _ID_PREFIXED,
)


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


# ------------------------------------------------------------------ references
@dataclass(frozen=True)
class Reference:
    """A resolved conversational reference ("esos", "el segundo", "el mismo")."""

    kind: str
    product_id: int | None = None
    product_ids: tuple[int, ...] = ()
    reason: str = ""
    needs_clarification: bool = False


_ORDINALS = {
    "primer": 0, "primero": 0, "primera": 0,
    "segundo": 1, "segunda": 1,
    "tercer": 2, "tercero": 2, "tercera": 2,
    "cuarto": 3, "cuarta": 3,
    "quinto": 4, "quinta": 4,
    "sexto": 5, "sexta": 5,
    "septimo": 6, "septima": 6,
    "octavo": 7, "octava": 7,
    "noveno": 8, "novena": 8,
    "decimo": 9, "decima": 9,
    "ultimo": -1, "ultima": -1,
}
_ORDINAL_RE = re.compile(
    r"\b(" + "|".join(sorted(_ORDINALS, key=len, reverse=True)) + r")\b"
)
_DEMONSTRATIVE_RE = re.compile(
    r"\b(?:esos|esas|estos|estas|aquellos|aquellas|todos\s+esos|todas\s+esas)\b"
)
_FOCUS_RE = re.compile(
    r"\b(?:el\s+de\s+antes|la\s+de\s+antes|el\s+mismo|la\s+misma|"
    r"ese\s+mismo|esa\s+misma|aquel|aquella|ese|esa|eso)\b"
)
_NUMBER_WORDS = {"dos": 2, "tres": 3, "cuatro": 4, "cinco": 5}


def _normalize(text: str) -> str:
    folded = unicodedata.normalize("NFKD", (text or "").lower())
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", folded).strip()


def resolve_reference(text: str, frame) -> Reference | None:
    """Resolve a conversational reference against the frame.

    Returns `None` when the text has no reference. Ambiguity returns a
    `Reference` with `needs_clarification=True`: the policy must ask, never
    guess. In L3.0 the result is only logged; L3.1 uses it to pick a product.
    """
    body = _normalize(text)
    if not body:
        return None
    listing = getattr(frame, "last_listing", None)
    ids = list(getattr(listing, "product_ids", []) or [])
    focus = getattr(frame, "focus_product", None)
    context = getattr(frame, "context", {}) or {}
    context_id = context.get("product_id")

    ordinal = _ORDINAL_RE.search(body)
    if ordinal:
        word = ordinal.group(1)
        index = _ORDINALS[word]
        if not ids:
            return Reference(
                "ordinal", reason="no_listing", needs_clarification=True
            )
        if index >= len(ids) or index < -len(ids):
            return Reference(
                "ordinal", reason="out_of_range", needs_clarification=True
            )
        product_id = ids[index]
        return Reference(
            "ordinal", product_id=product_id, product_ids=(product_id,), reason=word
        )

    if _DEMONSTRATIVE_RE.search(body):
        if not ids:
            return Reference(
                "demonstrative", reason="no_listing", needs_clarification=True
            )
        count = len(ids)
        for word, value in _NUMBER_WORDS.items():
            if re.search(rf"\b{word}\b", body):
                count = min(count, value)
                break
        selected = tuple(ids[:count])
        return Reference(
            "demonstrative",
            product_id=selected[0] if len(selected) == 1 else None,
            product_ids=selected,
            reason=f"{count} of {len(ids)}",
        )

    if _FOCUS_RE.search(body):
        if focus and focus.get("id") is not None:
            return Reference(
                "focus", product_id=int(focus["id"]), product_ids=(int(focus["id"]),)
            )
        if context_id:
            return Reference(
                "focus", product_id=int(context_id), product_ids=(int(context_id),)
            )
        if len(ids) == 1:
            return Reference("focus", product_id=ids[0], product_ids=(ids[0],))
        if len(ids) > 1:
            return Reference(
                "focus", product_ids=tuple(ids), reason="ambiguous",
                needs_clarification=True,
            )
        return Reference("focus", reason="no_reference", needs_clarification=True)

    return None

