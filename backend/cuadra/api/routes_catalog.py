"""Catalog CRUD endpoints: categories, suppliers (TCP), products, images."""

import logging

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status

from cuadra import config, db
from cuadra.ai import queue, retrieval, tcp_triage
from cuadra.api.auth import require_pin
from cuadra.api.models import (
    CategoryIn,
    CategoryPatch,
    MainImageIn,
    MutationReasonIn,
    ProductIn,
    ProductPatch,
    SupplierIn,
    SupplierPatch,
    ZoneIn,
    ZonePatch,
)
from cuadra.domain import catalog, catalog_mutations
from cuadra.domain.errors import Conflict

router = APIRouter(prefix="/api", dependencies=[Depends(require_pin)])

log = logging.getLogger("cuadra.routes_catalog")

_BOOL_FIELDS = ("active", "is_main")


def _media_url(local_path: str | None) -> str | None:
    """Relative image path -> public /media URL (mounted in main.py)."""
    if not local_path:
        return None
    return "/media/" + local_path.replace("\\", "/").lstrip("/")


def _out(entity: dict) -> dict:
    for key in _BOOL_FIELDS:
        if key in entity and entity[key] is not None:
            entity[key] = bool(entity[key])
    if isinstance(entity.get("local_path"), str):
        entity["url"] = _media_url(entity["local_path"])
    if isinstance(entity.get("main_image"), str):
        entity["main_image_url"] = _media_url(entity["main_image"])
    return entity


def _http(exc: catalog.CatalogError) -> HTTPException:
    if isinstance(exc, Conflict):
        return HTTPException(status.HTTP_409_CONFLICT, str(exc))
    code = (
        status.HTTP_404_NOT_FOUND
        if isinstance(exc, catalog.NotFound)
        else status.HTTP_400_BAD_REQUEST
    )
    return HTTPException(code, str(exc))


def _run(fn, *args, **kwargs):
    conn = db.connect()
    try:
        return fn(conn, *args, **kwargs)
    except catalog.CatalogError as exc:
        raise _http(exc) from exc
    finally:
        conn.close()


# ---------------------------------------------------------------- categories


@router.get("/categories")
def list_categories(active: bool | None = None) -> list[dict]:
    return [_out(c) for c in _run(catalog.list_categories, active=active)]


@router.post("/categories", status_code=status.HTTP_201_CREATED)
def create_category(payload: CategoryIn) -> dict:
    return _out(_run(catalog.create_category, payload.model_dump()))


@router.patch("/categories/{category_id}")
def update_category(category_id: int, payload: CategoryPatch) -> dict:
    data = payload.model_dump(exclude_unset=True)
    return _out(_run(catalog.update_category, category_id, data))


@router.delete("/categories/{category_id}")
def delete_category(category_id: int) -> dict:
    return _run(catalog.delete_category, category_id)


# --------------------------------------------------------------------- zones


@router.get("/zones")
def list_zones(active: bool | None = None) -> list[dict]:
    return [_out(z) for z in _run(catalog.list_zones, active=active)]


@router.post("/zones", status_code=status.HTTP_201_CREATED)
def create_zone(payload: ZoneIn) -> dict:
    return _out(_run(catalog.create_zone, payload.model_dump()))


@router.patch("/zones/{zone_id}")
def update_zone(zone_id: int, payload: ZonePatch) -> dict:
    data = payload.model_dump(exclude_unset=True)
    return _out(_run(catalog.update_zone, zone_id, data))


@router.delete("/zones/{zone_id}")
def delete_zone(zone_id: int) -> dict:
    """Soft delete: sets active=false (zones and their history are preserved)."""
    return _out(_run(catalog.delete_zone, zone_id))


# ----------------------------------------------------------------- suppliers


@router.get("/suppliers")
def list_suppliers(q: str | None = None, active: bool | None = None) -> list[dict]:
    return [
        _out(s) for s in _run(catalog.list_suppliers, q=q, active=active)
    ]


@router.post("/suppliers", status_code=status.HTTP_201_CREATED)
def create_supplier(payload: SupplierIn) -> dict:
    return _out(_run(catalog.create_supplier, payload.model_dump()))


@router.get("/suppliers/{supplier_id}")
def get_supplier(supplier_id: int) -> dict:
    return _out(_run(catalog.get_supplier, supplier_id))


@router.patch("/suppliers/{supplier_id}")
def update_supplier(supplier_id: int, payload: SupplierPatch) -> dict:
    data = payload.model_dump(exclude_unset=True)
    return _out(_run(catalog.update_supplier, supplier_id, data))


