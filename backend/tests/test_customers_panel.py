"""Customers panel: metrics, segments, signals, blocking and merge (phase F3).

The panel separates effort (turns, messages, escalations) from outcome (settled
sales and collected commission). These tests pin down the segment rules, the
masked-by-default contract, the audited reveal, the blocking behaviour in
ingest and the manual merge.

Run from `backend/`:

    .venv\\Scripts\\python.exe -m unittest discover -s tests -v
"""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cuadra import config, db
from cuadra.ai import ingest
from cuadra.domain import customer_panel, customers, settings


def _ts(days_ago: int = 0) -> str:
    moment = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return moment.strftime("%Y-%m-%d %H:%M:%S")


class CustomerPanelTestCase(unittest.TestCase):
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
        self.conn = db.connect()

    def tearDown(self):
        self.conn.close()
        config.DB_PATH = self._old_db_path
        self._tmp.cleanup()

    # --------------------------------------------------------------- seeding

    def _seed(self, conn) -> None:
        conn.execute("INSERT INTO categories (id, name) VALUES (1, 'Energía')")
        conn.execute(
            """INSERT INTO suppliers (id, name, telegram_id, delivery_zone)
               VALUES (1, 'TCP Uno', '111222333', 'La Habana')"""
        )
        conn.executemany(
            "INSERT INTO zones (id, name, active) VALUES (?, ?, 1)",
            [(1, "San Miguel"), (2, "Vedado")],
        )
        conn.execute(
            """INSERT INTO supplier_delivery_zones (supplier_id, zone_id)
               VALUES (1, 1)"""
        )
        conn.execute(
            """INSERT INTO products
               (id, supplier_id, category_id, name, description, price,
                currency, commission, commission_currency, commission_type,
                stock, status, specifications_json)
               VALUES (1, 1, 1, 'Power Bank 10000', 'Batería externa',
                       12.5, 'USD', 2, 'USD', 'FIXED', 50, 'ACTIVE', '{}')"""
        )
        conn.executemany(
            """INSERT INTO customers (id, name, kind, preferences_json)
               VALUES (?, ?, ?, '{}')""",
            [
                (1, "Ana Pérez", "customer"),
                (2, "Luis Gómez", "customer"),
                (3, "Pedro Ruiz", "customer"),
                (4, "TCP Dos", "supplier"),
            ],
        )
        conn.executemany(
            """INSERT INTO customer_channels (customer_id, channel, identifier)
               VALUES (?, 'telegram', ?)""",
            [(1, "55500001"), (2, "55500002"), (3, "55500003"), (4, "900001")],
        )
        conn.commit()

    # --------------------------------------------------------------- helpers

    def customer(self, customer_id: int) -> dict:
        return dict(
            self.conn.execute(
                "SELECT * FROM customers WHERE id = ?", (customer_id,)
            ).fetchone()
        )

    def add_conversation(
        self,
        conversation_id: int,
        customer_id: int,
        *,
        status: str = "ACTIVE",
        days_ago: int = 0,
    ) -> None:
        stamp = _ts(days_ago)
        self.conn.execute(
            """INSERT INTO conversations
               (id, customer_id, channel, status, created_at, updated_at)
               VALUES (?, ?, 'telegram', ?, ?, ?)""",
            (conversation_id, customer_id, status, stamp, stamp),
        )
        self.conn.commit()

    def add_message(
        self,
        conversation_id: int,
        direction: str,
        body: str,
        *,
        author: str | None = None,
    ) -> None:
        self.conn.execute(
            """INSERT INTO messages
               (conversation_id, direction, author, type, body, created_at)
               VALUES (?, ?, ?, 'text', ?, ?)""",
            (
                conversation_id,
                direction,
                author or ("customer" if direction == "INBOUND" else "bot"),
                body,
                _ts(),
            ),
        )
        self.conn.commit()

    def add_turn(
        self,
        conversation_id: int,
        *,
        intent: str | None = None,
        outcome: str = "ok",
        product_id: int | None = 1,
        intent_ms: int = 0,
        compose_ms: int = 0,
    ) -> int:
        cur = self.conn.execute(
            """INSERT INTO turns
               (conversation_id, status, due_at, intent, product_id, outcome,
                intent_ms, compose_ms, created_at, finished_at)
               VALUES (?, 'DONE', ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                conversation_id,
                _ts(),
                intent,
                product_id,
                outcome,
                intent_ms,
                compose_ms,
                _ts(),
                _ts(),
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def add_sale(
        self,
        customer_id: int,
        *,
        status: str = "SETTLED",
        amount: float = 100.0,
        currency: str = "USD",
        conversation_id: int | None = None,
        days_ago: int = 0,
        with_ledger: bool = False,
        commission: float = 10.0,
    ) -> int:
        cur = self.conn.execute(
            """INSERT INTO sales
               (order_code, conversation_id, product_id, supplier_id,
                customer_id, quantity, amount, currency, commission_type,
                commission, commission_currency, recipient_name,
                delivery_zone, delivery_zone_id, delivery_address,
                customer_phone, status, created_at, updated_at)
               VALUES (NULL, ?, 1, 1, ?, 1, ?, ?, 'FIXED', 2, 'USD',
                       'Ana Pérez', 'San Miguel', 1, 'Calle 23 #456, Vedado',
                       '71234567', ?, ?, ?)""",
            (
                conversation_id,
                customer_id,
                amount,
                currency,
                status,
                _ts(days_ago),
                _ts(days_ago),
            ),
        )
        sale_id = int(cur.lastrowid)
        self.conn.execute(
            "UPDATE sales SET order_code = 'V-' || printf('%06d', id) WHERE id = ?",
            (sale_id,),
        )
        if with_ledger:
            self.conn.execute(
                """INSERT INTO commission_ledger
                   (sale_id, product_id, supplier_id, product_name, sale_total,
                    currency, commission_type, commission_rate,
                    commission_earned, commission_currency, status)
                   VALUES (?, 1, 1, 'Power Bank 10000', ?, ?, 'FIXED', 2,
                           ?, 'USD', 'COLLECTED')""",
                (sale_id, amount, currency, commission),
            )
        self.conn.commit()
        return sale_id

    def add_signal(
        self,
        customer_id: int,
        kind: str,
        *,
        severity: str = "warning",
        status: str = "OPEN",
        count: int = 1,
    ) -> int:
        cur = self.conn.execute(
            """INSERT INTO customer_signals
               (customer_id, kind, severity, status, count, evidence_json)
               VALUES (?, ?, ?, ?, ?, '{}')""",
            (customer_id, kind, severity, status, count),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def set_last_seen(self, customer_id: int, days_ago: int) -> None:
        stamp = _ts(days_ago)
        self.conn.execute(
            """UPDATE customers SET first_seen_at = ?, last_seen_at = ?
                WHERE id = ?""",
            (stamp, stamp, customer_id),
        )
        self.conn.commit()

    def list_items(self, **kwargs) -> dict:
        return customer_panel.list_customers(self.conn, **kwargs)


class ListTests(CustomerPanelTestCase):
    def test_default_list_only_returns_customers_and_masks_phone(self):
        customers.record_contact(
            self.conn, 1, "phone", "71234567", source="fact"
        )
        result = self.list_items()
        self.assertEqual(result["total"], 3)
        self.assertEqual({item["id"] for item in result["items"]}, {1, 2, 3})
        ana = next(item for item in result["items"] if item["id"] == 1)
        self.assertEqual(ana["phone_masked"], "+53 7***-***67")
        self.assertNotIn("71234567", json.dumps(result))

    def test_suppliers_are_excluded_by_default(self):
        result = self.list_items(kind="supplier")
        self.assertEqual([item["id"] for item in result["items"]], [4])

    def test_search_by_phone_returns_the_customer_masked(self):
        customers.record_contact(self.conn, 1, "phone", "71234567", source="fact")
        result = self.list_items(q="7123")
        self.assertEqual([item["id"] for item in result["items"]], [1])
        self.assertNotIn("71234567", json.dumps(result))

    def test_filters_by_tag_zone_and_sales(self):
        customer_panel.add_tag(self.conn, 1, "mayorista")
        self.conn.execute(
            "UPDATE customers SET usual_zone_id = 2 WHERE id = 2"
        )
        self.conn.commit()
        self.add_sale(2, status="SETTLED")

        self.assertEqual(
            [item["id"] for item in self.list_items(tag="mayorista")["items"]],
            [1],
        )
        self.assertEqual(
            [item["id"] for item in self.list_items(zone_id=2)["items"]],
            [2],
        )
        self.assertEqual(
            [item["id"] for item in self.list_items(has_sales=True)["items"]],
            [2],
        )

    def test_pagination_keeps_the_total(self):
        for customer_id in (1, 2, 3):
            self.set_last_seen(customer_id, customer_id)
        page = self.list_items(limit=1, offset=1)
        self.assertEqual(page["total"], 3)
        self.assertEqual(len(page["items"]), 1)
        self.assertEqual(page["limit"], 1)
        self.assertEqual(page["offset"], 1)


class SegmentTests(CustomerPanelTestCase):
    def segment_of(self, customer_id: int) -> str:
        detail = customer_panel.get_detail(self.conn, customer_id)
        return detail["customer"]["segment"]

    def test_one_settled_sale_is_a_buyer_and_three_recent_is_vip(self):
        self.add_sale(1, status="SETTLED")
        self.assertEqual(self.segment_of(1), "comprador")

        for _ in range(2):
            self.add_sale(1, status="SETTLED")
        self.assertEqual(self.segment_of(1), "vip")

    def test_many_turns_without_sales_is_low_value_not_risk(self):
        self.add_conversation(10, 3)
        self.add_conversation(11, 3)
        for _ in range(10):
            self.add_turn(10, intent="consulta_precio")
            self.add_turn(11, intent="consulta_precio")
        self.set_last_seen(3, 1)
        detail = customer_panel.get_detail(self.conn, 3)
        self.assertEqual(detail["customer"]["segment"], "poco_rentable")
        self.assertEqual(detail["customer"]["risk"], "low")

    def test_recent_question_without_sale_is_potential(self):
        self.add_conversation(10, 1)
        self.add_turn(10, intent="consulta_precio")
        self.set_last_seen(1, 1)
        self.assertEqual(self.segment_of(1), "potencial")

    def test_no_activity_in_90_days_is_cold(self):
        self.set_last_seen(1, 200)
        self.assertEqual(self.segment_of(1), "frio")

    def test_new_customer_without_signals_is_new(self):
        self.set_last_seen(1, 1)
        self.assertEqual(self.segment_of(1), "nuevo")

    def test_blocked_status_wins_over_every_other_rule(self):
        self.add_sale(1, status="SETTLED")
        customer_panel.block(self.conn, 1)
        self.assertEqual(self.segment_of(1), "bloqueado")

    def test_open_signal_does_not_change_the_segment_until_confirmed(self):
        settings.set(self.conn, "customers.risk.high", "40")
        self.set_last_seen(1, 1)
        self.add_sale(1, status="SETTLED")
        signal_id = self.add_signal(1, "abuso_verbal", severity="critical")
        self.assertNotEqual(self.segment_of(1), "riesgo")

        customer_panel.confirm_signal(self.conn, 1, signal_id)
        self.assertEqual(self.segment_of(1), "riesgo")

    def test_dismissed_signal_does_not_add_risk(self):
        settings.set(self.conn, "customers.risk.high", "40")
        self.add_sale(1, status="SETTLED")
        signal_id = self.add_signal(1, "abuso_verbal", severity="critical")
        customer_panel.dismiss_signal(self.conn, 1, signal_id)
        detail = customer_panel.get_detail(self.conn, 1)
        self.assertEqual(detail["metrics"]["risk_score"], 0)
        self.assertNotEqual(detail["customer"]["segment"], "riesgo")


class DetailTests(CustomerPanelTestCase):
    def test_detail_returns_masked_contacts_and_history(self):
        customers.record_contact(self.conn, 1, "phone", "71234567", source="sale")
        customers.record_contact(
            self.conn, 1, "email", "ana@example.com", source="operator"
        )
        customer_panel.add_tag(self.conn, 1, "mayorista")
        customer_panel.add_note(self.conn, 1, "paga puntual")
        self.add_conversation(10, 1)
        self.add_message(10, "INBOUND", "Hola")
        sale_id = self.add_sale(1, status="SETTLED", with_ledger=True)

        detail = customer_panel.get_detail(self.conn, 1)
        self.assertEqual(detail["customer"]["segment"], "comprador")
        self.assertEqual(detail["customer"]["phone_masked"], "+53 7***-***67")
        self.assertEqual(detail["tags"], ["mayorista"])
        self.assertEqual(detail["notes"][0]["body"], "paga puntual")
        self.assertEqual(len(detail["contacts"]), 2)
        self.assertNotIn("71234567", json.dumps(detail["contacts"]))
        self.assertIn("a***@example.com", json.dumps(detail["contacts"]))
        self.assertEqual(detail["sales"][0]["id"], sale_id)
        self.assertEqual(detail["conversations"][0]["id"], 10)
        self.assertEqual(detail["metrics"]["sales_settled"], 1)
        self.assertEqual(detail["metrics"]["commission_usd"], 10.0)

    def test_effort_and_outcome_are_reported_separately(self):
        self.add_conversation(10, 1)
        self.add_turn(10, intent="consulta_precio", intent_ms=2000, compose_ms=1000)
        self.add_message(10, "INBOUND", "precio")
        self.add_sale(1, status="SETTLED", amount=250, with_ledger=True)

        metrics = customer_panel.get_detail(self.conn, 1)["metrics"]
        self.assertEqual(metrics["turns_total"], 1)
        self.assertEqual(metrics["compute_s"], 3.0)
        self.assertEqual(metrics["effort_score"], 1.5)
        self.assertEqual(metrics["revenue_usd"], 250.0)
        self.assertEqual(metrics["commission_usd"], 10.0)
        self.assertEqual(metrics["revenue_cup"], 0.0)

    def test_reveal_is_audited_and_returns_full_values(self):
        customers.record_contact(self.conn, 1, "phone", "71234567", source="sale")
        payload = customer_panel.reveal_contact(self.conn, 1)
        self.assertEqual(payload["phone"], "+53 71234567")
        self.assertEqual(payload["contacts"][0]["normalized"], "+53 71234567")

        event = self.conn.execute(
            """SELECT * FROM events WHERE type = 'customer_pii_revealed'
                AND entity_id = 1 ORDER BY id DESC LIMIT 1"""
        ).fetchone()
        self.assertIsNotNone(event)
        self.assertIn("phone", event["details_json"])

    def test_tags_and_notes_can_be_added_and_removed(self):
        customer_panel.add_tag(self.conn, 1, "vip")
        self.assertEqual(
            customer_panel.get_detail(self.conn, 1)["tags"], ["vip"]
        )
        customer_panel.remove_tag(self.conn, 1, "vip")
        self.assertEqual(customer_panel.get_detail(self.conn, 1)["tags"], [])

        customer_panel.add_note(self.conn, 1, "primera nota")
        notes = customer_panel.get_detail(self.conn, 1)["notes"]
        self.assertEqual([note["body"] for note in notes], ["primera nota"])

    def test_patch_updates_profile_and_zone(self):
        detail = customer_panel.update_profile(
            self.conn,
            1,
            {
                "name": "Ana P.",
                "display_name": "Ana de la tienda",
                "usual_zone_id": 2,
                "phone": "71234567",
                "notes": "prefiere entregas por la tarde",
            },
        )
        self.assertEqual(detail["customer"]["name"], "Ana P.")
        self.assertEqual(detail["customer"]["display_name"], "Ana de la tienda")
        self.assertEqual(detail["customer"]["zone"], "Vedado")
        self.assertEqual(detail["customer"]["phone_masked"], "+53 7***-***67")
        self.assertEqual(detail["notes"][0]["body"], "prefiere entregas por la tarde")


class SummaryTests(CustomerPanelTestCase):
    def test_summary_counts_only_customers(self):
        self.set_last_seen(1, 1)
        self.set_last_seen(2, 10)
        self.add_sale(1, status="SETTLED", amount=100, with_ledger=True)
        customer_panel.block(self.conn, 3)

        summary = customer_panel.summary(self.conn)
        self.assertEqual(summary["total"], 3)
        self.assertEqual(summary["buyers"], 1)
        self.assertEqual(summary["new_7d"], 1)
        self.assertEqual(summary["active_7d"], 1)
        self.assertEqual(summary["blocked"], 1)
        self.assertEqual(summary["revenue_usd"], 100.0)
        self.assertEqual(summary["commission_usd"], 10.0)


class BlockingTests(CustomerPanelTestCase):
    def test_block_and_unblock_are_reversible_and_audited(self):
        customer_panel.block(self.conn, 1)
        self.assertEqual(self.customer(1)["status"], "BLOCKED")
        customer_panel.unblock(self.conn, 1)
        self.assertEqual(self.customer(1)["status"], "ACTIVE")

        events = self.conn.execute(
            """SELECT type FROM events WHERE entity_id = 1
                AND type IN ('customer_blocked', 'customer_unblocked')
                ORDER BY id"""
        ).fetchall()
        self.assertEqual(
            [row["type"] for row in events],
            ["customer_blocked", "customer_unblocked"],
        )

    def test_blocked_inbound_is_stored_without_turn_or_job(self):
        self.add_conversation(10, 1)
        customer_panel.block(self.conn, 1)
        messages_before = self.conn.execute(
            "SELECT COUNT(*) FROM messages"
        ).fetchone()[0]
        turns_before = self.conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
        jobs_before = self.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]

        result = ingest.ingest_incoming(
            channel="telegram", identifier="55500001", body="Hola?"
        )

        self.assertTrue(result["blocked"])
        self.assertIsNone(result["turn_id"])
        self.assertIsNone(result["job_id"])
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
            messages_before + 1,
        )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0],
            turns_before,
        )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0],
            jobs_before,
        )
        event = self.conn.execute(
            "SELECT * FROM events WHERE type = 'blocked_inbound'"
        ).fetchone()
        self.assertIsNotNone(event)
        self.assertEqual(event["entity_id"], 1)


class ScanTests(CustomerPanelTestCase):
    def test_repeated_cancellations_raise_a_signal_once(self):
        self.add_sale(1, status="CANCELLED")
        self.add_sale(1, status="CANCELLED")

        first = customer_panel.scan_signals(self.conn)
        self.assertGreaterEqual(first["signals_created"], 1)
        signals = customer_panel.get_detail(self.conn, 1)["signals"]
        repeated = [s for s in signals if s["kind"] == "cancelaciones_repetidas"]
        self.assertEqual(len(repeated), 1)
        self.assertEqual(repeated[0]["count"], 2)

        second = customer_panel.scan_signals(self.conn)
        self.assertEqual(second["signals_created"], 0)
        self.assertEqual(
            len(customer_panel.get_detail(self.conn, 1)["signals"]), 1
        )

    def test_abusive_inbound_raises_a_critical_signal(self):
        self.add_conversation(10, 1)
        self.add_message(10, "INBOUND", "Eres un idiota, no me escribas más")
        customer_panel.scan_signals(self.conn)
        signals = customer_panel.get_detail(self.conn, 1)["signals"]
        abuse = [s for s in signals if s["kind"] == "abuso_verbal"]
        self.assertEqual(len(abuse), 1)
        self.assertEqual(abuse[0]["severity"], "critical")


class MergeTests(CustomerPanelTestCase):
    def test_preview_reports_what_moves_and_conflicts(self):
        self.add_conversation(10, 1)
        self.add_sale(1, status="SETTLED")
        customer_panel.add_tag(self.conn, 1, "vip")
        customer_panel.add_tag(self.conn, 2, "vip")
        self.add_signal(1, "flood")
        self.add_signal(2, "flood")

        preview = customer_panel.merge_preview(self.conn, 1, 2)
        self.assertEqual(preview["move"]["conversations"], 1)
        self.assertEqual(preview["move"]["sales"], 1)
        self.assertEqual(preview["move"]["tags"], 1)
        self.assertEqual(preview["conflicts"]["tags"], ["vip"])
        self.assertEqual(preview["conflicts"]["open_signals"], ["flood"])
        self.assertTrue(preview["irreversible"])

    def test_merge_moves_history_and_marks_the_old_customer(self):
        self.add_conversation(10, 1)
        self.add_conversation(11, 2)
        self.add_sale(1, status="SETTLED")
        customer_panel.add_tag(self.conn, 1, "vip")
        customer_panel.add_tag(self.conn, 2, "vip")
        customer_panel.add_note(self.conn, 1, "nota del origen")
        signal_from = self.add_signal(1, "flood", count=1)
        signal_into = self.add_signal(2, "flood", count=2)

        detail = customer_panel.merge(self.conn, 1, 2, reason="mismo cliente")

        self.assertEqual(self.customer(1)["status"], "MERGED")
        self.assertEqual(self.customer(1)["merged_into_id"], 2)
        self.assertEqual(
            self.conn.execute(
                "SELECT customer_id FROM conversations WHERE id = 10"
            ).fetchone()[0],
            2,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT customer_id FROM sales WHERE customer_id = 2"
            ).fetchone()[0],
            2,
        )
        self.assertEqual(detail["tags"], ["vip"])
        self.assertEqual(detail["notes"][0]["body"], "nota del origen")
        open_signals = [
            signal for signal in detail["signals"] if signal["status"] == "OPEN"
        ]
        self.assertEqual(len(open_signals), 1)
        self.assertEqual(open_signals[0]["count"], 3)
        self.assertNotEqual(signal_from, signal_into)

        merge_row = self.conn.execute(
            "SELECT * FROM customer_merges WHERE from_customer_id = 1"
        ).fetchone()
        self.assertIsNotNone(merge_row)
        self.assertEqual(merge_row["into_customer_id"], 2)
        self.assertEqual(merge_row["reason"], "mismo cliente")

    def test_merge_rejects_itself_and_already_merged_customers(self):
        with self.assertRaises(Exception):
            customer_panel.merge_preview(self.conn, 1, 1)

        customer_panel.merge(self.conn, 1, 2)
        with self.assertRaises(Exception):
            customer_panel.merge(self.conn, 1, 3)


if __name__ == "__main__":
    unittest.main()
