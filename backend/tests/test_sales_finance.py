"""Phase 3: sales, reserved stock, TCP dispatch and the commission ledger.

Throwaway SQLite database, no model server, no network. Run from `backend/`:

    .venv\\Scripts\\python.exe -m unittest discover -s tests -v
"""

import tempfile
import unittest
from pathlib import Path

from cuadra import config, db
from cuadra.ai import memory
from cuadra.domain import catalog, conversations, sales


class Phase3TestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_db_path = config.DB_PATH
        config.DB_PATH = Path(self._tmp.name) / "test.db"
        conn = db.connect()
        try:
            db.apply_migrations(conn)
            conn.execute("INSERT INTO categories (id, name) VALUES (1, 'Energía')")
            conn.execute(
                """INSERT INTO suppliers (id, name, telegram_id, delivery_zone)
                   VALUES (1, 'TCP Uno', '111222333', 'La Habana')"""
            )
            conn.execute(
                """INSERT INTO suppliers (id, name, telegram_id, delivery_zone)
                   VALUES (2, 'TCP Dos', '444555666', 'La Habana')"""
            )
            conn.executemany(
                "INSERT INTO zones (id, name, active) VALUES (?, ?, ?)",
                [
                    (1, "San Miguel", 1),
                    (2, "Vedado", 1),
                    (3, "Zona Inactiva", 0),
                ],
            )
            conn.execute(
                """INSERT INTO supplier_delivery_zones (supplier_id, zone_id)
                   VALUES (1, 1)"""
            )
            products = [
                # id, price, currency, commission, commission_currency, type, stock, status
                (1, 100.0, "USD", 10.0, "USD", "FIXED", 5, "ACTIVE"),
                (2, 500.0, "CUP", 50.0, "CUP", "FIXED", 3, "ACTIVE"),
                (3, 200.0, "USD", 10.0, "USD", "PERCENT", 2, "ACTIVE"),
                # A USD sale with a CUP commission: payout must stay NULL.
                (4, 50.0, "USD", 500.0, "CUP", "FIXED", 1, "ACTIVE"),
                (5, 30.0, "USD", 5.0, "USD", "FIXED", 1, "PAUSED"),
            ]
            conn.executemany(
                """INSERT INTO products
                   (id, supplier_id, category_id, name, description, price,
                    currency, commission, commission_currency, commission_type,
                    stock, status, specifications_json)
                   VALUES (?, 1, 1, ?, 'Producto de prueba', ?, ?, ?, ?, ?, ?, ?, '{}')""",
                [
                    (
                        pid,
                        f"Producto {pid}",
                        price,
                        currency,
                        commission,
                        commission_currency,
                        ctype,
                        stock,
                        status,
                    )
                    for pid, price, currency, commission, commission_currency, ctype, stock, status in products
                ],
            )
            conn.execute(
                """INSERT INTO customers (id, name, preferences_json)
                   VALUES (1, 'Carlos', '{}')"""
            )
            # A fresh customer with no stored profile, used by the fact tests.
            conn.execute(
                """INSERT INTO customers (id, name, preferences_json)
                   VALUES (2, '', '{}')"""
            )
            conn.execute(
                """INSERT INTO conversations (id, customer_id, channel, status)
                   VALUES (1, 1, 'telegram', 'ACTIVE')"""
            )
            conn.execute(
                """INSERT INTO conversations (id, customer_id, channel, status)
                   VALUES (2, 2, 'telegram', 'ACTIVE')"""
            )
            conn.commit()
        finally:
            conn.close()
        self.conn = db.connect()

    def tearDown(self):
        self.conn.close()
        config.DB_PATH = self._old_db_path
        self._tmp.cleanup()

    # ------------------------------------------------------------------ helpers
    def create_sale(self, product_id=1, **overrides):
        params = {
            "conversation_id": 1,
            "customer_id": 1,
            "product_id": product_id,
            "quantity": 1,
            "recipient_name": "Carlos Pérez",
            "delivery_zone": "San Miguel",
            "delivery_address": "Calle 23 #456, Vedado",
            "customer_phone": "71234567",
        }
        params.update(overrides)
        return sales.create_sale(self.conn, **params)

    def product(self, product_id=1):
        return catalog.get_product(self.conn, product_id)

    def dispatch(self, sale_id):
        sales.approve(self.conn, sale_id)
        return sales.mark_dispatched(self.conn, sale_id, "tcp-1")

    def count(self, table):
        return self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


