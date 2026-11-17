"""TCP message triage: case codes, correlation and conversation resumption.

Two responsibilities, both deterministic and testable without the model:

- `parse_case_ids` reads `[E-0104]` / `[V-000042]`, including the forms Whisper
  produces ("E 104", "e104").
- `correlate` decides which escalation/product a message refers to. Ambiguity
  never auto-applies (doc 03 §3.2): it returns `ambiguous=True` and the caller
  records a PENDING_REVIEW mutation.

`resume_escalations` closes the loop: it answers every waiting client of the
product (deduplicated by conversation) using a vetted summary, never raw TCP
text.
"""

import re
from dataclasses import dataclass, field

from cuadra.ai import guardrails, offers, prompts, retrieval
from cuadra.domain import catalog, conversations, escalations, events

CASE_ID_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])\[?([EV])[-_ ]?(\d{1,6})\]?", re.IGNORECASE
)
_ORDER_CODE = re.compile(r"\bV-(\d{6})\b", re.IGNORECASE)

CORRELATION_THRESHOLD = 0.35
CORRELATION_MARGIN = 0.15

_STOPWORDS = {
    "de", "del", "la", "el", "los", "las", "un", "una", "y", "o", "para",
    "con", "que", "por", "me", "mi", "te", "se", "es", "en", "al", "a",
    "lo", "su", "sus", "mas", "más", "pero", "como", "esta", "este",
    "producto", "productos", "tengo", "tienes", "hay", "del", "los",
}

_DELIVERED = re.compile(
    r"\b(ENTREGAD[OA]|ENTREGO|ENTREGUE|COBRAD[OA]|COBRE|VENDID[OA])\b"
)
_REJECTED = re.compile(
    r"\b(AGOTAD[OA]|SIN\s+STOCK|NO\s+HAY|NO\s+ME\s+QUEDA|NO\s+PUEDO)\b"
)
_ACK = re.compile(r"\b(RECIBID[OA]|CONFIRMAD[OA]|PREPARAD[OA]|LISTO)\b")
_CATALOG_HINT = re.compile(
    r"\b(PRECIO|STOCK|QUEDAN|QUEDA|BAJE|BAJO|SUBI[OÓ]|SUBE|DESCUENTO|"
    r"MONEDA|CUP|USD|MLC|GARANTIA|GARANTÍA|DISPONIBLE|DISPONIBILIDAD|"
    r"ZONA|ENTREGA|ENTREGO|REBAJA|CUESTA)\b"
)
_QUESTION = re.compile(r"^\s*(QUE|QUÉ|COMO|CÓMO|CUANDO|CUÁNDO|DONDE|DÓNDE|"
                       r"PUEDES|TIENES|HAY)\b|\?\s*$")


def _fold(text: str) -> str:
    return catalog._fold(text or "")


def parse_case_ids(text: str) -> tuple[list[int], list[str]]:
    """Return `(escalation_ids, order_codes)` found in the message.

    `E-104`/`e 104`/`[E-0104]` -> escalation 104; `V-000042` -> `V-000042`.
    Digits are kept as-is except for V codes, which are zero-padded to six.
    """
    escalation_ids: list[int] = []
    order_codes: list[str] = []
    for match in CASE_ID_PATTERN.finditer(text or ""):
        kind, digits = match.group(1).upper(), match.group(2)
        if kind == "E":
            value = int(digits)
            if value > 0 and value not in escalation_ids:
                escalation_ids.append(value)
        else:
            code = f"V-{int(digits):06d}"
            if code not in order_codes:
                order_codes.append(code)
    return escalation_ids, order_codes


def strip_case_ids(text: str) -> str:
    """Remove `[E-###]`/`[V-######]` references from customer-facing text."""
    return CASE_ID_PATTERN.sub("", text or "").strip()


