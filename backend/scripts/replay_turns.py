"""Replay 20 conversations through the turn pipeline, deterministically.

This is the L3.0 safety net: extraction must not change a single reply. The
script never touches the real database, the local LLM or Telegram:

- a temporary SQLite database is migrated from scratch and seeded;
- the NLU and the composer are scripted, so the deterministic route is what
  gets compared;
- only `process_turn` jobs run, so no outbound message is ever delivered.

Run it twice, once per checkout, and diff the JSON:

    .venv\\Scripts\\python.exe scripts\\replay_turns.py ^
        --backend . --out data/logs/replay_branch.json
    .venv\\Scripts\\python.exe scripts\\replay_turns.py ^
        --backend ..\\..\\main\\backend --out data/logs/replay_main.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import sys
import tempfile
from datetime import datetime, timedelta, timezone


def _past() -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()


# Each step is (customer text, the intent a plausible NLU would return).
CONVERSATIONS: list[tuple[str, list[tuple[str, dict]]]] = [
    (
        "greeting",
        [
            ("Hola", {"intencion": "otro", "confianza": 0.9}),
            ("Buenas tardes", {"intencion": "otro", "confianza": 0.9}),
        ],
    ),
    (
        "price-by-name",
        [
            (
                "¿Cuánto cuesta el Power Bank 10000?",
                {
                    "intencion": "consulta_precio",
                    "producto": "Power Bank 10000",
                    "confianza": 0.95,
                },
            )
        ],
    ),
    (
        "price-by-id",
        [("ID 1", {"intencion": "otro", "confianza": 0.9})],
    ),
    (
        "stock-question",
        [
            (
                "¿Tienes stock del Mouse Inalámbrico?",
                {
                    "intencion": "consulta_stock",
                    "producto": "Mouse Inalámbrico",
                    "confianza": 0.9,
                },
            )
        ],
    ),
    (
        "category-accessories",
        [("¿Qué accesorios tienes?", {"intencion": "otro", "confianza": 0.9})],
    ),
    (
        "category-laptops",
        [("¿Qué laptops tienes?", {"intencion": "otro", "confianza": 0.9})],
    ),
    (
        "out-of-stock",
        [
            (
                "¿Precio del Teclado Mecánico?",
                {
                    "intencion": "consulta_precio",
                    "producto": "Teclado Mecánico",
                    "confianza": 0.9,
                },
            )
        ],
    ),
    (
        "spec-question",
        [
            (
                "¿Qué procesador trae la Laptop Ultraligera?",
                {
                    "intencion": "consulta_dato",
                    "producto": "Laptop Ultraligera",
                    "datos_faltantes": ["procesador"],
                    "confianza": 0.9,
                },
            )
        ],
    ),
    (
        "compare",
        [
            (
                "Compara el Mouse Inalámbrico y el Teclado Mecánico",
                {
                    "intencion": "comparar",
                    "producto": "Mouse Inalámbrico",
                    "confianza": 0.9,
                },
            )
        ],
    ),
    (
        "sale-happy-path",
        [
            (
                "Quiero el Power Bank 10000",
                {
                    "intencion": "comprar",
                    "producto": "Power Bank 10000",
                    "confianza": 0.95,
                },
            ),
            ("Sí", {"intencion": "otro", "confianza": 0.8}),
            ("La Habana", {"intencion": "otro", "confianza": 0.8}),
            ("Calle 1 #2, Vedado", {"intencion": "otro", "confianza": 0.8}),
            ("55512345", {"intencion": "otro", "confianza": 0.8}),
            ("Sí, confirmo", {"intencion": "otro", "confianza": 0.8}),
        ],
    ),
    (
        "sale-declined",
        [
            (
                "Quiero el Mouse Inalámbrico",
                {
                    "intencion": "comprar",
                    "producto": "Mouse Inalámbrico",
                    "confianza": 0.95,
                },
            ),
            ("No, gracias", {"intencion": "otro", "confianza": 0.9}),
        ],
    ),
    (
        "banal-thanks",
        [
            ("Gracias", {"intencion": "otro", "confianza": 0.9}),
            ("Ok, vale", {"intencion": "otro", "confianza": 0.9}),
        ],
    ),
    (
        "banal-sorry",
        [("Disculpa", {"intencion": "otro", "confianza": 0.9})],
    ),
    (
        "zone-coverage",
        [
            (
                "¿Hacen entrega en Marianao?",
                {
                    "intencion": "consulta_dato",
                    "producto": "Power Bank 10000",
                    "confianza": 0.8,
                },
            )
        ],
    ),
    (
        "delivery-date",
        [
            (
                "¿Cuándo llega el Power Bank 10000?",
                {
                    "intencion": "consulta_dato",
                    "producto": "Power Bank 10000",
                    "datos_faltantes": ["tiempo de entrega"],
                    "confianza": 0.85,
                },
            )
        ],
    ),
    (
        "post-sale-status",
        [
            (
                "¿Ya llegó mi pedido?",
                {"intencion": "otro", "confianza": 0.9},
            )
        ],
    ),
    (
        "catalog-after-sale",
        [
            ("¿Qué laptops tienes?", {"intencion": "otro", "confianza": 0.9})
        ],
    ),
    (
        "unknown-product",
        [
            (
                "¿Tienen neveras?",
                {
                    "intencion": "consulta_stock",
                    "producto": "neveras",
                    "confianza": 0.7,
                },
            )
        ],
    ),
    (
        "reference-after-listing",
        [
            ("¿Qué accesorios tienes?", {"intencion": "otro", "confianza": 0.9}),
            ("El segundo me interesa", {"intencion": "otro", "confianza": 0.8}),
            ("El de antes", {"intencion": "otro", "confianza": 0.8}),
        ],
    ),
    (
        "human-escalation",
        [
            (
                "Quiero hablar con una persona",
                {"intencion": "queja", "confianza": 0.9},
            )
        ],
    ),
]


def seed(conn) -> None:
    conn.execute(
        """INSERT INTO suppliers (id, name, delivery_zone)
           VALUES (1, 'TCP Uno', 'La Habana')"""
    )
    conn.execute("INSERT INTO zones (id, name) VALUES (1, 'La Habana')")
    conn.execute("INSERT INTO zones (id, name) VALUES (2, 'Marianao')")
    conn.execute(
        "INSERT INTO supplier_delivery_zones (supplier_id, zone_id) VALUES (1, 1)"
    )
    conn.execute("INSERT INTO categories (id, name) VALUES (1, 'Accesorios')")
    conn.execute("INSERT INTO categories (id, name) VALUES (2, 'Laptops')")
    rows = [
        (1, 1, "Power Bank 10000", "Bateria 10000 mAh", 12.5, 5, "ACTIVE"),
        (2, 1, "Mouse Inalámbrico", "Mouse USB 2.4 GHz", 8, 10, "ACTIVE"),
        (3, 1, "Audífonos Bluetooth", "Audio BT 5.0", 20, 4, "ACTIVE"),
        (4, 1, "Teclado Mecánico", "Teclado USB", 30, 0, "OUT_OF_STOCK"),
        (5, 2, "Laptop Ultraligera", "Ryzen 7 / 16GB RAM / 1TB SSD", 750, 2, "ACTIVE"),
    ]
    for product_id, category_id, name, description, price, stock, status in rows:
        conn.execute(
            """INSERT INTO products
               (id, supplier_id, category_id, name, description, price, currency,
                commission, stock, status, specifications_json)
               VALUES (?, 1, ?, ?, ?, ?, 'USD', 2, ?, ?, '{}')""",
            (product_id, category_id, name, description, price, stock, status),
        )
    conn.commit()


def make_due(conn) -> None:
    conn.execute(
        "UPDATE turns SET due_at = ? WHERE status IN ('OPEN', 'PROCESSING')",
        (_past(),),
    )
    conn.execute(
        """UPDATE jobs SET next_retry_at = ?
           WHERE type = 'process_turn' AND status = 'PENDING'""",
        (_past(),),
    )
    conn.commit()


def claim_job(conn) -> dict | None:
    row = conn.execute(
        """SELECT * FROM jobs
           WHERE type = 'process_turn' AND status = 'PENDING'
             AND (next_retry_at IS NULL OR next_retry_at <= ?)
           ORDER BY id LIMIT 1""",
        (datetime.now(timezone.utc).isoformat(),),
    ).fetchone()
    if row is None:
        return None
    conn.execute(
        "UPDATE jobs SET status = 'IN_PROGRESS', updated_at = ? WHERE id = ?",
        (datetime.now(timezone.utc).isoformat(), row["id"]),
    )
    conn.commit()
    return dict(row)


def run(backend: pathlib.Path, out_path: pathlib.Path) -> None:
    sys.path.insert(0, str(backend.resolve()))

    from cuadra import config, db
    from cuadra.ai import handlers, ingest, llm, queue
    from cuadra.domain import conversations

    tmp = tempfile.TemporaryDirectory()
    config.DB_PATH = pathlib.Path(tmp.name) / "replay.db"
    config.TURN_DEBOUNCE_S = 0.0
    config.TURN_MAX_WAIT_S = 0.0
    config.CUADRA_TRACE_LEVEL = "OFF"
    # Branch-only settings; harmless on main.
    for name, value in (
        ("AGENT_TURN_WATCHDOG_S", 0),
        ("AGENT_TOOLS_MODE", "off"),
        ("AGENT_GROUNDING_MODE", "off"),
    ):
        setattr(config, name, value)

    conn = db.connect()
    try:
        db.apply_migrations(conn)
        seed(conn)
    finally:
        conn.close()
    handlers.register()

    pending: dict = {}

    async def scripted_classify(messages, text, *, conversation_id=None):
        return llm.Intent(**pending)

    async def scripted_compose(
        base_messages, last_message, *, extra="", conversation_id=None
    ):
        return "Te ayudo con eso. ¿Quieres que te cuente más?"

    import unittest.mock as mock

    results = []
    with mock.patch("cuadra.ai.llm.classify_light", new=scripted_classify), mock.patch(
        "cuadra.ai.llm.compose", new=scripted_compose
    ):
        for index, (name, steps) in enumerate(CONVERSATIONS, start=1):
            identifier = f"replay-{index:02d}"
            conversation_id = None
            for text, intent in steps:
                pending.clear()
                pending.update(intent)
                ingest.ingest_incoming(
                    channel="telegram", identifier=identifier, type="text", body=text
                )
                conn = db.connect()
                try:
                    if conversation_id is None:
                        row = conn.execute(
                            """SELECT c.id FROM conversations c
                               JOIN customer_channels ch
                                 ON ch.customer_id = c.customer_id
                               WHERE ch.identifier = ?
                               ORDER BY c.id DESC LIMIT 1""",
                            (identifier,),
                        ).fetchone()
                        conversation_id = row["id"]
                    make_due(conn)
                    job = claim_job(conn)
                finally:
                    conn.close()
                if job is None:
                    raise RuntimeError(f"no process_turn job for {name!r}")
                asyncio.run(queue._process(job))

            conn = db.connect()
            try:
                rows = conn.execute(
                    """SELECT body FROM messages
                       WHERE conversation_id = ? AND direction = 'OUTBOUND'
                       ORDER BY id""",
                    (conversation_id,),
                ).fetchall()
                context = conversations.get_context(conn, conversation_id)
            finally:
                conn.close()
            results.append(
                {
                    "name": name,
                    "replies": [row["body"] for row in rows],
                    "context": context,
                }
            )

    report = {
        "backend": str(backend.resolve()),
        "conversations": results,
        "reply_count": sum(len(row["replies"]) for row in results),
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"{len(results)} conversations, {report['reply_count']} replies -> {out_path}")
    tmp.cleanup()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", default=".", help="checkout's backend/ folder")
    parser.add_argument("--out", required=True, help="JSON report path")
    args = parser.parse_args()
    run(pathlib.Path(args.backend), pathlib.Path(args.out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