class CommissionTests(Phase3TestCase):
    def test_fixed_commission_in_same_currency_computes_payout(self):
        earned, payout, total, currency, ctype = sales.calculate_commission(
            self.product(1), 2, "USD"
        )
        self.assertEqual((earned, payout, total), (20.0, 180.0, 200.0))
        self.assertEqual((currency, ctype), ("USD", "FIXED"))

    def test_fixed_commission_in_another_currency_never_mixes_money(self):
        earned, payout, total, currency, ctype = sales.calculate_commission(
            self.product(4), 1, "USD"
        )
        self.assertEqual((earned, payout, total), (500.0, None, 50.0))
        self.assertEqual((currency, ctype), ("CUP", "FIXED"))

    def test_percent_commission_inherits_the_sale_currency(self):
        earned, payout, total, currency, ctype = sales.calculate_commission(
            self.product(3), 1, "USD"
        )
        self.assertEqual((earned, payout, total), (20.0, 180.0, 200.0))
        self.assertEqual((currency, ctype), ("USD", "PERCENT"))

    def test_phone_normalisation(self):
        self.assertEqual(sales.normalize_phone("+53 7 123 4567"), "+53 71234567")
        self.assertEqual(sales.normalize_phone("71234567"), "+53 71234567")
        self.assertIsNone(sales.normalize_phone("123"))
        self.assertIsNone(sales.normalize_phone(None))


class ReservationTests(Phase3TestCase):
    def test_sale_reserves_stock_and_writes_the_ledger(self):
        sale = self.create_sale()
        self.assertEqual(sale["status"], "PENDING_APPROVAL")
        self.assertEqual(sale["order_code"], "V-000001")
        self.assertEqual(sale["amount"], 100.0)
        self.assertEqual(sale["commission"], 10.0)
        self.assertEqual(sale["commission_currency"], "USD")

        product = self.product(1)
        self.assertEqual(product["stock"], 5)
        self.assertEqual(product["stock_reserved"], 1)

        ledger = sales.get_ledger(self.conn, sale["id"])
        self.assertEqual(ledger["sale_total"], 100.0)
        self.assertEqual(ledger["commission_earned"], 10.0)
        self.assertEqual(ledger["commission_currency"], "USD")
        self.assertEqual(ledger["supplier_payout"], 90.0)
        self.assertEqual(ledger["status"], "PENDING")

    def test_sale_is_all_or_nothing_when_stock_is_exhausted(self):
        self.create_sale(product_id=4)
        with self.assertRaises(sales.Invalid):
            self.create_sale(product_id=4)

        self.assertEqual(self.count("sales"), 1)
        self.assertEqual(self.count("commission_ledger"), 1)
        self.assertEqual(self.product(4)["stock_reserved"], 1)

    def test_quantity_over_available_is_rejected_without_side_effects(self):
        with self.assertRaises(sales.Invalid):
            self.create_sale(product_id=4, quantity=2)
        self.assertEqual(self.count("sales"), 0)
        self.assertEqual(self.product(4)["stock_reserved"], 0)

    def test_cancel_releases_the_reservation_and_voids_the_ledger(self):
        sale = self.create_sale()
        cancelled = sales.cancel(self.conn, sale["id"], "cliente se arrepintió")

        self.assertEqual(cancelled["status"], "CANCELLED")
        self.assertEqual(cancelled["notes"], "cliente se arrepintió")
        self.assertEqual(self.product(1)["stock_reserved"], 0)
        ledger = sales.get_ledger(self.conn, sale["id"])
        self.assertEqual(ledger["status"], "VOID")

    def test_open_sale_per_conversation_is_reported(self):
        self.assertIsNone(sales.active_sale_for_conversation(self.conn, 1))
        sale = self.create_sale()
        active = sales.active_sale_for_conversation(self.conn, 1)
        self.assertEqual(active["id"], sale["id"])

        sales.cancel(self.conn, sale["id"])
        self.assertIsNone(sales.active_sale_for_conversation(self.conn, 1))