def classify(text: str) -> str:
    """Coarse triage label for the audit log (never drives the mutation)."""
    body = _fold(text).upper()
    escalation_ids, order_codes = parse_case_ids(text or "")
    if escalation_ids:
        return "ESCALATION_ANSWER"
    if order_codes:
        return "ORDER_CASE"
    if _DELIVERED.search(body):
        return "ORDER_DELIVERED"
    if _REJECTED.search(body):
        return "ORDER_REJECTED"
    if _ACK.search(body):
        return "ORDER_ACK"
    if _CATALOG_HINT.search(body):
        return "CATALOG_UPDATE"
    if _QUESTION.search((text or "").strip().upper()):
        return "QUESTION"
    return "OTHER"


def _tokens(text: str) -> set[str]:
    cleaned = "".join(char if char.isalnum() else " " for char in _fold(text).lower())
    return {
        token
        for token in cleaned.split()
        if len(token) > 2 and token not in _STOPWORDS
    }


def _lexical_score(text: str, product_name: str) -> float:
    terms = _tokens(product_name)
    if not terms:
        return 0.0
    folded = _fold(text).lower()
    hits = sum(1 for term in terms if term in folded)
    return hits / len(terms)


def _rank_bonus(conn, text: str, product_ids: list[int]) -> dict[int, float]:
    if not product_ids:
        return {}
    try:
        ranked = retrieval.search_ids(conn, text, top_k=max(10, len(product_ids)))
    except Exception:  # noqa: BLE001 - correlation must survive retrieval errors
        ranked = []
    total = max(len(ranked), 1)
    return {
        int(product_id): (total - index) / total
        for index, product_id in enumerate(ranked)
        if int(product_id) in product_ids
    }


def _score_products(conn, text: str, products: list[dict]) -> list[tuple[dict, float]]:
    ids = [int(product["id"]) for product in products]
    bonus = _rank_bonus(conn, text, ids)
    scored = []
    for product in products:
        lexical = _lexical_score(text, product["name"])
        score = min(1.0, 0.7 * lexical + 0.3 * bonus.get(int(product["id"]), 0.0))
        scored.append((product, round(score, 4)))
    scored.sort(key=lambda item: (-item[1], int(item[0]["id"])))
    return scored


@dataclass
class Correlation:
    supplier_id: int
    escalation: dict | None = None
    product_id: int | None = None
    method: str = "manual"
    confidence: float = 0.0
    ambiguous: bool = False
    reason: str = ""
    candidates: list[dict] = field(default_factory=list)

    @property
    def found(self) -> bool:
        return self.escalation is not None or self.product_id is not None


def _with_product(correlation: Correlation, escalation: dict | None) -> Correlation:
    if escalation is not None and escalation.get("product_id"):
        correlation.product_id = int(escalation["product_id"])
    return correlation

def _supplier_products(conn, supplier_id: int) -> list[dict]:
    return [
        product
        for product in catalog.list_products(conn, supplier_id=supplier_id)
        if product["status"] in ("ACTIVE", "OUT_OF_STOCK")
    ]