@router.delete("/suppliers/{supplier_id}")
def delete_supplier(supplier_id: int) -> dict:
    """Soft delete: sets active=false (sales reference the supplier)."""
    return _out(_run(catalog.delete_supplier, supplier_id))


# ------------------------------------------------------------------ products


@router.get("/products")
def list_products(
    supplier_id: int | None = None,
    category_id: int | None = None,
    status_filter: str | None = Query(default=None, alias="status"),
    q: str | None = None,
) -> list[dict]:
    return [
        _out(p)
        for p in _run(
            catalog.list_products,
            supplier_id=supplier_id,
            category_id=category_id,
            status=status_filter,
            q=q,
        )
    ]


@router.post("/products", status_code=status.HTTP_201_CREATED)
def create_product(payload: ProductIn) -> dict:
    product = _out(_run(catalog.create_product, payload.model_dump()))
    retrieval.queue_reindex(product["id"])
    return product


@router.get("/products/{product_id}")
def get_product(product_id: int) -> dict:
    product = _run(catalog.get_product, product_id)
    return _out({**product, "images": [_out(i) for i in product["images"]]})


@router.patch("/products/{product_id}")
def update_product(product_id: int, payload: ProductPatch) -> dict:
    data = payload.model_dump(exclude_unset=True)
    product = _run(catalog.update_product, product_id, data)
    retrieval.queue_reindex(product_id)
    return _out({**product, "images": [_out(i) for i in product["images"]]})


@router.delete("/products/{product_id}")
def delete_product(product_id: int) -> dict:
    """Soft delete: sets status='REMOVED' (sales reference the product)."""
    result = _run(catalog.delete_product, product_id)
    retrieval.queue_reindex(product_id)
    return result


# ------------------------------------------------------------------- images


@router.post(
    "/products/{product_id}/images",
    status_code=status.HTTP_201_CREATED,
)
async def upload_image(product_id: int, file: UploadFile = File(...)) -> dict:
    content = await file.read()
    if len(content) == 0:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "empty file")
    return _out(_run(catalog.add_image, product_id, content))


@router.put("/products/{product_id}/main-image")
def set_main_image(product_id: int, payload: MainImageIn) -> dict:
    return _out(
        _run(catalog.set_main_image, product_id, payload.image_id)
    )


@router.delete("/products/{product_id}/images/{image_id}")
def delete_image(product_id: int, image_id: int) -> dict:
    return _run(catalog.delete_image, product_id, image_id)


# --------------------------------------------------------- catalog mutations
#
# Audit + undo surface for TCP-originated catalog changes (doc 03 §7).


@router.get("/catalog/mutations")
def list_mutations(
    status_filter: str | None = Query(default=None, alias="status"),
    supplier_id: int | None = None,
    product_id: int | None = None,
    ambiguous: bool | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict:
    filters = {
        "status": status_filter,
        "supplier_id": supplier_id,
        "product_id": product_id,
        "ambiguous": ambiguous,
        "date_from": date_from,
        "date_to": date_to,
    }
    items = _run(catalog_mutations.list_mutations, **filters, limit=limit, offset=offset)
    total = _run(catalog_mutations.count_mutations, **filters)
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.get("/catalog/mutations/{mutation_id}")
def get_mutation(mutation_id: int) -> dict:
    return _run(catalog_mutations.get, mutation_id)


def _resume_clients(mutation: dict) -> None:
    """After an operator approval, answer every client still waiting."""
    conn = db.connect()
    try:
        sends = tcp_triage.resume_escalations(
            conn,
            product_id=mutation["product_id"],
            supplier_id=mutation["supplier_id"],
            answer=mutation.get("source_text") or "",
            summary=None,
        )
    except Exception:  # noqa: BLE001 - approval already persisted; never 500 for this
        log.exception("could not resume clients for mutation %s", mutation["id"])
        sends = []
    finally:
        conn.close()
    for conversation_id, message_id in sends:
        queue.enqueue(
            "send_message",
            {"conversation_id": conversation_id, "message_id": message_id},
            priority=config.PRIORITY_CUSTOMER,
        )


@router.post("/catalog/mutations/{mutation_id}/approve")
def approve_mutation(mutation_id: int) -> dict:
    mutation = _run(catalog_mutations.approve, mutation_id)
    _resume_clients(mutation)
    return mutation


@router.post("/catalog/mutations/{mutation_id}/reject")
def reject_mutation(mutation_id: int, payload: MutationReasonIn) -> dict:
    return _run(catalog_mutations.reject, mutation_id, reason=payload.reason)


@router.post("/catalog/mutations/{mutation_id}/revert")
def revert_mutation(mutation_id: int, payload: MutationReasonIn) -> dict:
    return _run(catalog_mutations.revert, mutation_id, reason=payload.reason)
