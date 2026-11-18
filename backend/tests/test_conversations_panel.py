"""Conversations panel queries (phase F1).

The panel is read-only except for closing a chat. These tests pin down the
filters, the pagination, the PII masking and the close semantics (cancel OPEN
turns, delete their pending jobs, never touch a turn already PROCESSING).

Run from `backend/`:

    .venv\\Scripts\\python.exe -m unittest discover -s tests -v
"""

import json
import tempfile
import unittest
from pathlib import Path

from cuadra import config, db
from cuadra.domain import conversation_panel
from cuadra.domain.errors import NotFound
from cuadra.domain.pii import mask_phone


class ConversationPanelTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_db_path = config.DB_PATH
        config.DB_PATH = Path(self._tmp.name) / "test.db"
        conn = db.connect()
        try:
            db.apply_migrations(conn)
            self._seed(conn)
        finally:
            conn.close()

    def tearDown(self):
        config.DB_PATH = self._old_db_path
        self._tmp.cleanup()

    # --------------------------------------------------------------- seeding

    def _seed(self, conn) -> None:
        conn.execute("INSERT INTO suppliers (id, name) VALUES (1, 'TCP Uno')")
        conn.execute(
            """INSERT INTO products
               (id, supplier_id, name, price, currency, commission, stock, status)
               VALUES (1, 1, 'Power Bank 10000', 12.5, 'USD', 2, 5, 'ACTIVE')"""
        )

        # 1: normal customer, bot replied last.
        # 2: customer waiting for a TCP answer, open escalation and sale.
        # 3: supplier (TCP), closed, last message inbound.
        conn.execute(
            """INSERT INTO customers (id, name, kind, preferences_json)
               VALUES (1, 'Ana Pérez', 'customer',
                       '{"phone": "+53 55500001", "usual_zone": "Vedado",
                         "usual_address": "Calle 23 #456, Vedado",
                         "notes": "paga tarde"}')"""
        )
        conn.execute(
            """INSERT INTO customers (id, name, kind, preferences_json)
               VALUES (2, 'Luis Gómez', 'customer',
                       '{"phone": "+53 55500002"}')"""
        )
        conn.execute(
            "INSERT INTO customers (id, name, kind) VALUES (3, 'TCP Dos', 'supplier')"
        )
        conn.executemany(
            """INSERT INTO customer_channels (customer_id, channel, identifier)
               VALUES (?, 'telegram', ?)""",
            [(1, "55500001"), (2, "55500002"), (3, "900001")],
        )

        conn.executemany(
            """INSERT INTO conversations
               (id, customer_id, channel, status, context_json,
                created_at, updated_at, closed_at)
               VALUES (?, ?, 'telegram', ?, ?, ?, ?, ?)""",
            [
                (
                    1,
                    1,
                    "ACTIVE",
                    '{"stage": "QUOTE", "phone": "+53 55500001"}',
                    "2026-11-16T08:00:00+00:00",
                    "2026-11-17T10:01:00+00:00",
                    None,
                ),
                (
                    2,
                    2,
                    "WAITING_TCP",
                    '{"stage": "HANDOFF"}',
                    "2026-11-17T09:00:00+00:00",
                    "2026-11-17T11:00:00+00:00",
                    None,
                ),
                (
                    3,
                    3,
                    "CLOSED",
                    "{}",
                    "2026-11-17T12:00:00+00:00",
                    "2026-11-16T09:00:00+00:00",
                    "2026-11-16T09:05:00+00:00",
                ),
            ],
        )

        conn.executemany(
            """INSERT INTO messages
               (id, conversation_id, direction, author, type, body, created_at)
               VALUES (?, ?, ?, ?, 'text', ?, ?)""",
            [
                (
                    1,
                    1,
                    "INBOUND",
                    "customer",
                    "Hola, precio del Power Bank",
                    "2026-11-17T10:00:00+00:00",
                ),
                (
                    2,
                    1,
                    "OUTBOUND",
                    "bot",
                    "Cuesta 12.5 USD",
                    "2026-11-17T10:01:00+00:00",
                ),
                (
                    3,
                    2,
                    "OUTBOUND",
                    "bot",
                    "Le consulto al proveedor",
                    "2026-11-17T10:50:00+00:00",
                ),
                (
                    4,
                    2,
                    "INBOUND",
                    "customer",
                    "¿y la garantía?",
                    "2026-11-17T11:00:00+00:00",
                ),
                (
                    5,
                    3,
                    "INBOUND",
                    "customer",
                    "RECIBIDO",
                    "2026-11-16T09:00:00+00:00",
                ),
            ],
        )

        conn.executemany(
            """INSERT INTO turns
               (id, conversation_id, status, due_at, intent, product_id, stage,
                escalated, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    1,
                    1,
                    "DONE",
                    "2026-11-17T10:00:30+00:00",
                    "consulta_precio",
                    1,
                    "QUOTE",
                    0,
                    "2026-11-17T10:00:00+00:00",
                    "2026-11-17T10:01:00+00:00",
                ),
                (
                    2,
                    2,
                    "DONE",
                    "2026-11-17T10:59:00+00:00",
                    "consulta_dato",
                    1,
                    "HANDOFF",
                    1,
                    "2026-11-17T10:58:00+00:00",
                    "2026-11-17T11:00:00+00:00",
                ),
                (
                    3,
                    1,
                    "OPEN",
                    "2026-11-18T10:00:00+00:00",
                    None,
                    None,
                    None,
                    0,
                    "2026-11-17T10:02:00+00:00",
                    "2026-11-17T10:02:00+00:00",
                ),
                (
                    4,
                    1,
                    "PROCESSING",
                    "2026-11-17T10:03:00+00:00",
                    None,
                    None,
                    None,
                    0,
                    "2026-11-17T10:03:00+00:00",
                    "2026-11-17T10:03:00+00:00",
                ),
                (
                    5,
                    3,
                    "DONE",
                    "2026-11-16T09:00:30+00:00",
                    "otro",
                    None,
                    "DONE",
                    0,
                    "2026-11-16T09:00:00+00:00",
                    "2026-11-16T09:01:00+00:00",
                ),
            ],
        )

        conn.executemany(
            """INSERT INTO jobs (id, type, payload_json, status)
               VALUES (?, 'process_turn', ?, 'PENDING')""",
            [
                (1, json.dumps({"turn_id": 3, "conversation_id": 1})),
                (2, json.dumps({"turn_id": 2, "conversation_id": 2})),
            ],
        )

        conn.execute(
            """INSERT INTO escalations
               (id, conversation_id, product_id, supplier_id, question, status)
               VALUES (1, 2, 1, 1, '¿qué garantía trae?', 'PENDING')"""
        )
        conn.execute(
            """INSERT INTO sales
               (id, order_code, conversation_id, product_id, supplier_id,
                customer_id, quantity, amount, currency, commission_type,
                commission, commission_currency, status)
               VALUES (1, 'V-000009', 2, 1, 1, 2, 2, 25, 'USD', 'FIXED',
                       4, 'USD', 'NEEDS_OPERATOR')"""
        )

        conn.executemany(
            """INSERT INTO conversation_facts
               (conversation_id, fact_key, fact_value)
               VALUES (?, ?, ?)""",
            [(1, "phone", "+53 55500001"), (1, "zona", "Vedado")],
        )
        conn.execute(
            """INSERT INTO traces
               (timestamp, method, output_json, ok, duration_ms, conversation_id)
               VALUES ('2026-11-17T10:01:00+00:00', 'pipeline.handle_turn',
                       '{"stage": "QUOTE"}', 1, 1200, 1)"""
        )
        conn.commit()

    # --------------------------------------------------------------- helpers

    def _list(self, **kwargs) -> dict:
        conn = db.connect()
        try:
            return conversation_panel.list_conversations(conn, **kwargs)
        finally:
            conn.close()

    def _ids(self, **kwargs) -> list[int]:
        return [item["id"] for item in self._list(**kwargs)["items"]]

    def _detail(self, conversation_id: int, **kwargs) -> dict:
        conn = db.connect()
        try:
            return conversation_panel.get_detail(conn, conversation_id, **kwargs)
        finally:
            conn.close()

    def _scalar(self, sql: str, args=()):
        conn = db.connect()
        try:
            return conn.execute(sql, args).fetchone()[0]
        finally:
            conn.close()


class ListTests(ConversationPanelTestCase):
    def test_list_returns_last_message_counts_and_masked_identity(self):
        page = self._list()
        self.assertEqual(page["total"], 3)
        item = next(i for i in page["items"] if i["id"] == 1)

        self.assertEqual(item["customer"]["name"], "Ana Pérez")
        self.assertEqual(item["customer"]["handle"], "555***01")
        self.assertEqual(item["customer"]["phone_masked"], "+53 5***-***01")
        self.assertEqual(item["status"], "ACTIVE")
        self.assertEqual(item["stage"], "QUOTE")
        self.assertEqual(item["last_message"]["direction"], "OUTBOUND")
        self.assertEqual(item["last_message"]["body"], "Cuesta 12.5 USD")
        self.assertEqual(item["messages_count"], 2)
        self.assertEqual(item["turns_count"], 1)  # only DONE counts
        self.assertEqual(item["questions_count"], 1)
        self.assertFalse(item["needs_attention"])

        # The raw phone never leaves the domain layer.
        self.assertNotIn("55500001", json.dumps(page, ensure_ascii=False))

    def test_last_sale_is_summarised(self):
        item = next(i for i in self._list()["items"] if i["id"] == 2)
        self.assertEqual(item["last_sale"]["order_code"], "V-000009")
        self.assertEqual(item["last_sale"]["status"], "NEEDS_OPERATOR")
        self.assertEqual(item["last_sale"]["amount"], 25)
        self.assertEqual(item["last_sale"]["product_name"], "Power Bank 10000")

    def test_stage_falls_back_to_the_last_persisted_turn(self):
        item = next(i for i in self._list()["items"] if i["id"] == 3)
        self.assertEqual(item["stage"], "DONE")

    def test_kind_filter_separates_customers_from_suppliers(self):
        self.assertEqual(sorted(self._ids(kind="customer")), [1, 2])
        self.assertEqual(self._ids(kind="supplier"), [3])

    def test_needs_attention_is_computed_and_a_closed_chat_is_ignored(self):
        attention = self._ids(needs_attention=True)
        self.assertEqual(attention, [2])
        self.assertEqual(sorted(self._ids(needs_attention=False)), [1, 3])

        item = next(i for i in self._list()["items"] if i["id"] == 2)
        self.assertTrue(item["needs_attention"])
        self.assertEqual(item["open_escalations"], 1)

    def test_an_open_supplier_chat_never_needs_the_operator(self):
        conn = db.connect()
        try:
            conn.execute("UPDATE conversations SET status = 'ACTIVE' WHERE id = 3")
            conn.commit()
            page = conversation_panel.list_conversations(conn, needs_attention=True)
        finally:
            conn.close()
        self.assertEqual([i["id"] for i in page["items"]], [2])

    def test_status_channel_identity_and_boolean_filters(self):
        self.assertEqual(self._ids(status="WAITING_TCP"), [2])
        self.assertEqual(self._ids(channel="telegram"), [2, 1, 3])
        self.assertEqual(self._ids(identity="userbot"), [2, 1, 3])
        self.assertEqual(self._ids(identity="bot"), [])
        self.assertEqual(self._ids(stage="HANDOFF"), [2])
        self.assertEqual(self._ids(has_escalation=True), [2])
        self.assertEqual(sorted(self._ids(has_escalation=False)), [1, 3])
        self.assertEqual(self._ids(has_sale=True), [2])
        self.assertEqual(sorted(self._ids(has_sale=False)), [1, 3])

    def test_q_searches_name_phone_order_and_message_body(self):
        self.assertEqual(self._ids(q="Ana"), [1])
        self.assertEqual(self._ids(q="55500002"), [2])
        self.assertEqual(self._ids(q="V-000009"), [2])
        self.assertEqual(self._ids(q="garantía"), [2])
        self.assertEqual(self._ids(q="nada-que-no-exista"), [])

    def test_q_treats_wildcards_literally(self):
        self.assertEqual(self._ids(q="%"), [])
        self.assertEqual(self._ids(q="_"), [])

    def test_date_range_filters_use_the_conversation_update(self):
        self.assertEqual(sorted(self._ids(date_from="2026-11-17")), [1, 2])
        self.assertEqual(self._ids(date_to="2026-11-16"), [3])

    def test_pagination_and_sorting(self):
        first = self._list(limit=2, offset=0)
        self.assertEqual([i["id"] for i in first["items"]], [2, 1])
        self.assertEqual(first["total"], 3)
        self.assertEqual(first["limit"], 2)

        second = self._list(limit=2, offset=2)
        self.assertEqual([i["id"] for i in second["items"]], [3])

        by_created = self._list(sort="created_at")
        self.assertEqual([i["id"] for i in by_created["items"]], [3, 2, 1])

        unknown_sort = self._list(sort="no-existe")
        self.assertEqual(
            [i["id"] for i in unknown_sort["items"]], [2, 1, 3]
        )

    def test_summary_counts_every_group(self):
        conn = db.connect()
        try:
            summary = conversation_panel.summary(conn)
        finally:
            conn.close()
        self.assertEqual(
            summary,
            {
                "total": 3,
                "open": 2,
                "waiting_tcp": 1,
                "needs_attention": 1,
                "with_open_escalation": 1,
                "with_sale": 1,
                "customers": 2,
                "suppliers": 1,
            },
        )


class DetailTests(ConversationPanelTestCase):
    def test_detail_masks_context_and_facts_but_keeps_the_transcript(self):
        detail = self._detail(1)
        conversation = detail["conversation"]
        self.assertEqual(conversation["customer"]["phone_masked"], "+53 5***-***01")
        self.assertEqual(conversation["customer"]["handle"], "555***01")

        customer = detail["customer"]
        self.assertEqual(customer["kind"], "customer")
        self.assertEqual(customer["usual_zone"], "Vedado")
        self.assertEqual(customer["usual_address_masked"], "Calle 23…")
        self.assertEqual(customer["notes"], "paga tarde")

        self.assertEqual(detail["context"]["phone"], mask_phone("+53 55500001"))
        self.assertEqual(detail["context"]["stage"], "QUOTE")

        facts = {f["fact_key"]: f["fact_value"] for f in detail["facts"]}
        self.assertEqual(facts["phone"], mask_phone("+53 55500001"))
        self.assertEqual(facts["zona"], "Vedado")

        # The operator needs the real conversation to understand the case.
        self.assertEqual(
            [m["body"] for m in detail["messages"]],
            ["Hola, precio del Power Bank", "Cuesta 12.5 USD"],
        )

    def test_detail_drops_internal_context_keys(self):
        conn = db.connect()
        try:
            conn.execute(
                "UPDATE conversations SET context_json = ? WHERE id = 1",
                ('{"stage": "QUOTE", "_trace": "secreto", "phone": "+53 55500001"}',),
            )
            conn.commit()
            detail = conversation_panel.get_detail(conn, 1)
        finally:
            conn.close()
        self.assertNotIn("_trace", detail["context"])
        self.assertEqual(detail["context"]["phone"], mask_phone("+53 55500001"))

    def test_detail_includes_commercial_and_execution_history(self):
        detail = self._detail(2)
        self.assertEqual([s["order_code"] for s in detail["sales"]], ["V-000009"])
        self.assertEqual([e["question"] for e in detail["escalations"]], [
            "¿qué garantía trae?"
        ])
        self.assertEqual(len(detail["turns"]), 1)
        self.assertTrue(detail["turns"][0]["escalated"])
        self.assertEqual(detail["turns"][0]["intent"], "consulta_dato")

        traced = self._detail(1)
        self.assertEqual(traced["traces"][0]["method"], "pipeline.handle_turn")
        self.assertTrue(traced["traces"][0]["ok"])

    def test_messages_page_paginates_backwards(self):
        conn = db.connect()
        try:
            page = conversation_panel.messages_page(conn, 1, limit=1)
            self.assertEqual([m["id"] for m in page["items"]], [2])
            self.assertTrue(page["has_more"])

            older = conversation_panel.messages_page(
                conn, 1, limit=1, before_id=2
            )
            self.assertEqual([m["id"] for m in older["items"]], [1])
            self.assertFalse(older["has_more"])

            empty = conversation_panel.messages_page(
                conn, 1, limit=1, before_id=1
            )
            self.assertEqual(empty["items"], [])
        finally:
            conn.close()

    def test_unknown_conversation_raises_not_found(self):
        conn = db.connect()
        try:
            with self.assertRaises(NotFound):
                conversation_panel.get_detail(conn, 999)
            with self.assertRaises(NotFound):
                conversation_panel.messages_page(conn, 999)
            with self.assertRaises(NotFound):
                conversation_panel.close(conn, 999)
        finally:
            conn.close()


class CloseTests(ConversationPanelTestCase):
    def test_close_cancels_open_turns_and_their_pending_jobs(self):
        conn = db.connect()
        try:
            closed = conversation_panel.close(conn, 1)
            statuses = {
                row["id"]: row["status"]
                for row in conn.execute(
                    "SELECT id, status FROM turns WHERE conversation_id = 1"
                )
            }
            jobs = [
                row["id"]
                for row in conn.execute("SELECT id FROM jobs ORDER BY id")
            ]
        finally:
            conn.close()

        self.assertEqual(closed["status"], "CLOSED")
        self.assertIsNotNone(closed["closed_at"])
        self.assertFalse(closed["needs_attention"])
        self.assertEqual(statuses, {1: "DONE", 3: "CANCELLED", 4: "PROCESSING"})
        self.assertEqual(jobs, [2])  # only the other conversation's job remains
        self.assertEqual(
            self._scalar(
                """SELECT COUNT(*) FROM events
                    WHERE type = 'conversation_closed' AND entity_id = 1"""
            ),
            1,
        )

    def test_close_is_idempotent_and_does_not_reclose(self):
        conn = db.connect()
        try:
            first = conversation_panel.close(conn, 1)
            second = conversation_panel.close(conn, 1)
        finally:
            conn.close()

        self.assertEqual(first["closed_at"], second["closed_at"])
        self.assertEqual(
            self._scalar(
                """SELECT COUNT(*) FROM events
                    WHERE type = 'conversation_closed' AND entity_id = 1"""
            ),
            1,
        )

    def test_close_does_not_touch_a_processing_turn(self):
        conn = db.connect()
        try:
            conversation_panel.close(conn, 1)
            processing = conn.execute(
                "SELECT status FROM turns WHERE id = 4"
            ).fetchone()["status"]
        finally:
            conn.close()
        self.assertEqual(processing, "PROCESSING")


if __name__ == "__main__":
    unittest.main()
