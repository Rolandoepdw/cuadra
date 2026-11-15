"""Sales domain: reservation, commission, state machine and ledger.

One sale is one product (doc 02 §4). The agent creates the sale and reserves
stock; the operator approves in assisted mode, the TCP confirms and delivers,
and the operator collects the commission afterwards. Money is never mixed:
a commission in another currency leaves `supplier_payout` NULL instead of
subtracting USD from CUP.

All writes here run in a single SQLite transaction (`with conn:`) so a failed
ledger insert rolls the reservation back with the sale.
"""

import json
import re
from datetime import datetime, timezone

from cuadra.domain import catalog, clock, events, settings
from cuadra.domain.errors import Invalid, NotFound
from cuadra.ai import tracing

SALE_STATUSES = (
    "DRAFT",
    "PENDING_APPROVAL",
    "PENDING_DISPATCH",
    "DISPATCHED",
    "CONFIRMED",
    "DELIVERED",
    "SETTLED",
    "CANCELLED",
    "NEEDS_OPERATOR",
)
OPEN_STATUSES = (
    "DRAFT",
    "PENDING_APPROVAL",
    "PENDING_DISPATCH",
    "DISPATCHED",
    "CONFIRMED",
)
BEFORE_DELIVERY = OPEN_STATUSES + ("NEEDS_OPERATOR",)
# Open states plus the operator queue. DRAFT never reaches the database with a
# reservation, so it is left out of the follow-up list.
FOLLOWUP_STATUSES = tuple(status for status in OPEN_STATUSES if status != "DRAFT") + (
    "NEEDS_OPERATOR",
)
# `sale_followup` v2 (doc 13 §10.24): only these states owe the customer an
# automatic answer. The PENDING_* and DISPATCHED states are internal latency,
# already surfaced in the dashboard; messaging there would be an apology or a
# false promise. `sale_followup` must always be a subset of FOLLOWUP_STATUSES.
FOLLOWUP_MESSAGE_STATUSES = ("CONFIRMED", "NEEDS_OPERATOR")
# "Movement" = last status change OR last inbound customer message. Without the
# inbound part, a follow-up would nag someone who wrote five minutes ago: the
# sale's `updated_at` only moves on transitions.
_LAST_MOVEMENT_SQL = """MAX(
    s.updated_at,
    COALESCE((
        SELECT MAX(m.created_at) FROM messages m
        WHERE m.conversation_id = s.conversation_id AND m.direction = 'INBOUND'
    ), s.updated_at)
)"""
# Statuses in which the TCP already has the order in its chat. Only those need
# a cancellation notice: before a successful dispatch there is nothing to undo.
TCP_AWARE_STATUSES = ("DISPATCHED", "CONFIRMED")
COMMISSION_TYPES = ("FIXED", "PERCENT")
SALES_MODES = ("assisted", "hybrid", "autonomous")

MODE_KEY = "sales.mode"
HYBRID_MAX_AGE_KEY = "sales.hybrid_max_age_hours"

_ACK_CONFIRM = re.compile(
    r"\b(RECIBID[OA]|RECIVID[OA]|RECIBI|RECIVÍ|CONFIRMAD[OA]|CONFIRMO|LISTO|PREPARAD[OA])\b"
)
_ACK_DELIVERED = re.compile(r"\bENTREGAD[OA]\b|\bCOBRAD[OA]\b")
_ACK_OUT_OF_STOCK = re.compile(r"\bAGOTAD[OA]\b|\bSIN\s+STOCK\b")
_ORDER_CODE = re.compile(r"\bV-\d{6}\b", re.IGNORECASE)

_REJECTED_NAMES = {
    "yo",
    "mi",
    "mí",
    "mismo",
    "misma",
    "el mismo",
    "la misma",
    "el cliente",
    "la clienta",
}
_REJECTED_ADDRESSES = {"mi casa", "casa", "aqui", "aquí", "alla", "allá", "por ahi"}


def _now() -> str:
    # Match the schema's `datetime('now')` (UTC, no offset).
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _clean(value, limit: int) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def normalize_phone(raw: str | None) -> str | None:
    """Accept 6-8 digits with an optional +53 (doc 02 §3.2)."""
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 10 and digits.startswith("53"):
        digits = digits[2:]
    if not 6 <= len(digits) <= 8:
        return None
    return f"+53 {digits}"


def clean_recipient(raw: str | None) -> str:
    name = _clean(raw, 120)
    if len(name) < 2 or name.casefold() in _REJECTED_NAMES:
        raise Invalid("recipient name is required")
    return name


def clean_address(raw: str | None) -> str:
    address = _clean(raw, 300)
    if len(address) < 6 or address.casefold() in _REJECTED_ADDRESSES:
        raise Invalid("delivery address is too vague")
    return address


