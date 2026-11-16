"""Phase 3: sales, reserved stock, TCP dispatch and the commission ledger.

Throwaway SQLite database, no model server, no network. Run from `backend/`:

    .venv\\Scripts\\python.exe -m unittest discover -s tests -v
"""

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from cuadra import config, db
from cuadra.ai import memory
from cuadra.domain import catalog, clock, conversations, sales, settings


class ClockTests(unittest.TestCase):
    """H-18: the business day follows America/Havana, not UTC."""

    def test_early_utc_still_belongs_to_the_previous_havana_day(self):
        start, end = clock.day_bounds(
            datetime(2026, 1, 15, 3, 30, tzinfo=timezone.utc)
        )
        self.assertEqual(start, "2026-01-14 05:00:00")
        self.assertEqual(end, "2026-01-15 05:00:00")


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


class SalesModeSettingsTests(Phase3TestCase):
    """Fase 3 pending: the dashboard can change `sales.mode` safely."""

    def test_default_mode_is_assisted(self):
        self.assertEqual(sales.sale_mode(self.conn), "assisted")
        self.assertEqual(sales.summary(self.conn)["mode"], "assisted")

    def test_set_mode_persists_and_reaches_the_summary(self):
        self.assertEqual(sales.set_sale_mode(self.conn, "hybrid"), "hybrid")
        self.assertEqual(sales.sale_mode(self.conn), "hybrid")
        self.assertEqual(sales.summary(self.conn)["mode"], "hybrid")

    def test_invalid_mode_is_rejected_without_changing_the_setting(self):
        sales.set_sale_mode(self.conn, "assisted")
        with self.assertRaises(ValueError):
            sales.set_sale_mode(self.conn, "free")
        self.assertEqual(sales.sale_mode(self.conn), "assisted")

    def test_unknown_stored_mode_falls_back_to_assisted(self):
        settings.set(self.conn, sales.MODE_KEY, "manual")
        self.assertEqual(sales.sale_mode(self.conn), "assisted")


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

    def test_sales_today_uses_the_havana_day_window(self):
        sale = self.create_sale()
        self.conn.execute(
            "UPDATE sales SET created_at = '2026-01-15 03:30:00' WHERE id = ?",
            (sale["id"],),
        )
        self.conn.commit()
        window = ("2026-01-14 05:00:00", "2026-01-15 05:00:00")
        with patch(
            "cuadra.domain.sales.clock.day_bounds", return_value=window
        ):
            self.assertEqual(sales.summary(self.conn)["sales_today"], 1)

    def test_sales_today_excludes_a_sale_outside_the_havana_day(self):
        sale = self.create_sale()
        self.conn.execute(
            "UPDATE sales SET created_at = '2026-01-15 06:00:00' WHERE id = ?",
            (sale["id"],),
        )
        self.conn.commit()
        window = ("2026-01-14 05:00:00", "2026-01-15 05:00:00")
        with patch(
            "cuadra.domain.sales.clock.day_bounds", return_value=window
        ):
            self.assertEqual(sales.summary(self.conn)["sales_today"], 0)

    def test_pending_dispatch_has_its_own_counter(self):
        sale = self.create_sale()
        sales.approve(self.conn, sale["id"])
        summary = sales.summary(self.conn)
        self.assertEqual(summary["sales_pending_dispatch"], 1)
        self.assertEqual(summary["sales_pending_approval"], 0)

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

    def test_a_purchase_phrase_is_not_an_address(self):
        # Prueba #2 (F6): "Si, pero quiero 3 no 1" pisaba la dirección real.
        memory.capture_facts(
            self.conn, self.conversation(), "Mi direccion es Calle 23 #456, Vedado"
        )
        memory.capture_facts(self.conn, self.conversation(), "Si, pero quiero 3 no 1")
        self.assertEqual(
            memory.facts_map(self.conn, 2)[memory.FACT_ADDRESS],
            "Calle 23 #456, Vedado",
        )
        profile = memory.customer_profile(self.conn, 2)
        self.assertNotIn("quiero", profile["direccion_habitual"])


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

    def test_dispatch_pacing_parks_the_job_instead_of_flagging_the_operator(self):
        import asyncio
        from unittest.mock import AsyncMock, patch

        from cuadra.ai import handlers, queue
        from cuadra.channels import telegram_user

        sale = self.create_sale()
        sales.approve(self.conn, sale["id"])

        with (
            patch.object(telegram_user, "available", return_value=True),
            patch.object(
                telegram_user, "resolve_peer", new=AsyncMock(return_value="111222333")
            ),
            patch.object(handlers.channels, "get_adapter", return_value=object()),
            patch.object(
                handlers,
                "_check_token_bucket",
                side_effect=queue.RateLimited(
                    30, "userbot send gap", account=False, penalize=False
                ),
            ),
        ):
            with self.assertRaises(queue.RateLimited):
                asyncio.run(handlers.dispatch_sale({"sale_id": sale["id"]}))

        updated = sales.get_sale(self.conn, sale["id"])
        self.assertEqual(updated["status"], "PENDING_DISPATCH")