class ValidationTests(Phase3TestCase):
    def test_supplier_must_cover_the_delivery_zone(self):
        with self.assertRaises(sales.Invalid):
            self.create_sale(delivery_zone="Vedado")
        self.assertEqual(self.count("sales"), 0)

    def test_unknown_or_inactive_zone_is_rejected(self):
        for zone in ("Marianao", "Zona Inactiva", ""):
            with self.assertRaises(sales.Invalid):
                self.create_sale(delivery_zone=zone)

    def test_paused_product_cannot_be_sold(self):
        with self.assertRaises(sales.Invalid):
            self.create_sale(product_id=5)

    def test_vague_recipient_or_address_is_rejected(self):
        with self.assertRaises(sales.Invalid):
            self.create_sale(recipient_name="yo")
        with self.assertRaises(sales.Invalid):
            self.create_sale(delivery_address="mi casa")
        with self.assertRaises(sales.Invalid):
            self.create_sale(customer_phone="123")


class StateMachineTests(Phase3TestCase):
    def test_assisted_flow_runs_until_the_commission_is_settled(self):
        sale = self.create_sale()

        approved = sales.approve(self.conn, sale["id"])
        self.assertEqual(approved["status"], "PENDING_DISPATCH")
        self.assertIsNotNone(approved["approved_at"])

        dispatched = sales.mark_dispatched(self.conn, sale["id"], "tcp-77")
        self.assertEqual(dispatched["status"], "DISPATCHED")
        self.assertEqual(dispatched["tcp_message_id"], "tcp-77")

        confirmed = sales.confirm(self.conn, sale["id"])
        self.assertEqual(confirmed["status"], "CONFIRMED")

        delivered = sales.deliver(self.conn, sale["id"])
        self.assertEqual(delivered["status"], "DELIVERED")
        self.assertEqual(self.product(1)["stock"], 4)
        self.assertEqual(self.product(1)["stock_reserved"], 0)

        settled = sales.settle(self.conn, sale["id"])
        self.assertEqual(settled["status"], "SETTLED")
        ledger = sales.get_ledger(self.conn, sale["id"])
        self.assertEqual(ledger["status"], "COLLECTED")
        self.assertIsNotNone(ledger["collected_at"])

    def test_invalid_transition_is_rejected(self):
        sale = self.create_sale()
        with self.assertRaises(sales.Invalid):
            sales.settle(self.conn, sale["id"])
        with self.assertRaises(sales.Invalid):
            sales.mark_dispatched(self.conn, sale["id"], "tcp-1")
        with self.assertRaises(sales.Invalid):
            sales.deliver(self.conn, sale["id"])

    def test_cannot_cancel_after_delivery(self):
        sale = self.create_sale()
        self.dispatch(sale["id"])
        sales.deliver(self.conn, sale["id"])
        with self.assertRaises(sales.Invalid):
            sales.cancel(self.conn, sale["id"])

    def test_needs_operator_can_be_rearmed_for_dispatch(self):
        sale = self.create_sale()
        flagged = sales.mark_needs_operator(self.conn, sale["id"], "sin señal")
        self.assertEqual(flagged["status"], "NEEDS_OPERATOR")
        rearmed = sales.arm_dispatch(self.conn, sale["id"])
        self.assertEqual(rearmed["status"], "PENDING_DISPATCH")

    def test_autonomous_mode_starts_in_pending_dispatch(self):
        self.conn.execute(
            "UPDATE settings SET value = 'autonomous' WHERE key = 'sales.mode'"
        )
        self.conn.commit()
        sale = self.create_sale()
        self.assertEqual(sale["status"], "PENDING_DISPATCH")

    def test_hybrid_mode_falls_back_when_the_catalog_is_stale(self):
        self.conn.execute(
            "UPDATE settings SET value = 'hybrid' WHERE key = 'sales.mode'"
        )
        self.conn.execute(
            "UPDATE products SET updated_at = datetime('now', '-48 hours') WHERE id = 1"
        )
        self.conn.commit()
        self.assertEqual(self.create_sale()["status"], "PENDING_APPROVAL")

    def test_hybrid_mode_auto_dispatches_fresh_covered_products(self):
        self.conn.execute(
            "UPDATE settings SET value = 'hybrid' WHERE key = 'sales.mode'"
        )
        self.conn.commit()
        self.assertEqual(self.create_sale()["status"], "PENDING_DISPATCH")

    def test_hybrid_mode_falls_back_while_an_escalation_is_open(self):
        self.conn.execute(
            "UPDATE settings SET value = 'hybrid' WHERE key = 'sales.mode'"
        )
        self.conn.execute(
            """INSERT INTO escalations
               (conversation_id, product_id, supplier_id, question, status)
               VALUES (1, 1, 1, '¿trae cable?', 'PENDING')"""
        )
        self.conn.commit()
        self.assertEqual(self.create_sale()["status"], "PENDING_APPROVAL")