def clean_phone(raw: str | None) -> str:
    phone = normalize_phone(raw)
    if phone is None:
        raise Invalid("invalid customer phone")
    return phone


# ------------------------------------------------------------------ settings

def sale_mode(conn) -> str:
    try:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?", (MODE_KEY,)
        ).fetchone()
    except Exception:  # noqa: BLE001 - pre-migration database
        return "assisted"
    mode = (row["value"] if row is not None else "") or "assisted"
    return mode if mode in SALES_MODES else "assisted"


def hybrid_max_age_hours(conn) -> float:
    try:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?", (HYBRID_MAX_AGE_KEY,)
        ).fetchone()
        return float(row["value"]) if row is not None else 24.0
    except Exception:  # noqa: BLE001 - pre-migration or malformed setting
        return 24.0


def set_sale_mode(conn, value: str) -> str:
    """Persist the operating mode chosen from the dashboard (Fase 3 pending).

    `sale_mode` already falls back to `assisted` on an unknown stored value, so
    the API rejects anything outside SALES_MODES instead of storing garbage.
    """
    if value not in SALES_MODES:
        raise ValueError(f"invalid sales mode: {value}")
    settings.set(conn, MODE_KEY, value)
    return value


# ---------------------------------------------------------------- commission

def calculate_commission(
    product: dict, quantity: int, sale_currency: str
) -> tuple[float, float | None, float, str, str]:
    """Return (earned, payout, total, commission_currency, commission_type).

    FIXED is a per-unit amount in `products.commission_currency`. PERCENT is a
    percentage of the sale total, so it inherits the sale currency. `payout` is
    NULL when subtracting the commission would mix currencies.
    """
    quantity = int(quantity)
    total = round(float(product["price"]) * quantity, 2)
    ctype = product.get("commission_type") or "FIXED"
    if ctype not in COMMISSION_TYPES:
        ctype = "FIXED"
    rate = float(product.get("commission") or 0.0)
    ccurrency = product.get("commission_currency") or "USD"

    if ctype == "PERCENT":
        earned = round(total * (rate / 100.0), 2)
        ccurrency = sale_currency
    else:
        earned = round(rate * quantity, 2)

    payout = round(total - earned, 2) if ccurrency == sale_currency else None
    return earned, payout, total, ccurrency, ctype


# ------------------------------------------------------------------ coverage

def supplier_covers_zone(conn, supplier_id: int, zone_id: int) -> bool:
    row = conn.execute(
        """SELECT 1 FROM supplier_delivery_zones
           WHERE supplier_id = ? AND zone_id = ?""",
        (supplier_id, zone_id),
    ).fetchone()
    return row is not None


def _pending_escalation(conn, product_id: int) -> bool:
    row = conn.execute(
        """SELECT 1 FROM escalations
           WHERE product_id = ? AND status IN ('PENDING', 'ANSWERED')
           LIMIT 1""",
        (product_id,),
    ).fetchone()
    return row is not None


def _product_fresh(conn, product_id: int, hours: float) -> bool:
    row = conn.execute(
        """SELECT 1 FROM products
           WHERE id = ? AND datetime(updated_at) >= datetime('now', ?)""",
        (product_id, f"-{hours:g} hours"),
    ).fetchone()
    return row is not None


def initial_status(
    conn,
    mode: str,
    *,
    product_id: int,
    supplier_id: int,
    zone_id: int,
) -> str:
    """Pick the first state of a sale from the autonomy mode (doc 02 §6.2)."""
    if mode == "autonomous":
        return "PENDING_DISPATCH"
    if mode != "hybrid":
        return "PENDING_APPROVAL"
    if _pending_escalation(conn, product_id):
        return "PENDING_APPROVAL"
    if not _product_fresh(conn, product_id, hybrid_max_age_hours(conn)):
        return "PENDING_APPROVAL"
    if not supplier_covers_zone(conn, supplier_id, zone_id):
        return "PENDING_APPROVAL"
    return "PENDING_DISPATCH"


# ----------------------------------------------------------------------- CRUD

def get_sale(conn, sale_id: int) -> dict:
    row = conn.execute("SELECT * FROM sales WHERE id = ?", (sale_id,)).fetchone()
    if row is None:
        raise NotFound(f"sale {sale_id} not found")
    return dict(row)


def get_sale_by_code(conn, order_code: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM sales WHERE order_code = ?", (_clean(order_code, 20).upper(),)
    ).fetchone()
    return dict(row) if row is not None else None


def get_ledger(conn, sale_id: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM commission_ledger WHERE sale_id = ?", (sale_id,)
    ).fetchone()
    return dict(row) if row is not None else None