class CancelTcpNoticeTests(Phase3TestCase):
    """The TCP is told when an order already in its chat is cancelled."""

    def _cancel_jobs(self) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE type = 'notify_tcp_cancel'"
        ).fetchone()[0]

    def _event_count(self, event_type: str) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM events WHERE type = ?", (event_type,)
        ).fetchone()[0]

    def _dispatched_sale(self):
        sale = self.create_sale()
        self.dispatch(sale["id"])
        return sale

    def _cancelled_dispatched_sale(self, reason="cliente cambió de idea"):
        sale = self._dispatched_sale()
        sales.cancel(self.conn, sale["id"], reason)
        return sale

    def test_cancelling_a_dispatched_sale_queues_the_notice(self):
        from cuadra.api import routes_finance

        sale = self._dispatched_sale()
        cancelled = routes_finance.cancel_sale(sale["id"])

        self.assertEqual(cancelled["status"], "CANCELLED")
        self.assertEqual(self._cancel_jobs(), 1)

    def test_cancelling_a_confirmed_sale_queues_the_notice(self):
        from cuadra.api import routes_finance

        sale = self._dispatched_sale()
        sales.apply_tcp_ack(self.conn, 1, f"RECIBIDO {sale['order_code']}")
        self.assertEqual(sales.get_sale(self.conn, sale["id"])["status"], "CONFIRMED")

        routes_finance.cancel_sale(sale["id"])
        self.assertEqual(self._cancel_jobs(), 1)

    def test_cancelling_before_the_tcp_saw_the_order_does_not_queue_a_notice(self):
        from cuadra.api import routes_finance

        sale = self.create_sale()
        routes_finance.cancel_sale(sale["id"])

        self.assertEqual(self._cancel_jobs(), 0)

    def test_cancelling_a_failed_dispatch_does_not_queue_a_notice(self):
        from cuadra.api import routes_finance

        sale = self.create_sale()
        sales.approve(self.conn, sale["id"])
        sales.mark_needs_operator(self.conn, sale["id"], "dispatch failed: chat not found")
        routes_finance.cancel_sale(sale["id"])

        self.assertEqual(self._cancel_jobs(), 0)

    def _run_notice(self, sale_id, repeat=True):
        import asyncio
        from unittest.mock import patch

        from cuadra.ai import handlers
        from cuadra.channels import telegram_user

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
            patch.object(handlers.channels, "get_adapter", return_value=adapter),
            patch.object(handlers, "_human_delay", return_value=0),
        ):
            asyncio.run(handlers.notify_tcp_cancel({"sale_id": sale_id}))
            if repeat:
                # A requeued job must not send the notice twice.
                asyncio.run(handlers.notify_tcp_cancel({"sale_id": sale_id}))
        return adapter

    def test_handler_sends_the_notice_once_with_the_reason(self):
        sale = self._cancelled_dispatched_sale()

        adapter = self._run_notice(sale["id"])

        self.assertEqual(len(adapter.sent), 1)
        target, text = adapter.sent[0]
        self.assertEqual(target, "111222333")
        self.assertIn(sale["order_code"], text)
        self.assertIn("cliente cambió de idea", text)
        self.assertEqual(self._event_count("tcp_cancel_notified"), 1)

    def test_handler_ignores_a_sale_that_is_not_cancelled(self):
        sale = self._dispatched_sale()

        adapter = self._run_notice(sale["id"])

        self.assertEqual(adapter.sent, [])
        self.assertEqual(self._event_count("tcp_cancel_notified"), 0)

    def test_handler_skips_without_a_telegram_target(self):
        sale = self._cancelled_dispatched_sale()
        self.conn.execute(
            "UPDATE suppliers SET telegram_id = NULL, telegram_peer_id = NULL WHERE id = 1"
        )
        self.conn.commit()

        adapter = self._run_notice(sale["id"], repeat=False)

        self.assertEqual(adapter.sent, [])
        self.assertEqual(self._event_count("tcp_cancel_skipped"), 1)

    def test_handler_lets_pacing_requeue_the_job(self):
        import asyncio
        from unittest.mock import patch

        from cuadra.ai import handlers, queue
        from cuadra.channels import telegram_user

        sale = self._cancelled_dispatched_sale()
        with (
            patch.object(telegram_user, "available", return_value=True),
            patch.object(handlers.channels, "get_adapter", return_value=object()),
            patch.object(
                handlers,
                "_check_token_bucket",
                side_effect=queue.RateLimited(
                    30, "userbot send gap", account=False, penalize=False
                ),
            ),
        ):
            with self.assertRaises(queue.RateLimited):
                asyncio.run(handlers.notify_tcp_cancel({"sale_id": sale["id"]}))

        self.assertEqual(self._event_count("tcp_cancel_failed"), 0)


