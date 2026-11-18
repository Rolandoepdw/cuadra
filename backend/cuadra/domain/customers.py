"""Customer identity: profile columns, contacts and duplicate signals (F2).

Three capture points feed this module:

- an inbound message touches `first_seen_at` / `last_seen_at`;
- `memory.capture_facts` saves phone, zone and address;
- a sale saves the real delivery phone, zone and address.

A phone is never used to merge customers automatically. The first customer who
claimed a normalized phone keeps it in `customer_contacts`; any other customer
who reports the same number raises `posible_duplicado` on both sides so the
operator can review the pair (plan §5.1).
"""

import json
import re
from datetime import datetime, timezone

from cuadra.domain import catalog, sales
from cuadra.domain.pii import mask_phone

CONTACT_KINDS = ("phone", "email", "whatsapp")
DUPLICATE_SIGNAL = "posible_duplicado"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _db_ts(value=None) -> str:
    """Normalize any stored timestamp to SQLite's ``YYYY-MM-DD HH:MM:SS`` UTC."""
    if value is None or value == "":
        return _now()
    if isinstance(value, datetime):
        moment = value
    else:
        text = str(value).strip()
        try:
            moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return text[:19].replace("T", " ")
    if moment.tzinfo is not None:
        moment = moment.astimezone(timezone.utc)
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def _clean(value, limit: int = 300) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _json(raw) -> dict:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def normalize_contact(kind: str, value: str | None) -> str | None:
    """Return the canonical form used for matching, or None if it is unusable."""
    if kind in ("phone", "whatsapp"):
        return sales.normalize_phone(value)
    if kind == "email":
        text = _clean(value, 160).casefold()
        if "@" not in text or "." not in text.rsplit("@", 1)[-1]:
            return None
        return text
    return None


def _mask_value(kind: str, normalized: str) -> str:
    if kind == "email":
        local, _, domain = normalized.partition("@")
        return f"{local[:1]}***@{domain}" if domain else "***"
    return mask_phone(normalized) or "***"


def mask_contact(kind: str, value: str | None) -> str | None:
    """Mask any contact value for the panel, using its canonical form."""
    normalized = normalize_contact(kind, value)
    if normalized is None:
        return None
    return _mask_value(kind, normalized)


def get_contact(conn, contact_id: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM customer_contacts WHERE id = ?", (contact_id,)
    ).fetchone()
    return dict(row) if row is not None else None


def contacts_for(conn, customer_id: int) -> list[dict]:
    return [
        dict(row)
        for row in conn.execute(
            """SELECT * FROM customer_contacts
                WHERE customer_id = ?
                ORDER BY kind, last_seen_at DESC""",
            (customer_id,),
        )
    ]


def touch(conn, customer_id: int, *, when=None) -> None:
    """Update first/last activity without ever moving them backwards."""
    stamp = _db_ts(when)
    conn.execute(
        """UPDATE customers
              SET first_seen_at = COALESCE(first_seen_at, ?),
                  last_seen_at = CASE
                      WHEN last_seen_at IS NULL OR last_seen_at < ? THEN ?
                      ELSE last_seen_at
                  END
            WHERE id = ?""",
        (stamp, stamp, stamp, customer_id),
    )
    conn.commit()


def _flag_duplicate(
    conn,
    customer_id: int,
    other_customer_id: int,
    kind: str,
    normalized: str,
    stamp: str,
) -> bool:
    """Open (or refresh) ``posible_duplicado``. True only when it is new."""
    evidence = {
        "kind": kind,
        "other_customer_id": int(other_customer_id),
        "value_masked": _mask_value(kind, normalized),
    }
    row = conn.execute(
        """SELECT id, count, evidence_json FROM customer_signals
            WHERE customer_id = ? AND kind = ? AND status = 'OPEN'""",
        (customer_id, DUPLICATE_SIGNAL),
    ).fetchone()
    if row is None:
        conn.execute(
            """INSERT INTO customer_signals
               (customer_id, kind, severity, status, count, evidence_json,
                first_seen_at, last_seen_at)
               VALUES (?, ?, 'info', 'OPEN', 1, ?, ?, ?)""",
            (
                customer_id,
                DUPLICATE_SIGNAL,
                json.dumps(evidence, ensure_ascii=False),
                stamp,
                stamp,
            ),
        )
        return True

    previous = _json(row["evidence_json"])
    same_claim = (
        previous.get("kind") == kind
        and previous.get("other_customer_id") == int(other_customer_id)
    )
    count = int(row["count"]) + (0 if same_claim else 1)
    conn.execute(
        """UPDATE customer_signals
              SET count = ?, evidence_json = ?,
                  last_seen_at = MAX(last_seen_at, ?)
            WHERE id = ?""",
        (count, json.dumps(evidence, ensure_ascii=False), stamp, row["id"]),
    )
    return False