def create_sale(
    conn,
    *,
    conversation_id: int | None,
    customer_id: int,
    product_id: int,
    quantity: int = 1,
    recipient_name: str | None,
    delivery_zone: str | None,
    delivery_address: str | None,
    customer_phone: str | None,
    mode: str | None = None,
    source: str = "bot",
) -> dict:
    """Create the sale, its ledger row and the stock reservation atomically."""
    quantity = int(quantity or 0)
    if quantity <= 0:
        raise Invalid("quantity must be positive")

    product = catalog.get_product(conn, product_id)
    if product["status"] != "ACTIVE":
        raise Invalid("product is not sellable")
    supplier = catalog.get_supplier(conn, product["supplier_id"])
    if not supplier["active"]:
        raise Invalid("supplier is inactive")

    zone = catalog.resolve_zone(conn, delivery_zone or "")
    if zone is None:
        raise Invalid("unknown zone")
    if not supplier_covers_zone(conn, supplier["id"], zone["id"]):
        raise Invalid("supplier does not deliver to this zone")

    recipient = clean_recipient(recipient_name)
    address = clean_address(delivery_address)
    phone = clean_phone(customer_phone)

    available = int(product.get("stock") or 0) - int(product.get("stock_reserved") or 0)
    if available < quantity:
        raise Invalid("not enough stock")

    earned, payout, total, ccurrency, ctype = calculate_commission(
        product, quantity, product["currency"]
    )
    choice = mode or sale_mode(conn)
    status = initial_status(
        conn,
        choice,
        product_id=product_id,
        supplier_id=supplier["id"],
        zone_id=zone["id"],
    )

    with conn:
        cur = conn.execute(
            """INSERT INTO sales (
                   conversation_id, product_id, supplier_id, customer_id,
                   quantity, amount, currency, commission_type, commission,
                   commission_currency, recipient_name, delivery_zone,
                   delivery_zone_id, delivery_address, customer_phone,
                   status, source, context_json
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                conversation_id,
                product_id,
                product["supplier_id"],
                customer_id,
                quantity,
                total,
                product["currency"],
                ctype,
                earned,
                ccurrency,
                recipient,
                zone["name"],
                zone["id"],
                address,
                phone,
                status,
                source if source in ("bot", "operator") else "bot",
                json.dumps(
                    {"product_name": product["name"], "zone": zone["name"]},
                    ensure_ascii=False,
                ),
            ),
        )
        sale_id = cur.lastrowid
        conn.execute(
            "UPDATE sales SET order_code = 'V-' || printf('%06d', id) WHERE id = ?",
            (sale_id,),
        )
        conn.execute(
            """INSERT INTO commission_ledger (
                   sale_id, product_id, supplier_id, product_name,
                   sale_total, currency, commission_type, commission_rate,
                   commission_earned, commission_currency, supplier_payout
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                sale_id,
                product_id,
                product["supplier_id"],
                product["name"],
                total,
                product["currency"],
                ctype,
                float(product.get("commission") or 0.0),
                earned,
                ccurrency,
                payout,
            ),
        )
        reserved = conn.execute(
            """UPDATE products
               SET stock_reserved = stock_reserved + ?, updated_at = ?
               WHERE id = ? AND (stock - stock_reserved) >= ?""",
            (quantity, _now(), product_id, quantity),
        )
        if reserved.rowcount != 1:
            raise Invalid("not enough stock")

    sale = get_sale(conn, sale_id)
    events.log(
        conn,
        "bot" if sale["source"] == "bot" else "dashboard",
        "sale_created",
        entity="sales",
        entity_id=sale_id,
        details={
            "order_code": sale["order_code"],
            "product_id": product_id,
            "supplier_id": product["supplier_id"],
            "quantity": quantity,
            "status": status,
            "mode": choice,
        },
    )
    tracing.trace_call(
        "sales.create_sale",
        {
            "conversation_id": conversation_id,
            "customer_id": customer_id,
            "product_id": product_id,
            "quantity": quantity,
            "recipient_name": recipient_name,
            "delivery_zone": delivery_zone,
            "delivery_address": delivery_address,
            "customer_phone": customer_phone,
            "mode": mode,
            "source": source,
        },
        {
            "sale_id": sale["id"],
            "order_code": sale["order_code"],
            "amount": sale["amount"],
            "currency": sale["currency"],
            "status": sale["status"],
        },
        ok=True,
    )
    return sale


