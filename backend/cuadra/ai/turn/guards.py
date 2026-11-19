"""Guardrails for deterministic replies: safe templates and contexts."""

import json
import logging

from cuadra.ai import guardrails
from cuadra.domain import events

log = logging.getLogger("cuadra.guards")


def _guard_product(product: dict | None) -> dict | None:
    """Expose parsed `specifications` to the guardrails.

    `get_product` parses `specifications_json`, but the rows returned by
    `search_products`/`find_product` only carry the raw column. Without this
    normalization `guardrails.check_specs` saw no specifications at all and let
    the composer invent them (prueba #4, "16 GB de RAM y 1 TB de SSD").
    """
    if product is None or isinstance(product.get("specifications"), dict):
        return product
    raw = product.get("specifications_json")
    if raw is None:
        return product
    try:
        parsed = json.loads(raw or "{}")
    except (TypeError, ValueError):
        parsed = {}
    normalized = dict(product)
    normalized["specifications"] = parsed if isinstance(parsed, dict) else {}
    return normalized


def _guard_context(
    product: dict | None,
    candidates: list[dict] | None,
    context: dict,
    *,
    extra_products: list[dict] | None = None,
    product_source: str = "",
) -> dict:
    products = []
    for value in (product, *(candidates or []), *(extra_products or [])):
        value = _guard_product(value)
        if value is not None:
            products.append(value)
    product = _guard_product(product)
    return {
        "product": product,
        # H-09: a product inherited from the context must not be reused by the
        # deterministic fallback when this turn did not mention it.
        "product_from_turn": product_source in ("id", "intent", "text"),
        "products": products,
        "quoted_prices": context.get("quoted_prices") or [],
    }


def _log_guardrail(conn, conversation, reason: str, stage: str) -> None:
    events.log(
        conn,
        "llm",
        "guardrail_violation",
        entity="conversations",
        entity_id=conversation["id"],
        details={"reason": reason, "stage": stage},
    )
    log.warning(
        "guardrail violation (%s/%s) in conversation %s",
        stage,
        reason,
        conversation["id"],
    )


def _safe_template(
    conn,
    conversation,
    reply: str,
    guard_context: dict,
    generator: str,
    *,
    table: bool = False,
    card: bool = False,
):
    """Validate a deterministic reply; fall back instead of sending garbage.

    `table=True` only for the catalog listing: it is the one reply that carries
    a ``` block (aligned table), and the format guard must allow it. `card=True`
    is the product card, which needs a few more lines than a chat reply.
    """
    ok, reason = guardrails.validate_reply(
        reply, guard_context, table=table, card=card
    )
    if ok:
        return reply, generator
    _log_guardrail(conn, conversation, reason, "template")
    return guardrails.fallback_reply(guard_context), "fallback"