class TcpAckTests(Phase3TestCase):
    def test_received_then_delivered_edit_the_same_sale_by_order_code(self):
        sale = self.create_sale()
        self.dispatch(sale["id"])
        code = sale["order_code"]

        confirmed, action = sales.apply_tcp_ack(self.conn, 1, f"RECIBIDO {code}")
        self.assertEqual(action, "CONFIRMED")
        self.assertEqual(confirmed["status"], "CONFIRMED")

        delivered, action = sales.apply_tcp_ack(self.conn, 1, f"ENTREGADO {code}")
        self.assertEqual(action, "DELIVERED")
        self.assertEqual(delivered["status"], "DELIVERED")
        self.assertEqual(self.product(1)["stock"], 4)

    def test_ack_creates_the_customer_notification(self):
        import asyncio

        from cuadra.ai import handlers

        sale = self.create_sale()
        self.dispatch(sale["id"])
        inbound = conversations.add_message(
            self.conn,
            2,
            "INBOUND",
            "tcp",
            body=f"RECIBIDO {sale['order_code']}",
        )

        asyncio.run(
            handlers.tcp_process(
                {
                    "conversation_id": 2,
                    "message_id": inbound["id"],
                    "supplier_id": 1,
                }
            )
        )

        confirmed = sales.get_sale(self.conn, sale["id"])
        self.assertEqual(confirmed["status"], "CONFIRMED")
        outbound = self.conn.execute(
            """SELECT id, body FROM messages
               WHERE conversation_id = 1 AND direction = 'OUTBOUND'
               ORDER BY id DESC LIMIT 1"""
        ).fetchone()
        self.assertIn(sale["order_code"], outbound["body"])
        job = self.conn.execute(
            "SELECT type, payload_json FROM jobs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(job["type"], "send_message")
        self.assertIn('"message_id"', job["payload_json"])
        self.assertIn(str(outbound["id"]), job["payload_json"])

    def test_out_of_stock_flags_the_operator(self):
        sale = self.create_sale()
        self.dispatch(sale["id"])
        updated, action = sales.apply_tcp_ack(
            self.conn, 1, f"AGOTADO {sale['order_code']}"
        )
        self.assertEqual(action, "NEEDS_OPERATOR")
        self.assertEqual(updated["status"], "NEEDS_OPERATOR")
        self.assertEqual(self.product(1)["stock_reserved"], 1)

    def test_ack_without_a_code_targets_the_oldest_dispatched_sale(self):
        sale = self.create_sale()
        self.dispatch(sale["id"])
        confirmed, action = sales.apply_tcp_ack(self.conn, 1, "recibido, lo preparo")
        self.assertEqual(action, "CONFIRMED")
        self.assertEqual(confirmed["id"], sale["id"])

    def test_ack_from_another_supplier_is_ignored(self):
        sale = self.create_sale()
        self.dispatch(sale["id"])
        found, action = sales.apply_tcp_ack(
            self.conn, 2, f"RECIBIDO {sale['order_code']}"
        )
        self.assertIsNone(found)
        self.assertIsNone(action)

    def test_plain_message_is_not_an_ack(self):
        sale = self.create_sale()
        self.dispatch(sale["id"])
        self.assertEqual(sales.apply_tcp_ack(self.conn, 1, "¿cuándo pago?"), (None, None))

    def test_accents_and_case_are_folded(self):
        sale = self.create_sale()
        self.dispatch(sale["id"])
        confirmed, action = sales.apply_tcp_ack(
            self.conn, 1, f"Recibído: {sale['order_code'].lower()}"
        )
        self.assertEqual(action, "CONFIRMED")
        self.assertEqual(confirmed["status"], "CONFIRMED")


class SummaryTests(Phase3TestCase):
    def test_totals_never_mix_currencies(self):
        self.create_sale(product_id=1)  # 100 USD / 10 USD commission
        self.create_sale(product_id=2)  # 500 CUP / 50 CUP commission
        self.create_sale(product_id=4)  # 50 USD, commission 500 CUP (mismatch)

        summary = sales.summary(self.conn)
        totals = {row["currency"]: row for row in summary["totals_by_currency"]}

        self.assertEqual(totals["USD"]["gross_sales"], 150.0)
        self.assertEqual(totals["USD"]["commission_earned"], 10.0)
        self.assertEqual(totals["USD"]["commission_pending"], 10.0)
        self.assertEqual(totals["CUP"]["gross_sales"], 500.0)
        self.assertEqual(totals["CUP"]["commission_earned"], 50.0)
        self.assertEqual(summary["conversion_pending"], 1)
        self.assertEqual(summary["sales_today"], 3)
        self.assertEqual(summary["sales_pending_approval"], 3)

    def test_settling_moves_the_commission_from_pending_to_collected(self):
        sale = self.create_sale()
        self.dispatch(sale["id"])
        sales.deliver(self.conn, sale["id"])
        sales.settle(self.conn, sale["id"])

        summary = sales.summary(self.conn)
        usd = summary["totals_by_currency"][0]
        self.assertEqual(usd["currency"], "USD")
        self.assertEqual(usd["commission_collected"], 10.0)
        self.assertEqual(usd["commission_pending"], 0.0)
        self.assertEqual(summary["sales_delivered_pending_settlement"], 0)

    def test_cancelled_sales_do_not_enter_the_totals(self):
        sale = self.create_sale()
        sales.cancel(self.conn, sale["id"])
        summary = sales.summary(self.conn)
        self.assertEqual(summary["totals_by_currency"], [])
        self.assertEqual(summary["sales_today"], 0)
        self.assertEqual(summary["conversion_pending"], 0)

    def test_lists_filter_by_status_and_currency(self):
        first = self.create_sale(product_id=1)
        self.create_sale(product_id=2)

        usd = sales.list_sales(self.conn, currency="USD")
        self.assertEqual([row["id"] for row in usd], [first["id"]])
        self.assertEqual(sales.count_sales(self.conn, currency="USD"), 1)
        self.assertEqual(
            sales.count_sales(self.conn, status="PENDING_APPROVAL"), 2
        )

        ledger = sales.list_ledger(self.conn, currency="CUP")
        self.assertEqual(len(ledger), 1)
        self.assertEqual(ledger[0]["commission_currency"], "CUP")
        self.assertEqual(sales.count_ledger(self.conn, currency="CUP"), 1)

        self.assertEqual(sales.count_sales(self.conn, supplier_id=1), 2)
        self.assertEqual(sales.count_sales(self.conn, supplier_id=2), 0)
        self.assertEqual(sales.count_ledger(self.conn, supplier_id=1), 2)
        self.assertEqual(sales.count_ledger(self.conn, supplier_id=2), 0)
        self.assertEqual(sales.count_ledger(self.conn, date_from="2999-01-01"), 0)
        self.assertEqual(
            len(sales.list_ledger(self.conn, date_to="2999-01-01")), 2
        )


class AddressFactTests(Phase3TestCase):
    def conversation(self, conversation_id=2):
        return conversations.get_conversation(self.conn, conversation_id)

    def test_explicit_address_label_is_captured(self):
        found = memory.capture_facts(
            self.conn,
            self.conversation(),
            "Mi dirección es Calle 23 #456, Vedado",
        )
        address = memory.facts_map(self.conn, 2)[memory.FACT_ADDRESS]
        self.assertEqual(address, found[memory.FACT_ADDRESS])
        self.assertIn("Calle 23", address)

    def test_street_phrase_without_a_label_is_captured(self):
        memory.capture_facts(
            self.conn, self.conversation(), "Calle 10 número 5, Vedado"
        )
        self.assertEqual(
            memory.facts_map(self.conn, 2)[memory.FACT_ADDRESS],
            "Calle 10 número 5",
        )

    def test_a_bare_zone_is_not_an_address(self):
        found = memory.capture_facts(
            self.conn, self.conversation(), "Vivo en San Miguel"
        )
        self.assertEqual(found.get(memory.FACT_ZONE), "San Miguel")
        self.assertNotIn(memory.FACT_ADDRESS, found)
        self.assertNotIn(memory.FACT_ADDRESS, memory.facts_map(self.conn, 2))

    def test_vague_address_phrases_are_ignored(self):
        for text in ("Mi dirección es mi casa", "Mi dirección es aquí"):
            found = memory.capture_facts(self.conn, self.conversation(), text)
            self.assertNotIn(memory.FACT_ADDRESS, found)

    def test_address_is_kept_in_the_customer_profile(self):
        memory.capture_facts(
            self.conn, self.conversation(), "Entrega en Avenida 26 #12, Nuevo Vedado"
        )
        profile = memory.customer_profile(self.conn, 2)
        self.assertIn("Avenida 26", profile["direccion_habitual"])


class SupplierPeerTests(Phase3TestCase):
    def test_supplier_is_resolved_from_the_numeric_peer(self):
        catalog.set_supplier_telegram_peer(self.conn, 1, "777888999")
        found = catalog.find_supplier_by_telegram_id(self.conn, "777888999")
        self.assertIsNotNone(found)
        self.assertEqual(found["id"], 1)

    def test_handle_keeps_matching_before_the_peer_is_known(self):
        found = catalog.find_supplier_by_telegram_id(self.conn, "111222333")
        self.assertIsNotNone(found)
        self.assertEqual(found["id"], 1)

    def test_unknown_identifier_is_not_a_supplier(self):
        self.assertIsNone(catalog.find_supplier_by_telegram_id(self.conn, "000000000"))

    def test_dispatch_uses_the_userbot_and_persists_the_peer(self):
        import asyncio
        from unittest.mock import AsyncMock, patch

        from cuadra.ai import handlers
        from cuadra.channels import telegram_user

        sale = self.create_sale()
        sales.approve(self.conn, sale["id"])

        class FakeAdapter:
            def __init__(self):
                self.sent = []

            async def typing(self, target):
                return None

            async def send_text(self, target, text):
                self.sent.append((target, text))
                return "9001"

        adapter = FakeAdapter()
        with (
            patch.object(telegram_user, "available", return_value=True),
            patch.object(
                telegram_user, "resolve_peer", new=AsyncMock(return_value="777888999")
            ),
            patch.object(handlers.channels, "get_adapter", return_value=adapter),
            patch.object(handlers, "_human_delay", return_value=0),
        ):
            asyncio.run(handlers.dispatch_sale({"sale_id": sale["id"]}))

        updated = sales.get_sale(self.conn, sale["id"])
        self.assertEqual(updated["status"], "DISPATCHED")
        self.assertEqual(updated["tcp_message_id"], "9001")
        self.assertEqual(
            catalog.get_supplier(self.conn, 1)["telegram_peer_id"], "777888999"
        )
        self.assertEqual(adapter.sent[0][0], "777888999")
        self.assertIn(sale["order_code"], adapter.sent[0][1])


if __name__ == "__main__":
    unittest.main()
