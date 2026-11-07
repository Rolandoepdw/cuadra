"""TCP catalog mutations: policy, atomic apply, audit trail and undo.

The TCP (supplier) sends price/stock/spec/zone updates over Telegram. The bot
never trusts that message blindly:

- every field is normalized and range-checked;
- the auto-apply policy (migration 014 settings) decides APPLIED vs
  PENDING_REVIEW;
- ambiguous correlations and zone/currency changes always go to review;
- each applied change is reversible with conflict detection (§6 of doc 03).

This module is pure domain logic: no network, no LLM, no queue side effects
except the reindex job the caller explicitly asks for.
"""

import json
import math
from decimal import Decimal, InvalidOperation

from cuadra.domain import catalog, events, sales, settings
from cuadra.domain.errors import Conflict, Invalid, NotFound

STATUSES = ("APPLIED", "PENDING_REVIEW", "REVERTED", "REJECTED")
MUTATION_TYPES = ("PRICE", "STOCK", "STATUS", "SPECS", "ZONE", "MIXED")
CORRELATION_METHODS = ("case_id", "single_pending", "heuristic", "manual")
CURRENCIES = ("USD", "CUP")
PRODUCT_STATUSES = ("ACTIVE", "OUT_OF_STOCK", "PAUSED")
MAX_PRICE = 10_000_000.0
MAX_STOCK = 100_000

REINDEX_PRIORITY = 90


def _enqueue_reindex(product_id: int) -> None:
    # Imported here: the domain layer must not import the AI package at module
    # load time (ai.queue imports channels and events).
    from cuadra.ai import queue

    queue.enqueue(
        "reindex_product", {"product_id": product_id}, priority=REINDEX_PRIORITY
    )


# ------------------------------------------------------------------ snapshots

def snapshot(conn, product: dict) -> dict:
    """Reversible product state; includes the supplier's delivery zones.

    `updated_at` is carried for the audit column. Conflict detection compares
    the mutable fields below (not `stock_reserved`, which sales move on their
    own) so an unrelated reservation never blocks a legitimate undo.
    """
    specs = product.get("specifications")
    if specs is None:
        specs = json.loads(product.get("specifications_json") or "{}")
    return {
        "price": float(product["price"]),
        "currency": product["currency"],
        "stock": int(product["stock"] or 0),
        "stock_reserved": int(product.get("stock_reserved") or 0),
        "status": product["status"],
        "specifications": dict(specs or {}),
        "delivery_zone_ids": sorted(
            int(zone["id"])
            for zone in catalog.list_supplier_zones(conn, product["supplier_id"])
        ),
        "updated_at": product.get("updated_at"),
    }


def _decimal(value) -> Decimal | None:
    try:
        return Decimal(str(value)).quantize(Decimal("0.0001"))
    except (InvalidOperation, TypeError, ValueError):
        return None


def states_match(current: dict, expected: dict) -> bool:
    """True when the product still is exactly what the mutation produced.

    `stock_reserved` and `updated_at` are deliberately excluded: reservations
    and reindexes must not turn a valid undo into a false 409.
    """
    current_price = _decimal(current.get("price"))
    expected_price = _decimal(expected.get("price"))
    if current_price is None or expected_price is None or current_price != expected_price:
        return False
    if current.get("currency") != expected.get("currency"):
        return False
    if int(current.get("stock") or 0) != int(expected.get("stock") or 0):
        return False
    if current.get("status") != expected.get("status"):
        return False
    if (current.get("specifications") or {}) != (expected.get("specifications") or {}):
        return False
    current_zones = sorted(int(z) for z in (current.get("delivery_zone_ids") or []))
    expected_zones = sorted(int(z) for z in (expected.get("delivery_zone_ids") or []))
    return current_zones == expected_zones


# --------------------------------------------------------------- normalization

def _confidence(extraction) -> float:
    raw = extraction.get("confidence", 0.0) if isinstance(extraction, dict) else getattr(
        extraction, "confidence", 0.0
    )
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(value):
        return 0.0
    return max(0.0, min(1.0, value))


