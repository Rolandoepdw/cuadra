"""Customer maintenance jobs: signal scan, retention and General KPIs (F4).

These tests pin down three policies that must not drift:

- the signal scan only creates OPEN signals (risk changes only after an
  operator confirms);
- retention purges message bodies after the configured window while keeping
  the customer profile, sales and commissions;
- the General panel KPIs stay consistent with the Clients panel.
"""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cuadra import config, db
from cuadra.domain import customer_panel, settings
from cuadra.scheduler import maintenance


def _ts(days_ago: int = 0) -> str:
    moment = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return moment.strftime("%Y-%m-%d %H:%M:%S")


class CustomerMaintenanceTestCase(unittest.TestCase):
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
        conn.execute(
            """INSERT INTO products
               (id, supplier_id, category_id, name, description, price,
                currency, commission, commission_currency, commission_type,
                stock, status, specifications_json)
               VALUES (1, 1, 1, 'Power Bank 10000', 'Batería externa',
                       12.5, 'USD', 2, 'USD', 'FIXED', 50, 'ACTIVE', '{}')"""
        )
        conn.executemany(
            """INSERT INTO customers (id, name, kind, status, last_seen_at)
               VALUES (?, ?, ?, ?, ?)""",
            [
                (1, "Ana Pérez", "customer", "ACTIVE", _ts(1)),
                (2, "Luis Gómez", "customer", "ACTIVE", _ts(2)),
                (3, "Pedro Ruiz", "customer", "ACTIVE", _ts(30)),
                (4, "TCP Dos", "supplier", "ACTIVE", _ts(1)),
            ],
        )
        conn.execute(
            """INSERT INTO conversations
               (id, customer_id, channel, status, created_at, updated_at)
               VALUES (1, 1, 'telegram', 'ACTIVE', ?, ?)""",
            (_ts(2), _ts(1)),
        )
        conn.execute(
            """INSERT INTO conversations
               (id, customer_id, channel, status, created_at, updated_at)
               VALUES (2, 1, 'telegram', 'ACTIVE', ?, ?)""",
            (_ts(2), _ts(1)),
        )
        conn.commit()

    # --------------------------------------------------------------- helpers

    def add_message(self, conversation_id: int, body: str, *, days_ago: int) -> int:
        cur = self.conn.execute(
            """INSERT INTO messages
               (conversation_id, direction, author, type, body, created_at)
               VALUES (?, 'INBOUND', 'customer', 'text', ?, ?)""",
            (conversation_id, body, _ts(days_ago)),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def add_turn(self, conversation_id: int) -> None:
        self.conn.execute(
            """INSERT INTO turns
               (conversation_id, status, due_at, intent, product_id, outcome,
                created_at, finished_at)
               VALUES (?, 'DONE', ?, 'consulta_precio', 1, 'ok', ?, ?)""",
            (conversation_id, _ts(1), _ts(1), _ts(1)),
        )

    def add_sale(self, customer_id: int, *, status: str = "SETTLED") -> int:
        cur = self.conn.execute(
            """INSERT INTO sales
               (order_code, conversation_id, product_id, supplier_id,
                customer_id, quantity, amount, currency, commission_type,
                commission, commission_currency, recipient_name,
                delivery_zone, delivery_zone_id, delivery_address,
                customer_phone, status, created_at, updated_at)
               VALUES (NULL, 1, 1, 1, ?, 1, 100.0, 'USD', 'FIXED', 2, 'USD',
                       'Ana Pérez', 'La Habana', NULL, 'Calle 23', '71234567',
                       ?, ?, ?)""",
            (customer_id, status, _ts(1), _ts(1)),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def add_signal(self, customer_id: int, kind: str, *, status: str) -> None:
        self.conn.execute(
            """INSERT INTO customer_signals
               (customer_id, kind, severity, status, count, evidence_json)
               VALUES (?, ?, 'critical', ?, 1, '{}')""",
            (customer_id, kind, status),
        )
        self.conn.commit()

    def events_of(self, event_type: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM events WHERE type = ? ORDER BY id", (event_type,)
        ).fetchall()
        return [dict(row) for row in rows]

    # -------------------------------------------------------------- retention

    def test_retention_defaults_to_six_months_and_is_seeded(self):
        self.assertEqual(settings.message_retention_days(self.conn), 180)
        row = self.conn.execute(
            "SELECT value FROM settings WHERE key = 'customers.message_retention_days'"
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["value"], "180")

    def test_retention_purges_old_messages_and_keeps_recent_ones(self):
        old_id = self.add_message(1, "mensaje viejo", days_ago=200)
        recent_id = self.add_message(1, "mensaje reciente", days_ago=2)

        maintenance.purge_old_messages()

        ids = {
            row["id"]
            for row in self.conn.execute("SELECT id FROM messages").fetchall()
        }
        self.assertNotIn(old_id, ids)
        self.assertIn(recent_id, ids)
        events = self.events_of("messages_purged")
        self.assertEqual(len(events), 1)

    def test_retention_detaches_foreign_keys_before_deleting(self):
        old_id = self.add_message(1, "mensaje viejo", days_ago=200)
        turn = self.conn.execute(
            """INSERT INTO turns
               (conversation_id, status, due_at, reply_message_id)
               VALUES (1, 'DONE', ?, ?)""",
            (_ts(200), old_id),
        ).lastrowid
        self.conn.execute(
            """INSERT INTO conversation_facts
               (conversation_id, fact_key, fact_value, source_message_id)
               VALUES (1, 'zona', 'Vedado', ?)""",
            (old_id,),
        )
        mutation = self.conn.execute(
            """INSERT INTO catalog_mutation_log
               (product_id, supplier_id, source_message_id, mutation_type,
                previous_state_json, new_state_json, confidence,
                correlation_method, status, applied_by)
               VALUES (1, 1, ?, 'STOCK', '{}', '{}', 1.0, 'manual',
                       'APPLIED', 'operator')""",
            (old_id,),
        ).lastrowid
        self.conn.commit()

        maintenance.purge_old_messages()

        self.assertIsNone(
            self.conn.execute(
                "SELECT reply_message_id FROM turns WHERE id = ?", (turn,)
            ).fetchone()["reply_message_id"]
        )
        self.assertIsNone(
            self.conn.execute(
                "SELECT source_message_id FROM conversation_facts WHERE id = 1"
            ).fetchone()["source_message_id"]
        )
        self.assertIsNone(
            self.conn.execute(
                "SELECT source_message_id FROM catalog_mutation_log WHERE id = ?",
                (mutation,),
            ).fetchone()["source_message_id"]
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM messages WHERE id = ?", (old_id,)
            ).fetchone()[0],
            0,
        )

    def test_retention_setting_rejects_out_of_range_values(self):
        with self.assertRaises(ValueError):
            settings.set_message_retention_days(self.conn, 0)
        with self.assertRaises(ValueError):
            settings.set_message_retention_days(self.conn, 3651)

    def test_settings_endpoint_returns_and_updates_the_policy(self):
        current = customer_panel.get_settings(self.conn)
        self.assertEqual(current["message_retention_days"], 180)
        self.assertEqual(current["message_retention_default"], 180)

        updated = customer_panel.update_settings(
            self.conn, {"message_retention_days": 90}
        )
        self.assertEqual(updated["message_retention_days"], 90)
        self.assertEqual(len(self.events_of("setting_changed")), 1)

    # ------------------------------------------------------------ signal scan

    def test_signal_scan_creates_open_signal_that_does_not_raise_risk(self):
        for _ in range(20):
            self.add_turn(1)
        for _ in range(5):
            self.add_turn(2)
        self.conn.commit()

        maintenance.scan_customer_signals()

        signal = self.conn.execute(
            """SELECT id, status FROM customer_signals
                WHERE customer_id = 1 AND kind = 'muchas_preguntas_sin_compra'"""
        ).fetchone()
        self.assertIsNotNone(signal)
        self.assertEqual(signal["status"], "OPEN")
        self.assertEqual(customer_panel.get_detail(self.conn, 1)["metrics"]["risk_score"], 0)

        customer_panel.confirm_signal(self.conn, 1, int(signal["id"]))

        self.assertEqual(
            customer_panel.get_detail(self.conn, 1)["metrics"]["risk_score"], 10
        )

    # ------------------------------------------------------------- general KPIs

    def test_general_kpis_count_active_buyers_and_confirmed_risk(self):
        self.add_sale(1)
        self.add_signal(1, "abuso_verbal", status="CONFIRMED")
        self.add_signal(1, "pedido_falso", status="CONFIRMED")

        kpis = customer_panel.general_kpis(self.conn)

        self.assertEqual(kpis["customers_total"], 3)
        self.assertEqual(kpis["customers_active_7d"], 2)
        self.assertAlmostEqual(kpis["customers_conversion"], 1 / 3, places=4)
        self.assertEqual(kpis["customers_at_risk"], 1)


if __name__ == "__main__":
    unittest.main()
