"""Customer identity: profile columns, contacts and duplicate signals (phase F2).

Three capture points must survive a long conversation even when the model is
unavailable:

- an inbound message touches ``first_seen_at`` / ``last_seen_at``;
- ``memory.capture_facts`` writes phone, zone and address to queryable columns
  and to ``customer_contacts``;
- a sale stores the real delivery phone, zone, address and recipient name.

A shared phone is never an automatic merge: the first owner keeps the contact
and both customers receive a ``posible_duplicado`` signal for the operator.
The backfill replays old sales/facts and must be idempotent.

Run from `backend/`:

    .venv\\Scripts\\python.exe -m unittest discover -s tests -v
"""

import json
import tempfile
import unittest
from pathlib import Path

from cuadra import config, db
from cuadra.ai import ingest, memory
from cuadra.domain import catalog, conversations, customers, sales


class CustomerIdentityTestCase(unittest.TestCase):
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
                       12.5, 'USD', 2, 'USD', 'FIXED', 5, 'ACTIVE', '{}')"""
        )
        conn.execute(
            """INSERT INTO customers (id, name, kind, preferences_json)
               VALUES (1, 'Ana Pérez', 'customer', '{}')"""
        )
        conn.execute(
            """INSERT INTO customers (id, name, kind, preferences_json)
               VALUES (2, 'Luis Gómez', 'customer', '{}')"""
        )
        conn.executemany(
            """INSERT INTO customer_channels (customer_id, channel, identifier)
               VALUES (?, 'telegram', ?)""",
            [(1, "55500001"), (2, "55500002")],
        )
        conn.executemany(
            """INSERT INTO conversations
               (id, customer_id, channel, status, created_at, updated_at)
               VALUES (?, ?, 'telegram', 'ACTIVE', ?, ?)""",
            [
                (1, 1, "2026-11-16T08:00:00+00:00", "2026-11-17T10:00:00+00:00"),
                (2, 2, "2026-11-16T09:00:00+00:00", "2026-11-17T11:00:00+00:00"),
            ],
        )
        conn.commit()

    # --------------------------------------------------------------- helpers

    def customer(self, customer_id: int) -> dict:
        return conversations.get_customer(self.conn, customer_id)

    def contacts(self, customer_id: int) -> list[dict]:
        return customers.contacts_for(self.conn, customer_id)

    def signals(self, customer_id: int) -> list[dict]:
        return customers.open_signals(self.conn, customer_id)


class TouchTests(CustomerIdentityTestCase):
    def test_inbound_message_fills_first_and_last_seen(self):
        result = ingest.ingest_incoming(
            channel="telegram",
            identifier="55500001",
            body="Hola, precio del Power Bank",
        )
        customer = self.customer(result["customer_id"])
        self.assertIsNotNone(customer["first_seen_at"])
        self.assertIsNotNone(customer["last_seen_at"])

    def test_activity_never_moves_backwards(self):
        customers.touch(self.conn, 2, when="2026-01-01 00:00:00")
        customers.touch(self.conn, 2, when="2025-01-01 00:00:00")
        customer = self.customer(2)
        self.assertEqual(customer["first_seen_at"], "2026-01-01 00:00:00")
        self.assertEqual(customer["last_seen_at"], "2026-01-01 00:00:00")

    def test_a_later_touch_advances_last_seen_only(self):
        customers.touch(self.conn, 2, when="2026-01-01 00:00:00")
        customers.touch(self.conn, 2, when="2026-02-01 00:00:00")
        customer = self.customer(2)
        self.assertEqual(customer["first_seen_at"], "2026-01-01 00:00:00")
        self.assertEqual(customer["last_seen_at"], "2026-02-01 00:00:00")


class CaptureFactsTests(CustomerIdentityTestCase):
    def test_facts_are_written_to_columns_and_contacts(self):
        conversation = conversations.get_conversation(self.conn, 1)
        memory.capture_facts(
            self.conn,
            conversation,
            "Mi teléfono es 71234567 y mi dirección es Calle 23 #456, Vedado",
        )
        memory.capture_facts(self.conn, conversation, "Vivo en Vedado")
        customer = self.customer(1)
        self.assertEqual(customer["phone"], "+53 71234567")
        self.assertEqual(customer["phone_normalized"], "+53 71234567")
        self.assertEqual(customer["usual_zone_id"], 2)
        self.assertEqual(customer["usual_address"], "Calle 23 #456, Vedado")

        contacts = self.contacts(1)
        self.assertEqual(len(contacts), 1)
        self.assertEqual(contacts[0]["kind"], "phone")
        self.assertEqual(contacts[0]["normalized"], "+53 71234567")
        self.assertEqual(contacts[0]["source"], "fact")
        self.assertAlmostEqual(contacts[0]["confidence"], 0.8)

    def test_a_second_number_does_not_replace_the_first_contact(self):
        conversation = conversations.get_conversation(self.conn, 1)
        memory.capture_facts(
            self.conn, conversation, "Mi teléfono es 71234567"
        )
        memory.capture_facts(
            self.conn, conversation, "Mi otro teléfono es 76543210"
        )
        contacts = self.contacts(1)
        self.assertEqual(len(contacts), 2)
        self.assertEqual(
            {row["normalized"] for row in contacts},
            {"+53 71234567", "+53 76543210"},
        )


class ContactTests(CustomerIdentityTestCase):
    def test_phone_is_normalized_before_storage(self):
        result = customers.record_contact(
            self.conn, 1, "phone", "+53 7 123 4567", source="fact"
        )
        self.assertTrue(result["created"])
        self.assertEqual(result["contact"]["normalized"], "+53 71234567")

    def test_email_is_lowercased_and_trimmed(self):
        self.assertEqual(
            customers.normalize_contact("email", "  Ana.Perez@Example.COM "),
            "ana.perez@example.com",
        )
        self.assertIsNone(customers.normalize_contact("email", "sin-arroba"))
        result = customers.record_contact(
            self.conn, 1, "email", "Ana@Example.com", source="operator"
        )
        self.assertTrue(result["created"])
        self.assertEqual(result["contact"]["normalized"], "ana@example.com")

    def test_unusable_contact_is_ignored(self):
        result = customers.record_contact(
            self.conn, 1, "phone", "123", source="fact"
        )
        self.assertFalse(result["created"])
        self.assertIsNone(result["contact"])
        self.assertEqual(self.contacts(1), [])

    def test_invalid_kind_is_rejected(self):
        with self.assertRaises(ValueError):
            customers.record_contact(
                self.conn, 1, "telegram", "55500001", source="fact"
            )


class DuplicateTests(CustomerIdentityTestCase):
    def test_shared_phone_flags_both_customers_without_merging(self):
        first = customers.record_contact(
            self.conn, 1, "phone", "71234567", source="fact"
        )
        self.assertTrue(first["created"])

        second = customers.record_contact(
            self.conn, 2, "phone", "+53 7 123 4567", source="sale"
        )
        self.assertFalse(second["created"])
        self.assertEqual(second["duplicate_of"], 1)
        self.assertTrue(second["signal_created"])

        rows = self.conn.execute(
            "SELECT * FROM customer_contacts WHERE kind = 'phone'"
        ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["customer_id"], 1)
        self.assertEqual(rows[0]["source"], "fact")

        for customer_id in (1, 2):
            signals = self.signals(customer_id)
            self.assertEqual(len(signals), 1)
            signal = signals[0]
            self.assertEqual(signal["kind"], customers.DUPLICATE_SIGNAL)
            self.assertEqual(signal["status"], "OPEN")
            self.assertEqual(signal["count"], 1)
            evidence = json.loads(signal["evidence_json"])
            self.assertEqual(evidence["kind"], "phone")
            self.assertEqual(
                evidence["other_customer_id"], 2 if customer_id == 1 else 1
            )
            # The evidence is masked: the full number is never persisted.
            self.assertNotIn("+53 71234567", signal["evidence_json"])

        # Both customers stay active and unmerged: the operator decides.
        for customer_id in (1, 2):
            customer = self.customer(customer_id)
            self.assertEqual(customer["status"], "ACTIVE")
            self.assertIsNone(customer["merged_into_id"])

    def test_repeated_claim_does_not_inflate_the_signal(self):
        customers.record_contact(self.conn, 1, "phone", "71234567", source="fact")
        customers.record_contact(self.conn, 2, "phone", "71234567", source="fact")

        again = customers.record_contact(
            self.conn, 2, "phone", "71234567", source="fact"
        )
        self.assertFalse(again["signal_created"])
        self.assertEqual(again["duplicate_of"], 1)

        for customer_id in (1, 2):
            signals = self.signals(customer_id)
            self.assertEqual(len(signals), 1)
            self.assertEqual(signals[0]["count"], 1)

    def test_the_same_owner_refreshes_its_contact(self):
        first = customers.record_contact(
            self.conn, 1, "phone", "71234567", source="fact", confidence=0.6
        )
        again = customers.record_contact(
            self.conn,
            1,
            "phone",
            "71234567",
            source="sale",
            confidence=1.0,
            verified=True,
        )
        self.assertFalse(again["created"])
        self.assertIsNone(again["duplicate_of"])
        self.assertEqual(again["contact"]["id"], first["contact"]["id"])
        self.assertAlmostEqual(again["contact"]["confidence"], 1.0)
        self.assertEqual(again["contact"]["verified"], 1)
        self.assertEqual(len(self.contacts(1)), 1)


class SaleContactTests(CustomerIdentityTestCase):
    def test_sale_stores_phone_zone_address_and_recipient(self):
        sale = sales.create_sale(
            self.conn,
            conversation_id=1,
            customer_id=1,
            product_id=1,
            quantity=1,
            recipient_name="Carlos Pérez",
            delivery_zone="San Miguel",
            delivery_address="Calle 23 #456, Vedado",
            customer_phone="71234567",
        )
        customer = self.customer(1)
        self.assertEqual(customer["phone"], "+53 71234567")
        self.assertEqual(customer["usual_zone_id"], 1)
        self.assertEqual(customer["usual_address"], "Calle 23 #456, Vedado")
        self.assertEqual(customer["display_name"], "Carlos Pérez")

        contacts = self.contacts(1)
        self.assertEqual(len(contacts), 1)
        self.assertEqual(contacts[0]["normalized"], "+53 71234567")
        self.assertEqual(contacts[0]["source"], "sale")
        self.assertAlmostEqual(contacts[0]["confidence"], 1.0)
        self.assertEqual(sale["customer_phone"], "+53 71234567")

    def test_a_sale_does_not_overwrite_the_display_name(self):
        customers.record_sale_contact(
            self.conn,
            {
                "customer_id": 1,
                "recipient_name": "Carlos Pérez",
                "customer_phone": "71234567",
                "delivery_address": "Calle 23 #456, Vedado",
                "delivery_zone_id": 1,
            },
        )
        customers.record_sale_contact(
            self.conn,
            {
                "customer_id": 1,
                "recipient_name": "Otro Nombre",
                "customer_phone": "71234567",
                "delivery_address": "Calle 10 #5, Vedado",
                "delivery_zone_id": 2,
            },
        )
        self.assertEqual(self.customer(1)["display_name"], "Carlos Pérez")


class BackfillTests(CustomerIdentityTestCase):
    """Old sales/facts are replayed into columns, contacts and signals."""

    def setUp(self):
        super().setUp()
        # Simulate a pre-021 customer: data only lives in `sales` and facts.
        self.conn.execute(
            """INSERT INTO sales
               (id, order_code, conversation_id, product_id, supplier_id,
                customer_id, quantity, amount, currency, commission_type,
                commission, commission_currency, recipient_name, delivery_zone,
                delivery_zone_id, delivery_address, customer_phone, status,
                created_at, updated_at)
               VALUES (1, 'V-000001', 1, 1, 1, 1, 1, 12.5, 'USD', 'FIXED',
                       2, 'USD', 'Carlos Pérez', 'San Miguel', 1,
                       'Calle 23 #456, Vedado', '71234567', 'SETTLED',
                       '2026-11-15 09:00:00', '2026-11-15 09:00:00')"""
        )
        self.conn.executemany(
            """INSERT INTO conversation_facts
               (conversation_id, fact_key, fact_value)
               VALUES (1, ?, ?)""",
            [
                ("customer_phone", "71234567"),
                ("zone_name", "Vedado"),
                ("delivery_address", "Calle 23 #456, Vedado"),
                ("customer_name", "Carlos"),
            ],
        )
        self.conn.commit()

    def _run(self, dry_run=False):
        from scripts.backfill_customers import backfill

        return backfill(self.conn, dry_run=dry_run)

    def test_dry_run_reports_without_writing(self):
        report = self._run(dry_run=True)
        self.assertGreater(report["customers_updated"], 0)
        self.assertEqual(report["contacts_created"], 1)

        customer = self.customer(1)
        self.assertIsNone(customer["phone"])
        self.assertIsNone(customer["phone_normalized"])
        self.assertIsNone(customer["usual_zone_id"])
        self.assertIsNone(customer["usual_address"])
        self.assertIsNone(customer["display_name"])
        self.assertIsNone(customer["first_seen_at"])
        self.assertEqual(self.contacts(1), [])
        self.assertEqual(self.signals(1), [])

    def test_backfill_fills_the_profile_and_is_idempotent(self):
        first = self._run()
        self.assertEqual(first["contacts_created"], 1)
        self.assertEqual(first["duplicate_contacts"], 0)

        customer = self.customer(1)
        self.assertEqual(customer["phone"], "+53 71234567")
        self.assertEqual(customer["phone_normalized"], "+53 71234567")
        self.assertEqual(customer["usual_zone_id"], 1)  # sale wins over facts
        self.assertEqual(customer["usual_address"], "Calle 23 #456, Vedado")
        self.assertEqual(customer["display_name"], "Carlos Pérez")
        self.assertEqual(customer["first_seen_at"], "2026-11-15 09:00:00")
        self.assertEqual(customer["last_seen_at"], "2026-11-16 08:00:00")

        contacts = self.contacts(1)
        self.assertEqual(len(contacts), 1)
        self.assertEqual(contacts[0]["source"], "sale")

        second = self._run()
        self.assertEqual(second["customers_updated"], 0)
        self.assertEqual(second["first_last_filled"], 0)
        self.assertEqual(second["phones_filled"], 0)
        self.assertEqual(second["addresses_filled"], 0)
        self.assertEqual(second["zones_filled"], 0)
        self.assertEqual(second["names_filled"], 0)
        self.assertEqual(second["contacts_created"], 0)
        self.assertEqual(second["duplicate_contacts"], 0)
        self.assertEqual(second["duplicates_flagged"], 0)
        self.assertEqual(second["suppliers_marked"], 0)
        self.assertEqual(len(self.contacts(1)), 1)

    def test_backfill_never_merges_customers_that_share_a_phone(self):
        # The second customer also reported the same phone in a conversation.
        self.conn.execute(
            """INSERT INTO conversation_facts
               (conversation_id, fact_key, fact_value)
               VALUES (2, 'customer_phone', '71234567')"""
        )
        self.conn.commit()

        report = self._run()
        self.assertEqual(report["duplicate_contacts"], 1)
        self.assertEqual(report["duplicates_flagged"], 1)

        rows = self.conn.execute(
            "SELECT customer_id FROM customer_contacts WHERE kind = 'phone'"
        ).fetchall()
        self.assertEqual([row["customer_id"] for row in rows], [1])
        for customer_id in (1, 2):
            self.assertEqual(self.customer(customer_id)["status"], "ACTIVE")
            self.assertIsNone(self.customer(customer_id)["merged_into_id"])
            self.assertEqual(len(self.signals(customer_id)), 1)


class ZoneResolutionTests(CustomerIdentityTestCase):
    def test_profile_zone_uses_the_catalog_name(self):
        zone = catalog.resolve_zone(self.conn, "Vedado")
        self.assertEqual(zone["id"], 2)
        customers.set_profile_fields(self.conn, 1, zone_name="Vedado")
        self.assertEqual(self.customer(1)["usual_zone_id"], 2)

    def test_unknown_zone_is_not_guessed(self):
        result = customers.set_profile_fields(
            self.conn, 1, zone_name="Marianao"
        )
        self.assertEqual(result["updates"], {})
        self.assertIsNone(self.customer(1)["usual_zone_id"])


if __name__ == "__main__":
    unittest.main()