class CancelCustomerNoticeTests(Phase3TestCase):
    """A cancelled sale tells the customer, through the regular outbox."""

    def _customer_jobs(self) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE type = 'notify_sale_cancelled'"
        ).fetchone()[0]

    def _send_jobs(self) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE type = 'send_message'"
        ).fetchone()[0]

    def _outbound_messages(self):
        return self.conn.execute(
            "SELECT * FROM messages WHERE direction = 'OUTBOUND' ORDER BY id"
        ).fetchall()

    def _event_count(self, event_type: str) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM events WHERE type = ?", (event_type,)
        ).fetchone()[0]

    def _run_notice(self, sale_id, repeat=True):
        import asyncio

        from cuadra.ai import handlers

        asyncio.run(handlers.notify_sale_cancelled({"sale_id": sale_id}))
        if repeat:
            # A requeued job must not duplicate the message nor its send job.
            asyncio.run(handlers.notify_sale_cancelled({"sale_id": sale_id}))

    def test_cancelling_a_sale_with_a_conversation_queues_the_notice(self):
        from cuadra.api import routes_finance

        sale = self.create_sale()
        routes_finance.cancel_sale(sale["id"])

        self.assertEqual(self._customer_jobs(), 1)

    def test_cancelling_a_sale_without_a_conversation_queues_nothing(self):
        from cuadra.api import routes_finance

        sale = self.create_sale(conversation_id=None)
        routes_finance.cancel_sale(sale["id"])

        self.assertEqual(self._customer_jobs(), 0)

    def test_handler_stores_one_message_and_one_send_job(self):
        from cuadra.api import routes_finance

        sale = self.create_sale()
        routes_finance.cancel_sale(sale["id"])

        self._run_notice(sale["id"])

        messages = self._outbound_messages()
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["conversation_id"], 1)
        self.assertEqual(messages[0]["external_id"], f"sale-cancelled-{sale['id']}")
        self.assertIn(sale["order_code"], messages[0]["body"])
        job = self.conn.execute(
            "SELECT payload_json, status FROM jobs WHERE type = 'send_message'"
        ).fetchone()
        self.assertEqual(job["status"], "PENDING")
        self.assertEqual(
            json.loads(job["payload_json"])["message_id"], messages[0]["id"]
        )
        self.assertEqual(self._send_jobs(), 1)
        self.assertEqual(self._event_count("sale_cancelled_notified"), 1)

    def test_handler_recovers_a_message_whose_send_job_was_lost(self):
        from cuadra.api import routes_finance

        sale = self.create_sale()
        routes_finance.cancel_sale(sale["id"])
        self._run_notice(sale["id"], repeat=False)
        self.conn.execute("DELETE FROM jobs WHERE type = 'send_message'")
        self.conn.commit()

        self._run_notice(sale["id"])

        self.assertEqual(len(self._outbound_messages()), 1)
        self.assertEqual(self._send_jobs(), 1)

    def test_handler_ignores_a_sale_that_is_not_cancelled(self):
        sale = self.create_sale()

        self._run_notice(sale["id"])

        self.assertEqual(self._outbound_messages(), [])
        self.assertEqual(self._send_jobs(), 0)
        self.assertEqual(self._event_count("sale_cancelled_notified"), 0)

    def test_handler_ignores_a_cancelled_sale_without_a_conversation(self):
        sale = self.create_sale(conversation_id=None)
        sales.cancel(self.conn, sale["id"], "cliente cambió de idea")

        self._run_notice(sale["id"])

        self.assertEqual(self._outbound_messages(), [])
        self.assertEqual(self._send_jobs(), 0)