def update_sale_quantity(
    conn,
    sale_id: int,
    new_quantity: int,
) -> dict:
    """Update the quantity of an existing open sale.

    Recalculates amount and commission, adjusts stock_reserved by the
    difference, and returns the updated sale row.
    """
    new_quantity = int(new_quantity or 0)
    if new_quantity <= 0:
        raise Invalid("quantity must be positive")

    sale = get_sale(conn, sale_id)
    tracing.trace_call(
        "sales.update_sale_quantity",
        {"sale_id": sale_id, "new_quantity": new_quantity, "old_quantity": sale["quantity"] if sale else None},
        None,
        ok=True,
    )
    if sale is None:
        raise Invalid("sale not found")
    if sale["status"] not in ("PENDING_DISPATCH", "PENDING_APPROVAL"):
        raise Invalid("sale is not in an editable state")

    product = catalog.get_product(conn, sale["product_id"])
    if product["status"] != "ACTIVE":
        raise Invalid("product is not sellable")

    old_quantity = int(sale["quantity"] or 0)
    diff = new_quantity - old_quantity
    if diff == 0:
        return sale

    available = int(product.get("stock") or 0) - int(
        product.get("stock_reserved") or 0
    ) + old_quantity
    if available < new_quantity:
        raise Invalid("not enough stock")

    earned, payout, total, ccurrency, ctype = calculate_commission(
        product, new_quantity, product["currency"]
    )

    with conn:
        conn.execute(
            """UPDATE sales
               SET quantity = ?, amount = ?, commission_type = ?,
                   commission = ?, commission_currency = ?, updated_at = ?
             WHERE id = ?""",
            (
                new_quantity,
                total,
                ctype,
                earned,
                ccurrency,
                _now(),
                sale_id,
            ),
        )
        conn.execute(
            """UPDATE products
               SET stock_reserved = stock_reserved + ?, updated_at = ?
             WHERE id = ?""",
            (diff, _now(), sale["product_id"]),
        )
        conn.execute(
            """UPDATE commission_ledger
               SET sale_total = ?, commission = ?, commission_currency = ?,
                   updated_at = ?
             WHERE sale_id = ?""",
            (total, earned, ccurrency, _now(), sale_id),
        )

    updated_sale = get_sale(conn, sale_id)
    tracing.trace_call(
        "sales.update_sale_quantity",
        {"sale_id": sale_id, "new_quantity": new_quantity, "old_quantity": sale["quantity"] if sale else None},
        {
            "sale_id": updated_sale["id"],
            "order_code": updated_sale["order_code"],
            "quantity": updated_sale["quantity"],
            "amount": updated_sale["amount"],
            "status": updated_sale["status"],
        },
        ok=True,
    )
    return updated_sale


# ---------------------------------------------------------------- transitions

def _transition(conn, sale_id: int, from_statuses: tuple[str, ...], action: str) -> dict:
    sale = get_sale(conn, sale_id)
    if sale["status"] not in from_statuses:
        raise Invalid(f"cannot {action} a sale in status {sale['status']}")
    return sale


def approve(conn, sale_id: int) -> dict:
    sale = _transition(conn, sale_id, ("PENDING_APPROVAL",), "approve")
    with conn:
        conn.execute(
            """UPDATE sales
               SET status = 'PENDING_DISPATCH', approved_at = COALESCE(approved_at, ?),
                   updated_at = ?
               WHERE id = ?""",
            (_now(), _now(), sale_id),
        )
    events.log(
        conn,
        "dashboard",
        "sale_approved",
        entity="sales",
        entity_id=sale_id,
        details={"order_code": sale["order_code"]},
    )
    return get_sale(conn, sale_id)


def mark_dispatched(conn, sale_id: int, tcp_message_id: str | None) -> dict:
    sale = _transition(
        conn, sale_id, ("PENDING_DISPATCH",), "dispatch"
    )
    with conn:
        conn.execute(
            """UPDATE sales
               SET status = 'DISPATCHED', dispatched_at = ?, tcp_message_id = ?,
                   notes = NULL, updated_at = ?
               WHERE id = ?""",
            (_now(), tcp_message_id, _now(), sale_id),
        )
    events.log(
        conn,
        "bot",
        "sale_dispatched",
        entity="sales",
        entity_id=sale_id,
        details={"order_code": sale["order_code"], "tcp_message_id": tcp_message_id},
    )
    return get_sale(conn, sale_id)


def mark_needs_operator(conn, sale_id: int, note: str) -> dict:
    sale = get_sale(conn, sale_id)
    if sale["status"] in ("SETTLED", "CANCELLED"):
        raise Invalid(f"cannot requeue a sale in status {sale['status']}")
    with conn:
        conn.execute(
            """UPDATE sales
               SET status = 'NEEDS_OPERATOR', notes = ?, updated_at = ?
               WHERE id = ?""",
            (_clean(note, 500), _now(), sale_id),
        )
    events.log(
        conn,
        "bot",
        "sale_needs_operator",
        entity="sales",
        entity_id=sale_id,
        details={"order_code": sale["order_code"], "reason": _clean(note, 500)},
    )
    return get_sale(conn, sale_id)


