"""L3.2: sales, offers, zones and logistics as grounded reads.

Throwaway SQLite database, no model server. Run from `backend/`:

    .venv\\Scripts\\python.exe -m unittest tests.test_sales_tools -v
"""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from cuadra import config, db
from cuadra.ai import grounding
from cuadra.ai.orchestrator import _sales_evidence
from cuadra.ai.tools import base
from cuadra.ai.tools import sales as sales_tools
from cuadra.domain import catalog, conversations, sales


def _frame(conn, conversation_id=1):
    return SimpleNamespace(
        conn=conn,
        cache={},
        conversation={"id": conversation_id},
        turn_id=None,
        evidence=[],
        context={},
    )


class _SeededDB(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_db_path = config.DB_PATH
        config.DB_PATH = Path(self._tmp.name) / "test.db"
        self.conn = db.connect()
        db.apply_migrations(self.conn)
        self.registry = base.Registry()
        sales_tools.register(self.registry)
        self._seed()

    def tearDown(self):
        self.conn.close()
        config.DB_PATH = self._old_db_path
        self._tmp.cleanup()

    def _seed(self):
        conn = self.conn
        conn.execute(
            """INSERT INTO suppliers
               (id, name, telegram_id, telegram_peer_id, delivery_zone)
               VALUES (1, 'TCP Uno', '111222333', '999888777', 'La Habana')"""
        )
        conn.executemany(
            "INSERT INTO zones (id, name, active) VALUES (?, ?, 1)",
            [(1, "Playa"), (2, "Marianao"), (3, "Centro")],
        )
        conn.executemany(
            """INSERT INTO supplier_delivery_zones (supplier_id, zone_id)
               VALUES (1, ?)""",
            [(1,), (2,)],
        )
        conn.execute("INSERT INTO categories (id, name) VALUES (1, 'tecnología')")
        conn.execute(
            """INSERT INTO products
               (id, supplier_id, category_id, name, description, price, currency,
                commission, commission_currency, commission_type, stock,
                stock_reserved, status)
               VALUES (1, 1, 1, 'Laptop Ultraligera', 'Ryzen 7', 720, 'USD', 2,
                       'USD', 'FIXED', 5, 0, 'ACTIVE')"""
        )
        conn.execute(
            """INSERT INTO customers (id, name, preferences_json)
               VALUES (1, 'Cliente', '{}')"""
        )
        conn.execute(
            """INSERT INTO conversations (id, customer_id, channel, status)
               VALUES (1, 1, 'telegram', 'ACTIVE')"""
        )
        conn.commit()
        self.sale = sales.create_sale(
            conn,
            conversation_id=1,
            customer_id=1,
            product_id=1,
            quantity=1,
            recipient_name="Cliente",
            delivery_zone="Playa",
            delivery_address="Calle 1 #2",
            customer_phone="55500001",
            mode="autonomous",
        )


class SalesToolsTestCase(_SeededDB):
    # ---------------------------------------------------------------- sales
    def test_status_by_id_and_code(self):
        by_id = self.registry.run(
            "sales.status", {"sale_id": self.sale["id"]}, _frame(self.conn)
        )
        self.assertTrue(by_id.ok)
        self.assertEqual(by_id.data["order_code"], self.sale["order_code"])
        self.assertEqual(by_id.data["status"], self.sale["status"])
        self.assertNotIn("commission", by_id.data)
        self.assertTrue(by_id.evidence)

        by_code = self.registry.run(
            "sales.status",
            {"order_code": self.sale["order_code"]},
            _frame(self.conn),
        )
        self.assertEqual(by_code.data["sale_id"], self.sale["id"])

    def test_status_by_conversation(self):
        result = self.registry.run(
            "sales.status", {"conversation_id": 1}, _frame(self.conn)
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.data["sale_id"], self.sale["id"])

    def test_status_without_sale_is_ok_with_a_reason(self):
        result = self.registry.run(
            "sales.status", {"conversation_id": 99}, _frame(self.conn)
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.data, {"empty_reason": "no_sale"})
        self.assertEqual(result.empty_reason, "no_sale")

    def test_history_lists_the_conversation(self):
        result = self.registry.run(
            "sales.history", {"conversation_id": 1, "limit": 5}, _frame(self.conn)
        )
        self.assertTrue(result.ok)
        self.assertEqual(len(result.data["sales"]), 1)
        self.assertEqual(result.data["sales"][0]["sale_id"], self.sale["id"])

    # --------------------------------------------------------------- offers
    def test_pending_offer_is_read_from_the_context(self):
        conversations.set_context(
            self.conn,
            1,
            {
                "pending_offer": {
                    "kind": "alternatives",
                    "product_id": 1,
                    "product_ids": [2, 3],
                    "query": "laptop",
                }
            },
        )
        result = self.registry.run(
            "offers.pending", {"conversation_id": 1}, _frame(self.conn)
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.data["kind"], "alternatives")
        self.assertEqual(result.data["product_ids"], [2, 3])
        self.assertEqual(result.data["query"], "laptop")

    def test_no_pending_offer_has_a_reason(self):
        result = self.registry.run(
            "offers.pending", {"conversation_id": 1}, _frame(self.conn)
        )
        self.assertEqual(result.empty_reason, "no_pending_offer")

    # ---------------------------------------------------------------- zones
    def test_zones_check_covered(self):
        result = self.registry.run(
            "zones.check", {"product_id": 1, "zone": "Playa"}, _frame(self.conn)
        )
        self.assertTrue(result.ok)
        self.assertTrue(result.data["covered"])
        self.assertEqual(result.data["source"], "supplier")
        self.assertIn("Marianao", result.data["effective_zones"])

    def test_zones_check_not_covered(self):
        result = self.registry.run(
            "zones.check", {"product_id": 1, "zone": "Centro"}, _frame(self.conn)
        )
        self.assertFalse(result.data["covered"])

    def test_zones_check_unknown_zone(self):
        result = self.registry.run(
            "zones.check", {"product_id": 1, "zone": "Narnia"}, _frame(self.conn)
        )
        self.assertTrue(result.ok)
        self.assertIsNone(result.data["covered"])
        self.assertEqual(result.empty_reason, "unknown_zone")

    def test_zones_list(self):
        result = self.registry.run("zones.list", {"product_id": 1}, _frame(self.conn))
        self.assertEqual(set(result.data["zones"]), {"Playa", "Marianao"})

    def test_zones_list_unknown_product_is_a_tool_error(self):
        result = self.registry.run("zones.list", {"product_id": 99}, _frame(self.conn))
        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, base.NOT_FOUND)

    # ------------------------------------------------------------ logistics
    def test_eta_without_tcp_is_not_invented(self):
        result = self.registry.run(
            "logistics.eta", {"product_id": 1}, _frame(self.conn)
        )
        self.assertTrue(result.ok)
        self.assertIsNone(result.data["eta"])
        self.assertIsNone(result.data["source"])
        self.assertEqual(result.empty_reason, "no_tcp_eta")

    def test_all_six_reads_are_registered_as_read(self):
        names = {spec.name for spec in self.registry.specs()}
        self.assertEqual(
            names,
            {
                "sales.status",
                "sales.history",
                "offers.pending",
                "zones.check",
                "zones.list",
                "logistics.eta",
            },
        )
        self.assertTrue(
            all(spec.permission == base.READ for spec in self.registry.specs())
        )


