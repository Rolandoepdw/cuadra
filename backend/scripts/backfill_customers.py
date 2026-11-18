"""Backfill customer identity from sales and conversation facts (phase F2).

Before migration 021 the only contact data lived in `sales` (the real delivery
phone, zone and address), `conversation_facts` (what the customer said) and
`customers.preferences_json`. This script copies that into the queryable columns
and `customer_contacts`, and raises `posible_duplicado` when two customers share
a phone. It never merges anything.

Idempotent: only empty columns are filled, contacts are unique by normalized
value, and a repeated duplicate claim does not inflate the signal count. A
second run reports zero changes.

Usage (from ``backend/``):

    .venv\\Scripts\\python.exe scripts\\backfill_customers.py --dry-run
    .venv\\Scripts\\python.exe scripts\\backfill_customers.py
"""

import argparse
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from cuadra import config, db  # noqa: E402
from cuadra.domain import catalog, customers, sales  # noqa: E402

_PHONE_FACT = "customer_phone"
_ADDRESS_FACT = "delivery_address"
_ZONE_FACT = "zone_name"
_NAME_FACT = "customer_name"


def _latest_facts(conn, customer_id: int) -> dict[str, str]:
    rows = conn.execute(
        """SELECT f.fact_key, f.fact_value
             FROM conversation_facts f
             JOIN conversations c ON c.id = f.conversation_id
            WHERE c.customer_id = ?
              AND f.fact_key IN (?, ?, ?, ?)
            ORDER BY f.updated_at DESC, f.id DESC""",
        (customer_id, _PHONE_FACT, _ADDRESS_FACT, _ZONE_FACT, _NAME_FACT),
    ).fetchall()
    facts: dict[str, str] = {}
    for row in rows:
        facts.setdefault(row["fact_key"], row["fact_value"])
    return facts


def _latest_sale(conn, customer_id: int) -> dict | None:
    row = conn.execute(
        """SELECT id, customer_phone, delivery_zone_id, delivery_zone,
                  delivery_address, recipient_name, created_at, updated_at
             FROM sales
            WHERE customer_id = ? AND status <> 'CANCELLED'
            ORDER BY id DESC LIMIT 1""",
        (customer_id,),
    ).fetchone()
    return dict(row) if row is not None else None


def _activity(conn, customer_id: int) -> tuple[str | None, str | None]:
    row = conn.execute(
        """SELECT MIN(ts) AS first, MAX(ts) AS last FROM (
               SELECT datetime(created_at) AS ts
                 FROM conversations WHERE customer_id = ?
               UNION ALL
               SELECT datetime(m.created_at)
                 FROM messages m
                 JOIN conversations c ON c.id = m.conversation_id
                WHERE c.customer_id = ?
               UNION ALL
               SELECT datetime(created_at)
                 FROM sales WHERE customer_id = ?
           )""",
        (customer_id, customer_id, customer_id),
    ).fetchone()
    return (row["first"], row["last"])


def _contact_state(conn, customer_id: int, kind: str, normalized: str) -> str:
    row = conn.execute(
        "SELECT customer_id FROM customer_contacts WHERE kind = ? AND normalized = ?",
        (kind, normalized),
    ).fetchone()
    if row is None:
        return "new"
    if int(row["customer_id"]) == customer_id:
        return "existing"
    return "duplicate"


def _supplier_updates(conn) -> int:
    return conn.execute(
        """SELECT COUNT(*) FROM customers
            WHERE kind <> 'supplier'
              AND id IN (
                  SELECT cc.customer_id
                    FROM customer_channels cc
                    JOIN suppliers s
                      ON cc.identifier = s.telegram_peer_id
                      OR cc.identifier = s.telegram_id
                   WHERE cc.channel = 'telegram'
              )"""
    ).fetchone()[0]


def _mark_suppliers(conn) -> int:
    cur = conn.execute(
        """UPDATE customers
              SET kind = 'supplier'
            WHERE kind <> 'supplier'
              AND id IN (
                  SELECT cc.customer_id
                    FROM customer_channels cc
                    JOIN suppliers s
                      ON cc.identifier = s.telegram_peer_id
                      OR cc.identifier = s.telegram_id
                   WHERE cc.channel = 'telegram'
              )"""
    )
    conn.commit()
    return cur.rowcount