def arm_dispatch(conn, sale_id: int) -> dict:
    """Operator decision after NEEDS_OPERATOR: send the order again."""
    sale = _transition(conn, sale_id, ("NEEDS_OPERATOR",), "re-arm for dispatch")
    with conn:
        conn.execute(
            """UPDATE sales
               SET status = 'PENDING_DISPATCH', updated_at = ?
               WHERE id = ?""",
            (_now(), sale_id),
        )
    events.log(
        conn,
        "dashboard",
        "sale_dispatch_rearmed",
        entity="sales",
        entity_id=sale_id,
        details={"order_code": sale["order_code"]},
    )
    return get_sale(conn, sale_id)


def confirm(conn, sale_id: int) -> dict:
    sale = _transition(conn, sale_id, ("DISPATCHED",), "confirm")
    with conn:
        conn.execute(
            """UPDATE sales
               SET status = 'CONFIRMED', tcp_ack_at = ?, updated_at = ?
               WHERE id = ?""",
            (_now(), _now(), sale_id),
        )
    events.log(
        conn,
        "bot",
        "sale_confirmed",
        entity="sales",
        entity_id=sale_id,
        details={"order_code": sale["order_code"]},
    )
    return get_sale(conn, sale_id)


def deliver(conn, sale_id: int) -> dict:
    """TCP delivered and collected: physical stock leaves, reservation clears."""
    sale = _transition(
        conn, sale_id, ("DISPATCHED", "CONFIRMED"), "deliver"
    )
    quantity = int(sale["quantity"])
    with conn:
        moved = conn.execute(
            """UPDATE products
               SET stock = stock - ?,
                   stock_reserved = MAX(0, stock_reserved - ?),
                   updated_at = ?
               WHERE id = ? AND stock >= ?""",
            (quantity, quantity, _now(), sale["product_id"], quantity),
        )
        if moved.rowcount != 1:
            raise Invalid("physical stock is below the reserved quantity")
        conn.execute(
            """UPDATE sales
               SET status = 'DELIVERED', delivered_at = ?, tcp_ack_at = COALESCE(tcp_ack_at, ?),
                   updated_at = ?
               WHERE id = ?""",
            (_now(), _now(), _now(), sale_id),
        )
    events.log(
        conn,
        "bot",
        "sale_delivered",
        entity="sales",
        entity_id=sale_id,
        details={"order_code": sale["order_code"], "quantity": quantity},
    )
    return get_sale(conn, sale_id)


def deliver_or_flag(conn, sale_id: int) -> tuple[dict, bool]:
    """Deliver, or move to NEEDS_OPERATOR when physical stock is missing."""
    try:
        return deliver(conn, sale_id), True
    except Invalid as exc:
        return mark_needs_operator(conn, sale_id, str(exc)), False


def settle(conn, sale_id: int) -> dict:
    sale = _transition(conn, sale_id, ("DELIVERED",), "settle")
    with conn:
        conn.execute(
            """UPDATE commission_ledger
               SET status = 'COLLECTED', collected_at = ?
               WHERE sale_id = ? AND status = 'PENDING'""",
            (_now(), sale_id),
        )
        conn.execute(
            """UPDATE sales
               SET status = 'SETTLED', settled_at = ?, updated_at = ?
               WHERE id = ?""",
            (_now(), _now(), sale_id),
        )
    events.log(
        conn,
        "dashboard",
        "commission_settled",
        entity="sales",
        entity_id=sale_id,
        details={"order_code": sale["order_code"]},
    )
    return get_sale(conn, sale_id)


def cancel(conn, sale_id: int, reason: str = "") -> dict:
    """Cancel before delivery and release the reserved stock (never below 0)."""
    sale = _transition(
        conn, sale_id, BEFORE_DELIVERY, "cancel"
    )
    quantity = int(sale["quantity"])
    note = _clean(reason, 500) or "cancelled by operator"
    with conn:
        conn.execute(
            """UPDATE products
               SET stock_reserved = MAX(0, stock_reserved - ?), updated_at = ?
               WHERE id = ?""",
            (quantity, _now(), sale["product_id"]),
        )
        conn.execute(
            """UPDATE commission_ledger
               SET status = 'VOID', notes = ?
               WHERE sale_id = ? AND status = 'PENDING'""",
            (note, sale_id),
        )
        conn.execute(
            """UPDATE sales
               SET status = 'CANCELLED', cancelled_at = ?, notes = ?, updated_at = ?
               WHERE id = ?""",
            (_now(), note, _now(), sale_id),
        )
    events.log(
        conn,
        "dashboard",
        "sale_cancelled",
        entity="sales",
        entity_id=sale_id,
        details={"order_code": sale["order_code"], "reason": note},
    )
    return get_sale(conn, sale_id)


# --------------------------------------------------------------- TCP replies

def _fold(text: str) -> str:
    replacements = str.maketrans("áéíóúüñÁÉÍÓÚÜÑ", "aeiouunAEIOUUN")
    return (text or "").translate(replacements).upper()