def correlate(conn, *, supplier_id: int, text: str) -> Correlation:
    """Pick the escalation/product this TCP message refers to.

    Order: explicit `[E-###]`, single pending escalation, heuristic by product
    name. Multiple candidates without a clear winner set `ambiguous=True`.
    """
    escalation_ids, _ = parse_case_ids(text or "")
    if escalation_ids:
        for escalation_id in escalation_ids:
            try:
                escalation = escalations.get(conn, escalation_id)
            except escalations.NotFound:
                continue
            if int(escalation["supplier_id"]) != int(supplier_id):
                continue
            if escalation["status"] not in escalations.OPEN_STATUSES:
                return Correlation(
                    supplier_id=supplier_id,
                    escalation=escalation,
                    method="case_id",
                    confidence=1.0,
                    reason=f"case_not_open:{escalation['status']}",
                )
            return _with_product(
                Correlation(
                    supplier_id=supplier_id,
                    escalation=escalation,
                    method="case_id",
                    confidence=1.0,
                ),
                escalation,
            )
        return Correlation(
            supplier_id=supplier_id,
            method="case_id",
            reason="case_not_found",
        )

    pending = escalations.pending_for_supplier(conn, supplier_id)
    if len(pending) == 1:
        escalation = pending[0]
        return _with_product(
            Correlation(
                supplier_id=supplier_id,
                escalation=escalation,
                method="single_pending",
                confidence=0.95,
                candidates=[escalation],
            ),
            escalation,
        )

    if len(pending) > 1:
        products_by_id: dict[int, dict] = {}
        for escalation in pending:
            if not escalation.get("product_id"):
                continue
            product_id = int(escalation["product_id"])
            if product_id in products_by_id:
                continue
            try:
                products_by_id[product_id] = catalog.get_product(conn, product_id)
            except catalog.NotFound:
                continue
        products = list(products_by_id.values())
        scored = _score_products(conn, text, products)
        if not scored:
            return Correlation(
                supplier_id=supplier_id,
                method="heuristic",
                ambiguous=True,
                reason="pending_without_products",
                candidates=pending,
            )
        best_product, best_score = scored[0]
        second_score = scored[1][1] if len(scored) > 1 else 0.0
        best_escalation = next(
            (
                escalation
                for escalation in pending
                if escalation.get("product_id") == best_product["id"]
            ),
            pending[0],
        )
        clear = (
            best_score >= CORRELATION_THRESHOLD
            and best_score - second_score >= CORRELATION_MARGIN
        )
        return _with_product(
            Correlation(
                supplier_id=supplier_id,
                escalation=best_escalation,
                method="heuristic",
                confidence=best_score,
                ambiguous=not clear,
                reason="" if clear else "multiple_pending",
                candidates=pending,
            ),
            best_escalation,
        )

    # No pending escalation: a crash may have left the row ANSWERED already.
    retried = escalations.answered_recent_for_supplier(conn, supplier_id, text)
    if retried is not None:
        return _with_product(
            Correlation(
                supplier_id=supplier_id,
                escalation=retried,
                method="single_pending",
                confidence=0.95,
                reason="retry_answered",
            ),
            retried,
        )

    # Proactive update ("bajé el precio"): match the product by name.
    products = _supplier_products(conn, supplier_id)
    scored = _score_products(conn, text, products)
    if not scored:
        return Correlation(supplier_id=supplier_id, method="heuristic")
    best_product, best_score = scored[0]
    second_score = scored[1][1] if len(scored) > 1 else 0.0
    clear = (
        best_score >= CORRELATION_THRESHOLD
        and best_score - second_score >= CORRELATION_MARGIN
    )
    return Correlation(
        supplier_id=supplier_id,
        product_id=int(best_product["id"]),
        method="heuristic",
        confidence=best_score,
        ambiguous=not clear,
        reason="" if clear else "no_escalation_ambiguous",
    )


# ------------------------------------------------------------------ resumption

_MAX_CUSTOMER_ANSWER = 400
_MENTIONED_CURRENCY = re.compile(r"\b(USD|CUP|MLC)\b", re.IGNORECASE)
_NO_STOCK = re.compile(
    r"\b(?:0\s*unidades|agotad[oa]|sin\s+stock|"
    r"no\s+(?:hay|queda|quedan|tenemos|me\s+queda))\b",
    re.IGNORECASE,
)
# Un mensaje del TCP que reclama datos o niega el proceso no es una respuesta
# de producto. Prueba #3: "No me has dado los datos de la venta..." se guardó
# como especificación y se reenvió literal al cliente (F2/F3). "No tengo
# unidades" sí es una respuesta y no entra aquí.
_NOT_AN_ANSWER = re.compile(
    r"\bno\s+me\s+(?:has|ha|han|diste|dio|dieron)\b|"
    r"\bno\s+te\s+puedo\s+confirmar\b|"
    r"\bno\s+puedo\s+confirmar\b|"
    r"\bno\s+tengo\s+(?:ni\s+)?(?:los\s+datos|la\s+direcci[oó]n|"
    r"el\s+tel[eé]fono|los\s+detalles)\b|"
    r"\bno\s+(?:s[eé]|tengo\s+ese\s+dato|dispongo\s+de)\b",
    re.IGNORECASE,
)


