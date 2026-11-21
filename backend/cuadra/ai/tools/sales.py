"""Sales, offers, zones and logistics reads (L3.2).

The deterministic route already talks to these domains directly. These tools
give the same reads a stable shape, an `empty_reason` and evidence, so L3.6 can
let the model propose them without rewriting the state machine. Writes are not
here: `domain/sales` remains the only writer.

Contract: an empty answer is `ok=True` with `empty_reason` (`no_sale`,
`no_pending_offer`, `unknown_zone`, `no_zones`, `no_tcp_eta`).
"""

from __future__ import annotations

import json

from pydantic import BaseModel, Field

from cuadra.ai.tools.base import (
    NOT_FOUND,
    READ,
    Registry,
    ToolError,
    ToolSpec,
    timeout_seconds,
)
from cuadra.domain import catalog, conversations, sales as sales_domain


class StatusArgs(BaseModel):
    sale_id: int | None = Field(default=None, ge=1)
    order_code: str | None = Field(default=None, max_length=20)
    conversation_id: int | None = Field(default=None, ge=1)


class HistoryArgs(BaseModel):
    conversation_id: int = Field(ge=1)
    limit: int = Field(default=5, ge=1, le=20)


class PendingOfferArgs(BaseModel):
    conversation_id: int = Field(ge=1)


class ZoneCheckArgs(BaseModel):
    product_id: int = Field(ge=1)
    zone: str = Field(min_length=1, max_length=120)


class ZoneListArgs(BaseModel):
    product_id: int = Field(ge=1)


class EtaArgs(BaseModel):
    product_id: int = Field(ge=1)
    zone_id: int | None = Field(default=None, ge=1)


def _product_name(sale: dict) -> str | None:
    try:
        context = json.loads(sale.get("context_json") or "{}")
    except (TypeError, ValueError):
        return None
    name = context.get("product_name") if isinstance(context, dict) else None
    return str(name) if name else None


def _summary(sale: dict) -> dict:
    """Customer-safe sale view: no commission, payout or contact data."""
    return {
        "sale_id": int(sale["id"]),
        "order_code": sale.get("order_code"),
        "status": sale.get("status"),
        "product_id": sale.get("product_id"),
        "product_name": _product_name(sale),
        "quantity": sale.get("quantity"),
        "amount": sale.get("amount"),
        "currency": sale.get("currency"),
        "delivery_zone": sale.get("delivery_zone"),
        "created_at": sale.get("created_at"),
        "delivered_at": sale.get("delivered_at"),
        "cancelled_at": sale.get("cancelled_at"),
    }


def status(
    conn,
    sale_id: int | None = None,
    order_code: str | None = None,
    conversation_id: int | None = None,
) -> dict:
    sale = None
    if sale_id is not None:
        try:
            sale = sales_domain.get_sale(conn, int(sale_id))
        except sales_domain.NotFound:
            sale = None
    elif order_code:
        sale = sales_domain.get_sale_by_code(conn, order_code)
    elif conversation_id is not None:
        sale = sales_domain.active_sale_for_conversation(conn, int(conversation_id))
    if sale is None:
        return {"empty_reason": "no_sale"}
    return {**_summary(sale), "empty_reason": None}


def history(conn, conversation_id: int, limit: int = 5) -> dict:
    rows = sales_domain.list_sales(
        conn, conversation_id=int(conversation_id), limit=int(limit)
    )
    sales = [_summary(row) for row in rows]
    return {
        "conversation_id": int(conversation_id),
        "sales": sales,
        "empty_reason": None if sales else "no_sale",
    }


def pending_offer(conn, conversation_id: int) -> dict:
    context = conversations.get_context(conn, int(conversation_id))
    offer = context.get("pending_offer")
    if not isinstance(offer, dict) or not offer:
        return {
            "conversation_id": int(conversation_id),
            "empty_reason": "no_pending_offer",
        }
    return {
        "conversation_id": int(conversation_id),
        "kind": offer.get("kind"),
        "product_id": offer.get("product_id"),
        "product_ids": [int(value) for value in offer.get("product_ids") or []],
        "query": offer.get("query") or "",
        "empty_reason": None,
    }


def _product(conn, product_id: int) -> dict:
    try:
        return catalog.get_product(conn, int(product_id))
    except catalog.NotFound as exc:
        raise ToolError(NOT_FOUND, f"product {product_id} not found") from exc


def zones_check(conn, product_id: int, zone: str) -> dict:
    product = _product(conn, product_id)
    resolved = catalog.resolve_zones(conn, zone, active_only=False)
    effective = catalog.effective_delivery_zone_names(conn, product)
    if not resolved:
        return {
            "product_id": product["id"],
            "zone": zone,
            "covered": None,
            "effective_zones": effective,
            "empty_reason": "unknown_zone",
        }
    first = resolved[0]
    covered = bool(first.get("active")) and catalog.product_covers_zone(
        conn, product, int(first["id"])
    )
    return {
        "product_id": product["id"],
        "zone_id": int(first["id"]),
        "zone_name": first["name"],
        "covered": covered,
        "source": "product"
        if catalog.product_zone_ids(conn, product["id"])
        else "supplier",
        "effective_zones": effective,
        "matches": [
            {"zone_id": int(item["id"]), "zone_name": item["name"]}
            for item in resolved
        ],
        "empty_reason": None,
    }


def zones_list(conn, product_id: int) -> dict:
    product = _product(conn, product_id)
    zones = catalog.effective_delivery_zones(conn, product)
    names = [str(zone["name"]) for zone in zones]
    return {
        "product_id": product["id"],
        "zones": names,
        "empty_reason": None if names else "no_zones",
    }


def eta(conn, product_id: int, zone_id: int | None = None) -> dict:
    """The TCP is the only source of a delivery date. Without it, say so."""
    _product(conn, product_id)
    return {
        "product_id": int(product_id),
        "zone_id": zone_id,
        "eta": None,
        "source": None,
        "as_of": None,
        "empty_reason": "no_tcp_eta",
    }


def register(registry: Registry) -> None:
    specs = (
        (
            "sales.status",
            "sales",
            "Estado real de una venta por id, código o conversación.",
            StatusArgs,
            status,
        ),
        (
            "sales.history",
            "sales",
            "Ventas de una conversación, de la más reciente a la más antigua.",
            HistoryArgs,
            history,
        ),
        (
            "offers.pending",
            "offers",
            "Oferta que el bot dejó pendiente en la conversación.",
            PendingOfferArgs,
            pending_offer,
        ),
        (
            "zones.check",
            "zones",
            "Confirma si un producto entrega en una zona.",
            ZoneCheckArgs,
            zones_check,
        ),
        (
            "zones.list",
            "zones",
            "Zonas efectivas de entrega de un producto.",
            ZoneListArgs,
            zones_list,
        ),
        (
            "logistics.eta",
            "logistics",
            "Fecha de entrega confirmada por el TCP, si existe.",
            EtaArgs,
            eta,
        ),
    )
    for name, owner, description, model, fn in specs:
        registry.register(
            ToolSpec(
                name=name,
                domain=owner,
                owner=owner,
                description=description,
                input_model=model,
                permission=READ,
                timeout_s=timeout_seconds(),
            ),
            fn,
        )
