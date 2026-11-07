"""Catalog domain: categories, suppliers (TCP), products and product images.

Images are compressed on upload (max 1280 px edge, JPEG ~82) and stored under
`data/fotos/product_{id}/img_001.jpg`; the DB stores the path relative to
`data/` so the database stays portable.
"""

import difflib
import io
import json
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps

from cuadra import config
from cuadra.domain import events
from cuadra.domain.errors import CatalogError, Invalid, NotFound

__all__ = ["CatalogError", "Invalid", "NotFound"]

MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_IMAGE_EDGE = 1280
JPEG_QUALITY = 82

CURRENCIES = ("USD", "CUP")
PRODUCT_STATUSES = ("ACTIVE", "OUT_OF_STOCK", "PAUSED", "REMOVED")
COMMISSION_TYPES = ("FIXED", "PERCENT")

_TABLES = ("categories", "zones", "suppliers", "products", "product_images")


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
    events.log(
        conn,
        "dashboard",
        "category_created",
        entity="categories",
        entity_id=cur.lastrowid,
        details={"name": name},
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
    events.log(
        conn,
        "dashboard",
        "category_updated",
        entity="categories",
        entity_id=category_id,
        details={"fields": sorted(data)},
    )
    conn.commit()
    return _get(conn, "categories", category_id, "category")


def delete_category(conn, category_id: int) -> dict:
    category = _get(conn, "categories", category_id, "category")
    used = conn.execute(
        "SELECT COUNT(*) FROM products WHERE category_id = ?", (category_id,)
    ).fetchone()[0]
    group_links = conn.execute(
        "SELECT group_id FROM group_categories WHERE category_id = ? ORDER BY group_id",
        (category_id,),
    ).fetchall()
    conn.execute(
        "UPDATE products SET category_id = NULL WHERE category_id = ?",
        (category_id,),
    )
    conn.execute("DELETE FROM group_categories WHERE category_id = ?", (category_id,))
    conn.execute("DELETE FROM categories WHERE id = ?", (category_id,))
    unlinked = {"products": used, "groups": len(group_links)}
    events.log(
        conn,
        "dashboard",
        "category_deleted",
        entity="categories",
        entity_id=category_id,
        details={"name": category["name"], "unlinked": unlinked},
    )
    conn.commit()
    return {
        "ok": True,
        "id": category_id,
        "deleted": True,
        "unlinked": unlinked,
    }


# --------------------------------------------------------------------- zones

def list_zones(conn, active: bool | None = None) -> list[dict]:
    sql, args = "SELECT * FROM zones", []
    if active is not None:
        sql += " WHERE active = ?"
        args.append(int(active))
    return [dict(r) for r in conn.execute(sql + " ORDER BY name", args)]


def create_zone(conn, data: dict) -> dict:
    name = _clean_name(data.get("name"), "name")
    dupe = conn.execute("SELECT 1 FROM zones WHERE name = ?", (name,)).fetchone()
    _require(dupe is None, f"zone '{name}' already exists")
    cur = conn.execute(
        "INSERT INTO zones (name, active) VALUES (?, ?)",
        (name, int(data.get("active", True))),
    )
    events.log(
        conn,
        "dashboard",
        "zone_created",
        entity="zones",
        entity_id=cur.lastrowid,
        details={"name": name},
    )
    conn.commit()
    return _get(conn, "zones", cur.lastrowid, "zone")


def update_zone(conn, zone_id: int, data: dict) -> dict:
    _get(conn, "zones", zone_id, "zone")
    fields: dict = {}
    if "name" in data:
        name = _clean_name(data["name"], "name")
        dupe = conn.execute(
            "SELECT 1 FROM zones WHERE name = ? AND id != ?", (name, zone_id)
        ).fetchone()
        _require(dupe is None, f"zone '{name}' already exists")
        fields["name"] = name
    if "active" in data:
        fields["active"] = int(bool(data["active"]))
    _require(bool(fields), "nothing to update")
    clause, values = _set_clause(fields)
    conn.execute(f"UPDATE zones SET {clause} WHERE id = ?", (*values, zone_id))
    events.log(
        conn,
        "dashboard",
        "zone_updated",
        entity="zones",
        entity_id=zone_id,
        details={"fields": sorted(data)},
    )
    conn.commit()
    return _get(conn, "zones", zone_id, "zone")


def delete_zone(conn, zone_id: int) -> dict:
    """Soft delete: zones are never removed, only deactivated (D3).

    Group links and aliases stay in place so reactivating the zone restores the
    previous configuration. `resolve_zone` only considers active zones, so an
    inactive zone is never offered to a customer.
    """
    zone = _get(conn, "zones", zone_id, "zone")
    conn.execute("UPDATE zones SET active = 0 WHERE id = ?", (zone_id,))
    events.log(
        conn,
        "dashboard",
        "zone_deactivated",
        entity="zones",
        entity_id=zone_id,
        details={"name": zone["name"], "active": False},
    )
    conn.commit()
    return _get(conn, "zones", zone_id, "zone")


def list_zone_aliases(conn, zone_id: int | None = None) -> list[dict]:
    sql = "SELECT * FROM zone_aliases"
    args: list = []
    if zone_id is not None:
        sql += " WHERE zone_id = ?"
        args.append(zone_id)
    return [dict(r) for r in conn.execute(sql + " ORDER BY alias", args)]


def add_zone_alias(conn, zone_id: int, alias: str) -> dict:
    """Register a colloquial name for an existing zone.

    The alias is normalised with `_fold` (lowercase, no accents) so callers can
    write "San Miguel del Padrón" while the catalog keeps "San Miguel" untouched.
    """
    _get(conn, "zones", zone_id, "zone")
    normalized = _fold(alias).strip()
    _require(bool(normalized), "'alias' is required")
    conn.execute(
        """INSERT INTO zone_aliases (alias, zone_id) VALUES (?, ?)
           ON CONFLICT(alias) DO UPDATE SET zone_id = excluded.zone_id""",
        (normalized, zone_id),
    )
    events.log(
        conn,
        "dashboard",
        "zone_alias_set",
        entity="zones",
        entity_id=zone_id,
        details={"alias": normalized},
    )
    conn.commit()
    return {"alias": normalized, "zone_id": zone_id}


def delete_zone_alias(conn, alias: str) -> dict:
    normalized = _fold(alias).strip()
    conn.execute("DELETE FROM zone_aliases WHERE alias = ?", (normalized,))
    conn.commit()
    return {"ok": True, "alias": normalized, "deleted": True}


# A zone mentioned inside a sentence only counts with delivery context; else
# common words such as "regla" (a ruler) would be read as the Regla municipality.
_ZONE_HINT_RE = re.compile(
    r"\b(vivo|vive|resido|reside|estoy en|entrega|entregar|env[ií]o|enviar|"
    r"zona|municipio|direcci[oó]n|domicilio|reparto|llevar|mandar)\b",
    re.IGNORECASE,
)


def resolve_zone(conn, raw: str) -> dict | None:
    """Map free text to an active zone using canonical names or aliases.

    There is no hardcoded municipality list: the active `zones` catalog is the
    source of truth. Returns the zone row or None when the text is ambiguous or
    unknown (the caller must ask instead of guessing).
    """
    folded = _fold(raw).strip()
    if not folded:
        return None
    zones = list_zones(conn, active=True)
    by_id = {int(zone["id"]): zone for zone in zones}

    for zone in zones:
        if _fold(zone["name"]) == folded:
            return zone
    for alias in list_zone_aliases(conn):
        zone = by_id.get(int(alias["zone_id"]))
        if zone is not None and _fold(alias["alias"]) == folded:
            return zone

    # A sentence like "vivo en San Miguel del Padrón" contains the real name.
    # Longest match wins, with word boundaries to avoid partial-word collisions.
    if not _ZONE_HINT_RE.search(raw or ""):
        return None
    candidates = [(zone["name"], zone) for zone in zones]
    candidates += [
        (alias["alias"], by_id[int(alias["zone_id"])])
        for alias in list_zone_aliases(conn)
        if int(alias["zone_id"]) in by_id
    ]
    for name, zone in sorted(candidates, key=lambda item: -len(item[0])):
        folded_name = _fold(name)
        if len(folded_name) >= 4 and re.search(
            rf"(?<!\w){re.escape(folded_name)}(?!\w)", folded
        ):
            return zone
    return None


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
    rows = [dict(r) for r in conn.execute(sql + " ORDER BY priority, name", args)]
    if rows:
        ids = [int(r["id"]) for r in rows]
        placeholders = ",".join("?" * len(ids))
        zones: dict[int, list[int]] = {supplier_id: [] for supplier_id in ids}
        for link in conn.execute(
            "SELECT supplier_id, zone_id FROM supplier_delivery_zones"
            f" WHERE supplier_id IN ({placeholders}) ORDER BY zone_id",
            ids,
        ):
            zones[int(link["supplier_id"])].append(int(link["zone_id"]))
        for row in rows:
            row["delivery_zone_ids"] = zones[int(row["id"])]
    return rows


def get_supplier(conn, supplier_id: int) -> dict:
    supplier = _get(conn, "suppliers", supplier_id, "supplier")
    supplier["delivery_zone_ids"] = [
        int(r["zone_id"])
        for r in conn.execute(
            "SELECT zone_id FROM supplier_delivery_zones WHERE supplier_id = ? ORDER BY zone_id",
            (supplier_id,),
        )
    ]
    return supplier


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
    delivery_zone_ids = sorted({int(v) for v in (data.get("delivery_zone_ids") or [])})
    for zone_id in delivery_zone_ids:
        if conn.execute("SELECT 1 FROM zones WHERE id = ?", (zone_id,)).fetchone() is None:
            raise Invalid(f"unknown zone id: {zone_id}")
    set_supplier_zones(conn, cur.lastrowid, delivery_zone_ids)
    events.log(
        conn,
        "dashboard",
        "supplier_created",
        entity="suppliers",
        entity_id=cur.lastrowid,
        details={"name": name, "zones": delivery_zone_ids},
    )
    conn.commit()
    return get_supplier(conn, cur.lastrowid)


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
    supplier = _get(conn, "suppliers", supplier_id, "supplier")
    fields: dict = {}
    delivery_zone_ids: list[int] | None = None
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
    if "delivery_zone_ids" in data:
        delivery_zone_ids = sorted({int(v) for v in (data.get("delivery_zone_ids") or [])})
        for zone_id in delivery_zone_ids:
            if conn.execute("SELECT 1 FROM zones WHERE id = ?", (zone_id,)).fetchone() is None:
                raise Invalid(f"unknown zone id: {zone_id}")
    _require(bool(fields) or delivery_zone_ids is not None, "nothing to update")
    clause, values = _set_clause(fields)
    if fields:
        conn.execute(
            f"UPDATE suppliers SET {clause}, updated_at = ? WHERE id = ?",
            (*values, _now(), supplier_id),
        )
    if delivery_zone_ids is not None:
        set_supplier_zones(conn, supplier_id, delivery_zone_ids)
    changed = sorted(
        set(fields) | ({"delivery_zone_ids"} if delivery_zone_ids is not None else set())
    )
    events.log(
        conn,
        "dashboard",
        "supplier_updated",
        entity="suppliers",
        entity_id=supplier_id,
        details={"name": supplier["name"], "fields": changed},
    )
    conn.commit()
    return get_supplier(conn, supplier_id)


def delete_supplier(conn, supplier_id: int) -> dict:
    """Soft delete: sales and history reference this row."""
    supplier = _get(conn, "suppliers", supplier_id, "supplier")
    conn.execute(
        "UPDATE suppliers SET active = 0, updated_at = ? WHERE id = ?",
        (_now(), supplier_id),
    )
    events.log(
        conn,
        "dashboard",
        "supplier_deleted",
        entity="suppliers",
        entity_id=supplier_id,
        details={"name": supplier["name"], "active": False},
    )
    conn.commit()
    return get_supplier(conn, supplier_id)


def list_supplier_zones(conn, supplier_id: int) -> list[dict]:
    return [
        dict(r)
        for r in conn.execute(
            """SELECT z.* FROM supplier_delivery_zones sz
               JOIN zones z ON z.id = sz.zone_id
               WHERE sz.supplier_id = ? ORDER BY z.name""",
            (supplier_id,),
        )
    ]


def get_supplier_delivery_zones(conn, supplier_id: int) -> list[dict]:
    """Zone rows assigned to a supplier (used to render the `{zona}` token)."""
    return list_supplier_zones(conn, supplier_id)


def set_supplier_zones(conn, supplier_id: int, zone_ids: list[int]) -> list[int]:
    ids = sorted({int(v) for v in (zone_ids or [])})
    conn.execute("DELETE FROM supplier_delivery_zones WHERE supplier_id = ?", (supplier_id,))
    for zone_id in ids:
        conn.execute(
            "INSERT INTO supplier_delivery_zones (supplier_id, zone_id) VALUES (?, ?)",
            (supplier_id, zone_id),
        )
    return ids


def find_supplier_by_telegram_id(conn, telegram_id: str) -> dict | None:
    """Resolve a Telegram chat id to its supplier (for the inbound TCP flow)."""
    if not telegram_id:
        return None
    row = conn.execute(
        "SELECT * FROM suppliers WHERE telegram_id = ? AND active = 1 LIMIT 1",
        (str(telegram_id),),
    ).fetchone()
    return dict(row) if row is not None else None


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

    if "commission_type" in data and data["commission_type"] is not None:
        _require(
            data["commission_type"] in COMMISSION_TYPES,
            f"'commission_type' must be one of {COMMISSION_TYPES}",
        )
        fields["commission_type"] = data["commission_type"]

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
    events.log(
        conn,
        "dashboard",
        "product_created",
        entity="products",
        entity_id=cur.lastrowid,
        details={"name": fields["name"], "supplier_id": fields["supplier_id"]},
    )
    conn.commit()
    return get_product(conn, cur.lastrowid)


def update_product(conn, product_id: int, data: dict) -> dict:
    product = _get(conn, "products", product_id, "product")
    fields = _validate_product_fields(conn, data, partial=True)
    _require(bool(fields), "nothing to update")
    clause, values = _set_clause(fields)
    conn.execute(
        f"UPDATE products SET {clause}, updated_at = ? WHERE id = ?",
        (*values, _now(), product_id),
    )
    events.log(
        conn,
        "dashboard",
        "product_updated",
        entity="products",
        entity_id=product_id,
        details={"name": product["name"], "fields": sorted(data)},
    )
    conn.commit()
    return get_product(conn, product_id)


def delete_product(conn, product_id: int) -> dict:
    """Soft delete: sales reference this row."""
    product = _get(conn, "products", product_id, "product")
    conn.execute(
        "UPDATE products SET status = 'REMOVED', updated_at = ? WHERE id = ?",
        (_now(), product_id),
    )
    events.log(
        conn,
        "dashboard",
        "product_deleted",
        entity="products",
        entity_id=product_id,
        details={"name": product["name"], "status": "REMOVED"},
    )
    conn.commit()
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
    events.log(
        conn,
        "dashboard",
        "product_image_added",
        entity="product_images",
        entity_id=cur.lastrowid,
        details={"product_id": product_id, "is_main": is_main, "sort_order": sort_order},
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
    events.log(
        conn,
        "dashboard",
        "product_image_main_set",
        entity="product_images",
        entity_id=image_id,
        details={"product_id": product_id},
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
    events.log(
        conn,
        "dashboard",
        "product_image_deleted",
        entity="product_images",
        entity_id=image_id,
        details={"product_id": product_id},
    )
    conn.commit()
    abs_path = config.DATA_DIR / image["local_path"]
    try:
        abs_path.unlink(missing_ok=True)
    except OSError:
        pass  # file already gone or locked — DB stays authoritative
    return {"ok": True, "id": image_id, "deleted": True}


# ------------------------------------------------------- matching for the bot

_STOPWORDS = {
    "de", "del", "la", "el", "los", "las", "un", "una", "y", "o", "para", "con",
    "que", "cuanto", "cuánto", "cuesta", "esta", "está", "tiene", "hay", "por",
    "me", "mi", "te", "se", "es", "en", "mas", "más", "al", "a", "the", "of",
}


def _fold(text: str) -> str:
    """Lowercase + strip accents for tolerant matching."""
    normalized = unicodedata.normalize("NFKD", (text or "").lower())
    return "".join(c for c in normalized if not unicodedata.combining(c))


def _tokens(text: str) -> set[str]:
    cleaned = "".join(c if c.isalnum() else " " for c in _fold(text))
    return {t for t in cleaned.split() if len(t) > 2 and t not in _STOPWORDS}


def search_products(
    conn, query: str, *, limit: int = 5, statuses: tuple[str, ...] = ("ACTIVE",)
) -> list[dict]:
    """Rank catalog products against a free-text query (used by the NLU pipeline).

    Scoring: exact-substring bonus + token overlap + difflib ratio on the name.
    Returns products with `match_score` (0..1) sorted desc.
    """
    query = (query or "").strip()
    if not query:
        return []
    placeholders = ", ".join("?" for _ in statuses)
    rows = conn.execute(
        f"""SELECT p.*, s.delivery_zone AS supplier_zone, s.payment_terms AS supplier_payment_terms,
                   s.name AS supplier_name
            FROM products p JOIN suppliers s ON s.id = p.supplier_id
            WHERE p.status IN ({placeholders})""",
        statuses,
    ).fetchall()
    folded_query = _fold(query)
    query_tokens = _tokens(query)
    scored: list[tuple[float, dict]] = []
    for row in rows:
        product = dict(row)
        folded_name = _fold(product["name"])
        ratio = difflib.SequenceMatcher(None, folded_query, folded_name).ratio()
        name_tokens = _tokens(product["name"])
        overlap = (
            len(query_tokens & name_tokens) / len(query_tokens) if query_tokens else 0.0
        )
        bonus = 0.25 if folded_query in folded_name else 0.0
        score = min(1.0, 0.5 * overlap + 0.4 * ratio + bonus)
        if score >= 0.35:
            product["match_score"] = round(score, 3)
            scored.append((score, product))
    scored.sort(key=lambda item: (-item[0], item[1]["id"]))
    return [product for _, product in scored[:limit]]


def find_product(conn, query: str, *, min_score: float = 0.45) -> dict | None:
    """Match by name, including out-of-stock items (so the bot can say so honestly)."""
    matches = search_products(
        conn, query, limit=1, statuses=("ACTIVE", "OUT_OF_STOCK")
    )
    if matches and matches[0]["match_score"] >= min_score:
        return matches[0]
    return None


def delivery_zone_names(conn, supplier_id: int) -> list[str]:
    """Active zones covered by a supplier (N:M `supplier_delivery_zones`)."""
    names = [
        row["name"]
        for row in conn.execute(
            """SELECT z.name FROM supplier_delivery_zones sdz
               JOIN zones z ON z.id = sdz.zone_id
               WHERE sdz.supplier_id = ? AND z.active = 1
               ORDER BY z.name""",
            (supplier_id,),
        )
    ]
    return names


def llm_product_json(conn, product: dict) -> dict:
    """Customer-safe product payload injected into the LLM context (02 §4, 01 §6.1).

    Deliberately EXCLUDES internal economics (commission, supplier terms): whatever
    goes into the context can be repeated to the customer, so internal fields must
    never be there (see cuadra_doc/06 §Hallazgos).
    """
    supplier_zone = product.get("supplier_zone")
    if supplier_zone is None:
        supplier = conn.execute(
            "SELECT delivery_zone, payment_terms FROM suppliers WHERE id = ?",
            (product["supplier_id"],),
        ).fetchone()
        supplier_zone = supplier["delivery_zone"] if supplier else None

    zonas = delivery_zone_names(conn, product["supplier_id"])
    if not zonas and (supplier_zone or "").strip():
        zonas = [supplier_zone.strip()]

    return {
        "id": product["id"],
        "nombre": product["name"],
        "descripcion": product.get("description"),
        "precio": product["price"],
        "moneda": product["currency"],
        "stock": product.get("stock"),
        "estado": product["status"],
        # Kept as text for the existing templates; the list is what the LLM uses.
        "zona_entrega": ", ".join(zonas) or supplier_zone,
        "zonas_entrega": zonas,
        "especificaciones": json.loads(product.get("specifications_json") or "{}"),
    }


def enrich_specifications(conn, product_id: int, key: str, value: str) -> dict:
    """Merge one TCP-answered spec into the product (Hito 5 resumption).

    Keeps existing specs; empty key/value are ignored. Returns the updated product.
    """
    product = _get(conn, "products", product_id, "product")
    specs = json.loads(product.get("specifications_json") or "{}")
    key = (key or "").strip()
    value = (value or "").strip()
    if key and value:
        specs[key] = value
    conn.execute(
        "UPDATE products SET specifications_json = ?, updated_at = ? WHERE id = ?",
        (json.dumps(specs, ensure_ascii=False, separators=(",", ":")), _now(), product_id),
    )
    conn.commit()
    return get_product(conn, product_id)


def alternatives(conn, product: dict, *, limit: int = 2) -> list[dict]:
    """Similar active products (same category first) to offer when out of stock."""
    rows = conn.execute(
        """SELECT * FROM products
           WHERE status = 'ACTIVE' AND id != ?
             AND (category_id IS ? OR category_id = ?)
           ORDER BY (category_id IS ?) DESC, price
           LIMIT ?""",
        (
            product["id"],
            product.get("category_id"),
            product.get("category_id"),
            product.get("category_id"),
            limit,
        ),
    ).fetchall()
    return [dict(r) for r in rows]
