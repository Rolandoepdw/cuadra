"""Catalog reads exposed as tools (L3.1).

The deterministic ranking lives in `domain/catalog.search_catalog`; these
tools only validate arguments and give the result a stable shape, source and
`empty_reason`. In L3.1 the deterministic route calls the domain function
directly; `agent.tools.catalog.mode` keeps the model from proposing tools until
L3.6.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from cuadra.ai.tools.base import (
    NOT_FOUND,
    READ,
    Registry,
    ToolError,
    ToolSpec,
    timeout_seconds,
)
from cuadra.domain import catalog


class SearchArgs(BaseModel):
    query: str = Field(min_length=1, max_length=200)
    limit: int = Field(default=10, ge=1, le=10)
    include_out_of_stock: bool = True
    category_hint: str | None = Field(default=None, max_length=120)


class GetArgs(BaseModel):
    product_id: int = Field(ge=1)


class ListCategoryArgs(BaseModel):
    category: str = Field(min_length=1, max_length=120)
    limit: int = Field(default=10, ge=1, le=10)


class CheckStockArgs(BaseModel):
    product_id: int = Field(ge=1)
    quantity: int = Field(default=1, ge=1, le=999)


def _product_row(product: dict) -> dict:
    """The public `CatalogProduct` shape; internal economics never leave."""
    return {
        "id": int(product["id"]),
        "name": product["name"],
        "categories": list(product.get("categories") or []),
        "price": float(product["price"]),
        "currency": product["currency"],
        "stock": int(product.get("available_stock", product.get("stock") or 0)),
        "status": product["status"],
        "availability": product.get("availability")
        or catalog.availability_of(product),
        "highlights": product.get("highlights")
        or catalog.product_highlights(product),
    }


def _search_result(data: dict) -> dict:
    return {
        "query": data.get("query", ""),
        "products": [_product_row(product) for product in data.get("products") or []],
        "matched_categories": list(data.get("matched_categories") or []),
        "empty_reason": data.get("empty_reason"),
        "match_kind": data.get("match_kind", ""),
        "match_term": data.get("match_term", ""),
        "qualifiers": list(data.get("qualifiers") or []),
    }


def search(
    conn,
    query: str,
    limit: int = 10,
    include_out_of_stock: bool = True,
    category_hint: str | None = None,
) -> dict:
    return _search_result(
        catalog.search_catalog(
            conn,
            query,
            limit=limit,
            include_out_of_stock=include_out_of_stock,
            category_hint=category_hint,
        )
    )


def get(conn, product_id: int) -> dict:
    try:
        product = catalog.get_product(conn, product_id)
    except catalog.NotFound as exc:
        raise ToolError(NOT_FOUND, f"product {product_id} not found") from exc
    # The prompt never sees commission, cost or margin.
    data = catalog.llm_product_json(conn, product)
    data["categories"] = list(product.get("categories") or [])
    data["zones"] = list(data.get("zonas_entrega") or [])
    data["availability"] = catalog.availability_of(product)
    data["available_stock"] = catalog.available_units(product)
    return data


def list_category(conn, category: str, limit: int = 10) -> dict:
    return _search_result(
        catalog.search_catalog(
            conn, category, limit=limit, category_hint=category
        )
    )


def check_stock(conn, product_id: int, quantity: int = 1) -> dict:
    try:
        return catalog.check_stock(conn, product_id, quantity=quantity)
    except catalog.NotFound as exc:
        raise ToolError(NOT_FOUND, f"product {product_id} not found") from exc


def register(registry: Registry) -> None:
    registry.register(
        ToolSpec(
            name="catalog.search",
            domain="catalog",
            owner="catalog",
            description="Busca productos del catálogo por texto libre.",
            input_model=SearchArgs,
            permission=READ,
            timeout_s=timeout_seconds(),
        ),
        search,
    )
    registry.register(
        ToolSpec(
            name="catalog.get",
            domain="catalog",
            owner="catalog",
            description="Lee la ficha pública de un producto por id.",
            input_model=GetArgs,
            permission=READ,
            timeout_s=timeout_seconds(),
        ),
        get,
    )
    registry.register(
        ToolSpec(
            name="catalog.list_category",
            domain="catalog",
            owner="catalog",
            description="Lista los productos de una categoría o familia.",
            input_model=ListCategoryArgs,
            permission=READ,
            timeout_s=timeout_seconds(),
        ),
        list_category,
    )
    registry.register(
        ToolSpec(
            name="catalog.check_stock",
            domain="catalog",
            owner="catalog",
            description="Confirma si hay unidades suficientes de un producto.",
            input_model=CheckStockArgs,
            permission=READ,
            timeout_s=timeout_seconds(),
        ),
        check_stock,
    )
