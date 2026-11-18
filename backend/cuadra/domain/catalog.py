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


# --------------------------------------------------------------- telegram ids
#
# Customers are keyed by their numeric Telegram id (`customer_channels`), which
# never changes. Suppliers, however, are registered by `@usuario`, which the
# owner can rename or release; `telegram_peer_id` keeps the numeric id so
# inbound matching survives a rename (migration 016).

_TELEGRAM_NUMERIC_RE = re.compile(r"^-?\d+$")
_TELEGRAM_LINK_RE = re.compile(
    r"^(?:https?://)?(?:t\.me|telegram\.me)/([A-Za-z][A-Za-z0-9_]*)/?$",
    re.IGNORECASE,
)


def normalize_telegram_handle(value: Any) -> str | None:
    """Canonical `@usuario` form (accepts `nombre`, `@nombre` and t.me links)."""
    raw = str(value or "").strip()
    if not raw:
        return None
    if _TELEGRAM_NUMERIC_RE.match(raw):
        return raw
    match = _TELEGRAM_LINK_RE.match(raw)
    if match is not None:
        raw = match.group(1)
    raw = raw.lstrip("@").strip()
    return f"@{raw}" if raw else None


def clean_telegram_peer_id(value: Any) -> str | None:
    """Numeric Telegram id (negative for groups); empty clears the field."""
    raw = str(value or "").strip()
    if not raw:
        return None
    _require(bool(_TELEGRAM_NUMERIC_RE.match(raw)), "telegram_peer_id must be numeric")
    return raw


def _supplier_telegram_ids(data: dict) -> tuple[str | None, str | None]:
    """Split handle and numeric id for a supplier row.

    Operators often paste a numeric id in the `telegram_id` field; it is stored
    as the stable peer instead of as a bogus `@usuario`.
    """
    handle = normalize_telegram_handle(data.get("telegram_id"))
    peer = clean_telegram_peer_id(data.get("telegram_peer_id"))
    if handle and not peer and _TELEGRAM_NUMERIC_RE.match(handle):
        return None, handle
    return handle, peer


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
    r"\b(vivo|vive|resido|reside|radico|radica|estoy en|soy de|somos de|"
    r"es de|ubicad[oa] en|entrega|entregan|entregamos|entrego|entregar|"
    r"env[ií]o|env[ií]an|enviar|"
    r"zona|municipio|direcci[oó]n|domicilio|reparto|llevar|mandar|mi casa)\b",
    re.IGNORECASE,
)

# Palabras que no distinguen una zona: se descartan para comparar "La UCI" con
# la zona "UCI" y "La Lisa" con "Lisa" (prueba #5).
_ZONE_NOISE = {
    "la", "el", "los", "las", "de", "del", "en", "mi", "tu", "su", "para",
    "por", "a", "al", "con", "y", "o", "zona", "municipio", "entrega",
    "entregar", "envio", "envío", "soy", "es", "son", "estoy", "vivo", "vive",
    "resido", "reside", "radico", "radica", "ubicado", "ubicada",
}


def _zone_key(text: str) -> str:
    words = [
        word
        for word in re.findall(r"[a-z0-9]+", _fold(text or ""))
        if word not in _ZONE_NOISE
    ]
    return " ".join(words)


def resolve_zones(conn, raw: str, *, active_only: bool = True) -> list[dict]:
    """Every zone named in the text, in catalog order (prueba #5).

    `resolve_zone` stays strict (one zone or None) because a sale needs a single
    delivery zone. A coverage question can legitimately name several
    ("Marianao o La Lisa"), so the caller uses this list to answer which of them
    the supplier covers instead of escalating or guessing.
    """
    folded = _fold(raw).strip()
    if not folded:
        return []
    zones = list_zones(conn, active=True if active_only else None)
    by_id = {int(zone["id"]): zone for zone in zones}
    raw_key = _zone_key(raw)

    # 1. The whole message is the zone ("La UCI", "lisa"): exact key match.
    if raw_key:
        for zone in zones:
            if _zone_key(zone["name"]) == raw_key:
                return [zone]
        for alias in list_zone_aliases(conn):
            zone = by_id.get(int(alias["zone_id"]))
            if zone is not None and _zone_key(alias["alias"]) == raw_key:
                return [zone]

    # 2. Whole-word match inside a sentence. Short names (< 6 chars) need a
    #    delivery hint or an article before them to avoid matching common words.
    has_hint = bool(_ZONE_HINT_RE.search(raw or ""))
    candidates: dict[int, dict] = {}

    def add(zone: dict, name: str) -> None:
        if len(name) < 3:
            return
        if len(name) < 6 and not has_hint:
            if not re.search(rf"\b(?:la|el|los|las)\s+{re.escape(name)}\b", folded):
                return
        if re.search(rf"(?<!\w){re.escape(name)}(?!\w)", folded):
            candidates.setdefault(int(zone["id"]), zone)

    for zone in zones:
        add(zone, _fold(zone["name"]))
    for alias in list_zone_aliases(conn):
        zone = by_id.get(int(alias["zone_id"]))
        if zone is not None:
            add(zone, _fold(alias["alias"]))
    return list(candidates.values())