class FollowupTests(Phase3TestCase):
    """Dashboard reminder for sales left open too long (no message is sent)."""

    def _age(self, sale_id, hours):
        self.conn.execute(
            "UPDATE sales SET updated_at = datetime('now', ?) WHERE id = ?",
            (f"-{hours} hours", sale_id),
        )
        self.conn.commit()

    def test_a_fresh_sale_is_not_a_followup(self):
        self.create_sale()

        self.assertEqual(sales.followups(self.conn, hours=24), [])

    def test_a_stale_open_sale_appears_with_its_age_and_links(self):
        sale = self.create_sale()
        self._age(sale["id"], 30)

        items = sales.followups(self.conn, hours=24)

        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item["order_code"], sale["order_code"])
        self.assertEqual(item["product_name"], "Producto 1")
        self.assertEqual(item["supplier_name"], "TCP Uno")
        self.assertEqual(item["status"], "PENDING_APPROVAL")
        self.assertGreaterEqual(item["age_hours"], 29.5)
        self.assertLessEqual(item["age_hours"], 30.5)

    def test_cancelled_sales_are_not_followups(self):
        sale = self.create_sale()
        self._age(sale["id"], 48)
        sales.cancel(self.conn, sale["id"], "cliente cambió de idea")

        self.assertEqual(sales.followups(self.conn, hours=24), [])

    def test_needs_operator_sales_do_appear(self):
        sale = self.create_sale()
        sales.approve(self.conn, sale["id"])
        sales.mark_needs_operator(self.conn, sale["id"], "dispatch failed")
        self._age(sale["id"], 30)

        items = sales.followups(self.conn, hours=24)

        self.assertEqual([item["status"] for item in items], ["NEEDS_OPERATOR"])

    def test_the_threshold_and_order_are_respected(self):
        old = self.create_sale()
        recent = self.create_sale()
        self._age(old["id"], 50)
        self._age(recent["id"], 30)

        self.assertEqual(
            [item["id"] for item in sales.followups(self.conn, hours=24)],
            [old["id"], recent["id"]],
        )
        self.assertEqual(
            [item["id"] for item in sales.followups(self.conn, hours=40)],
            [old["id"]],
        )
        self.assertEqual(
            sales.followups(self.conn, hours=24, limit=1)[0]["id"], old["id"]
        )

    def test_the_api_returns_hours_and_items(self):
        from cuadra.api import routes_finance

        sale = self.create_sale()
        self._age(sale["id"], 30)

        result = routes_finance.followup_sales(hours=24, limit=50)

        self.assertEqual(result["hours"], 24)
        self.assertEqual([item["id"] for item in result["items"]], [sale["id"]])