def looks_like_answer(text: str | None) -> bool:
    """False when a TCP message complains or refuses instead of answering."""
    return not _NOT_AN_ANSWER.search(text or "")


def _sanitize_customer_text(text: str | None) -> str:
    """Strip case codes, internal sentences and stray whitespace (H-22)."""
    clean = guardrails.redact_internal_terms(strip_case_ids(text or ""))
    clean = " ".join(clean.split())
    if len(clean) > _MAX_CUSTOMER_ANSWER:
        cut = clean[:_MAX_CUSTOMER_ANSWER].rstrip()
        clean = cut.rsplit(" ", 1)[0] + "…" if " " in cut else cut + "…"
    if clean and clean[-1] not in ".!?…":
        clean += "."
    return clean


def _answer_currencies_ok(text: str, product: dict | None) -> bool:
    """A raw TCP echo may only mention the currency the catalog stores (H-22).

    `4200 MLC` is never a valid customer price (H-21); dropping the candidate
    makes the caller fall back to the vetted summary or the generic template.
    """
    mentioned = {match.upper() for match in _MENTIONED_CURRENCY.findall(text)}
    if not mentioned:
        return True
    allowed = {str((product or {}).get("currency") or "").upper()}
    return mentioned <= allowed


def _customer_answers(
    answer: str | None, summary: str | None, product: dict | None
) -> list[str]:
    """Complete TCP answer first, then the model summary (H-22).

    The extractor summary is a paraphrase and used to drop details the supplier
    did answer (maletín, recetario, garantía). The raw answer is preferred when
    it survives sanitisation; the summary remains the fallback for text that
    cannot be shown verbatim (internal terms, foreign currency, bad price).
    """
    candidates: list[str] = []
    for value in (answer, summary):
        if not looks_like_answer(value):
            continue
        clean = _sanitize_customer_text(value)
        if clean and clean not in candidates and _answer_currencies_ok(clean, product):
            candidates.append(clean)
    return candidates


def _customer_reply(
    product: dict | None,
    summary: str | None = None,
    *,
    answer: str | None = None,
) -> str:
    product_name = product["name"] if product else "el producto"
    # H-20: never offer to reserve a product that is out of stock or paused.
    available = bool(
        product
        and product.get("status") == "ACTIVE"
        and (product.get("stock") or 0) > 0
    )
    context = {
        "quoted_prices": (
            [{"amount": product["price"], "currency": product["currency"]}]
            if product
            else []
        )
    }
    for respuesta in _customer_answers(answer, summary, product):
        if available:
            template = "escalation_resolved_summary"
        elif _NO_STOCK.search(respuesta):
            template = "escalation_resolved_out_of_stock_known"
        else:
            template = "escalation_resolved_out_of_stock"
        candidate = prompts.TEMPLATES[template].format(
            producto=product_name, respuesta=respuesta
        )
        ok, _reason = guardrails.validate_reply(candidate, context)
        if ok:
            return candidate
    # Ningún candidato utilizable: ni una queja del TCP ni un dato con moneda
    # ajena se reenvían. Se dice la verdad: el dato sigue pendiente.
    return prompts.TEMPLATES["escalation_unconfirmed"].format(producto=product_name)


def _operator_decision_reply(product: dict | None, kind: str) -> str:
    """Safe customer text for a rejected or reverted catalog mutation (H-21)."""
    if kind == "reverted" and product is not None:
        candidate = prompts.TEMPLATES["mutation_reverted"].format(
            producto=product["name"],
            precio=f"{float(product['price']):g}",
            moneda=product["currency"],
        )
        context = {
            "quoted_prices": [
                {"amount": product["price"], "currency": product["currency"]}
            ]
        }
        ok, _reason = guardrails.validate_reply(candidate, context)
        if ok:
            return candidate
        return prompts.TEMPLATES["mutation_reverted_generic"]
    return prompts.TEMPLATES["mutation_rejected"]