def _as_dict(extraction) -> dict:
    if isinstance(extraction, dict):
        return dict(extraction)
    if hasattr(extraction, "model_dump"):
        return extraction.model_dump()
    raise Invalid("extraction must be a mapping or a pydantic model")


def normalize_extraction(conn, extraction) -> tuple[dict, list[str]]:
    """Return a clean, JSON-safe update plus the reasons it needs review.

    Never raises for bad values: an out-of-range number is dropped and reported
    so the operator still gets an auditable PENDING_REVIEW row.
    """
    raw = _as_dict(extraction)
    reasons: list[str] = []
    clean = {
        "price": None,
        "currency": None,
        "stock": None,
        "status": None,
        "specifications": {},
        "delivery_zone_ids": None,
    }

    price = raw.get("price")
    if price is not None:
        try:
            value = float(price)
        except (TypeError, ValueError):
            value = float("nan")
        if isinstance(price, bool) or not math.isfinite(value) or value <= 0:
            reasons.append("precio inválido (debe ser > 0)")
        elif value > MAX_PRICE:
            reasons.append(f"precio fuera de rango (máx {MAX_PRICE:g})")
        else:
            clean["price"] = round(value, 4)

    stock = raw.get("stock")
    if stock is not None:
        try:
            value = int(stock)
            integral = not isinstance(stock, bool) and float(stock) == value
        except (TypeError, ValueError):
            integral, value = False, -1
        if not integral or not 0 <= value <= MAX_STOCK:
            reasons.append(f"stock inválido (entero 0..{MAX_STOCK})")
        else:
            clean["stock"] = value

    currency = raw.get("currency")
    if currency is not None:
        value = str(currency).strip().upper()
        if value == "MLC":
            reasons.append("MLC no es una moneda del catálogo; requiere revisión")
        elif value not in CURRENCIES:
            reasons.append(f"moneda no soportada: {value or '(vacía)'}")
        else:
            clean["currency"] = value

    status = raw.get("status")
    if status is not None:
        value = str(status).strip().upper()
        if value not in PRODUCT_STATUSES:
            reasons.append(f"estado no soportado: {value or '(vacío)'}")
        else:
            clean["status"] = value

    specs = raw.get("new_specifications") or raw.get("specifications") or {}
    if not isinstance(specs, dict):
        reasons.append("especificaciones inválidas (debe ser un objeto)")
        specs = {}
    for key, value in specs.items():
        key = str(key).strip()
        if not key:
            continue
        if isinstance(value, bool) or isinstance(value, (int, float)):
            if isinstance(value, float) and not math.isfinite(value):
                reasons.append(f"especificación '{key}' no es un valor simple")
                continue
            clean["specifications"][key] = value
        elif isinstance(value, str):
            value = value.strip()
            if value:
                clean["specifications"][key] = value
        else:
            reasons.append(f"especificación '{key}' no es un valor simple")

    zone_names = raw.get("delivery_zone_names") or []
    if not isinstance(zone_names, list):
        reasons.append("zonas inválidas (debe ser una lista)")
        zone_names = []
    resolved: list[int] = []
    unresolved: list[str] = []
    for name in zone_names:
        text = str(name).strip()
        if not text:
            continue
        zone = catalog.resolve_zone(conn, text)
        if zone is None:
            unresolved.append(text)
        elif int(zone["id"]) not in resolved:
            resolved.append(int(zone["id"]))
    if unresolved:
        # Never wipe the supplier's whole zone list because of a typo.
        reasons.append("zonas desconocidas: " + ", ".join(unresolved))
    elif resolved:
        clean["delivery_zone_ids"] = sorted(resolved)
    # An empty list simply means "no zone change": the extractor always emits
    # the field, so it must not be treated as a malformed update.

    return clean, reasons


# --------------------------------------------------------------------- policy