class SalesEvidenceTests(_SeededDB):
    """The orchestrator records the reads that back status/zones/dates."""

    def _product(self):
        return catalog.get_product(self.conn, 1)

    def _evidence(self, *, text, stage, context=None, sale=None):
        return _sales_evidence(
            self.conn,
            context=context or {},
            text=text,
            product=self._product(),
            stage=stage,
            sale=sale,
        )

    def test_post_sale_turn_includes_the_sale_read(self):
        evidence = self._evidence(
            text="¿Ya llegó?",
            stage="DONE",
            context={
                "sale_id": self.sale["id"],
                "order_code": self.sale["order_code"],
            },
        )
        status = [item for item in evidence if item["tool"] == "sales.status"]
        self.assertEqual(status[0]["sale_id"], self.sale["id"])
        self.assertEqual(status[0]["status"], self.sale["status"])

    def test_zone_turn_includes_the_zone_read(self):
        evidence = self._evidence(text="¿Entregan en Playa?", stage="QUOTE")
        zones = [item for item in evidence if item["tool"] == "zones.check"]
        self.assertTrue(zones)
        self.assertTrue(zones[0]["covered"])

    def test_uncovered_zone_is_recorded(self):
        evidence = self._evidence(text="¿Entregan en Centro?", stage="QUOTE")
        zones = [item for item in evidence if item["tool"] == "zones.check"]
        self.assertFalse(zones[0]["covered"])

    def test_coverage_question_without_a_zone_lists_effective_zones(self):
        evidence = self._evidence(text="¿Puedes entregar en mi zona?", stage="QUOTE")
        listed = [item for item in evidence if item["tool"] == "zones.list"]
        self.assertTrue(listed)
        self.assertIn("Playa", listed[0]["zones"])

    def test_logistics_question_records_no_tcp_eta(self):
        evidence = self._evidence(text="¿Cuándo llega?", stage="QUOTE")
        eta = [item for item in evidence if item["tool"] == "logistics.eta"]
        self.assertEqual(eta[0]["empty_reason"], "no_tcp_eta")
        self.assertIsNone(eta[0]["source"])

    def test_done_without_context_sale_records_no_sale(self):
        evidence = self._evidence(text="¿Ya llegó?", stage="DONE")
        status = [item for item in evidence if item["tool"] == "sales.status"]
        self.assertEqual(status[0]["empty_reason"], "no_sale")


