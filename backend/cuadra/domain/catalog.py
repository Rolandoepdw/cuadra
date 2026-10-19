"""Catalog domain: categories, suppliers (TCP), products and product images.

Images are compressed on upload (max 1280 px edge, JPEG ~82) and stored under
`data/fotos/product_{id}/img_001.jpg`; the DB stores the path relative to
`data/` so the database stays portable.
"""

import io
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps

from cuadra import config

MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_IMAGE_EDGE = 1280
JPEG_QUALITY = 82

CURRENCIES = ("USD", "CUP")
PRODUCT_STATUSES = ("ACTIVE", "OUT_OF_STOCK", "PAUSED", "REMOVED")

_TABLES = ("categories", "suppliers", "products", "product_images")


class CatalogError(Exception):
    """Domain rule violation; the API maps it to HTTP 4xx."""


class NotFound(CatalogError):
    pass


class Invalid(CatalogError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise Invalid(message)


def _get(conn, table: str, entity_id: int, label: str) -> dict:
    if table not in _TABLES:
        raise ValueError(f"unknown table {table!r}")
    row = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (entity_id,)).fetchone()
    if row is None:
        raise NotFound(f"{label} {entity_id} not found")
    return dict(row)


def _exists(conn, table: str, entity_id: int | None, label: str) -> None:
    if entity_id is not None:
        _get(conn, table, entity_id, label)


def _clean_name(value: Any, field: str) -> str:
    name = (value or "").strip()
    _require(bool(name), f"'{field}' is required")
    return name


def _set_clause(fields: dict) -> tuple[str, list]:
    return ", ".join(f"{k} = ?" for k in fields), list(fields.values())


# ---------------------------------------------------------------- categories

def list_categories(conn, active: bool | None = None) -> list[dict]:
    sql, args = "SELECT * FROM categories", []
    if active is not None:
        sql += " WHERE active = ?"
        args.append(int(active))
    return [dict(r) for r in conn.execute(sql + " ORDER BY name", args)]


def create_category(conn, data: dict) -> dict:
    name = _clean_name(data.get("name"), "name")
    dupe = conn.execute("SELECT 1 FROM categories WHERE name = ?", (name,)).fetchone()
    _require(dupe is None, f"category '{name}' already exists")
    cur = conn.execute(
        "INSERT INTO categories (name, active) VALUES (?, ?)",
        (name, int(data.get("active", True))),
    )
    conn.commit()
    return _get(conn, "categories", cur.lastrowid, "category")


def update_category(conn, category_id: int, data: dict) -> dict:
    _get(conn, "categories", category_id, "category")
    fields: dict = {}
    if "name" in data:
        name = _clean_name(data["name"], "name")
        dupe = conn.execute(
            "SELECT 1 FROM categories WHERE name = ? AND id != ?", (name, category_id)
        ).fetchone()
        _require(dupe is None, f"category '{name}' already exists")
        fields["name"] = name
    if "active" in data:
        fields["active"] = int(bool(data["active"]))
    _require(bool(fields), "nothing to update")
    clause, values = _set_clause(fields)
    conn.execute(f"UPDATE categories SET {clause} WHERE id = ?", (*values, category_id))
    conn.commit()
    return _get(conn, "categories", category_id, "category")


def delete_category(conn, category_id: int) -> dict:
    _get(conn, "categories", category_id, "category")
    used = conn.execute(
        "SELECT COUNT(*) FROM products WHERE category_id = ?", (category_id,)
    ).fetchone()[0]
    _require(
        used == 0,
        f"category is referenced by {used} product(s); deactivate it instead",
    )
    conn.execute("DELETE FROM group_categories WHERE category_id = ?", (category_id,))
    conn.execute("DELETE FROM categories WHERE id = ?", (category_id,))
    conn.commit()
    return {"ok": True, "id": category_id, "deleted": True}


# ----------------------------------------------------------------- suppliers

def list_suppliers(
    conn, *, q: str | None = None, active: bool | None = None
) -> list[dict]:
    sql, args = "SELECT * FROM suppliers", []
    where: list[str] = []
    if active is not None:
        where.append("active = ?")
        args.append(int(active))
    if q:
        where.append("(name LIKE ? OR business_alias LIKE ? OR whatsapp_phone LIKE ?)")
        like = f"%{q}%"
        args += [like, like, like]
    if where:
        sql += " WHERE " + " AND ".join(where)
    return [dict(r) for r in conn.execute(sql + " ORDER BY priority, name", args)]


def get_supplier(conn, supplier_id: int) -> dict:
    return _get(conn, "suppliers", supplier_id, "supplier")


