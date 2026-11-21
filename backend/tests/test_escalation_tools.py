"""L3.4: escalations as grounded reads.

Throwaway SQLite database, no model server. Run from `backend/`:

    .venv\\Scripts\\python.exe -m unittest tests.test_escalation_tools -v
"""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from cuadra import config, db
from cuadra.ai import grounding
from cuadra.ai.orchestrator import _escalation_evidence
from cuadra.ai.tools import base
from cuadra.ai.tools import escalations as escalation_tools
from cuadra.domain import escalations


def _frame(conn, conversation_id=1, evidence=None):
    return SimpleNamespace(
        conn=conn,
        cache={},
        conversation={"id": conversation_id},
        turn_id=None,
        evidence=evidence or [],
        context={},
    )


class EscalationToolsTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_db_path = config.DB_PATH
        config.DB_PATH = Path(self._tmp.name) / "test.db"
        self.conn = db.connect()
        db.apply_migrations(self.conn)
        self.registry = base.Registry()
        escalation_tools.register(self.registry)
        self._seed()
        self.escalation, _ = escalations.create(
            self.conn,
            conversation_id=1,
            supplier_id=1,
            question="¿Tiene garantía?",
            product_id=1,
        )

    def tearDown(self):
        self.conn.close()
        config.DB_PATH = self._old_db_path
        self._tmp.cleanup()

    def _seed(self):
        conn = self.conn
        conn.execute("INSERT INTO suppliers (id, name) VALUES (1, 'TCP Uno')")
        conn.execute("INSERT INTO categories (id, name) VALUES (1, 'tecnología')")
        conn.execute(
            """INSERT INTO products
               (id, supplier_id, category_id, name, price, currency, commission,
                stock, status)
               VALUES (1, 1, 1, 'Laptop', 720, 'USD', 2, 5, 'ACTIVE')"""
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

    # ---------------------------------------------------------------- status
    def test_status_by_id(self):
        result = self.registry.run(
            "escalations.status",
            {"escalation_id": self.escalation["id"]},
            _frame(self.conn),
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.data["status"], "PENDING")
        self.assertEqual(result.data["question"], "¿Tiene garantía?")
        self.assertEqual(result.data["supplier_name"], "TCP Uno")
        self.assertEqual(result.empty_reason, None)

    def test_status_by_product(self):
        result = self.registry.run(
            "escalations.status", {"product_id": 1}, _frame(self.conn)
        )
        self.assertEqual(result.data["escalation_id"], self.escalation["id"])

    def test_status_without_an_escalation_has_a_reason(self):
        result = self.registry.run(
            "escalations.status", {"product_id": 99}, _frame(self.conn)
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.data, {"empty_reason": "no_escalation"})
        self.assertEqual(result.empty_reason, "no_escalation")

    # --------------------------------------------------------------- pending
    def test_pending_lists_open_escalations(self):
        result = self.registry.run(
            "escalations.pending", {"conversation_id": 1}, _frame(self.conn)
        )
        self.assertTrue(result.ok)
        self.assertEqual(len(result.data["escalations"]), 1)
        self.assertEqual(
            result.data["escalations"][0]["escalation_id"], self.escalation["id"]
        )

    def test_resolved_escalation_is_not_pending(self):
        escalations.resolve(self.conn, self.escalation["id"])
        result = self.registry.run(
            "escalations.pending", {"conversation_id": 1}, _frame(self.conn)
        )
        self.assertEqual(result.data["escalations"], [])
        self.assertEqual(result.empty_reason, "no_escalation")

    def test_an_identical_question_is_not_duplicated(self):
        duplicate, created = escalations.create(
            self.conn,
            conversation_id=1,
            supplier_id=1,
            question="¿Tiene garantía?",
            product_id=1,
        )
        self.assertFalse(created)
        self.assertEqual(duplicate["id"], self.escalation["id"])

    def test_two_reads_are_registered_as_read(self):
        names = {spec.name for spec in self.registry.specs()}
        self.assertEqual(names, {"escalations.status", "escalations.pending"})
        self.assertTrue(
            all(spec.permission == base.READ for spec in self.registry.specs())
        )

    # -------------------------------------------------------------- evidence
    def test_evidence_includes_pending_and_latest_status(self):
        evidence = _escalation_evidence(self.conn, {"id": 1}, {"id": 1})
        tools = {item["tool"] for item in evidence}
        self.assertIn("escalations.pending", tools)
        self.assertIn("escalations.status", tools)
        status = [
            item for item in evidence if item["tool"] == "escalations.status"
        ][0]
        self.assertEqual(status["status"], "PENDING")
        self.assertFalse(status["answered"])
        self.assertNotIn("question", status)

    def test_answered_escalation_marks_the_evidence(self):
        escalations.set_answer(self.conn, self.escalation["id"], "Sí, 12 meses.")
        evidence = _escalation_evidence(self.conn, {"id": 1}, {"id": 1})
        status = [
            item for item in evidence if item["tool"] == "escalations.status"
        ][0]
        self.assertTrue(status["answered"])
        self.assertEqual(status["status"], "ANSWERED")


class GroundingEscalationTests(unittest.TestCase):
    def _frame(self, evidence):
        return SimpleNamespace(context={}, evidence=evidence)

    def test_confirmed_claim_needs_an_answered_escalation(self):
        report = grounding.validate_claims(
            "Ya me confirmó el almacén que sí está disponible.",
            self._frame([]),
        )
        self.assertIn(
            "escalation_answer", {v.kind for v in report.violations}
        )

    def test_confirmed_claim_with_an_answer_is_grounded(self):
        report = grounding.validate_claims(
            "Ya me confirmó el almacén que sí está disponible.",
            self._frame(
                [
                    {
                        "tool": "escalations.status",
                        "status": "ANSWERED",
                        "answered": True,
                    }
                ]
            ),
        )
        self.assertNotIn(
            "escalation_answer", {v.kind for v in report.violations}
        )

    def test_already_asked_needs_a_created_escalation(self):
        report = grounding.validate_claims(
            "Ya lo consulté con el almacén.", self._frame([])
        )
        self.assertIn("escalation_asked", {v.kind for v in report.violations})

    def test_already_asked_with_an_escalation_is_grounded(self):
        report = grounding.validate_claims(
            "Ya lo consulté con el almacén.",
            self._frame(
                [{"tool": "escalations.status", "status": "PENDING"}]
            ),
        )
        self.assertNotIn(
            "escalation_asked", {v.kind for v in report.violations}
        )

    def test_still_waiting_needs_a_pending_escalation(self):
        report = grounding.validate_claims(
            "Sigo esperando que el almacén me confirme.", self._frame([])
        )
        self.assertIn(
            "escalation_pending", {v.kind for v in report.violations}
        )

    def test_still_waiting_with_a_pending_escalation_is_grounded(self):
        report = grounding.validate_claims(
            "Sigo esperando que el almacén me confirme.",
            self._frame(
                [
                    {
                        "tool": "escalations.pending",
                        "escalations": [{"status": "PENDING"}],
                    }
                ]
            ),
        )
        self.assertNotIn(
            "escalation_pending", {v.kind for v in report.violations}
        )

    def test_a_conditional_offer_to_consult_is_not_a_claim(self):
        report = grounding.validate_claims(
            "Si quieres, lo consulto con el almacén.", self._frame([])
        )
        self.assertNotIn(
            "escalation_asked", {v.kind for v in report.violations}
        )

    def test_a_future_confirmation_is_not_an_answer(self):
        report = grounding.validate_claims(
            "Ese detalle no lo tengo a mano, pero le consulto al almacén ahora "
            "mismo y te confirmo en un rato.",
            self._frame([]),
        )
        self.assertNotIn(
            "escalation_answer", {v.kind for v in report.violations}
        )
        self.assertNotIn(
            "escalation_asked", {v.kind for v in report.violations}
        )


if __name__ == "__main__":
    unittest.main()
