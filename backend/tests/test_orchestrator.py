"""L3.0 orchestrator: lifecycle, watchdog and the compatibility facade.

The watchdog detects a hung turn. It is not a latency budget: when it fires the
turn gets one safe answer and is not retried.

Run from `backend/`:

    .venv\\Scripts\\python.exe -m unittest tests.test_orchestrator -v
"""

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cuadra import config, db
from cuadra.ai import orchestrator, pipeline, prompts
from cuadra.ai.turn.frame import TurnFrame
from cuadra.ai.turn.policy import TurnPlan, plan_turn
from cuadra.domain import conversations


class OrchestratorTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_db_path = config.DB_PATH
        self._old_watchdog = config.AGENT_TURN_WATCHDOG_S
        config.DB_PATH = Path(self._tmp.name) / "test.db"
        conn = db.connect()
        try:
            db.apply_migrations(conn)
            conn.execute("INSERT INTO customers (id, name) VALUES (1, 'Cliente')")
            conn.execute(
                """INSERT INTO conversations (id, customer_id, channel)
                   VALUES (1, 1, 'telegram')"""
            )
            conn.commit()
        finally:
            conn.close()

    def tearDown(self):
        config.DB_PATH = self._old_db_path
        config.AGENT_TURN_WATCHDOG_S = self._old_watchdog
        self._tmp.cleanup()

    def _conn(self):
        return db.connect()

    def _conversation(self, conn):
        return conversations.get_conversation(conn, 1)

    def _turn(self):
        return {
            "id": None,
            "conversation_id": 1,
            "last_message_id": None,
            "message_ids_json": "[]",
        }

    def _rows(self, sql, args=()):
        conn = self._conn()
        try:
            return [dict(row) for row in conn.execute(sql, args).fetchall()]
        finally:
            conn.close()


class FacadeTests(OrchestratorTestCase):
    def test_pipeline_keeps_the_public_entry_points(self):
        self.assertIs(pipeline.handle_turn, orchestrator.handle_turn)
        self.assertIs(pipeline._run_turn, orchestrator.run_turn)
        for name in (
            "_resolve_product",
            "_maybe_handle_sale",
            "_stage_for",
            "_classify_light",
            "_logistics_outcome",
            "_post_sale_outcome",
            "_render_product_card",
            "_guard_context",
        ):
            self.assertTrue(hasattr(pipeline, name), name)

    def test_plan_turn_is_a_no_op_plan_in_l3_0(self):
        frame = TurnFrame(
            conn=None, conversation={"id": 1}, turn={"id": None}, context={}
        )
        plan = plan_turn(frame, None)
        self.assertIsInstance(plan, TurnPlan)
        self.assertEqual(plan.decision, "legacy")
        self.assertEqual(plan.reads, [])


class WatchdogTests(OrchestratorTestCase):
    def test_a_disabled_watchdog_returns_the_turn_result_untouched(self):
        config.AGENT_TURN_WATCHDOG_S = 0
        expected = {"reply": "hola", "outcome": "ok", "intent": {}}

        async def fake_run(conn, conversation, turn):
            return expected

        with patch("cuadra.ai.orchestrator.run_turn", new=fake_run):
            conn = self._conn()
            try:
                result = asyncio.run(
                    orchestrator.handle_turn(
                        conn, self._conversation(conn), self._turn()
                    )
                )
            finally:
                conn.close()
        self.assertEqual(result, expected)

    def test_a_hung_turn_gets_one_safe_answer_and_is_not_retried(self):
        config.AGENT_TURN_WATCHDOG_S = 0.05
        calls = []

        async def hung(conn, conversation, turn):
            calls.append(1)
            await asyncio.sleep(0.5)
            return {"reply": "never", "outcome": "ok", "intent": {}}

        with patch("cuadra.ai.orchestrator.run_turn", new=hung):
            conn = self._conn()
            try:
                result = asyncio.run(
                    orchestrator.handle_turn(
                        conn, self._conversation(conn), self._turn()
                    )
                )
            finally:
                conn.close()

        self.assertEqual(len(calls), 1)
        self.assertEqual(result["outcome"], "timeout")
        self.assertEqual(result["generator"], "watchdog")
        self.assertTrue(result["degraded"])
        self.assertEqual(result["reply"], prompts.TEMPLATES["turn_timeout"])
        events = self._rows(
            "SELECT * FROM events WHERE type = 'turn_timeout'"
        )
        self.assertEqual(len(events), 1)

    def test_the_safe_template_does_not_promise_or_invent(self):
        text = prompts.TEMPLATES["turn_timeout"]
        self.assertNotIn("$", text)
        self.assertNotIn("{", text)
        self.assertIn("no puedo confirmarte", text.lower())


if __name__ == "__main__":
    unittest.main()