def create_supplier(conn, data: dict) -> dict:
    name = _clean_name(data.get("name"), "name")
    cur = conn.execute(
        """INSERT INTO suppliers
           (name, business_alias, whatsapp_phone, telegram_id, delivery_zone,
            payment_terms, priority, notes, active)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            name,
            data.get("business_alias"),
            data.get("whatsapp_phone"),
            data.get("telegram_id"),
            data.get("delivery_zone"),
            data.get("payment_terms"),
            int(data.get("priority", 100)),
            data.get("notes"),
            int(data.get("active", True)),
        ),
    )
    conn.commit()
    return _get(conn, "suppliers", cur.lastrowid, "supplier")


_SUPPLIER_FIELDS = (
    "name",
    "business_alias",
    "whatsapp_phone",
    "telegram_id",
    "delivery_zone",
    "payment_terms",
    "priority",
    "notes",
    "active",
)


def update_supplier(conn, supplier_id: int, data: dict) -> dict:
    _get(conn, "suppliers", supplier_id, "supplier")
    fields: dict = {}
    for key in _SUPPLIER_FIELDS:
        if key not in data:
            continue
        if key == "name":
            fields["name"] = _clean_name(data["name"], "name")
        elif key == "priority":
            fields["priority"] = int(data["priority"])
        elif key == "active":
            fields["active"] = int(bool(data["active"]))
        else:
            fields[key] = data[key]
    _require(bool(fields), "nothing to update")
    clause, values = _set_clause(fields)
    conn.execute(
        f"UPDATE suppliers SET {clause}, updated_at = ? WHERE id = ?",
        (*values, _now(), supplier_id),
    )
    conn.commit()
    return _get(conn, "suppliers", supplier_id, "supplier")


def delete_supplier(conn, supplier_id: int) -> dict:
    """Soft delete: sales and history reference this row."""
    return update_supplier(conn, supplier_id, {"active": False})


# ------------------------------------------------------------------ products

_PRODUCT_FIELDS = (
    "category_id",
    "name",
    "description",
    "price",
    "currency",
    "commission",
    "commission_currency",
    "stock",
    "status",
    "specifications",
)

_IMAGE_SELECT = """
    SELECT p.*,
           (SELECT local_path FROM product_images pi
            WHERE pi.product_id = p.id AND pi.is_main = 1
            ORDER BY sort_order LIMIT 1) AS main_image
    FROM products p
"""


def list_products(
    conn,
    *,
    supplier_id: int | None = None,
    category_id: int | None = None,
    status: str | None = None,
    q: str | None = None,
) -> list[dict]:
    sql, args = _IMAGE_SELECT, []
    where: list[str] = []
    if supplier_id is not None:
        where.append("p.supplier_id = ?")
        args.append(supplier_id)
    if category_id is not None:
        where.append("p.category_id = ?")
        args.append(category_id)
    if status:
        where.append("p.status = ?")
        args.append(status)
    if q:
        where.append("(p.name LIKE ? OR p.description LIKE ?)")
        args += [f"%{q}%", f"%{q}%"]
    if where:
        sql += " WHERE " + " AND ".join(where)
    return [dict(r) for r in conn.execute(sql + " ORDER BY p.id", args)]


def get_product(conn, product_id: int) -> dict:
    row = conn.execute(_IMAGE_SELECT + " WHERE p.id = ?", (product_id,)).fetchone()
    if row is None:
        raise NotFound(f"product {product_id} not found")
    product = dict(row)
    product["specifications"] = json.loads(product.get("specifications_json") or "{}")
    product["images"] = [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM product_images WHERE product_id = ? "
            "ORDER BY is_main DESC, sort_order",
            (product_id,),
        )
    ]
    return product


def _validate_product_fields(conn, data: dict, *, partial: bool) -> dict:
    fields: dict = {}

    if "supplier_id" in data and not partial:
        fields["supplier_id"] = data["supplier_id"]
    if not partial and "supplier_id" not in data:
        raise Invalid("'supplier_id' is required")
    if "supplier_id" in data and partial:
        raise Invalid("supplier cannot be changed; create a new listing instead")

    if "name" in data and (not partial or data["name"] is not None):
        fields["name"] = _clean_name(data["name"], "name")

    for key in ("price", "commission"):
        if key in data:
            value = data[key]
            _require(
                isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0,
                f"'{key}' must be a number >= 0",
            )
            fields[key] = float(value)

    for key in ("currency", "commission_currency"):
        if key in data and data[key] is not None:
            _require(data[key] in CURRENCIES, f"'{key}' must be one of {CURRENCIES}")
            fields[key] = data[key]

    if "status" in data and data["status"] is not None:
        _require(
            data["status"] in PRODUCT_STATUSES,
            f"'status' must be one of {PRODUCT_STATUSES}",
        )
        fields["status"] = data["status"]

    if "category_id" in data:
        _exists(conn, "categories", data["category_id"], "category")
        fields["category_id"] = data["category_id"]

    if "stock" in data and data["stock"] is not None:
        fields["stock"] = int(data["stock"])

    if "description" in data:
        fields["description"] = data["description"]

    if "specifications" in data:
        specs = data["specifications"]
        _require(isinstance(specs, dict), "'specifications' must be an object")
        fields["specifications_json"] = json.dumps(
            specs, ensure_ascii=False, separators=(",", ":")
        )

    if not partial and "name" not in fields:
        raise Invalid("'name' is required")
    if not partial and "price" not in fields:
        raise Invalid("'price' is required")
    if not partial and "commission" not in fields:
        raise Invalid("'commission' is required")
    return fields


def create_product(conn, data: dict) -> dict:
    fields = _validate_product_fields(conn, data, partial=False)
    _exists(conn, "suppliers", fields["supplier_id"], "supplier")
    fields.setdefault("currency", "USD")
    fields.setdefault("commission_currency", "USD")
    fields.setdefault("status", "ACTIVE")
    fields.setdefault("category_id", None)
    fields.setdefault("description", None)
    fields.setdefault("specifications_json", "{}")

    cols = ", ".join(fields)
    marks = ", ".join("?" for _ in fields)
    cur = conn.execute(
        f"INSERT INTO products ({cols}) VALUES ({marks})", tuple(fields.values())
    )
    conn.commit()
    return get_product(conn, cur.lastrowid)


def update_product(conn, product_id: int, data: dict) -> dict:
    _get(conn, "products", product_id, "product")
    fields = _validate_product_fields(conn, data, partial=True)
    _require(bool(fields), "nothing to update")
    clause, values = _set_clause(fields)
    conn.execute(
        f"UPDATE products SET {clause}, updated_at = ? WHERE id = ?",
        (*values, _now(), product_id),
    )
    conn.commit()
    return get_product(conn, product_id)


def delete_product(conn, product_id: int) -> dict:
    """Soft delete: sales reference this row."""
    update_product(conn, product_id, {"status": "REMOVED"})
    return {"ok": True, "id": product_id, "status": "REMOVED"}


# ------------------------------------------------------------------- images

def add_image(conn, product_id: int, data: bytes) -> dict:
    _get(conn, "products", product_id, "product")
    _require(
        0 < len(data) <= MAX_IMAGE_BYTES,
        f"image must be between 1 byte and {MAX_IMAGE_BYTES // (1024 * 1024)} MB",
    )
    try:
        img = Image.open(io.BytesIO(data))
        img = ImageOps.exif_transpose(img)
        if img.mode != "RGB":
            img = img.convert("RGB")
    except Exception as exc:  # noqa: BLE001 — any decode failure means bad upload
        raise Invalid(f"not a valid image: {exc}") from exc
    img.thumbnail((MAX_IMAGE_EDGE, MAX_IMAGE_EDGE))

    max_sort = conn.execute(
        "SELECT COALESCE(MAX(sort_order), 0) FROM product_images WHERE product_id = ?",
        (product_id,),
    ).fetchone()[0]
    sort_order = int(max_sort) + 1
    is_main = conn.execute(
        "SELECT COUNT(*) FROM product_images WHERE product_id = ?", (product_id,)
    ).fetchone()[0] == 0

    rel = Path("fotos") / f"product_{product_id}" / f"img_{sort_order:03d}.jpg"
    abs_path = config.DATA_DIR / rel
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(abs_path, "JPEG", quality=JPEG_QUALITY, optimize=True)

    cur = conn.execute(
        "INSERT INTO product_images (product_id, local_path, is_main, sort_order) "
        "VALUES (?, ?, ?, ?)",
        (product_id, rel.as_posix(), int(is_main), sort_order),
    )
    conn.commit()
    return _get(conn, "product_images", cur.lastrowid, "image")


def _image_of(conn, product_id: int, image_id: int) -> dict:
    image = _get(conn, "product_images", image_id, "image")
    _require(
        image["product_id"] == product_id,
        f"image {image_id} does not belong to product {product_id}",
    )
    return image


def set_main_image(conn, product_id: int, image_id: int) -> dict:
    _image_of(conn, product_id, image_id)
    conn.execute(
        "UPDATE product_images SET is_main = 0 WHERE product_id = ?", (product_id,)
    )
    conn.execute(
        "UPDATE product_images SET is_main = 1 WHERE id = ?", (image_id,)
    )
    conn.commit()
    return _get(conn, "product_images", image_id, "image")


def delete_image(conn, product_id: int, image_id: int) -> dict:
    image = _image_of(conn, product_id, image_id)
    conn.execute("DELETE FROM product_images WHERE id = ?", (image_id,))
    if image["is_main"]:
        conn.execute(
            """UPDATE product_images SET is_main = 1
               WHERE id = (SELECT id FROM product_images
                           WHERE product_id = ?
                           ORDER BY sort_order LIMIT 1)""",
            (product_id,),
        )
    conn.commit()
    abs_path = config.DATA_DIR / image["local_path"]
    try:
        abs_path.unlink(missing_ok=True)
    except OSError:
        pass  # file already gone or locked — DB stays authoritative
    return {"ok": True, "id": image_id, "deleted": True}