def record_contact(
    conn,
    customer_id: int,
    kind: str,
    value: str | None,
    *,
    source: str,
    confidence: float = 1.0,
    label: str | None = None,
    verified: bool = False,
    when=None,
) -> dict:
    """Link a contact to a customer without ever stealing another's number.

    Returns ``{"contact", "created", "duplicate_of", "signal_created"}``.
    """
    if kind not in CONTACT_KINDS:
        raise ValueError(f"invalid contact kind: {kind}")
    normalized = normalize_contact(kind, value)
    if normalized is None:
        return {
            "contact": None,
            "created": False,
            "duplicate_of": None,
            "signal_created": False,
        }

    stamp = _db_ts(when)
    row = conn.execute(
        "SELECT * FROM customer_contacts WHERE kind = ? AND normalized = ?",
        (kind, normalized),
    ).fetchone()

    if row is not None:
        owner = int(row["customer_id"])
        if owner == customer_id:
            conn.execute(
                """UPDATE customer_contacts
                      SET last_seen_at = MAX(last_seen_at, ?),
                          confidence = MAX(confidence, ?),
                          verified = MAX(verified, ?),
                          label = COALESCE(label, ?)
                    WHERE id = ?""",
                (stamp, float(confidence), 1 if verified else 0, label, row["id"]),
            )
            conn.commit()
            return {
                "contact": get_contact(conn, row["id"]),
                "created": False,
                "duplicate_of": None,
                "signal_created": False,
            }

        # Two customers claim the same phone: keep the first owner, flag both.
        created = _flag_duplicate(
            conn, customer_id, owner, kind, normalized, stamp
        )
        created = (
            _flag_duplicate(conn, owner, customer_id, kind, normalized, stamp)
            or created
        )
        conn.commit()
        return {
            "contact": dict(row),
            "created": False,
            "duplicate_of": owner,
            "signal_created": created,
        }

    cur = conn.execute(
        """INSERT INTO customer_contacts
           (customer_id, kind, value, normalized, label, source, confidence,
            verified, first_seen_at, last_seen_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            customer_id,
            kind,
            _clean(value, 160),
            normalized,
            label,
            source,
            float(confidence),
            1 if verified else 0,
            stamp,
            stamp,
        ),
    )
    conn.commit()
    return {
        "contact": get_contact(conn, cur.lastrowid),
        "created": True,
        "duplicate_of": None,
        "signal_created": False,
    }


def set_profile_fields(
    conn,
    customer_id: int,
    *,
    phone: str | None = None,
    address: str | None = None,
    zone_name: str | None = None,
    zone_id: int | None = None,
    source: str = "fact",
    confidence: float = 0.8,
    when=None,
) -> dict:
    """Persist the profile values captured from a message or a sale."""
    updates: dict = {}
    normalized = None

    if phone:
        normalized = sales.normalize_phone(phone)
        if normalized:
            updates["phone"] = normalized
            updates["phone_normalized"] = normalized
    if address:
        cleaned = _clean(address)
        if len(cleaned) >= 6:
            updates["usual_address"] = cleaned
    if zone_id is None and zone_name:
        zone = catalog.resolve_zone(conn, zone_name)
        if zone is not None:
            zone_id = int(zone["id"])
    if zone_id:
        updates["usual_zone_id"] = int(zone_id)

    if updates:
        assignments = ", ".join(f"{key} = ?" for key in updates)
        conn.execute(
            f"UPDATE customers SET {assignments} WHERE id = ?",
            (*updates.values(), customer_id),
        )
        conn.commit()

    contact = None
    if normalized:
        contact = record_contact(
            conn,
            customer_id,
            "phone",
            normalized,
            source=source,
            confidence=confidence,
            when=when,
        )
    touch(conn, customer_id, when=when)
    return {"updates": updates, "contact": contact}


def record_sale_contact(conn, sale: dict) -> dict:
    """A sale is the most reliable contact data the customer ever gave us."""
    customer_id = sale.get("customer_id")
    if not customer_id:
        return {}
    when = sale.get("created_at") or sale.get("updated_at")
    result = set_profile_fields(
        conn,
        int(customer_id),
        phone=sale.get("customer_phone"),
        address=sale.get("delivery_address"),
        zone_id=sale.get("delivery_zone_id"),
        source="sale",
        confidence=1.0,
        when=when,
    )
    recipient = _clean(sale.get("recipient_name"), 120)
    if len(recipient) >= 2:
        conn.execute(
            """UPDATE customers
                  SET display_name = COALESCE(NULLIF(TRIM(display_name), ''), ?)
                WHERE id = ?""",
            (recipient, customer_id),
        )
        conn.commit()
        result["display_name"] = recipient
    return result


def open_signals(conn, customer_id: int) -> list[dict]:
    return [
        dict(row)
        for row in conn.execute(
            """SELECT * FROM customer_signals
                WHERE customer_id = ? AND status = 'OPEN'
                ORDER BY severity DESC, id DESC""",
            (customer_id,),
        )
    ]
