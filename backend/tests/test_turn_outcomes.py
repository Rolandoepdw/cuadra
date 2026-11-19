"""Migration 020: turn outcomes and customer kind (phase F0).

The Conversations panel reads what each turn understood and produced from
`turns`, not from the 30-day `traces` log. These tests cover the write path,
the failure path, the supplier/customer split and the one-shot backfill.

Run from `backend/`:

    .venv\\Scripts\\python.exe -m unittest discover -s tests -v
"""

import asyncio
import importlib.util
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from cuadra import config, db
from cuadra.ai import handlers, ingest, llm, pipeline, queue
from cuadra.domain import conversations, turns


def _past() -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()


def _load_backfill():
    path = Path(__file__).resolve().parents[1] / "scripts" / "backfill_turn_outcomes.py"
    spec = importlib.util.spec_from_file_location("backfill_turn_outcomes", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TurnOutcomeTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_db_path = config.DB_PATH
        config.DB_PATH = Path(self._tmp.name) / "test.db"
        conn = db.connect()
        try:
            db.apply_migrations(conn)
            conn.execute(
                """INSERT INTO suppliers (id, name, delivery_zone)
                   VALUES (1, 'TCP Uno', 'La Habana')"""
            )
            conn.execute(
                """INSERT INTO products
                   (id, supplier_id, name, price, currency, commission, stock, status)
                   VALUES (1, 1, 'Power Bank 10000', 12.5, 'USD', 2, 5, 'ACTIVE')"""
            )
            conn.commit()
        finally:
            conn.close()
        handlers.register()

    def tearDown(self):
        config.DB_PATH = self._old_db_path
        self._tmp.cleanup()

    # --------------------------------------------------------------- helpers

    def _ingest(self, text: str = "ID 1") -> dict:
        return ingest.ingest_incoming(
            channel="telegram",
            identifier="55500001",
            type="text",
            body=text,
        )

    def _rows(self, sql: str, args=()) -> list[dict]:
        conn = db.connect()
        try:
            return [dict(row) for row in conn.execute(sql, args).fetchall()]
        finally:
            conn.close()

    def _scalar(self, sql: str, args=()):
        conn = db.connect()
        try:
            return conn.execute(sql, args).fetchone()[0]
        finally:
            conn.close()

    def _make_due(self) -> None:
        conn = db.connect()
        try:
            conn.execute(
                "UPDATE turns SET due_at = ? WHERE status IN ('OPEN', 'PROCESSING')",
                (_past(),),
            )
            conn.execute(
                """UPDATE jobs SET next_retry_at = ?
                   WHERE type = 'process_turn' AND status = 'PENDING'""",
                (_past(),),
            )
            conn.commit()
        finally:
            conn.close()

    def _run_process(self) -> None:
        conn = db.connect()
        try:
            row = conn.execute(
                """SELECT * FROM jobs
                   WHERE type = 'process_turn' AND status = 'PENDING'
                   ORDER BY id LIMIT 1"""
            ).fetchone()
            self.assertIsNotNone(row, "no process_turn job available")
            conn.execute(
                "UPDATE jobs SET status = 'IN_PROGRESS', updated_at = ? WHERE id = ?",
                (datetime.now(timezone.utc).isoformat(), row["id"]),
            )
            conn.commit()
            job = dict(row)
        finally:
            conn.close()
        asyncio.run(queue._process(job))


class OutcomeWriteTests(TurnOutcomeTestCase):
    def test_a_successful_turn_records_its_outcome_and_reply(self):
        intent = llm.Intent(
            intencion="consulta_precio", producto="Power Bank", confianza=0.95
        )
        with patch("cuadra.ai.llm.classify_light", new=AsyncMock(return_value=intent)):
            info = self._ingest("precio del Power Bank")
            self._make_due()
            self._run_process()

        turn = self._rows("SELECT * FROM turns WHERE id = ?", (info["turn_id"],))[0]
        self.assertEqual(turn["status"], "DONE")
        self.assertEqual(turn["outcome"], "ok")
        self.assertEqual(turn["intent"], "consulta_precio")
        self.assertEqual(turn["product_id"], 1)
        self.assertIsNotNone(turn["stage"])
        self.assertIsNotNone(turn["generator"])
        self.assertEqual(turn["escalated"], 0)
        self.assertIsNotNone(turn["latency_ms"])
        self.assertIsNotNone(turn["intent_ms"])
        self.assertIsNotNone(turn["compose_ms"])
        self.assertIsNotNone(turn["finished_at"])

        outbound_id = self._scalar(
            "SELECT id FROM messages WHERE direction = 'OUTBOUND'"
        )
        self.assertEqual(turn["reply_message_id"], outbound_id)

    def test_an_escalated_turn_sets_the_flag(self):
        intent = llm.Intent(
            intencion="consulta_dato",
            producto="Power Bank",
            datos_faltantes=["garantía"],
            confianza=0.9,
        )
        with patch("cuadra.ai.llm.classify_light", new=AsyncMock(return_value=intent)):
            info = self._ingest("ID 1, ¿qué garantía trae?")
            self._make_due()
            self._run_process()

        turn = self._rows("SELECT * FROM turns WHERE id = ?", (info["turn_id"],))[0]
        self.assertEqual(turn["escalated"], 1)
        self.assertEqual(turn["stage"], "HANDOFF")

    def test_a_failed_turn_is_marked_as_error_and_the_exception_survives(self):
        info = self._ingest("hola")
        conn = db.connect()
        try:
            turn = turns.get(conn, info["turn_id"])
            conversation = conversations.get_conversation(
                conn, info["conversation_id"]
            )
            with patch(
                "cuadra.ai.orchestrator.run_turn",
                new=AsyncMock(side_effect=RuntimeError("boom")),
            ):
                with self.assertRaises(RuntimeError):
                    asyncio.run(
                        pipeline.handle_turn(conn, conversation, turn)
                    )
            stored = turns.get(conn, info["turn_id"])
        finally:
            conn.close()

        self.assertEqual(stored["outcome"], "error")
        self.assertIn("boom", stored["error"])
        self.assertIsNotNone(stored["finished_at"])

    def test_record_outcome_ignores_unknown_keys_and_a_missing_turn(self):
        conn = db.connect()
        try:
            turns.record_outcome(conn, None, intent="otro")
            info = self._ingest("hola")
            turns.record_outcome(
                conn, info["turn_id"], intent="otro", not_a_column="ignored"
            )
            stored = turns.get(conn, info["turn_id"])
        finally:
            conn.close()
        self.assertEqual(stored["intent"], "otro")


class CustomerKindTests(TurnOutcomeTestCase):
    def test_new_customers_default_to_customer(self):
        self.assertEqual(
            self._scalar(
                "SELECT kind FROM customers WHERE id = ?",
                (self._ingest("hola")["customer_id"],),
            ),
            "customer",
        )

    def test_a_tcp_conversation_is_marked_supplier(self):
        conn = db.connect()
        try:
            conn.execute(
                """INSERT INTO suppliers
                   (id, name, telegram_id, telegram_peer_id)
                   VALUES (2, 'TCP Dos', '@tcp2', '900001')"""
            )
            conn.commit()
        finally:
            conn.close()

        info = ingest.ingest_incoming(
            channel="telegram",
            identifier="900001",
            type="text",
            body="RECIBIDO",
        )
        self.assertEqual(info["source"], "supplier")
        kind = self._scalar(
            """SELECT c.kind FROM customers c
               JOIN customer_channels cc ON cc.customer_id = c.id
               WHERE cc.identifier = '900001'"""
        )
        self.assertEqual(kind, "supplier")

    def test_a_customer_promoted_to_supplier_is_never_demoted(self):
        conn = db.connect()
        try:
            first = conversations.get_or_create_customer(
                conn, "telegram", "900002", "Alguien"
            )
            promoted = conversations.get_or_create_customer(
                conn, "telegram", "900002", "Alguien", kind="supplier"
            )
            again = conversations.get_or_create_customer(
                conn, "telegram", "900002", "Alguien", kind="customer"
            )
        finally:
            conn.close()

        self.assertEqual(first["kind"], "customer")
        self.assertEqual(promoted["kind"], "supplier")
        self.assertEqual(again["kind"], "supplier")


class BackfillTests(TurnOutcomeTestCase):
    def _seed_traces(self, turn_id: int) -> None:
        rows = [
            (
                "pipeline._classify_light",
                '{"intencion": "consulta_precio", "confianza": 0.9}',
                1,
                None,
                None,
            ),
            (
                "pipeline._resolve_product",
                '{"product_id": 1, "product_name": "Power Bank 10000"}',
                1,
                None,
                None,
            ),
            (
                "pipeline.handle_turn",
                '{"stage": "QUOTE", "generator": "template", "escalate": false,'
                ' "intent_ms": 100, "compose_ms": 200, "sale_id": null}',
                1,
                None,
                1500.0,
            ),
        ]
        conn = db.connect()
        try:
            for method, output, ok, error, duration in rows:
                conn.execute(
                    """INSERT INTO traces
                       (timestamp, method, output_json, ok, error, duration_ms, turn_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        "2026-11-17T10:00:00+00:00",
                        method,
                        output,
                        ok,
                        error,
                        duration,
                        turn_id,
                    ),
                )
            conn.commit()
        finally:
            conn.close()

    def test_backfill_fills_nulls_and_is_idempotent(self):
        info = self._ingest("hola")
        self._seed_traces(info["turn_id"])
        module = _load_backfill()

        conn = db.connect()
        try:
            first = module.backfill(conn)
            second = module.backfill(conn)
            stored = turns.get(conn, info["turn_id"])
        finally:
            conn.close()

        self.assertEqual(first["turns_updated"], 1)
        self.assertEqual(second["turns_updated"], 0)
        self.assertGreaterEqual(second["turns_unchanged"], 1)
        self.assertEqual(stored["intent"], "consulta_precio")
        self.assertEqual(stored["product_id"], 1)
        self.assertEqual(stored["stage"], "QUOTE")
        self.assertEqual(stored["generator"], "template")
        self.assertEqual(stored["latency_ms"], 1500)
        self.assertEqual(stored["intent_ms"], 100)
        self.assertEqual(stored["compose_ms"], 200)
        self.assertIsNotNone(stored["finished_at"])

    def test_dry_run_reports_but_does_not_write(self):
        info = self._ingest("hola")
        self._seed_traces(info["turn_id"])
        module = _load_backfill()

        conn = db.connect()
        try:
            report = module.backfill(conn, dry_run=True)
            stored = turns.get(conn, info["turn_id"])
        finally:
            conn.close()

        self.assertEqual(report["turns_updated"], 1)
        self.assertIsNone(stored["intent"])