def _ack_sale(conn, supplier_id: int, text: str) -> dict | None:
    code = _ORDER_CODE.search(text or "")
    if code:
        sale = get_sale_by_code(conn, code.group(0))
        if sale is not None and int(sale["supplier_id"]) == int(supplier_id):
            return sale
        return None
    rows = conn.execute(
        """SELECT * FROM sales
           WHERE supplier_id = ? AND status = 'DISPATCHED'
           ORDER BY id""",
        (supplier_id,),
    ).fetchall()
    if not rows:
        return None
    if len(rows) == 1:
        return dict(rows[0])
    # Multiple DISPATCHED sales for this supplier: ambiguous, do not guess.
    return None


def apply_tcp_ack(conn, supplier_id: int, text: str) -> tuple[dict | None, str | None]:
    """Interpret RECIBIDO / ENTREGADO / AGOTADO from a supplier message.

    Returns (sale, action) where action is CONFIRMED, DELIVERED, NEEDS_OPERATOR,
    AMBIGUOUS, or None when the message is not an acknowledgement for this supplier.

    AMBIGUOUS: an ack keyword was found but no single sale could be identified
    (no order code in the text, and multiple DISPATCHED sales exist for this
    supplier). The caller must flag it for operator review instead of guessing.
    """
    body = _fold(text)
    is_ack = bool(
        _ACK_CONFIRM.search(body)
        or _ACK_DELIVERED.search(body)
        or _ACK_OUT_OF_STOCK.search(body)
    )
    if not is_ack:
        tracing.trace_call(
            "sales.apply_tcp_ack",
            {"supplier_id": supplier_id, "text": text[:200]},
            None,
            ok=True,
        )
        return None, None
    sale = _ack_sale(conn, supplier_id, text)
    if sale is None:
        # No matching sale: wrong supplier (code exists but not for this supplier)
        # or no DISPATCHED sales at all. Not ambiguous -- just ignore.
        if _ORDER_CODE.search(text or ""):
            tracing.trace_call(
                "sales.apply_tcp_ack",
                {"supplier_id": supplier_id, "text": text[:200]},
                {"action": None, "reason": "wrong_supplier_or_no_dispatched"},
                ok=True,
            )
            return None, None
        # No code: ambiguous only if multiple DISPATCHED sales exist for this supplier.
        rows = conn.execute(
            """SELECT id FROM sales
               WHERE supplier_id = ? AND status = 'DISPATCHED'""",
            (supplier_id,),
        ).fetchall()
        if len(rows) > 1:
            tracing.trace_call(
                "sales.apply_tcp_ack",
                {"supplier_id": supplier_id, "text": text[:200]},
                {"action": "AMBIGUOUS", "candidate_count": len(rows)},
                ok=True,
            )
            return None, "AMBIGUOUS"
        return None, None

    if _ACK_OUT_OF_STOCK.search(body):
        return mark_needs_operator(conn, sale["id"], "TCP reported out of stock"), "NEEDS_OPERATOR"
    if _ACK_DELIVERED.search(body):
        updated, ok = deliver_or_flag(conn, sale["id"])
        return updated, "DELIVERED" if ok else "NEEDS_OPERATOR"
    if _ACK_CONFIRM.search(body):
        if sale["status"] == "DISPATCHED":
            return confirm(conn, sale["id"]), "CONFIRMED"
        return sale, None
    return None, None

