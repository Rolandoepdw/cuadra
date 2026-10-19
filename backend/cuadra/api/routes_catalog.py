"""Catalog CRUD endpoints: categories, suppliers (TCP), products, images."""

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status

from cuadra import db
from cuadra.api.auth import require_pin
from cuadra.api.models import (
    CategoryIn,
    CategoryPatch,
    MainImageIn,
    ProductIn,
    ProductPatch,
    SupplierIn,
    SupplierPatch,
)
from cuadra.domain import catalog

router = APIRouter(prefix="/api", dependencies=[Depends(require_pin)])

_BOOL_FIELDS = ("active", "is_main")


def _out(entity: dict) -> dict:
    for key in _BOOL_FIELDS:
        if key in entity and entity[key] is not None:
            entity[key] = bool(entity[key])
    return entity


def _http(exc: catalog.CatalogError) -> HTTPException:
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
    return _out(_run(catalog.create_product, payload.model_dump()))


@router.get("/products/{product_id}")
def get_product(product_id: int) -> dict:
    product = _run(catalog.get_product, product_id)
    return _out({**product, "images": [_out(i) for i in product["images"]]})


@router.patch("/products/{product_id}")
def update_product(product_id: int, payload: ProductPatch) -> dict:
    data = payload.model_dump(exclude_unset=True)
    product = _run(catalog.update_product, product_id, data)
    return _out({**product, "images": [_out(i) for i in product["images"]]})


@router.delete("/products/{product_id}")
def delete_product(product_id: int) -> dict:
    """Soft delete: sets status='REMOVED' (sales reference the product)."""
    return _run(catalog.delete_product, product_id)


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