class FollowupMessageTests(Phase3TestCase):
    """`sale_followup` v2: one truthful reminder, only where the customer waits."""

    def _age(self, sale_id, hours):
        self.conn.execute(
            "UPDATE sales SET updated_at = datetime('now', ?) WHERE id = ?",
            (f"-{hours} hours", sale_id),
        )
        self.conn.commit()

    def _confirmed(self):
        sale = self.create_sale()
        sales.approve(self.conn, sale["id"])
        sales.mark_dispatched(self.conn, sale["id"], "tcp-1")
        sales.confirm(self.conn, sale["id"])
        return sale

    def _needs_operator(self):
        sale = self.create_sale()
        sales.approve(self.conn, sale["id"])
        sales.mark_needs_operator(self.conn, sale["id"], "AGOTADO")
        return sale

    def _set_conversation(self, status):
        self.conn.execute(
            "UPDATE conversations SET status = ? WHERE id = 1", (status,)
        )
        self.conn.commit()

    def _messages(self):
        return self.conn.execute(
            "SELECT * FROM messages WHERE direction = 'OUTBOUND' ORDER BY id"
        ).fetchall()

    def _jobs(self, job_type):
        return self.conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE type = ?", (job_type,)
        ).fetchone()[0]

    def _events(self, event_type):
        return self.conn.execute(
            "SELECT * FROM events WHERE type = ?", (event_type,)
        ).fetchall()

    def _run(self, sale_id, trigger="manual"):
        import asyncio

        from cuadra.ai import handlers

        asyncio.run(
            handlers.sale_followup({"sale_id": sale_id, "trigger": trigger})
        )

    # ------------------------------------------------------------ candidates

    def test_a_confirmed_sale_is_due_after_the_threshold(self):
        sale = self._confirmed()
        self._age(sale["id"], 30)

        ids = [item["id"] for item in sales.followup_candidates(self.conn, hours=24)]

        self.assertEqual(ids, [sale["id"]])

    def test_internal_latency_states_are_not_message_candidates(self):
        for status in ("PENDING_APPROVAL", "PENDING_DISPATCH", "DISPATCHED"):
            sale = self.create_sale()
            if status in ("PENDING_DISPATCH", "DISPATCHED"):
                sales.approve(self.conn, sale["id"])
            if status == "DISPATCHED":
                sales.mark_dispatched(self.conn, sale["id"], "tcp-1")
            self._age(sale["id"], 30)
            self.assertEqual(sales.followup_candidates(self.conn, hours=24), [])

    def test_needs_operator_is_due_too(self):
        sale = self._needs_operator()
        self._age(sale["id"], 30)

        ids = [item["id"] for item in sales.followup_candidates(self.conn, hours=24)]

        self.assertEqual(ids, [sale["id"]])

    def test_a_customer_message_resets_the_clock(self):
        sale = self._confirmed()
        self._age(sale["id"], 40)
        conversations.add_message(self.conn, 1, "INBOUND", "customer", body="¿ya va?")

        self.assertEqual(sales.followup_candidates(self.conn, hours=24), [])

    def test_closed_conversations_are_not_message_candidates(self):
        sale = self._confirmed()
        self._age(sale["id"], 30)
        self._set_conversation("CLOSED")

        self.assertEqual(sales.followup_candidates(self.conn, hours=24), [])

    def test_muted_and_already_reminded_sales_never_come_back(self):
        sale = self._confirmed()
        self._age(sale["id"], 30)

        sales.mute_followup(self.conn, sale["id"])
        self.assertEqual(sales.followup_candidates(self.conn, hours=24), [])

        sales.mute_followup(self.conn, sale["id"], muted=False)
        self.assertEqual(
            [item["id"] for item in sales.followup_candidates(self.conn, hours=24)],
            [sale["id"]],
        )

        sales.mark_followup_sent(self.conn, sale["id"])
        self.assertEqual(sales.followup_candidates(self.conn, hours=24), [])

    # --------------------------------------------------------------- handler

    def test_handler_stores_the_message_and_the_send_job(self):
        sale = self._confirmed()

        self._run(sale["id"])

        messages = self._messages()
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["external_id"], f"sale-followup-{sale['id']}")
        self.assertIn(sale["order_code"], messages[0]["body"])
        self.assertEqual(self._jobs("send_message"), 1)
        self.assertEqual(len(self._events("sale_followup_sent")), 1)
        saved = sales.get_sale(self.conn, sale["id"])
        self.assertIsNotNone(saved["followup_sent_at"])

    def test_handler_is_idempotent_on_retry(self):
        sale = self._confirmed()

        self._run(sale["id"])
        self._run(sale["id"])

        self.assertEqual(len(self._messages()), 1)
        self.assertEqual(self._jobs("send_message"), 1)
        self.assertEqual(len(self._events("sale_followup_sent")), 1)

    def test_handler_noops_when_the_sale_left_the_reminder_states(self):
        sale = self._confirmed()
        sales.deliver(self.conn, sale["id"])

        self._run(sale["id"])

        self.assertEqual(self._messages(), [])

    def test_handler_noops_on_a_closed_conversation(self):
        sale = self._confirmed()
        self._set_conversation("CLOSED")

        self._run(sale["id"])

        self.assertEqual(self._messages(), [])

    def test_muted_sale_is_skipped_by_the_scheduler_but_not_by_the_operator(self):
        sale = self._needs_operator()
        sales.mute_followup(self.conn, sale["id"])
        settings.set_sales_followup_mode(self.conn, "on")

        self._run(sale["id"], trigger="scheduler")
        self.assertEqual(self._messages(), [])

        self._run(sale["id"], trigger="manual")
        self.assertEqual(len(self._messages()), 1)

    def test_a_parked_scheduler_job_respects_the_mode_switch(self):
        sale = self._confirmed()

        # Mode is off by default: a scheduler job queued earlier must not send.
        self._run(sale["id"], trigger="scheduler")
        self.assertEqual(self._messages(), [])

        settings.set_sales_followup_mode(self.conn, "on")
        self._run(sale["id"], trigger="scheduler")
        self.assertEqual(len(self._messages()), 1)

    # ----------------------------------------------------------------- sweep

    def test_the_sweep_does_nothing_when_the_mode_is_off(self):
        from cuadra.scheduler import maintenance

        sale = self._confirmed()
        self._age(sale["id"], 30)

        maintenance.queue_sale_followups()

        self.assertEqual(self._jobs("sale_followup"), 0)

    def test_shadow_mode_logs_once_without_sending(self):
        from cuadra.scheduler import maintenance

        sale = self._confirmed()
        self._age(sale["id"], 30)
        settings.set_sales_followup_mode(self.conn, "shadow")

        maintenance.queue_sale_followups()
        maintenance.queue_sale_followups()

        self.assertEqual(self._jobs("sale_followup"), 0)
        self.assertEqual(self._messages(), [])
        self.assertEqual(len(self._events("sale_followup_due")), 1)

    def test_on_mode_enqueues_one_job_per_sale_without_duplicates(self):
        from cuadra.scheduler import maintenance

        sale = self._confirmed()
        self._age(sale["id"], 30)
        settings.set_sales_followup_mode(self.conn, "on")

        maintenance.queue_sale_followups()
        maintenance.queue_sale_followups()

        self.assertEqual(self._jobs("sale_followup"), 1)
        self.assertEqual(len(self._events("sale_followup_queued")), 1)

    def test_quiet_hours_wait_until_the_morning(self):
        from datetime import datetime, timezone
        from zoneinfo import ZoneInfo

        from cuadra.scheduler import maintenance

        havana = ZoneInfo("America/Havana")
        midday = datetime(2026, 11, 12, 17, 0, tzinfo=timezone.utc)  # 12:00 Havana
        night = datetime(2026, 11, 12, 8, 0, tzinfo=timezone.utc)  # 03:00 Havana

        self.assertIsNone(maintenance._next_send_window(midday))
        target = maintenance._next_send_window(night)
        self.assertIsNotNone(target)
        self.assertEqual(target.astimezone(havana).hour, 9)

    # ------------------------------------------------------------------- API

    def test_the_manual_route_enqueues_the_job(self):
        from cuadra.api import routes_finance

        sale = self._confirmed()

        routes_finance.send_followup(sale["id"])

        self.assertEqual(self._jobs("sale_followup"), 1)

    def test_the_manual_route_rejects_other_statuses_and_a_second_reminder(self):
        from fastapi import HTTPException

        from cuadra.api import routes_finance

        fresh = self.create_sale()
        with self.assertRaises(HTTPException) as ctx:
            routes_finance.send_followup(fresh["id"])
        self.assertEqual(ctx.exception.status_code, 409)

        sale = self._confirmed()
        sales.mark_followup_sent(self.conn, sale["id"])
        with self.assertRaises(HTTPException) as ctx:
            routes_finance.send_followup(sale["id"])
        self.assertEqual(ctx.exception.status_code, 409)

    def test_mute_route_and_followup_settings(self):
        from cuadra.api import routes_finance, routes_settings
        from cuadra.api.models import FollowupSettingsIn

        sale = self._confirmed()

        muted = routes_finance.mute_followup(sale["id"])
        self.assertIsNotNone(muted["followup_muted_at"])

        self.assertEqual(routes_settings.get_followup_settings(), {"mode": "off"})
        self.assertEqual(
            routes_settings.set_followup_settings(FollowupSettingsIn(mode="shadow")),
            {"mode": "shadow"},
        )
        self.assertEqual(
            routes_settings.get_followup_settings(), {"mode": "shadow"}
        )


if __name__ == "__main__":
    unittest.main()
