"""Seed the L3 evaluation fixtures (customers, sales, escalations, memory).

`seed_single_supplier.py` / `seed_demo_data.py` create the catalog, suppliers,
zones and groups. They do not create customers, conversations, sales,
escalations, facts or pending offers, so the L3 read layer has nothing to read
in a fresh database.

This script adds those fixtures **on top of an existing seed**. It is meant for
a test database or after a backup: it writes customers, conversations, sales,
escalations, facts and messages with an `eval-` prefix so they are easy to spot
and reset.

The `eval-client-*` customers are **synthetic fixtures** for the dashboard and
the read layer; they are NOT your Telegram accounts. The `EVAL ...` products are
assigned to the supplier passed with `--supplier` (or the first active one; if
there is none, `EVAL TCP` is created). Pass `--telegram-id` (the TCP's numeric
peer id) and optionally `--telegram-handle` (@usuario) and the seed links that
supplier to your real TCP account; otherwise you can link it later in the
dashboard.

For a one-command evaluation start:

    .venv\\Scripts\\python.exe scripts\\seed_l3_eval.py --telegram-id 123456789 --telegram-handle @mi_tcp

Run from `backend/`:

    .venv\\Scripts\\python.exe scripts\\seed_l3_eval.py --supplier "Nombre del TCP"
    .venv\\Scripts\\python.exe scripts\\seed_l3_eval.py --db data\\cuadra_eval.db
    .venv\\Scripts\\python.exe scripts\\seed_l3_eval.py --reset

`--reset` deletes only the fixtures whose customer identifier starts with
`eval-`; it never touches the real catalog, suppliers, zones or customers.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from cuadra import config, db  # noqa: E402
from cuadra.ai import memory  # noqa: E402
from cuadra.ai.turn.frame import Listing  # noqa: E402
from cuadra.domain import catalog, conversations, escalations, sales  # noqa: E402

ZONES = ("Playa", "Marianao", "Centro")
COVERED = ("Playa", "Marianao")
CATEGORIES = (
    "EVAL Tecnología",
    "EVAL Computación",
    "EVAL Accesorios",
    "EVAL Motos",
    "EVAL Cuatro",
    "EVAL Seis",
    "EVAL Doce",
    "EVAL Vacía",
)

PRODUCTS = [
    {
        "name": "EVAL Laptop",
        "categories": ["EVAL Tecnología", "EVAL Computación"],
        "price": 720.0,
        "commission": 20.0,
        "stock": 5,
        "description": "Laptop de evaluación con Ryzen 7.",
        "specifications": {
            "procesador": "Ryzen 7",
            "ram": "16GB",
            "almacenamiento": "1TB SSD",
        },
    },
    {
        "name": "EVAL Mouse",
        "categories": ["EVAL Accesorios"],
        "price": 15.0,
        "commission": 2.0,
        "stock": 25,
        "description": "Mouse inalámbrico de evaluación.",
        "specifications": {"conexion": "USB 2.4 GHz"},
    },
    {
        "name": "EVAL Teclado",
        "categories": ["EVAL Accesorios"],
        "price": 45.0,
        "commission": 3.0,
        "stock": 0,
        "status": "OUT_OF_STOCK",
        "description": "Teclado mecánico de evaluación.",
        "specifications": {"tipo": "mecánico"},
    },
    {
        "name": "EVAL Audífonos",
        "categories": ["EVAL Accesorios"],
        "price": 35.0,
        "commission": 3.0,
        "stock": 8,
        "description": "Audífonos Bluetooth de evaluación.",
        "specifications": {"bateria": "20 horas"},
    },
    {
        "name": "EVAL Moto",
        "categories": ["EVAL Motos"],
        "price": 1200.0,
        "commission": 50.0,
        "stock": 3,
        "description": "Moto eléctrica de evaluación.",
        "specifications": {"autonomia": "80 km"},
    },
    {
        "name": "EVAL Sin Categoría",
        "categories": [],
        "price": 25.0,
        "commission": 2.0,
        "stock": 2,
        "description": "Producto de evaluación sin categoría.",
        "specifications": {},
    },
]
for index in range(1, 5):
    PRODUCTS.append(
        {
            "name": f"EVAL Cuatro {index}",
            "categories": ["EVAL Cuatro"],
            "price": 10.0,
            "commission": 1.0,
            "stock": 5,
            "description": "Producto de la categoría de 4.",
            "specifications": {},
        }
    )
for index in range(1, 7):
    PRODUCTS.append(
        {
            "name": f"EVAL Seis {index}",
            "categories": ["EVAL Seis"],
            "price": 10.0,
            "commission": 1.0,
            "stock": 5,
            "description": "Producto de la categoría de 6.",
            "specifications": {},
        }
    )
for index in range(1, 13):
    PRODUCTS.append(
        {
            "name": f"EVAL Doce {index}",
            "categories": ["EVAL Doce"],
            "price": 10.0,
            "commission": 1.0,
            "stock": 5,
            "description": "Producto de la categoría de 12.",
            "specifications": {},
        }
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _zone(conn, name: str) -> dict:
    row = conn.execute("SELECT * FROM zones WHERE name = ?", (name,)).fetchone()
    if row is not None:
        return dict(row)
    return catalog.create_zone(conn, {"name": name, "active": 1})


def _category(conn, name: str) -> int:
    row = conn.execute("SELECT id FROM categories WHERE name = ?", (name,)).fetchone()
    if row is not None:
        return int(row["id"])
    cur = conn.execute("INSERT INTO categories (name) VALUES (?)", (name,))
    conn.commit()
    return int(cur.lastrowid)


def _supplier(conn, name: str | None) -> tuple[dict, bool]:
    if name:
        row = conn.execute(
            "SELECT * FROM suppliers WHERE name = ?", (name,)
        ).fetchone()
        if row is None:
            raise SystemExit(
                f"No existe el proveedor {name!r}. "
                "Primero corre seed_single_supplier.py/seed_demo_data.py o "
                "crea el proveedor en el dashboard."
            )
        return dict(row), False
    row = conn.execute(
        "SELECT * FROM suppliers WHERE active = 1 ORDER BY id LIMIT 1"
    ).fetchone()
    if row is not None:
        return dict(row), False
    return (
        catalog.create_supplier(
            conn, {"name": "EVAL TCP", "delivery_zone": "La Habana", "active": 1}
        ),
        True,
    )


def _product(conn, supplier_id: int, spec: dict) -> dict:
    row = conn.execute(
        "SELECT id FROM products WHERE supplier_id = ? AND name = ?",
        (supplier_id, spec["name"]),
    ).fetchone()
    if row is not None:
        return catalog.get_product(conn, int(row["id"]))
    category_ids = [_category(conn, name) for name in spec["categories"]]
    data = {
        "supplier_id": supplier_id,
        "name": spec["name"],
        "price": spec["price"],
        "commission": spec["commission"],
        "stock": spec["stock"],
        "description": spec["description"],
        "specifications": spec["specifications"],
        "category_ids": category_ids,
    }
    if spec.get("status"):
        data["status"] = spec["status"]
    return catalog.create_product(conn, data)


def _customer(conn, identifier: str, name: str) -> dict:
    return conversations.get_or_create_customer(
        conn, "telegram", identifier, name
    )


def _conversation(conn, customer_id: int) -> dict:
    return conversations.get_or_create_open_conversation(
        conn, customer_id, "telegram", identity="bot"
    )


def _sale(conn, *, conversation_id: int, customer_id: int, product: dict) -> dict:
    return sales.create_sale(
        conn,
        conversation_id=conversation_id,
        customer_id=customer_id,
        product_id=product["id"],
        quantity=1,
        recipient_name="Cliente Evaluación",
        delivery_zone="Playa",
        delivery_address="Calle Eval 1 #2, Playa",
        customer_phone="55500001",
        mode="autonomous",
    )


def _reset(conn) -> None:
    customers = [
        int(row["id"])
        for row in conn.execute(
            """SELECT DISTINCT c.id FROM customers c
               JOIN customer_channels cc ON cc.customer_id = c.id
               WHERE cc.identifier LIKE 'eval-%'"""
        )
    ]
    if not customers:
        print("No hay fixtures eval- que borrar.")
        return
    marks = ", ".join("?" for _ in customers)
    conversations_ids = [
        int(row["id"])
        for row in conn.execute(
            f"SELECT id FROM conversations WHERE customer_id IN ({marks})",
            customers,
        )
    ]
    if conversations_ids:
        cmarks = ", ".join("?" for _ in conversations_ids)
        conn.execute(
            f"""DELETE FROM commission_ledger WHERE sale_id IN
                (SELECT id FROM sales WHERE conversation_id IN ({cmarks}))""",
            conversations_ids,
        )
        conn.execute(
            f"DELETE FROM sales WHERE conversation_id IN ({cmarks})",
            conversations_ids,
        )
        conn.execute(
            f"DELETE FROM escalations WHERE conversation_id IN ({cmarks})",
            conversations_ids,
        )
        conn.execute(
            f"DELETE FROM conversations WHERE id IN ({cmarks})",
            conversations_ids,
        )
    conn.execute(
        f"DELETE FROM customer_channels WHERE customer_id IN ({marks})", customers
    )
    conn.execute(f"DELETE FROM customers WHERE id IN ({marks})", customers)
    # Restore the eval products' stock so a reset can be repeated safely.
    for spec in PRODUCTS:
        conn.execute(
            "UPDATE products SET stock = ?, stock_reserved = 0 WHERE name = ?",
            (spec["stock"], spec["name"]),
        )
    conn.commit()
    print(f"Reset: {len(customers)} clientes eval- borrados.")


def _set_profile(conn, customer_id: int, name: str) -> None:
    prefs = {
        "name": name,
        "usual_zone": "Playa",
        "usual_address": "Calle Eval 1 #2, Playa",
        "phone": "55500001",
    }
    conn.execute(
        "UPDATE customers SET name = ?, preferences_json = ? WHERE id = ?",
        (name, json.dumps(prefs, ensure_ascii=False), customer_id),
    )
    conn.commit()


def _facts(conn, conversation_id: int, name: str) -> None:
    memory.upsert_fact(
        conn, conversation_id, memory.FACT_CUSTOMER_NAME, name, confidence=0.9
    )
    memory.upsert_fact(
        conn, conversation_id, memory.FACT_ZONE, "Playa", confidence=0.9
    )
    memory.upsert_fact(
        conn,
        conversation_id,
        memory.FACT_ADDRESS,
        "Calle Eval 1 #2, Playa",
        confidence=0.9,
    )
    memory.upsert_fact(
        conn, conversation_id, memory.FACT_PHONE, "55500001", confidence=0.9
    )
    memory.upsert_fact(
        conn, conversation_id, memory.FACT_BUDGET, "800 USD", confidence=0.7
    )


def seed(
    conn,
    *,
    supplier_name: str | None = None,
    client_name: str = "Cliente Evaluación",
    telegram_id: str | None = None,
    telegram_handle: str | None = None,
) -> dict:
    zones = {name: _zone(conn, name) for name in ZONES}
    for name in CATEGORIES:
        _category(conn, name)
    supplier, supplier_created = _supplier(conn, supplier_name)
    link = {}
    if telegram_handle:
        link["telegram_id"] = telegram_handle
    if telegram_id:
        link["telegram_peer_id"] = str(telegram_id)
    if link:
        supplier = catalog.update_supplier(conn, supplier["id"], link)
    current = {int(row["id"]) for row in catalog.list_supplier_zones(conn, supplier["id"])}
    current |= {zones[name]["id"] for name in COVERED}
    catalog.set_supplier_zones(conn, supplier["id"], sorted(current))

    products = {spec["name"]: _product(conn, supplier["id"], spec) for spec in PRODUCTS}

    main = _customer(conn, "eval-client-main", client_name)
    recurrent = _customer(conn, "eval-client-recurrent", "Cliente Recurrente")
    conv_main = _conversation(conn, main["id"])
    conv_rec = _conversation(conn, recurrent["id"])

    # Messages first: the summary marker points to the last one.
    conversations.add_message(
        conn,
        conv_main["id"],
        "INBOUND",
        "customer",
        body="Hola, busco accesorios para laptop",
        external_id="eval-main-1",
    )
    conversations.add_message(
        conn,
        conv_main["id"],
        "OUTBOUND",
        "bot",
        body="Esto es lo que tengo en EVAL Accesorios: EVAL Mouse, EVAL Teclado, EVAL Audífonos.",
        external_id="eval-main-2",
    )
    conversations.add_message(
        conn,
        conv_main["id"],
        "INBOUND",
        "customer",
        body="El segundo me interesa",
        external_id="eval-main-3",
    )
    last_main = conversations.add_message(
        conn,
        conv_main["id"],
        "OUTBOUND",
        "bot",
        body="Te paso la ficha del EVAL Teclado. ¿Te lo reservo?",
        external_id="eval-main-4",
    )
    conversations.add_message(
        conn,
        conv_rec["id"],
        "INBOUND",
        "customer",
        body="Quiero el EVAL Cuatro 1",
        external_id="eval-rec-1",
    )
    conversations.add_message(
        conn,
        conv_rec["id"],
        "OUTBOUND",
        "bot",
        body="Te lo reservo. ¿Me confirmas la entrega?",
        external_id="eval-rec-2",
    )

    # Sales in every state the post-sale route can read.
    existing = conn.execute(
        "SELECT COUNT(*) FROM sales WHERE customer_id IN (?, ?)",
        (main["id"], recurrent["id"]),
    ).fetchone()[0]
    sale_rows = []
    if not existing:
        pending = _sale(
            conn,
            conversation_id=conv_main["id"],
            customer_id=main["id"],
            product=products["EVAL Laptop"],
        )
        dispatched = _sale(
            conn,
            conversation_id=conv_main["id"],
            customer_id=main["id"],
            product=products["EVAL Mouse"],
        )
        dispatched = sales.mark_dispatched(conn, dispatched["id"], "eval-tcp-msg-1")
        delivered = _sale(
            conn,
            conversation_id=conv_main["id"],
            customer_id=main["id"],
            product=products["EVAL Audífonos"],
        )
        sales.mark_dispatched(conn, delivered["id"], "eval-tcp-msg-2")
        delivered = sales.deliver(conn, delivered["id"])
        cancelled = _sale(
            conn,
            conversation_id=conv_main["id"],
            customer_id=main["id"],
            product=products["EVAL Moto"],
        )
        cancelled = sales.cancel(conn, cancelled["id"], "prueba de evaluación")
        operator = _sale(
            conn,
            conversation_id=conv_main["id"],
            customer_id=main["id"],
            product=products["EVAL Sin Categoría"],
        )
        operator = sales.mark_needs_operator(
            conn, operator["id"], "stock físico insuficiente (eval)"
        )
        settled = _sale(
            conn,
            conversation_id=conv_rec["id"],
            customer_id=recurrent["id"],
            product=products["EVAL Cuatro 1"],
        )
        sales.mark_dispatched(conn, settled["id"], "eval-tcp-msg-3")
        settled = sales.deliver(conn, settled["id"])
        settled = sales.settle(conn, settled["id"])
        sale_rows = [
            ("pending", pending),
            ("dispatched", dispatched),
            ("delivered", delivered),
            ("cancelled", cancelled),
            ("needs_operator", operator),
            ("settled", settled),
        ]
    else:
        delivered = conn.execute(
            """SELECT * FROM sales WHERE customer_id = ?
               AND status = 'DELIVERED' ORDER BY id DESC LIMIT 1""",
            (main["id"],),
        ).fetchone()
        delivered = dict(delivered) if delivered is not None else None

    # Escalations in every state the TCP loop can read.
    existing_esc = conn.execute(
        "SELECT COUNT(*) FROM escalations WHERE conversation_id IN (?, ?)",
        (conv_main["id"], conv_rec["id"]),
    ).fetchone()[0]
    escalation_rows = []
    if not existing_esc:
        pending_esc, _ = escalations.create(
            conn,
            conversation_id=conv_main["id"],
            supplier_id=supplier["id"],
            question="¿Tiene garantía de 2 años?",
            product_id=products["EVAL Laptop"]["id"],
        )
        answered_esc, _ = escalations.create(
            conn,
            conversation_id=conv_main["id"],
            supplier_id=supplier["id"],
            question="¿Es compatible con Mac?",
            product_id=products["EVAL Mouse"]["id"],
        )
        answered_esc = escalations.set_answer(
            conn, answered_esc["id"], "Sí, es compatible."
        )
        resolved_esc, _ = escalations.create(
            conn,
            conversation_id=conv_main["id"],
            supplier_id=supplier["id"],
            question="¿Cuántas horas de batería?",
            product_id=products["EVAL Audífonos"]["id"],
        )
        resolved_esc = escalations.set_answer(conn, resolved_esc["id"], "20 horas")
        resolved_esc = escalations.resolve(conn, resolved_esc["id"])
        timeout_esc, _ = escalations.create(
            conn,
            conversation_id=conv_main["id"],
            supplier_id=supplier["id"],
            question="¿Tiene retroiluminación?",
            product_id=products["EVAL Teclado"]["id"],
        )
        timeout_esc = escalations.mark_timeout(conn, timeout_esc["id"])
        escalation_rows = [
            ("pending", pending_esc),
            ("answered", answered_esc),
            ("resolved", resolved_esc),
            ("timeout", timeout_esc),
        ]
    else:
        pending_esc = conn.execute(
            """SELECT * FROM escalations WHERE conversation_id = ?
               AND status = 'PENDING' ORDER BY id DESC LIMIT 1""",
            (conv_main["id"],),
        ).fetchone()
        pending_esc = dict(pending_esc) if pending_esc is not None else None

    _set_profile(conn, main["id"], client_name)
    _facts(conn, conv_main["id"], client_name)

    mouse = products["EVAL Mouse"]
    teclado = products["EVAL Teclado"]
    audifonos = products["EVAL Audífonos"]
    listing = Listing(
        product_ids=[mouse["id"], teclado["id"], audifonos["id"]],
        labels=[mouse["name"], teclado["name"], audifonos["name"]],
        query="accesorios eval",
        kind="table",
        created_at=_now(),
    )
    context = {
        "conversation_id": conv_main["id"],
        "stage": "DONE" if delivered else "DISCOVERY",
        "last_listing": listing.as_dict(),
        "pending_offer": {
            "kind": "alternatives",
            "product_id": teclado["id"],
            "product_ids": [mouse["id"], audifonos["id"]],
            "query": "teclado",
        },
        "summary": (
            "El cliente pidió accesorios para laptop y se interesó por el "
            "segundo. Hay una venta entregada y una consulta pendiente."
        ),
        "summary_through_message_id": last_main["id"],
    }
    if delivered:
        context.update(
            {
                "sale_id": delivered["id"],
                "order_code": delivered["order_code"],
                "product_id": delivered["product_id"],
            }
        )
    conversations.set_context(conn, conv_main["id"], context)
    conversations.set_context(
        conn,
        conv_rec["id"],
        {
            "conversation_id": conv_rec["id"],
            "stage": "DISCOVERY",
            "summary": "Cliente recurrente; compró EVAL Cuatro 1.",
        },
    )

    return {
        "supplier": supplier,
        "supplier_created": supplier_created,
        "supplier_linked": bool(link),
        "zones": {name: zones[name] for name in ZONES},
        "products": products,
        "customers": {"main": main, "recurrent": recurrent},
        "identifiers": {
            "main": "eval-client-main",
            "recurrent": "eval-client-recurrent",
        },
        "conversations": {"main": conv_main, "recurrent": conv_rec},
        "sales": sale_rows,
        "escalations": escalation_rows,
        "context": context,
    }


def _print_manifest(data: dict) -> None:
    supplier = data["supplier"]
    print("")
    print("== Fixtures L3 ==")
    print(f"Proveedor: {supplier['name']} (id={supplier['id']})")
    if data.get("supplier_created"):
        if data.get("supplier_linked"):
            print(
                "  (creado y enlazado con --telegram-id/--telegram-handle)"
            )
        else:
            print(
                "  (creado sin enlace; pasa --telegram-id o enlázalo en el "
                "dashboard)"
            )
    print(
        "  Telegram: "
        f"{supplier.get('telegram_id') or '(sin @usuario)'} / "
        f"peer {supplier.get('telegram_peer_id') or '(sin peer id)'}"
    )
    print(
        "Zonas: "
        + ", ".join(
            f"{name} (id={zone['id']})" for name, zone in data["zones"].items()
        )
    )
    print("")
    print("Productos:")
    for name, product in data["products"].items():
        print(
            f"  {name:<20} id={product['id']:<4} "
            f"stock={product.get('stock')!s:<4} "
            f"categorias={', '.join(product.get('categories') or []) or '(sin categoría)'}"
        )
    print("")
    for label, customer in data["customers"].items():
        print(
            f"Cliente {label}: {customer['name']} id={customer['id']} "
            f"identifier={data['identifiers'][label]}"
        )
    print("")
    print("Ventas:")
    for label, sale in data["sales"]:
        print(
            f"  {label:<15} {sale['order_code']}  status={sale['status']}  "
            f"product_id={sale['product_id']}  sale_id={sale['id']}"
        )
    print("")
    print("Escalaciones:")
    for label, esc in data["escalations"]:
        print(
            f"  {label:<10} id={esc['id']:<4} status={esc['status']:<9} "
            f"product_id={esc.get('product_id')}  {esc.get('question')}"
        )
    print("")
    print(
        "Contexto principal: "
        f"conversation_id={data['context']['conversation_id']}  "
        f"stage={data['context']['stage']}  "
        f"last_listing={data['context']['last_listing']['product_ids']}  "
        f"pending_offer={data['context']['pending_offer']['kind']}"
    )
    print("")
    print("Siguiente: abre el dashboard y revisa Conversaciones, Catálogo,")
    print("Finanzas, Escalaciones y Métricas -> Herramientas.")


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(config.DB_PATH))
    parser.add_argument(
        "--supplier",
        default=None,
        help=(
            "nombre del proveedor/TCP existente. Si se omite, usa el primero "
            "activo; si no hay ninguno, crea 'EVAL TCP'."
        ),
    )
    parser.add_argument("--client-name", default="Cliente Evaluación")
    parser.add_argument(
        "--telegram-id",
        default=None,
        help="id numérico del TCP (telegram_peer_id); lo da @userinfobot",
    )
    parser.add_argument(
        "--telegram-handle", default=None, help="@usuario del TCP (opcional)"
    )
    parser.add_argument(
        "--reset", action="store_true", help="borra y recrea solo los fixtures eval-"
    )
    args = parser.parse_args()

    config.DB_PATH = Path(args.db)
    conn = db.connect()
    try:
        db.apply_migrations(conn)
        if args.reset:
            _reset(conn)
        data = seed(
            conn,
            supplier_name=args.supplier,
            client_name=args.client_name,
            telegram_id=args.telegram_id,
            telegram_handle=args.telegram_handle,
        )
    finally:
        conn.close()
    _print_manifest(data)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