def merge_state(previous: dict, clean: dict) -> dict:
    """Build the proposed new state, applying the coherence rules (§5.3)."""
    new = {
        "price": previous["price"],
        "currency": previous["currency"],
        "stock": previous["stock"],
        "stock_reserved": previous["stock_reserved"],
        "status": previous["status"],
        "specifications": dict(previous["specifications"]),
        "delivery_zone_ids": list(previous["delivery_zone_ids"]),
        "updated_at": previous.get("updated_at"),
    }
    if clean["price"] is not None:
        new["price"] = clean["price"]
    if clean["currency"] is not None:
        new["currency"] = clean["currency"]
    if clean["stock"] is not None:
        new["stock"] = clean["stock"]
    if clean["status"] is not None:
        new["status"] = clean["status"]
    new["specifications"].update(clean["specifications"])
    if clean["delivery_zone_ids"] is not None:
        new["delivery_zone_ids"] = clean["delivery_zone_ids"]

    # stock == 0 means OUT_OF_STOCK; the other way round only reactivates a
    # product that was not paused by the operator.
    if clean["stock"] == 0 and previous["status"] != "PAUSED":
        new["status"] = "OUT_OF_STOCK"
    if (
        new["status"] == "OUT_OF_STOCK"
        and new["stock"] > 0
        and previous["status"] != "PAUSED"
    ):
        new["status"] = "ACTIVE"
    # Physical stock below what is already reserved: do not offer more units.
    if new["stock"] < int(new["stock_reserved"] or 0):
        new["status"] = "OUT_OF_STOCK"
    return new


def changed_fields(previous: dict, new: dict) -> list[str]:
    changed: list[str] = []
    if _decimal(previous["price"]) != _decimal(new["price"]):
        changed.append("price")
    if previous["currency"] != new["currency"]:
        changed.append("currency")
    if int(previous["stock"]) != int(new["stock"]):
        changed.append("stock")
    if previous["status"] != new["status"]:
        changed.append("status")
    if previous["specifications"] != new["specifications"]:
        changed.append("specs")
    if sorted(previous["delivery_zone_ids"]) != sorted(new["delivery_zone_ids"]):
        changed.append("zones")
    return changed


def mutation_type(previous: dict, new: dict) -> str:
    changed = changed_fields(previous, new)
    labels = {
        "price": "PRICE",
        "currency": "PRICE",
        "stock": "STOCK",
        "status": "STATUS",
        "specs": "SPECS",
        "zones": "ZONE",
    }
    kinds = {labels[field] for field in changed}
    if not kinds:
        return "MIXED"
    return kinds.pop() if len(kinds) == 1 else "MIXED"


def mutation_policy(
    conn,
    *,
    previous: dict,
    clean: dict,
    new_state: dict,
    confidence: float,
    ambiguous: bool,
    reasons: list[str] | None = None,
) -> tuple[str, list[str]]:
    """Decide APPLIED vs PENDING_REVIEW and explain every review reason."""
    review = list(reasons or [])
    rules = settings.catalog_autoapply(conn)
    changed = changed_fields(previous, new_state)

    if not changed:
        review.append("la actualización no cambia el producto")
    if ambiguous:
        review.append("correlación ambigua: no se aplica automáticamente")
    if confidence < rules["min_confidence"]:
        review.append(
            f"confianza {confidence:.2f} < {rules['min_confidence']:.2f}"
        )

    if "price" in changed or "currency" in changed:
        if not rules["price"]:
            review.append("auto-apply de precio desactivado")
        if clean["price"] is not None and previous["price"] > 0:
            delta = abs(new_state["price"] - previous["price"]) / previous["price"] * 100
            if delta > rules["price_max_delta_pct"]:
                review.append(
                    f"variación de precio {delta:.1f}% > "
                    f"{rules['price_max_delta_pct']:g}%"
                )
    if "currency" in changed:
        review.append("cambio de moneda: requiere revisión del operador")
    if "stock" in changed and not rules["stock"]:
        review.append("auto-apply de stock desactivado")
    if "specs" in changed and not rules["specs"]:
        review.append("auto-apply de especificaciones desactivado")
    if "zones" in changed:
        review.append("cambios de zona siempre requieren revisión")

    return ("PENDING_REVIEW" if review else "APPLIED"), review


# ---------------------------------------------------------------- persistence