def backfill(conn, *, dry_run: bool = False) -> dict:
    report = {
        "customers_scanned": 0,
        "suppliers_skipped": 0,
        "customers_updated": 0,
        "first_last_filled": 0,
        "phones_filled": 0,
        "addresses_filled": 0,
        "zones_filled": 0,
        "names_filled": 0,
        "contacts_created": 0,
        "contacts_existing": 0,
        "duplicate_contacts": 0,
        "duplicates_flagged": 0,
        "suppliers_marked": 0,
        "dry_run": dry_run,
    }

    report["suppliers_marked"] = (
        _supplier_updates(conn) if dry_run else _mark_suppliers(conn)
    )

    rows = [dict(row) for row in conn.execute("SELECT * FROM customers ORDER BY id")]
    for customer in rows:
        if customer.get("kind") != "customer":
            report["suppliers_skipped"] += 1
            continue
        report["customers_scanned"] += 1
        customer_id = int(customer["id"])
        sale = _latest_sale(conn, customer_id)
        facts = _latest_facts(conn, customer_id)
        first, last = _activity(conn, customer_id)

        sale_phone = (sale or {}).get("customer_phone")
        fact_phone = facts.get(_PHONE_FACT)
        phone = customer.get("phone") or sale_phone or fact_phone
        normalized = sales.normalize_phone(phone) if phone else None

        address = (
            customer.get("usual_address")
            or (sale or {}).get("delivery_address")
            or facts.get(_ADDRESS_FACT)
        )
        recipient = (sale or {}).get("recipient_name")

        updates: dict = {}
        if not customer.get("first_seen_at") and first:
            updates["first_seen_at"] = first
            report["first_last_filled"] += 1
        if not customer.get("last_seen_at") and last:
            updates["last_seen_at"] = last
            report["first_last_filled"] += 1
        if not customer.get("phone") and normalized:
            updates["phone"] = normalized
            report["phones_filled"] += 1
        if not customer.get("phone_normalized") and normalized:
            updates["phone_normalized"] = normalized
            if "phone" not in updates:
                report["phones_filled"] += 1
        if not customer.get("usual_address") and address:
            updates["usual_address"] = address
            report["addresses_filled"] += 1
        if not customer.get("usual_zone_id"):
            zone_id = (sale or {}).get("delivery_zone_id")
            if zone_id is None and facts.get(_ZONE_FACT):
                zone = catalog.resolve_zone(conn, facts[_ZONE_FACT])
                zone_id = zone["id"] if zone is not None else None
            if zone_id:
                updates["usual_zone_id"] = int(zone_id)
                report["zones_filled"] += 1
        if not customer.get("display_name") and recipient:
            updates["display_name"] = recipient
            report["names_filled"] += 1

        if updates:
            report["customers_updated"] += 1
            if not dry_run:
                assignments = ", ".join(f"{key} = ?" for key in updates)
                conn.execute(
                    f"UPDATE customers SET {assignments} WHERE id = ?",
                    (*updates.values(), customer_id),
                )
                conn.commit()

        if not normalized:
            continue
        source = "sale" if sale_phone else "fact"
        confidence = 1.0 if sale_phone else 0.8

        if dry_run:
            state = _contact_state(conn, customer_id, "phone", normalized)
            if state == "new":
                report["contacts_created"] += 1
            elif state == "duplicate":
                report["duplicate_contacts"] += 1
            else:
                report["contacts_existing"] += 1
            continue

        result = customers.record_contact(
            conn,
            customer_id,
            "phone",
            normalized,
            source=source,
            confidence=confidence,
        )
        if result["created"]:
            report["contacts_created"] += 1
        elif result["duplicate_of"] is not None:
            report["duplicate_contacts"] += 1
            if result["signal_created"]:
                report["duplicates_flagged"] += 1
        else:
            report["contacts_existing"] += 1

    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would change without writing",
    )
    args = parser.parse_args()

    conn = db.connect()
    try:
        applied = db.apply_migrations(conn)
        if applied:
            print(f"migraciones aplicadas: {', '.join(applied)}")
        print(f"base de datos: {config.DB_PATH}")
        report = backfill(conn, dry_run=args.dry_run)
    finally:
        conn.close()

    prefix = "[dry-run] " if args.dry_run else ""
    labels = (
        ("customers_scanned", "clientes escaneados"),
        ("suppliers_skipped", "proveedores omitidos"),
        ("customers_updated", "clientes actualizados"),
        ("first_last_filled", "primera/última actividad rellenadas"),
        ("phones_filled", "teléfonos rellenados"),
        ("addresses_filled", "direcciones rellenadas"),
        ("zones_filled", "zonas rellenadas"),
        ("names_filled", "nombres de entrega rellenados"),
        ("contacts_created", "contactos creados"),
        ("contacts_existing", "contactos ya existentes"),
        ("duplicate_contacts", "contactos duplicados detectados"),
        ("duplicates_flagged", "señales posible_duplicado nuevas"),
        ("suppliers_marked", "proveedores marcados"),
    )
    for key, label in labels:
        print(f"{prefix}{label + ':':38} {report[key]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
