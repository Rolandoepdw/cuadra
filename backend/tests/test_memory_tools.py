"""L3.3: memory facts, customer profile and summary as grounded reads.

Throwaway SQLite database, no model server. Run from `backend/`:

    .venv\\Scripts\\python.exe -m unittest tests.test_memory_tools -v
"""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from cuadra import config, db
from cuadra.ai import grounding, memory, redact
from cuadra.ai.orchestrator import _memory_evidence
from cuadra.ai.tools import base
from cuadra.ai.tools import memory as memory_tools
from cuadra.domain import conversations


def _frame(conn, conversation_id=1):
    return SimpleNamespace(
        conn=conn,
        cache={},
        conversation={"id": conversation_id},
        turn_id=None,
        evidence=[],
        context={},
    )


class MemoryToolsTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_db_path = config.DB_PATH
        config.DB_PATH = Path(self._tmp.name) / "test.db"
        self.conn = db.connect()
        db.apply_migrations(self.conn)
        self.registry = base.Registry()
        memory_tools.register(self.registry)
        self._seed()

    def tearDown(self):
        self.conn.close()
        config.DB_PATH = self._old_db_path
        self._tmp.cleanup()

    def _seed(self):
        conn = self.conn
        conn.execute(
            """INSERT INTO customers (id, name, preferences_json)
               VALUES (1, 'Cliente', '{"name": "Cliente", "usual_zone": "Playa"}')"""
        )
        conn.executemany(
            """INSERT INTO conversations (id, customer_id, channel, status)
               VALUES (?, 1, 'telegram', 'ACTIVE')""",
            [(1,), (2,)],
        )
        conn.commit()
        memory.upsert_fact(
            conn, 1, memory.FACT_CUSTOMER_NAME, "Cliente", confidence=0.8
        )
        memory.upsert_fact(
            conn, 1, memory.FACT_ADDRESS, "Calle 1 #2, Vedado", confidence=0.8
        )
        memory.upsert_fact(
            conn, 1, memory.FACT_PHONE, "55500001", confidence=0.8
        )

    # ----------------------------------------------------------------- facts
    def test_facts_returns_values_with_updated_at(self):
        result = self.registry.run(
            "memory.facts", {"conversation_id": 1}, _frame(self.conn)
        )
        self.assertTrue(result.ok)
        by_key = {row["key"]: row for row in result.data["facts"]}
        self.assertEqual(by_key["customer_name"]["value"], "Cliente")
        self.assertIn(by_key["delivery_address"]["value"], "Calle 1 #2, Vedado")
        self.assertTrue(by_key["customer_name"]["updated_at"])
        self.assertEqual(result.empty_reason, None)

    def test_facts_are_per_conversation(self):
        result = self.registry.run(
            "memory.facts", {"conversation_id": 2}, _frame(self.conn, 2)
        )
        self.assertEqual(result.data["facts"], [])
        self.assertEqual(result.empty_reason, "no_facts")

    def test_a_correction_replaces_the_current_fact(self):
        memory.upsert_fact(self.conn, 1, memory.FACT_ADDRESS, "Calle 2 #3, Centro")
        result = self.registry.run(
            "memory.facts", {"conversation_id": 1}, _frame(self.conn)
        )
        by_key = {row["key"]: row["value"] for row in result.data["facts"]}
        self.assertEqual(by_key["delivery_address"], "Calle 2 #3, Centro")

    def test_memory_evidence_carries_keys_not_values(self):
        evidence = _memory_evidence(self.conn, {"id": 1})
        item = evidence[0]
        self.assertEqual(item["tool"], "memory.facts")
        self.assertIn("customer_name", item["present"])
        self.assertIn("delivery_address", item["present"])
        self.assertNotIn("value", item)

    # --------------------------------------------------------------- profile
    def test_profile_returns_preferences_across_conversations(self):
        result = self.registry.run(
            "memory.profile", {"customer_id": 1}, _frame(self.conn)
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.data["profile"]["nombre"], "Cliente")
        self.assertEqual(result.data["profile"]["zona_habitual"], "Playa")
        self.assertEqual(result.empty_reason, None)

    def test_profile_without_customer_has_a_reason(self):
        result = self.registry.run(
            "memory.profile", {"customer_id": 99}, _frame(self.conn)
        )
        self.assertEqual(result.empty_reason, "no_profile")

    # --------------------------------------------------------------- summary
    def test_summary_is_read_from_the_context(self):
        conversations.set_context(
            self.conn,
            1,
            {"summary": "El cliente busca una laptop.", "summary_through_message_id": 10},
        )
        result = self.registry.run(
            "memory.summary", {"conversation_id": 1}, _frame(self.conn)
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.data["summary"], "El cliente busca una laptop.")
        self.assertEqual(result.data["through_message_id"], 10)

    def test_summary_without_text_has_a_reason(self):
        result = self.registry.run(
            "memory.summary", {"conversation_id": 2}, _frame(self.conn, 2)
        )
        self.assertEqual(result.empty_reason, "no_summary")

    def test_all_three_reads_are_registered_as_read(self):
        names = {spec.name for spec in self.registry.specs()}
        self.assertEqual(
            names, {"memory.facts", "memory.profile", "memory.summary"}
        )
        self.assertTrue(
            all(spec.permission == base.READ for spec in self.registry.specs())
        )

    # ------------------------------------------------------------------- PII
    def test_tool_output_is_masked_before_tracing(self):
        masked = base._redact(
            {
                "facts": [{"key": "customer_phone", "value": "55500001"}],
                "address": "Calle 1 #2, Vedado",
            }
        )
        text = json.dumps(masked, ensure_ascii=False)
        self.assertNotIn("55500001", text)
        self.assertIn(redact.PHONE, text)
        self.assertIn(redact.ADDRESS, text)

    def test_persisted_tool_trace_does_not_store_pii(self):
        self.registry.run("memory.facts", {"conversation_id": 1}, _frame(self.conn))
        row = self.conn.execute(
            """SELECT output_json FROM traces
               WHERE method = 'tool.memory.facts'
               ORDER BY id DESC LIMIT 1"""
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertNotIn("55500001", row["output_json"])
        self.assertNotIn("Calle 1", row["output_json"])
        self.assertIn(redact.PHONE, row["output_json"])
        self.assertIn(redact.ADDRESS, row["output_json"])


class GroundingMemoryTests(unittest.TestCase):
    def _frame(self, present):
        return SimpleNamespace(
            context={},
            evidence=[{"tool": "memory.facts", "present": present}],
        )

    def test_ya_me_dijiste_needs_the_fact(self):
        report = grounding.validate_claims(
            "Ya me dijiste tu dirección.", self._frame([])
        )
        self.assertIn("memory", {v.kind for v in report.violations})

    def test_ya_me_dijiste_with_the_fact_is_grounded(self):
        report = grounding.validate_claims(
            "Ya me dijiste tu dirección.", self._frame(["delivery_address"])
        )
        self.assertNotIn("memory", {v.kind for v in report.violations})

    def test_no_me_has_dicho_needs_the_fact_absent(self):
        report = grounding.validate_claims(
            "No me has dicho tu nombre.", self._frame([])
        )
        self.assertNotIn("memory", {v.kind for v in report.violations})

    def test_no_me_has_dicho_with_the_fact_present_is_a_violation(self):
        report = grounding.validate_claims(
            "No me has dicho tu nombre.", self._frame(["customer_name"])
        )
        self.assertIn("memory", {v.kind for v in report.violations})

    def test_recuerdo_que_is_checked_too(self):
        report = grounding.validate_claims(
            "Recuerdo que tu zona es Playa.", self._frame([])
        )
        self.assertIn("memory", {v.kind for v in report.violations})

    def test_a_correction_is_never_blocked(self):
        report = grounding.validate_claims(
            "Perfecto, actualicé tu dirección.", self._frame([])
        )
        self.assertNotIn("memory", {v.kind for v in report.violations})


if __name__ == "__main__":
    unittest.main()