def _write_state(conn, product_id: int, state: dict) -> None:
    """Apply a snapshot to `products` and, when present, the supplier's zones."""
    conn.execute(
        """UPDATE products
           SET price = ?, currency = ?, stock = ?, status = ?,
               specifications_json = ?, updated_at = datetime('now')
           WHERE id = ?""",
        (
            float(state["price"]),
            state["currency"],
            int(state["stock"]),
            state["status"],
            json.dumps(
                state.get("specifications") or {},
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            product_id,
        ),
    )
    if state.get("delivery_zone_ids") is not None:
        row = conn.execute(
            "SELECT supplier_id FROM products WHERE id = ?", (product_id,)
        ).fetchone()
        if row is not None:
            catalog.set_supplier_zones(
                conn, int(row["supplier_id"]), state["delivery_zone_ids"]
            )


def _parse_row(row: dict) -> dict:
    parsed = dict(row)
    parsed["ambiguous"] = bool(parsed.get("ambiguous"))
    for key in ("previous_state_json", "new_state_json"):
        target = key.replace("_json", "")
        try:
            parsed[target] = json.loads(parsed.get(key) or "{}")
        except (TypeError, ValueError):
            parsed[target] = {}
    return parsed


def get(conn, mutation_id: int) -> dict:
    row = conn.execute(
        """SELECT m.*, p.name AS product_name, s.name AS supplier_name
           FROM catalog_mutation_log m
           LEFT JOIN products p ON p.id = m.product_id
           LEFT JOIN suppliers s ON s.id = m.supplier_id
           WHERE m.id = ?""",
        (mutation_id,),
    ).fetchone()
    if row is None:
        raise NotFound(f"mutation {mutation_id} not found")
    return _parse_row(dict(row))


def find_by_source_message(conn, message_id: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM catalog_mutation_log WHERE source_message_id = ?",
        (message_id,),
    ).fetchone()
    return _parse_row(dict(row)) if row is not None else None


def apply_tcp_update(
    conn,
    *,
    product_id: int,
    supplier_id: int,
    escalation_id: int | None = None,
    sale_id: int | None = None,
    source_message_id: int | None = None,
    source_text: str | None = None,
    extraction,
    correlation_method: str = "manual",
    ambiguous: bool = False,
    applied_by: str = "bot",
) -> dict:
    """Validate, decide and atomically apply (or propose) one TCP update.

    Idempotent by `source_message_id`: replaying a queue job returns the row
    created the first time and never writes twice.
    """
    if source_message_id is not None:
        existing = find_by_source_message(conn, source_message_id)
        if existing is not None:
            return existing

    product = catalog.get_product(conn, product_id)
    if int(product["supplier_id"]) != int(supplier_id):
        raise Invalid("product does not belong to that supplier")

    previous = snapshot(conn, product)
    clean, reasons = normalize_extraction(conn, extraction)
    confidence = _confidence(extraction)
    new_state = merge_state(previous, clean)
    decision, review_reasons = mutation_policy(
        conn,
        previous=previous,
        clean=clean,
        new_state=new_state,
        confidence=confidence,
        ambiguous=ambiguous,
        reasons=reasons,
    )
    kind = mutation_type(previous, new_state)

    with conn:
        if decision == "APPLIED":
            _write_state(conn, product_id, new_state)
        cur = conn.execute(
            """INSERT INTO catalog_mutation_log (
                   product_id, supplier_id, escalation_id, sale_id,
                   source_message_id, source_text, mutation_type,
                   previous_state_json, new_state_json, previous_updated_at,
                   confidence, correlation_method, ambiguous, status, notes,
                   applied_by
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                product_id,
                supplier_id,
                escalation_id,
                sale_id,
                source_message_id,
                (source_text or "")[:4000] or None,
                kind,
                json.dumps(previous, ensure_ascii=False, sort_keys=True),
                json.dumps(new_state, ensure_ascii=False, sort_keys=True),
                previous.get("updated_at"),
                confidence,
                correlation_method if correlation_method in CORRELATION_METHODS else "manual",
                int(bool(ambiguous)),
                decision,
                "; ".join(review_reasons) or None,
                applied_by if applied_by in ("bot", "operator") else "bot",
            ),
        )
        mutation_id = int(cur.lastrowid)

    if decision == "APPLIED":
        events.log(
            conn,
            "bot",
            "catalog_mutation_applied",
            entity="products",
            entity_id=product_id,
            details={
                "mutation_id": mutation_id,
                "mutation_type": kind,
                "fields": changed_fields(previous, new_state),
                "source_message_id": source_message_id,
            },
        )
        if new_state["stock"] < int(new_state["stock_reserved"] or 0):
            _flag_oversold(conn, product, new_state)
        _enqueue_reindex(product_id)
    else:
        events.log(
            conn,
            "bot",
            "catalog_mutation_pending_review",
            entity="products",
            entity_id=product_id,
            details={
                "mutation_id": mutation_id,
                "mutation_type": kind,
                "ambiguous": bool(ambiguous),
                "reasons": review_reasons,
                "source_message_id": source_message_id,
            },
        )
    return get(conn, mutation_id)


def _flag_oversold(conn, product: dict, state: dict) -> None:
    """Mark open sales of an oversold product for operator resolution."""
    product_id = int(product["id"])
    placeholders = ", ".join("?" for _ in sales.BEFORE_DELIVERY)
    rows = conn.execute(
        f"""SELECT id FROM sales
            WHERE product_id = ? AND status IN ({placeholders})
              AND status != 'NEEDS_OPERATOR'""",
        (product_id, *sales.BEFORE_DELIVERY),
    ).fetchall()
    for row in rows:
        try:
            sales.mark_needs_operator(
                conn,
                int(row["id"]),
                f"oversold: reserved {state['stock_reserved']} > stock {state['stock']}",
            )
        except Exception:  # noqa: BLE001 - one bad sale must not undo the mutation
            continue
    events.log(
        conn,
        "bot",
        "product_oversold",
        entity="products",
        entity_id=product_id,
        details={
            "stock": state["stock"],
            "stock_reserved": state["stock_reserved"],
            "sales_flagged": len(rows),
        },
    )


# --------------------------------------------------------------- operator ops

def approve(conn, mutation_id: int, *, actor: str = "operator") -> dict:
    """Apply a PENDING_REVIEW mutation, refusing if the product moved on."""
    mutation = get(conn, mutation_id)
    if mutation["status"] != "PENDING_REVIEW":
        raise Conflict(
            f"la mutación está en estado {mutation['status']}, no en PENDING_REVIEW"
        )
    product = catalog.get_product(conn, mutation["product_id"])
    current = snapshot(conn, product)
    previous = mutation["previous_state"]
    if not states_match(current, previous):
        raise Conflict(
            "el producto cambió desde que se propuso la mutación; revisa el mensaje de nuevo"
        )

    new_state = mutation["new_state"]
    with conn:
        _write_state(conn, mutation["product_id"], new_state)
        conn.execute(
            """UPDATE catalog_mutation_log
               SET status = 'APPLIED', applied_by = ?
               WHERE id = ?""",
            (actor, mutation_id),
        )
    events.log(
        conn,
        "dashboard",
        "catalog_mutation_approved",
        entity="products",
        entity_id=mutation["product_id"],
        details={"mutation_id": mutation_id, "applied_by": actor},
    )
    _enqueue_reindex(mutation["product_id"])
    if new_state["stock"] < int(new_state["stock_reserved"] or 0):
        _flag_oversold(conn, product, new_state)
    return get(conn, mutation_id)


def reject(conn, mutation_id: int, *, reason: str = "", actor: str = "operator") -> dict:
    mutation = get(conn, mutation_id)
    if mutation["status"] != "PENDING_REVIEW":
        raise Conflict(
            f"la mutación está en estado {mutation['status']}, no en PENDING_REVIEW"
        )
    note = (reason or "").strip() or "rechazada por el operador"
    conn.execute(
        """UPDATE catalog_mutation_log
           SET status = 'REJECTED', applied_by = ?, notes = ?
           WHERE id = ?""",
        (actor, note, mutation_id),
    )
    conn.commit()
    events.log(
        conn,
        "dashboard",
        "catalog_mutation_rejected",
        entity="products",
        entity_id=mutation["product_id"],
        details={"mutation_id": mutation_id, "reason": note},
    )
    return get(conn, mutation_id)


def revert(conn, mutation_id: int, *, reason: str = "", actor: str = "operator") -> dict:
    """Undo an APPLIED mutation only if the product still matches its result."""
    mutation = get(conn, mutation_id)
    if mutation["status"] != "APPLIED":
        raise Conflict(f"la mutación está en estado {mutation['status']}")
    product = catalog.get_product(conn, mutation["product_id"])
    current = snapshot(conn, product)
    if not states_match(current, mutation["new_state"]):
        raise Conflict(
            "el producto cambió después de esta mutación; revierte la más reciente"
        )

    note = (reason or "").strip() or "revertida por el operador"
    previous = mutation["previous_state"]
    with conn:
        _write_state(conn, mutation["product_id"], previous)
        conn.execute(
            """UPDATE catalog_mutation_log
               SET status = 'REVERTED', reverted_at = datetime('now'),
                   reverted_by = ?, revert_reason = ?
               WHERE id = ?""",
            (actor, note, mutation_id),
        )
    events.log(
        conn,
        "dashboard",
        "catalog_mutation_reverted",
        entity="products",
        entity_id=mutation["product_id"],
        details={"mutation_id": mutation_id, "reason": note},
    )
    _enqueue_reindex(mutation["product_id"])
    return get(conn, mutation_id)


# ---------------------------------------------------------------- list/search

def _filters(
    *,
    status: str | None = None,
    supplier_id: int | None = None,
    product_id: int | None = None,
    ambiguous: bool | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> tuple[str, list]:
    clauses: list[str] = []
    args: list = []
    if status:
        clauses.append("m.status = ?")
        args.append(status)
    if supplier_id is not None:
        clauses.append("m.supplier_id = ?")
        args.append(int(supplier_id))
    if product_id is not None:
        clauses.append("m.product_id = ?")
        args.append(int(product_id))
    if ambiguous is not None:
        clauses.append("m.ambiguous = ?")
        args.append(int(bool(ambiguous)))
    if date_from:
        clauses.append("date(m.created_at) >= date(?)")
        args.append(date_from)
    if date_to:
        clauses.append("date(m.created_at) <= date(?)")
        args.append(date_to)
    return (" WHERE " + " AND ".join(clauses) if clauses else ""), args


def list_mutations(
    conn,
    *,
    status: str | None = None,
    supplier_id: int | None = None,
    product_id: int | None = None,
    ambiguous: bool | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    where, args = _filters(
        status=status,
        supplier_id=supplier_id,
        product_id=product_id,
        ambiguous=ambiguous,
        date_from=date_from,
        date_to=date_to,
    )
    rows = conn.execute(
        f"""SELECT m.*, p.name AS product_name, s.name AS supplier_name
            FROM catalog_mutation_log m
            LEFT JOIN products p ON p.id = m.product_id
            LEFT JOIN suppliers s ON s.id = m.supplier_id
            {where}
            ORDER BY m.id DESC LIMIT ? OFFSET ?""",
        (*args, int(limit), int(offset)),
    ).fetchall()
    return [_parse_row(dict(row)) for row in rows]


def count_mutations(
    conn,
    *,
    status: str | None = None,
    supplier_id: int | None = None,
    product_id: int | None = None,
    ambiguous: bool | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> int:
    where, args = _filters(
        status=status,
        supplier_id=supplier_id,
        product_id=product_id,
        ambiguous=ambiguous,
        date_from=date_from,
        date_to=date_to,
    )
    row = conn.execute(
        f"SELECT COUNT(*) FROM catalog_mutation_log m {where}", tuple(args)
    ).fetchone()
    return int(row[0])


def pending_review_count(conn) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM catalog_mutation_log WHERE status = 'PENDING_REVIEW'"
    ).fetchone()
    return int(row[0])