class GroundingL32Tests(unittest.TestCase):
    def _frame(self, evidence=None):
        return SimpleNamespace(context={}, evidence=evidence or [])

    def test_status_claim_needs_a_sale_read(self):
        report = grounding.validate_claims(
            "Tu pedido V-000001 ya fue entregado.", self._frame()
        )
        self.assertFalse(report.ok)
        self.assertIn("status", {violation.kind for violation in report.violations})

    def test_status_claim_with_a_sale_read_is_grounded(self):
        report = grounding.validate_claims(
            "Tu pedido V-000001 ya fue entregado.",
            self._frame(
                [{"tool": "sales.status", "sale_id": 1, "status": "DELIVERED"}]
            ),
        )
        self.assertNotIn(
            "status", {violation.kind for violation in report.violations}
        )

    def test_coverage_claim_needs_a_zone_read(self):
        report = grounding.validate_claims("Sí entregamos en Marianao.", self._frame())
        self.assertIn(
            "coverage", {violation.kind for violation in report.violations}
        )

    def test_coverage_claim_with_a_zone_read_is_grounded(self):
        report = grounding.validate_claims(
            "Sí entregamos en Marianao.",
            self._frame([{"tool": "zones.check", "zone_id": 2, "covered": True}]),
        )
        self.assertNotIn(
            "coverage", {violation.kind for violation in report.violations}
        )

    def test_date_claim_needs_a_tcp_eta(self):
        report = grounding.validate_claims("Te llega mañana.", self._frame())
        self.assertIn("date", {violation.kind for violation in report.violations})

    def test_date_claim_with_a_tcp_eta_is_grounded(self):
        report = grounding.validate_claims(
            "Te llega mañana.",
            self._frame(
                [{"tool": "logistics.eta", "source": "tcp", "eta": "mañana"}]
            ),
        )
        self.assertNotIn("date", {violation.kind for violation in report.violations})

    def test_a_coordinated_date_is_not_a_promise(self):
        report = grounding.validate_claims(
            "La fecha de entrega se coordina al confirmar el pedido; el almacén "
            "confirma el día. No quiero darte un plazo que no pueda cumplir.",
            self._frame(),
        )
        self.assertNotIn("date", {violation.kind for violation in report.violations})


if __name__ == "__main__":
    unittest.main()
