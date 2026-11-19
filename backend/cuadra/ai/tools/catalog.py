"""Catalog reads exposed as tools (L3.0: contract proof, not yet in the turn).

These three reads already exist in `domain/catalog.py`; the tools only wrap them
with stable arguments, an `empty_reason` and a source. In L3.0 nothing calls
them from the conversation flow: `agent.tools.mode=off` keeps the deterministic
route in charge.
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
    limit: int = Field(default=5, ge=1, le=10)


class GetArgs(BaseModel):
    product_id: int = Field(ge=1)


class ListCategoryArgs(BaseModel):
    category: str = Field(min_length=1, max_length=120)
    limit: int = Field(default=10, ge=1, le=10)


def search(conn, query: str, limit: int = 5) -> list[dict]:
    return catalog.search_products(
        conn,
        query,
        limit=limit,
        statuses=("ACTIVE", "OUT_OF_STOCK"),
    )


def get(conn, product_id: int) -> dict:
    try:
        product = catalog.get_product(conn, product_id)
    except catalog.NotFound as exc:
        raise ToolError(NOT_FOUND, f"product {product_id} not found") from exc
    # The prompt never sees commission, cost or margin.
    return catalog.llm_product_json(conn, product)


def list_category(conn, category: str, limit: int = 10) -> list[dict]:
    products = catalog.family_products(conn, category, limit=limit)
    if not products:
        products = catalog.category_products(conn, category, limit=limit)
    return products


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