def resolve_zone(conn, raw: str, *, active_only: bool = True) -> dict | None:
    """Map free text to a single zone using canonical names or aliases.

    There is no hardcoded municipality list: the `zones` catalog is the source
    of truth. `active_only=False` also matches inactive zones, so the caller can
    answer "no cubrimos esa zona" instead of silently ignoring the mention.
    Returns the zone row or None when the text is ambiguous or unknown (the
    caller must ask instead of guessing).
    """
    zones = resolve_zones(conn, raw, active_only=active_only)
    return zones[0] if len(zones) == 1 else None


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
    handle, peer_id = _supplier_telegram_ids(data)
    cur = conn.execute(
        """INSERT INTO suppliers
           (name, business_alias, whatsapp_phone, telegram_id, telegram_peer_id,
            delivery_zone, payment_terms, priority, notes, active)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            name,
            data.get("business_alias"),
            data.get("whatsapp_phone"),
            handle,
            peer_id,
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
    "telegram_peer_id",
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
        elif key == "telegram_id":
            handle = normalize_telegram_handle(data["telegram_id"])
            if (
                handle
                and _TELEGRAM_NUMERIC_RE.match(handle)
                and "telegram_peer_id" not in data
            ):
                # A pasted numeric id is the stable peer, not a handle.
                fields["telegram_id"] = None
                fields["telegram_peer_id"] = handle
            else:
                fields["telegram_id"] = handle
        elif key == "telegram_peer_id":
            fields["telegram_peer_id"] = clean_telegram_peer_id(data["telegram_peer_id"])
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


# ------------------------------------------------------- product delivery zones
#
# The supplier's zones are the outer boundary; a product may narrow them (a
# small or low-margin item that is not worth a long trip). No rows for a product
# means "inherit the supplier's zones", which keeps every existing listing valid.


def list_product_zones(conn, product_id: int) -> list[dict]:
    """Zones explicitly assigned to a product (empty = inherit the supplier's)."""
    return [
        dict(r)
        for r in conn.execute(
            """SELECT z.* FROM product_delivery_zones pz
               JOIN zones z ON z.id = pz.zone_id
               WHERE pz.product_id = ? ORDER BY z.name""",
            (product_id,),
        )
    ]


def product_zone_ids(conn, product_id: int) -> list[int]:
    return [
        int(r["zone_id"])
        for r in conn.execute(
            """SELECT zone_id FROM product_delivery_zones
               WHERE product_id = ? ORDER BY zone_id""",
            (product_id,),
        )
    ]


def set_product_zones(conn, product_id: int, zone_ids: list[int]) -> list[int]:
    """Replace a product's explicit zones. An empty list means "inherit"."""
    _get(conn, "products", product_id, "product")
    ids = sorted({int(v) for v in (zone_ids or [])})
    for zone_id in ids:
        _exists(conn, "zones", zone_id, "zone")
    conn.execute(
        "DELETE FROM product_delivery_zones WHERE product_id = ?", (product_id,)
    )
    for zone_id in ids:
        conn.execute(
            "INSERT INTO product_delivery_zones (product_id, zone_id) VALUES (?, ?)",
            (product_id, zone_id),
        )
    return ids


def effective_delivery_zones(conn, product: dict) -> list[dict]:
    """Zones a product can actually be delivered to.

    Explicit product zones restrict the supplier's coverage (intersection).
    Without explicit rows the supplier's zones are inherited.
    """
    supplier_zones = list_supplier_zones(conn, product["supplier_id"])
    explicit = list_product_zones(conn, product["id"])
    if not explicit:
        return supplier_zones
    if not supplier_zones:
        # The supplier has no structured coverage yet: the product's own list is
        # the only signal available.
        return explicit
    allowed = {int(zone["id"]) for zone in explicit}
    return [zone for zone in supplier_zones if int(zone["id"]) in allowed]


def effective_delivery_zone_names(conn, product: dict) -> list[str]:
    return [zone["name"] for zone in effective_delivery_zones(conn, product)]


def product_covers_zone(conn, product: dict, zone_id: int) -> bool:
    """Effective coverage of one product in one zone.

    Mirrors `sales.supplier_covers_zone` (a link row is enough) and adds the
    product restriction. When the supplier has no structured zones, the product
    list is the only signal.
    """
    zone_id = int(zone_id)
    explicit = conn.execute(
        "SELECT 1 FROM product_delivery_zones WHERE product_id = ? AND zone_id = ?",
        (product["id"], zone_id),
    ).fetchone()
    has_explicit = conn.execute(
        "SELECT 1 FROM product_delivery_zones WHERE product_id = ? LIMIT 1",
        (product["id"],),
    ).fetchone()
    supplier = conn.execute(
        "SELECT 1 FROM supplier_delivery_zones WHERE supplier_id = ? AND zone_id = ?",
        (product["supplier_id"], zone_id),
    ).fetchone()
    if has_explicit is not None:
        # The product narrowed its coverage: the zone must be in its own list.
        # If the supplier has no structured zones, that list is the only signal.
        if explicit is None:
            return False
        if supplier is not None:
            return True
        supplier_any = conn.execute(
            "SELECT 1 FROM supplier_delivery_zones WHERE supplier_id = ? LIMIT 1",
            (product["supplier_id"],),
        ).fetchone()
        return supplier_any is None
    return supplier is not None


def set_supplier_telegram_peer(conn, supplier_id: int, peer_id: str | None) -> None:
    """Persist the numeric Telegram id resolved for a supplier handle.

    Telegram reports inbound messages with a numeric id, while the catalog stores
    the human handle (`@usuario`). Keeping the resolved peer lets TCP acks match
    without resolving the username on every inbound message.
    """
    raw = clean_telegram_peer_id(peer_id)
    if not raw:
        return
    _get(conn, "suppliers", supplier_id, "supplier")
    conn.execute(
        "UPDATE suppliers SET telegram_peer_id = ?, updated_at = ? WHERE id = ?",
        (raw, _now(), supplier_id),
    )
    conn.commit()


def find_supplier_by_telegram_id(conn, telegram_id: str) -> dict | None:
    """Resolve a Telegram chat id to its supplier (for the inbound TCP flow)."""
    if not telegram_id:
        return None
    raw = str(telegram_id).strip()
    row = conn.execute(
        """SELECT * FROM suppliers
           WHERE active = 1
             AND (telegram_id = ? OR telegram_peer_id = ?)
           LIMIT 1""",
        (raw, raw),
    ).fetchone()
    return dict(row) if row is not None else None


def find_supplier_by_telegram_handle(conn, handle: str) -> dict | None:
    """Resolve an inbound sender's `@usuario` to its supplier.

    Used as a fallback when the numeric id is still unknown: the supplier row
    stores the mutable handle, so a reply from them is the chance to learn their
    stable id (see `ai.ingest`).
    """
    normalized = normalize_telegram_handle(handle)
    if not normalized:
        return None
    row = conn.execute(
        """SELECT * FROM suppliers
           WHERE active = 1
             AND LOWER(telegram_id) IN (?, ?)
           LIMIT 1""",
        (normalized.lower(), normalized.lstrip("@").lower()),
    ).fetchone()
    return dict(row) if row is not None else None


def list_suppliers_missing_peer(conn) -> list[dict]:
    """Active suppliers still identified only by their mutable handle."""
    rows = conn.execute(
        """SELECT id, name, telegram_id FROM suppliers
           WHERE active = 1
             AND (telegram_peer_id IS NULL OR telegram_peer_id = '')
             AND telegram_id IS NOT NULL AND telegram_id != ''
           ORDER BY id"""
    )
    return [dict(r) for r in rows]


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
    return _attach_product_zones(
        conn, [dict(r) for r in conn.execute(sql + " ORDER BY p.id", args)]
    )


def _attach_product_zones(conn, products: list[dict]) -> list[dict]:
    """Add `delivery_zone_ids` to a batch of products with one query."""
    if not products:
        return products
    ids = [int(product["id"]) for product in products]
    marks = ", ".join("?" for _ in ids)
    zones: dict[int, list[int]] = {product_id: [] for product_id in ids}
    for row in conn.execute(
        f"""SELECT product_id, zone_id FROM product_delivery_zones
            WHERE product_id IN ({marks}) ORDER BY zone_id""",
        ids,
    ):
        zones[int(row["product_id"])].append(int(row["zone_id"]))
    for product in products:
        product["delivery_zone_ids"] = zones[int(product["id"])]
    return products


def get_product(conn, product_id: int) -> dict:
    row = conn.execute(_IMAGE_SELECT + " WHERE p.id = ?", (product_id,)).fetchone()
    if row is None:
        raise NotFound(f"product {product_id} not found")
    product = dict(row)
    product["specifications"] = json.loads(product.get("specifications_json") or "{}")
    product["delivery_zone_ids"] = product_zone_ids(conn, product_id)
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
    if data.get("delivery_zone_ids") is not None:
        set_product_zones(conn, cur.lastrowid, data["delivery_zone_ids"])
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
    zone_ids = data["delivery_zone_ids"] if "delivery_zone_ids" in data else None
    _require(bool(fields) or zone_ids is not None, "nothing to update")
    if fields:
        clause, values = _set_clause(fields)
        conn.execute(
            f"UPDATE products SET {clause}, updated_at = ? WHERE id = ?",
            (*values, _now(), product_id),
        )
    if zone_ids is not None:
        set_product_zones(conn, product_id, zone_ids)
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


def _words(text: str) -> list[str]:
    """Content words of a phrase, in order, without duplicates."""
    cleaned = "".join(c if c.isalnum() else " " for c in _fold(text))
    found: list[str] = []
    for word in cleaned.split():
        if len(word) > 2 and word not in _STOPWORDS and word not in found:
            found.append(word)
    return found


def _singular(word: str) -> str:
    """Best-effort Spanish singular ("laptops" -> "laptop", "motos" -> "moto")."""
    value = _fold(word).strip()
    if len(value) <= 3:
        return value
    # "lápices"/"jueves"-style endings keep the consonant; "laptops" just drops the s.
    if value.endswith("es") and len(value) > 4 and value[-3] in "rlnzjd":
        return value[:-2]
    if value.endswith("s"):
        return value[:-1]
    return value


def _name_head(name: str) -> str:
    """First significant word of a product name ("Laptop Gamer RTX" -> "laptop").

    Used to recognise the same product family: the real catalog keeps every
    laptop under the "tecnología" category, together with a monitor and a tablet.
    """
    for word in _words(name):
        if not word.isdigit():
            return word
    return ""


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


def _products_for_word(conn, word: str, *, limit: int) -> list[dict]:
    """Active products named by a category ("motos") or a name family ("laptop")."""
    if not word:
        return []
    # 1) A real category whose name is that word (singular or plural). Category
    #    names like "motos_eléctricas" are split, so "motos" still matches.
    for row in conn.execute("SELECT id, name FROM categories ORDER BY id"):
        names = {_singular(part) for part in _words(row["name"])}
        names.add(_fold(row["name"]))
        if word not in names:
            continue
        rows = conn.execute(
            """SELECT * FROM products
               WHERE status IN ('ACTIVE', 'OUT_OF_STOCK') AND category_id = ?
               ORDER BY price LIMIT ?""",
            (row["id"], limit),
        ).fetchall()
        if rows:
            return [dict(r) for r in rows]
    # 2) The word is part of the product name ("laptops" -> "Laptop Empresarial").
    #    Out-of-stock products are listed too: the customer may want to wait for
    #    a restock, and the stock may simply be stale.
    ranked = search_products(
        conn, word, limit=max(limit * 4, 12), statuses=("ACTIVE", "OUT_OF_STOCK")
    )
    found = []
    for product in ranked:
        name_tokens = _tokens(product["name"])
        if any(word in token or token in word for token in name_tokens):
            found.append(product)
    return found[:limit]


def category_products(conn, query: str, *, limit: int = 5) -> list[dict]:
    """Products the customer asked about as a group ("¿qué laptops tienes?").

    F3 (prueba #2): "laptops" is not a product name, so `find_product` returned
    nothing and the bot answered with the generic "dime cuál producto" template.
    The query is tried word by word, singularized, so "laptops que tengas en
    venta" still lands on the laptop family.
    """
    for word in _words(query):
        found = _products_for_word(conn, _singular(word), limit=limit)
        if found:
            return found
    return []


def family_products(conn, term: str, *, limit: int = 10) -> list[dict]:
    """Products the word names as a *family*, not as a qualifier.

    `category_products` matches any token of a name, so "empresarial" would
    return the Laptop Empresarial. That is useful to resolve a concrete product,
    but not to decide that the customer asked for the whole family: a real
    category, or a word that is the head of the name ("laptop"), qualifies.
    """
    products = category_products(conn, term, limit=limit)
    if not products:
        return []
    wanted = {_singular(term), _fold(term)}
    for row in conn.execute("SELECT name FROM categories"):
        names = {_singular(part) for part in _words(row["name"])}
        names.add(_fold(row["name"]))
        if wanted & names:
            # A real category is authoritative: its products need not share a
            # head noun ("tecnología" holds laptops, a monitor and a tablet).
            return products
    head = _singular(term)
    return [product for product in products if _name_head(product["name"]) == head]


def category_terms(text: str) -> list[str]:
    """Content words of a phrase, in order, for category matching.

    Kept in their original form so replies can echo what the customer wrote
    ("laptops", not "laptop"); `category_products` singularizes internally.
    """
    return _words(text)


def narrow_products(
    products: list[dict], query: str, *, exclude: str = "", fallback: bool = True
) -> list[dict]:
    """Keep the products whose name matches every extra word in `query`.

    F14 (prueba #4): "laptops gaming" is a category plus a qualifier. The
    category lists three laptops; the qualifier must narrow the list to the
    gamer one instead of answering about the cheapest laptop. With `fallback`
    the original list is returned when nothing matches (the caller can still
    show it); without it the empty list tells the caller the qualifier named no
    product.
    """
    skip = _fold(exclude)
    words = [word for word in _words(query) if word != skip and len(word) >= 4]
    if not words or not products:
        return list(products)
    kept = []
    for product in products:
        tokens = _tokens(product["name"])
        if all(_word_matches(word, tokens) for word in words):
            kept.append(product)
    return kept or (list(products) if fallback else [])


def _word_matches(word: str, tokens: set[str]) -> bool:
    prefix = word[:3] if len(word) >= 5 else word[:4]
    for token in tokens:
        token_prefix = token[:3] if len(token) >= 5 else token[:4]
        if token.startswith(prefix) or word.startswith(token_prefix):
            return True
        if difflib.SequenceMatcher(None, word, token).ratio() >= 0.7:
            return True
    return False


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


_NON_SPEC_VALUE = re.compile(
    r"\bno\s+recuerdo\b|"
    r"\bconsulta(?:lo)?\s+(?:en\s+)?(?:la\s+)?base\s+de\s+datos\b|"
    r"\bno\s+me\s+(?:has|ha|dieron)\b|"
    r"\bno\s+puedo\s+confirmar\b|"
    r"\bno\s+s[eé]\b",
    re.IGNORECASE,
)


def customer_specifications(product: dict) -> dict:
    """Parsed specs safe to show a customer.

    Prueba #4: un TCP respondió "no recuerdo, consulta en la base de datos" y
    quedó guardado como `dato_proveedor`. Ese texto no es una especificación:
    se filtra para que no llegue a la ficha, al prompt ni a la tabla.
    """
    specs = product.get("specifications")
    if not isinstance(specs, dict):
        try:
            specs = json.loads(product.get("specifications_json") or "{}")
        except (TypeError, ValueError):
            specs = {}
    if not isinstance(specs, dict):
        return {}
    clean = {}
    for key, value in specs.items():
        text = str(value or "").strip()
        if not text or str(key).startswith("_"):
            continue
        if _NON_SPEC_VALUE.search(text):
            continue
        clean[str(key)] = value
    return clean


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

    zonas = effective_delivery_zone_names(conn, product)
    if (
        not zonas
        and not product_zone_ids(conn, product["id"])
        and (supplier_zone or "").strip()
    ):
        # Legacy supplier with a free-text zone and no structured coverage: the
        # product has no restriction of its own, so the supplier's text applies.
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
        "especificaciones": customer_specifications(product),
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


def siblings(conn, product: dict, *, limit: int = 3) -> list[dict]:
    """Other active products the customer may want instead (F4, prueba #2).

    The name family wins over the category: the real catalog keeps every laptop
    in "tecnología" together with a monitor and a tablet, so category alone would
    offer unrelated items to someone asking for "otro tipo de laptop".
    """
    head = _name_head(product.get("name") or "")
    rows = conn.execute(
        "SELECT * FROM products WHERE status = 'ACTIVE' AND id != ? ORDER BY price",
        (product["id"],),
    ).fetchall()
    found: list[dict] = []
    if head:
        for row in rows:
            if len(found) >= limit:
                break
            if _name_head(row["name"]) == head:
                found.append(dict(row))
    # A real family is never padded with unrelated items: someone asking for
    # "otro tipo de laptop" must not be offered a monitor. The category is only
    # a fallback for products whose name is unique in the catalog.
    if found or product.get("category_id") is None:
        return found
    for row in rows:
        if len(found) >= limit:
            break
        if row["category_id"] == product["category_id"]:
            found.append(dict(row))
    return found