def list_sales(
    conn,
    *,
    status: str | None = None,
    currency: str | None = None,
    supplier_id: int | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    sql = """SELECT s.*, p.name AS product_name, sup.name AS supplier_name,
                    l.status AS ledger_status, l.commission_earned,
                    l.commission_currency AS ledger_currency
             FROM sales s
             LEFT JOIN products p ON p.id = s.product_id
             LEFT JOIN suppliers sup ON sup.id = s.supplier_id
             LEFT JOIN commission_ledger l ON l.sale_id = s.id"""
    where: list[str] = []
    args: list = []
    if status:
        where.append("s.status = ?")
        args.append(status)
    if currency:
        where.append("s.currency = ?")
        args.append(currency)
    if supplier_id is not None:
        where.append("s.supplier_id = ?")
        args.append(int(supplier_id))
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY s.id DESC LIMIT ? OFFSET ?"
    args += [max(1, min(int(limit), 200)), max(0, int(offset))]
    return [dict(row) for row in conn.execute(sql, args)]


def count_sales(
    conn,
    *,
    status: str | None = None,
    currency: str | None = None,
    supplier_id: int | None = None,
) -> int:
    sql, args = "SELECT COUNT(*) FROM sales", []
    where: list[str] = []
    if status:
        where.append("status = ?")
        args.append(status)
    if currency:
        where.append("currency = ?")
        args.append(currency)
    if supplier_id is not None:
        where.append("supplier_id = ?")
        args.append(int(supplier_id))
    if where:
        sql += " WHERE " + " AND ".join(where)
    return int(conn.execute(sql, args).fetchone()[0])


def followups(conn, *, hours: float = 24, limit: int = 50) -> list[dict]:
    """Sales still open after `hours` without real movement (dashboard reminder).

    "Movement" is the last status change or the last inbound customer message
    (see `_LAST_MOVEMENT_SQL`), so a customer who just wrote does not look
    abandoned. This list is the operator's view; sending to the customer is the
    separate `sale_followup` job (v2, doc 13 §10.24).
    """
    threshold = max(1.0, min(float(hours), 24 * 30))
    placeholders = ", ".join("?" for _ in FOLLOWUP_STATUSES)
    rows = conn.execute(
        f"""SELECT s.*, p.name AS product_name, sup.name AS supplier_name,
                   ROUND((julianday('now') - julianday({_LAST_MOVEMENT_SQL})) * 24, 1)
                       AS age_hours
            FROM sales s
            LEFT JOIN products p ON p.id = s.product_id
            LEFT JOIN suppliers sup ON sup.id = s.supplier_id
            WHERE s.status IN ({placeholders})
              AND (julianday('now') - julianday({_LAST_MOVEMENT_SQL})) * 24 >= ?
            ORDER BY age_hours DESC, s.id ASC
            LIMIT ?""",
        (*FOLLOWUP_STATUSES, threshold, max(1, min(int(limit), 200))),
    )
    return [dict(row) for row in rows]


def followup_candidates(conn, *, hours: float = 24, limit: int = 50) -> list[dict]:
    """Sales that owe the customer one automatic reminder (doc 13 §10.24).

    Only `CONFIRMED` and `NEEDS_OPERATOR`, only open conversations, and never a
    sale already reminded or muted. The caller decides the send window; this
    function only answers "who is due" in a deterministic order.
    """
    threshold = max(1.0, min(float(hours), 24 * 30))
    placeholders = ", ".join("?" for _ in FOLLOWUP_MESSAGE_STATUSES)
    rows = conn.execute(
        f"""SELECT s.*, p.name AS product_name, sup.name AS supplier_name,
                   ROUND((julianday('now') - julianday({_LAST_MOVEMENT_SQL})) * 24, 1)
                       AS age_hours
            FROM sales s
            JOIN conversations c ON c.id = s.conversation_id
            LEFT JOIN products p ON p.id = s.product_id
            LEFT JOIN suppliers sup ON sup.id = s.supplier_id
            WHERE s.status IN ({placeholders})
              AND s.followup_sent_at IS NULL
              AND s.followup_muted_at IS NULL
              AND c.status != 'CLOSED'
              AND (julianday('now') - julianday({_LAST_MOVEMENT_SQL})) * 24 >= ?
            ORDER BY age_hours DESC, s.id ASC
            LIMIT ?""",
        (*FOLLOWUP_MESSAGE_STATUSES, threshold, max(1, min(int(limit), 200))),
    )
    return [dict(row) for row in rows]


def mark_followup_sent(conn, sale_id: int) -> dict:
    """Record the one reminder sent for a sale.

    Deliberately leaves `updated_at` untouched: that column means "last status
    change" and moving it would hide the real age in the dashboard.
    """
    with conn:
        conn.execute(
            "UPDATE sales SET followup_sent_at = ? WHERE id = ?", (_now(), sale_id)
        )
    return get_sale(conn, sale_id)


def mute_followup(conn, sale_id: int, muted: bool = True) -> dict:
    """Operator opt-out for the automatic reminder of a single sale."""
    with conn:
        conn.execute(
            "UPDATE sales SET followup_muted_at = ? WHERE id = ?",
            (_now() if muted else None, sale_id),
        )
    events.log(
        conn,
        "dashboard",
        "sale_followup_muted" if muted else "sale_followup_unmuted",
        entity="sales",
        entity_id=sale_id,
        details={"muted": bool(muted)},
    )
    return get_sale(conn, sale_id)


def list_ledger(
    conn,
    *,
    status: str | None = None,
    currency: str | None = None,
    supplier_id: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    sql = """SELECT l.*, s.order_code, s.status AS sale_status,
                    sup.name AS supplier_name
             FROM commission_ledger l
             LEFT JOIN sales s ON s.id = l.sale_id
             LEFT JOIN suppliers sup ON sup.id = l.supplier_id"""
    where: list[str] = []
    args: list = []
    if status:
        where.append("l.status = ?")
        args.append(status)
    if currency:
        where.append("l.commission_currency = ?")
        args.append(currency)
    if supplier_id is not None:
        where.append("l.supplier_id = ?")
        args.append(int(supplier_id))
    if date_from:
        where.append("date(l.created_at) >= date(?)")
        args.append(date_from)
    if date_to:
        where.append("date(l.created_at) <= date(?)")
        args.append(date_to)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY l.id DESC LIMIT ? OFFSET ?"
    args += [max(1, min(int(limit), 200)), max(0, int(offset))]
    return [dict(row) for row in conn.execute(sql, args)]


def count_ledger(
    conn,
    *,
    status: str | None = None,
    currency: str | None = None,
    supplier_id: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> int:
    sql, args = "SELECT COUNT(*) FROM commission_ledger", []
    where: list[str] = []
    if status:
        where.append("status = ?")
        args.append(status)
    if currency:
        where.append("commission_currency = ?")
        args.append(currency)
    if supplier_id is not None:
        where.append("supplier_id = ?")
        args.append(int(supplier_id))
    if date_from:
        where.append("date(created_at) >= date(?)")
        args.append(date_from)
    if date_to:
        where.append("date(created_at) <= date(?)")
        args.append(date_to)
    if where:
        sql += " WHERE " + " AND ".join(where)
    return int(conn.execute(sql, args).fetchone()[0])


def summary(conn) -> dict:
    """Counts plus totals grouped by currency; never one mixed grand total.

    Commission figures only enter the row for their own currency. A commission
    in a different currency is counted in `conversion_pending` instead of being
    added to the wrong total (doc 02 §6.1).
    """
    # H-18: "today" is the Havana business day, not the UTC day.
    day_start, day_end = clock.day_bounds()
    today = conn.execute(
        """SELECT COUNT(*) FROM sales
           WHERE created_at >= ? AND created_at < ? AND status != 'CANCELLED'""",
        (day_start, day_end),
    ).fetchone()[0]
    pending_approval = conn.execute(
        "SELECT COUNT(*) FROM sales WHERE status = 'PENDING_APPROVAL'"
    ).fetchone()[0]
    pending_dispatch = conn.execute(
        "SELECT COUNT(*) FROM sales WHERE status = 'PENDING_DISPATCH'"
    ).fetchone()[0]
    dispatched = conn.execute(
        "SELECT COUNT(*) FROM sales WHERE status IN ('DISPATCHED', 'CONFIRMED')"
    ).fetchone()[0]
    delivered_pending = conn.execute(
        "SELECT COUNT(*) FROM sales WHERE status = 'DELIVERED'"
    ).fetchone()[0]
    needs_operator = conn.execute(
        "SELECT COUNT(*) FROM sales WHERE status = 'NEEDS_OPERATOR'"
    ).fetchone()[0]

    rows = conn.execute(
        """SELECT currency,
                  SUM(sale_total) AS gross_sales,
                  SUM(CASE WHEN commission_currency = currency
                           THEN commission_earned ELSE 0 END) AS commission_earned,
                  SUM(CASE WHEN commission_currency = currency AND status = 'COLLECTED'
                           THEN commission_earned ELSE 0 END) AS commission_collected,
                  SUM(CASE WHEN commission_currency = currency AND status = 'PENDING'
                           THEN commission_earned ELSE 0 END) AS commission_pending
           FROM commission_ledger
           WHERE status != 'VOID'
           GROUP BY currency
           ORDER BY currency"""
    ).fetchall()
    conversion_pending = conn.execute(
        """SELECT COUNT(*) FROM commission_ledger
           WHERE status != 'VOID' AND commission_currency != currency"""
    ).fetchone()[0]

    totals = [
        {
            "currency": row["currency"],
            "gross_sales": round(float(row["gross_sales"] or 0), 2),
            "commission_earned": round(float(row["commission_earned"] or 0), 2),
            "commission_collected": round(float(row["commission_collected"] or 0), 2),
            "commission_pending": round(float(row["commission_pending"] or 0), 2),
        }
        for row in rows
    ]
    return {
        "mode": sale_mode(conn),
        "sales_today": int(today),
        "sales_pending_approval": int(pending_approval),
        "sales_pending_dispatch": int(pending_dispatch),
        "sales_dispatched": int(dispatched),
        "sales_delivered_pending_settlement": int(delivered_pending),
        "sales_needs_operator": int(needs_operator),
        "conversion_pending": int(conversion_pending),
        "totals_by_currency": totals,
    }


def active_sale_for_conversation(conn, conversation_id: int) -> dict | None:
    """Prevent creating a second sale while the first one is still open."""
    placeholders = ", ".join("?" for _ in OPEN_STATUSES)
    row = conn.execute(
        f"""SELECT * FROM sales
            WHERE conversation_id = ? AND status IN ({placeholders})
            ORDER BY id DESC LIMIT 1""",
        (conversation_id, *OPEN_STATUSES),
    ).fetchone()
    return dict(row) if row is not None else None