def notify_mutation_outcome(conn, mutation: dict, *, kind: str) -> list[tuple[int, int]]:
    """Answer the waiting client after the operator rejects or reverts a change.

    The escalation is closed even when the data could not be confirmed, so the
    client never waits forever without an answer. Returns `(conversation_id,
    outbound_message_id)` pairs for the caller to enqueue `send_message` jobs.
    """
    if kind not in ("rejected", "reverted"):
        raise ValueError(f"unsupported mutation outcome: {kind}")

    product = None
    if mutation.get("product_id"):
        try:
            product = catalog.get_product(conn, mutation["product_id"])
        except catalog.NotFound:
            product = None

    targets: list[dict] = []
    escalation_id = mutation.get("escalation_id")
    if escalation_id:
        try:
            targets = [escalations.get(conn, int(escalation_id))]
        except escalations.NotFound:
            targets = []
    if not targets and mutation.get("product_id") and mutation.get("supplier_id"):
        targets = escalations.open_for_product(
            conn, mutation["product_id"], mutation["supplier_id"]
        )

    reply = _operator_decision_reply(product, kind)
    sends: list[tuple[int, int]] = []
    seen: set[int] = set()
    for escalation in targets:
        conversation_id = int(escalation["conversation_id"])
        if conversation_id in seen:
            continue
        seen.add(conversation_id)
        conversation = conversations.get_conversation(conn, conversation_id)
        outbound = None
        if conversation["status"] in conversations.OPEN_STATUSES:
            outbound = conversations.add_message(
                conn, conversation_id, "OUTBOUND", "bot", body=reply
            )
            conversations.set_status(conn, conversation_id, "ACTIVE")
        if escalation["status"] in escalations.OPEN_STATUSES:
            escalations.resolve(conn, int(escalation["id"]))
        events.log(
            conn,
            "bot",
            f"catalog_mutation_{kind}_notified",
            entity="escalations",
            entity_id=int(escalation["id"]),
            details={
                "mutation_id": mutation["id"],
                "product_id": mutation.get("product_id"),
                "reply_sent": outbound is not None,
            },
        )
        if outbound is not None:
            sends.append((conversation_id, int(outbound["id"])))
    return sends


def resume_escalations(
    conn,
    *,
    product_id: int,
    supplier_id: int,
    answer: str,
    summary: str | None = None,
) -> list[tuple[int, int]]:
    """Resolve every open escalation of the product and answer each client once.

    Returns `(conversation_id, outbound_message_id)` pairs for the caller to
    enqueue `send_message` jobs.
    """
    open_rows = escalations.open_for_product(conn, product_id, supplier_id)
    if not open_rows:
        return []

    try:
        product = catalog.get_product(conn, product_id)
    except catalog.NotFound:
        product = None

    answer = (answer or "").strip()

    grouped: dict[int, list[dict]] = {}
    for escalation in open_rows:
        grouped.setdefault(int(escalation["conversation_id"]), []).append(escalation)

    reply = _customer_reply(product, summary, answer=answer)
    sends: list[tuple[int, int]] = []
    for conversation_id, group in grouped.items():
        conversation = conversations.get_conversation(conn, conversation_id)
        outbound = None
        if conversation["status"] in conversations.OPEN_STATUSES:
            outbound = conversations.add_message(
                conn, conversation_id, "OUTBOUND", "bot", body=reply
            )
            conversations.set_status(conn, conversation_id, "ACTIVE")
            # F5: la respuesta del almacén termina en "¿Te reservo ...?"; el
            # siguiente "Si" del cliente debe ejecutar esa oferta.
            context = conversations.get_context(conn, conversation_id)
            offers.save(context, offers.detect(reply, product_id))
            conversations.set_context(conn, conversation_id, context)
        for escalation in group:
            escalations.resolve(conn, int(escalation["id"]))
            events.log(
                conn,
                "bot",
                "escalation_resolved",
                entity="escalations",
                entity_id=int(escalation["id"]),
                details={
                    "supplier_id": supplier_id,
                    "product_id": product_id,
                    "answer": (answer or "")[:500],
                    "resumed": outbound is not None,
                },
            )
        if outbound is not None:
            sends.append((conversation_id, int(outbound["id"])))
    return sends
