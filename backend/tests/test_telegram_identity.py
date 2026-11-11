"""Telegram identity: numeric ids are the key, handles are a mutable label.

Customers are already keyed by their numeric Telegram id. Suppliers are stored
by `@usuario`, so these tests cover the backfill paths that learn and protect the
numeric id (`suppliers.telegram_peer_id`).

Everything runs against a throwaway SQLite database and never calls Telegram or a
model server. Run from `backend/`:

    .venv\\Scripts\\python.exe -m unittest discover -s tests -v
"""

import tempfile
import unittest
from pathlib import Path

from cuadra import config, db
from cuadra.ai import ingest
from cuadra.domain import catalog


class TelegramIdentityTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_db_path = config.DB_PATH
        config.DB_PATH = Path(self._tmp.name) / "test.db"
        conn = db.connect()
        try:
            db.apply_migrations(conn)
            conn.execute(
                """INSERT INTO suppliers (id, name, telegram_id)
                   VALUES (1, 'Claudia', 'claudia_tcp')"""
            )
            conn.execute(
                """INSERT INTO suppliers (id, name, telegram_id, telegram_peer_id)
                   VALUES (2, 'Roger', '@roger_tcp', '222333444')"""
            )
            conn.commit()
        finally:
            conn.close()

    def tearDown(self):
        config.DB_PATH = self._old_db_path
        self._tmp.cleanup()

    def _supplier(self, supplier_id: int) -> dict:
        conn = db.connect()
        try:
            return catalog.get_supplier(conn, supplier_id)
        finally:
            conn.close()

    def _events(self, event_type: str) -> list[dict]:
        conn = db.connect()
        try:
            return [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM events WHERE type = ? ORDER BY id", (event_type,)
                )
            ]
        finally:
            conn.close()

    # ------------------------------------------------------------ normalizing

    def test_handle_accepts_bare_handle_at_handle_and_links(self):
        self.assertEqual(catalog.normalize_telegram_handle("claudia_tcp"), "@claudia_tcp")
        self.assertEqual(catalog.normalize_telegram_handle("@claudia_tcp"), "@claudia_tcp")
        self.assertEqual(
            catalog.normalize_telegram_handle("https://t.me/claudia_tcp"), "@claudia_tcp"
        )
        self.assertIsNone(catalog.normalize_telegram_handle("   "))
        self.assertEqual(catalog.normalize_telegram_handle("123456789"), "123456789")

    def test_peer_id_must_be_numeric(self):
        conn = db.connect()
        try:
            with self.assertRaises(catalog.Invalid):
                catalog.update_supplier(conn, 1, {"telegram_peer_id": "@claudia_tcp"})
        finally:
            conn.close()
        self.assertIsNone(self._supplier(1)["telegram_peer_id"])

    def test_create_supplier_normalizes_the_handle(self):
        conn = db.connect()
        try:
            supplier = catalog.create_supplier(
                conn,
                {
                    "name": "Nuevo",
                    "telegram_id": "https://t.me/Nuevo_TCP",
                    "telegram_peer_id": "999888777",
                },
            )
        finally:
            conn.close()
        self.assertEqual(supplier["telegram_id"], "@Nuevo_TCP")
        self.assertEqual(supplier["telegram_peer_id"], "999888777")

    # ------------------------------------------------------- passive learning

    def test_inbound_from_a_known_handle_learns_the_numeric_id(self):
        result = ingest.ingest_incoming(
            channel="telegram",
            identifier="555666777",
            handle="claudia_tcp",
            body="hola, tengo el pedido",
            external_id="tg-learn-1",
        )

        self.assertEqual(result["source"], "supplier")
        self.assertEqual(result["supplier_id"], 1)
        self.assertEqual(self._supplier(1)["telegram_peer_id"], "555666777")
        events = self._events("supplier_peer_resolved")
        self.assertEqual(len(events), 1)
        self.assertIn('"source": "inbound"', events[0]["details_json"])

    def test_next_message_matches_by_numeric_id_even_without_the_handle(self):
        ingest.ingest_incoming(
            channel="telegram",
            identifier="555666777",
            handle="claudia_tcp",
            body="primero",
            external_id="tg-learn-2",
        )

        result = ingest.ingest_incoming(
            channel="telegram",
            identifier="555666777",
            body="segundo",
            external_id="tg-learn-3",
        )

        self.assertEqual(result["source"], "supplier")
        self.assertEqual(result["supplier_id"], 1)

    def test_learned_peer_takes_priority_when_the_handle_is_renamed(self):
        # The supplier renamed themselves: the handle no longer matches, but the
        # numeric id still does.
        result = ingest.ingest_incoming(
            channel="telegram",
            identifier="222333444",
            handle="roger_renamed",
            body="sigo siendo yo",
            external_id="tg-rename-1",
        )

        self.assertEqual(result["source"], "supplier")
        self.assertEqual(result["supplier_id"], 2)

    def test_a_handle_taken_over_by_someone_else_is_not_the_supplier(self):
        result = ingest.ingest_incoming(
            channel="telegram",
            identifier="999000111",
            handle="roger_tcp",
            body="hola, soy nuevo aqui",
            external_id="tg-takeover-1",
        )

        self.assertEqual(result["source"], "customer")
        self.assertEqual(self._supplier(2)["telegram_peer_id"], "222333444")
        events = self._events("supplier_peer_mismatch")
        self.assertEqual(len(events), 1)
        self.assertIn('"actual_peer_id": "999000111"', events[0]["details_json"])

    def test_repeated_mismatch_is_reported_once(self):
        for index in range(3):
            ingest.ingest_incoming(
                channel="telegram",
                identifier="999000111",
                handle="roger_tcp",
                body=f"spam {index}",
                external_id=f"tg-takeover-{index}",
            )

        self.assertEqual(len(self._events("supplier_peer_mismatch")), 1)

    def test_unknown_handle_stays_a_customer(self):
        result = ingest.ingest_incoming(
            channel="telegram",
            identifier="123123123",
            handle="cliente_nuevo",
            body="busco un power bank",
            external_id="tg-client-1",
        )

        self.assertEqual(result["source"], "customer")
        self.assertIsNone(self._supplier(1)["telegram_peer_id"])

    def test_group_chat_id_is_never_learned_as_a_supplier_peer(self):
        # The bot can be added to a group: its negative chat id is not the TCP's
        # personal id, and escalations must not go to the whole group.
        result = ingest.ingest_incoming(
            channel="telegram",
            identifier="-1001234567890",
            handle="claudia_tcp",
            body="hola desde el grupo",
            external_id="tg-group-1",
        )

        self.assertEqual(result["source"], "customer")
        self.assertIsNone(self._supplier(1)["telegram_peer_id"])
        self.assertEqual(self._events("supplier_peer_resolved"), [])

    # ------------------------------------------------------------- visibility

    def test_missing_peer_list_only_includes_handles_without_a_numeric_id(self):
        conn = db.connect()
        try:
            self.assertEqual(
                [s["id"] for s in catalog.list_suppliers_missing_peer(conn)], [1]
            )
        finally:
            conn.close()

        ingest.ingest_incoming(
            channel="telegram",
            identifier="555666777",
            handle="claudia_tcp",
            body="hola",
            external_id="tg-missing-1",
        )

        conn = db.connect()
        try:
            self.assertEqual(catalog.list_suppliers_missing_peer(conn), [])
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
