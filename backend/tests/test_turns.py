"""Phase 1: persistent turns, idempotency, context budget and guardrails.

These tests run against a throwaway SQLite database (never the real
`backend/data/cuadra.db`) and never call a model server.

Run from `backend/`:

    .venv\\Scripts\\python.exe -m unittest discover -s tests -v
"""

import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from cuadra import config, db
from cuadra.ai import (
    assembler,
    context,
    guardrails,
    handlers,
    ingest,
    llm,
    memory,
    pipeline,
    prompts,
    queue,
)
from cuadra.domain import catalog, conversations, settings, turns


def _past() -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()


class Phase1TestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_db_path = config.DB_PATH
        self._old_debounce = config.TURN_DEBOUNCE_S
        self._old_max_wait = config.TURN_MAX_WAIT_S
        config.DB_PATH = Path(self._tmp.name) / "test.db"
        config.TURN_DEBOUNCE_S = 2.5
        config.TURN_MAX_WAIT_S = 8.0
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
        config.TURN_DEBOUNCE_S = self._old_debounce
        config.TURN_MAX_WAIT_S = self._old_max_wait
        self._tmp.cleanup()

    # --------------------------------------------------------------- helpers

    def _ingest(self, text: str, *, external_id: str | None = None) -> dict:
        return ingest.ingest_incoming(
            channel="telegram",
            identifier="55500001",
            type="text",
            body=text,
            external_id=external_id,
        )

    def _scalar(self, sql: str, args=()):
        conn = db.connect()
        try:
            return conn.execute(sql, args).fetchone()[0]
        finally:
            conn.close()

    def _rows(self, sql: str, args=()) -> list[dict]:
        conn = db.connect()
        try:
            return [dict(row) for row in conn.execute(sql, args).fetchall()]
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

    def _claim_job(self, job_type: str = "process_turn") -> dict | None:
        conn = db.connect()
        try:
            row = conn.execute(
                """SELECT * FROM jobs
                   WHERE type = ? AND status = 'PENDING'
                     AND (next_retry_at IS NULL OR next_retry_at <= ?)
                   ORDER BY id LIMIT 1""",
                (job_type, datetime.now(timezone.utc).isoformat()),
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                "UPDATE jobs SET status = 'IN_PROGRESS', updated_at = ? WHERE id = ?",
                (datetime.now(timezone.utc).isoformat(), row["id"]),
            )
            conn.commit()
            return dict(row)
        finally:
            conn.close()

    def _run_process(self) -> None:
        job = self._claim_job("process_turn")
        self.assertIsNotNone(job, "no process_turn job available")
        asyncio.run(queue._process(job))


class BurstTests(Phase1TestCase):
    def test_three_messages_become_one_turn_one_reply(self):
        intent = llm.Intent(intencion="consulta_precio", confianza=0.95)
        with patch(
            "cuadra.ai.llm.classify_light", new=AsyncMock(return_value=intent)
        ) as classify_light:
            self._ingest("Hola")
            self._ingest("busco el Power Bank")
            self._ingest("ID 1")

            self.assertEqual(self._scalar("SELECT COUNT(*) FROM turns"), 1)
            turn = self._rows("SELECT * FROM turns")[0]
            self.assertEqual(len(turns.message_ids(turn)), 3)
            self.assertEqual(
                self._scalar(
                    "SELECT COUNT(*) FROM jobs WHERE type = 'process_turn'"
                ),
                1,
            )

            self._make_due()
            self._run_process()

        self.assertEqual(
            self._scalar(
                "SELECT COUNT(*) FROM messages WHERE direction = 'OUTBOUND'"
            ),
            1,
        )
        self.assertEqual(self._scalar("SELECT status FROM turns"), "DONE")
        classify_light.assert_awaited_once()

    def test_duplicate_external_id_creates_neither_message_nor_turn(self):
        first = self._ingest("Hola", external_id="upd-1")
        second = self._ingest("Hola", external_id="upd-1")

        self.assertFalse(first["duplicate"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(first["message_id"], second["message_id"])
        self.assertEqual(self._scalar("SELECT COUNT(*) FROM messages"), 1)
        self.assertEqual(self._scalar("SELECT COUNT(*) FROM turns"), 1)
        self.assertEqual(
            self._scalar("SELECT COUNT(*) FROM jobs WHERE type = 'process_turn'"),
            1,
        )

    def test_debounce_is_capped_by_max_wait(self):
        first = self._ingest("uno")
        conn = db.connect()
        try:
            created = turns.parse_ts(
                conn.execute(
                    "SELECT created_at FROM turns WHERE id = ?",
                    (first["turn_id"],),
                ).fetchone()[0]
            )
            # Keep re-arming with a huge debounce: the cap still wins.
            seen = assembler.on_inbound(
                conn,
                first["conversation_id"],
                first["message_id"],
                debounce_s=999,
                max_wait_s=8,
            )
        finally:
            conn.close()
        due = turns.parse_ts(seen["due_at"])
        self.assertLessEqual(due, created + timedelta(seconds=8))


class AudioTurnTests(Phase1TestCase):
    def test_audio_is_transcribed_before_the_turn_is_answered(self):
        ingest.ingest_incoming(
            channel="telegram",
            identifier="55500001",
            type="audio",
            file_path="audios/test.ogg",
        )
        self._make_due()

        # process_turn must enqueue STT and leave without replying.
        self._run_process()
        self.assertEqual(
            self._scalar(
                "SELECT COUNT(*) FROM messages WHERE direction = 'OUTBOUND'"
            ),
            0,
        )
        self.assertEqual(
            self._scalar(
                "SELECT COUNT(*) FROM jobs WHERE type = 'transcribe_audio'"
            ),
            1,
        )

        transcript = {
            "text": "Hola, ID 1",
            "language": "es",
            "language_probability": 0.9,
            "duration_s": 1.0,
            "elapsed_s": 0.1,
        }
        with patch(
            "cuadra.ai.stt.transcribe", new=AsyncMock(return_value=transcript)
        ):
            job = self._claim_job("transcribe_audio")
            self.assertIsNotNone(job)
            asyncio.run(queue._process(job))

        # Transcription re-arms the same turn; one reply, one outbound message.
        intent = llm.Intent(intencion="consulta_precio", confianza=0.95)
        with patch("cuadra.ai.llm.classify_light", new=AsyncMock(return_value=intent)):
            self._make_due()
            self._run_process()

        self.assertEqual(
            self._scalar(
                "SELECT COUNT(*) FROM messages WHERE direction = 'OUTBOUND'"
            ),
            1,
        )
        self.assertEqual(self._scalar("SELECT status FROM turns"), "DONE")


class RecoveryTests(Phase1TestCase):
    def test_processing_turn_is_recovered_and_answers_once(self):
        intent = llm.Intent(intencion="consulta_precio", confianza=0.95)
        with patch("cuadra.ai.llm.classify_light", new=AsyncMock(return_value=intent)):
            info = self._ingest("ID 1")
            turn_id = info["turn_id"]

            # Simulate a crash after claiming the turn, before replying.
            conn = db.connect()
            try:
                conn.execute(
                    "UPDATE jobs SET status = 'IN_PROGRESS' WHERE id = ?",
                    (info["job_id"],),
                )
                conn.execute(
                    "UPDATE turns SET status = 'PROCESSING' WHERE id = ?",
                    (turn_id,),
                )
                conn.commit()
            finally:
                conn.close()

            queue.requeue_in_progress()
            self.assertEqual(assembler.recover_open(), 0)

            conn = db.connect()
            try:
                self.assertEqual(turns.get(conn, turn_id)["status"], "OPEN")
            finally:
                conn.close()

            self._make_due()
            self._run_process()

            # A duplicated job after DONE must not produce a second reply.
            queue.enqueue(
                "process_turn",
                {"turn_id": turn_id},
                priority=config.PRIORITY_CUSTOMER,
                available_at=_past(),
            )
            self._run_process()

        self.assertEqual(
            self._scalar(
                "SELECT COUNT(*) FROM messages WHERE direction = 'OUTBOUND'"
            ),
            1,
        )

    def test_open_turn_without_job_is_reenqueued(self):
        info = self._ingest("ID 1")
        conn = db.connect()
        try:
            conn.execute("DELETE FROM jobs WHERE id = ?", (info["job_id"],))
            conn.execute(
                "UPDATE turns SET job_id = NULL WHERE id = ?", (info["turn_id"],)
            )
            conn.commit()
        finally:
            conn.close()

        self.assertEqual(assembler.recover_open(), 1)
        self.assertEqual(
            self._scalar(
                "SELECT COUNT(*) FROM jobs WHERE type = 'process_turn' "
                "AND status = 'PENDING'"
            ),
            1,
        )


class ReplyOutboxTests(Phase1TestCase):
    """Crash windows between storing the reply and enqueueing its delivery."""

    def _finish_one_turn(self) -> dict:
        intent = llm.Intent(intencion="consulta_precio", confianza=0.95)
        with patch(
            "cuadra.ai.llm.classify_light", new=AsyncMock(return_value=intent)
        ) as classify_light:
            info = self._ingest("ID 1")
            self._make_due()
            self._run_process()
            self.assertEqual(classify_light.await_count, 1)
        self.assertEqual(
            self._scalar(
                "SELECT COUNT(*) FROM messages WHERE direction = 'OUTBOUND'"
            ),
            1,
        )
        return info

    def _drop_send_jobs(self) -> None:
        conn = db.connect()
        try:
            conn.execute("DELETE FROM jobs WHERE type = 'send_message'")
            conn.commit()
        finally:
            conn.close()

    def _requeue_turn(self, turn_id: int) -> None:
        queue.enqueue(
            "process_turn",
            {"turn_id": turn_id},
            priority=config.PRIORITY_CUSTOMER,
            available_at=_past(),
        )
        self._run_process()

    def test_done_turn_with_lost_send_job_reenqueues_delivery_only(self):
        info = self._finish_one_turn()
        self._drop_send_jobs()

        self._requeue_turn(info["turn_id"])

        self.assertEqual(
            self._scalar(
                "SELECT COUNT(*) FROM messages WHERE direction = 'OUTBOUND'"
            ),
            1,
        )
        self.assertEqual(
            self._scalar(
                "SELECT COUNT(*) FROM jobs WHERE type = 'send_message' "
                "AND status = 'PENDING'"
            ),
            1,
        )

    def test_processing_turn_with_stored_reply_is_closed_not_answered_twice(self):
        info = self._finish_one_turn()
        conn = db.connect()
        try:
            conn.execute(
                "UPDATE turns SET status = 'PROCESSING' WHERE id = ?",
                (info["turn_id"],),
            )
            conn.commit()
        finally:
            conn.close()
        self._drop_send_jobs()

        with patch(
            "cuadra.ai.llm.classify_light",
            new=AsyncMock(
                side_effect=AssertionError("the turn must not be regenerated")
            ),
        ):
            self._requeue_turn(info["turn_id"])

        self.assertEqual(
            self._scalar(
                "SELECT COUNT(*) FROM messages WHERE direction = 'OUTBOUND'"
            ),
            1,
        )
        self.assertEqual(
            self._scalar("SELECT status FROM turns WHERE id = ?", (info["turn_id"],)),
            "DONE",
        )
        self.assertEqual(
            self._scalar(
                "SELECT COUNT(*) FROM jobs WHERE type = 'send_message' "
                "AND status = 'PENDING'"
            ),
            1,
        )

    def test_crash_between_message_and_event_does_not_duplicate_outbound(self):
        info = self._finish_one_turn()
        conn = db.connect()
        try:
            conn.execute(
                "DELETE FROM events WHERE type = 'reply_queued' AND entity_id = ?",
                (info["turn_id"],),
            )
            conn.execute(
                "UPDATE turns SET status = 'PROCESSING' WHERE id = ?",
                (info["turn_id"],),
            )
            conn.commit()
        finally:
            conn.close()
        self._drop_send_jobs()

        # Real restart sequence: the queue recovers the job and the assembler
        # returns the orphaned PROCESSING turn to OPEN.
        queue.requeue_in_progress()
        self.assertEqual(assembler.recover_open(), 1)
        # El turno se vuelve a clasificar: el NLU va mockeado para que la suite
        # no dependa de un servidor de modelo vivo.
        intent = llm.Intent(intencion="consulta_precio", confianza=0.95)
        with patch(
            "cuadra.ai.llm.classify_light", new=AsyncMock(return_value=intent)
        ):
            self._run_process()

        # The model may run again, but the deterministic outbound key
        # (`turn:<id>`) keeps a single message in the conversation.
        self.assertEqual(
            self._scalar(
                "SELECT COUNT(*) FROM messages WHERE direction = 'OUTBOUND'"
            ),
            1,
        )
        self.assertEqual(
            self._scalar(
                "SELECT COUNT(*) FROM jobs WHERE type = 'send_message' "
                "AND status = 'PENDING'"
            ),
            1,
        )


class ClaimRaceTests(Phase1TestCase):
    def test_message_joining_before_claim_gets_one_combined_reply(self):
        intent = llm.Intent(intencion="consulta_precio", confianza=0.95)
        info = self._ingest("ID 1")
        self._make_due()
        job = self._claim_job()
        self.assertIsNotNone(job)

        real_claim = turns.claim
        seen = {}

        def racing_claim(conn, turn_id):
            # The customer types again just before the handler claims the turn.
            seen["late"] = self._ingest("y el Power Bank?")
            return real_claim(conn, turn_id)

        with patch(
            "cuadra.ai.llm.classify_light", new=AsyncMock(return_value=intent)
        ), patch("cuadra.domain.turns.claim", side_effect=racing_claim):
            asyncio.run(queue._process(job))

        self.assertEqual(seen["late"]["turn_id"], info["turn_id"])
        self.assertEqual(
            self._scalar(
                "SELECT COUNT(*) FROM messages WHERE direction = 'OUTBOUND'"
            ),
            0,
        )
        self.assertEqual(
            self._scalar("SELECT status FROM turns WHERE id = ?", (info["turn_id"],)),
            "OPEN",
        )

        # The re-armed turn is processed later as one single burst.
        self._make_due()
        follow = self._claim_job()
        self.assertIsNotNone(follow)
        with patch("cuadra.ai.llm.classify_light", new=AsyncMock(return_value=intent)):
            asyncio.run(queue._process(follow))

        self.assertEqual(
            self._scalar(
                "SELECT COUNT(*) FROM messages WHERE direction = 'OUTBOUND'"
            ),
            1,
        )
        turn = self._rows("SELECT * FROM turns WHERE id = ?", (info["turn_id"],))[0]
        self.assertEqual(turn["status"], "DONE")
        self.assertEqual(len(turns.message_ids(turn)), 2)


class EscalationTests(Phase1TestCase):
    def test_consulta_dato_creates_escalation_and_asks_supplier(self):
        intent = llm.Intent(
            intencion="consulta_dato",
            producto="Power Bank",
            datos_faltantes=["garantía"],
            confianza=0.9,
        )
        with patch("cuadra.ai.llm.classify_light", new=AsyncMock(return_value=intent)):
            self._ingest("ID 1, ¿qué garantía trae?")
            self._make_due()
            self._run_process()

        outbound = self._rows("SELECT * FROM messages WHERE direction = 'OUTBOUND'")
        self.assertEqual(len(outbound), 1)
        self.assertIn("consulto al almacén", outbound[0]["body"])

        rows = self._rows("SELECT * FROM escalations")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "PENDING")
        self.assertEqual(rows[0]["product_id"], 1)
        self.assertEqual(rows[0]["supplier_id"], 1)
        self.assertIn("garantía", rows[0]["question"])
        self.assertIn("Power Bank", rows[0]["question"])
        self.assertEqual(
            self._scalar(
                "SELECT COUNT(*) FROM jobs WHERE type = 'send_escalation' "
                "AND status = 'PENDING'"
            ),
            1,
        )


class ProductResolutionTests(Phase1TestCase):
    """H-07 / H-09 / H-11: answer about the product this turn actually names."""

    def _add_product(self, product_id: int, name: str, price: float, stock: int = 5):
        conn = db.connect()
        try:
            conn.execute(
                """INSERT INTO products
                   (id, supplier_id, name, price, currency, commission, stock, status)
                   VALUES (?, 1, ?, ?, 'USD', 1, ?, 'ACTIVE')""",
                (product_id, name, price, stock),
            )
            conn.commit()
        finally:
            conn.close()

    def test_turn_text_wins_over_the_product_stored_in_context(self):
        self._add_product(2, "Teclado Mecánico", 45.0)
        conn = db.connect()
        try:
            product, source = pipeline._resolve_product(
                conn,
                llm.Intent(intencion="consulta_precio", confianza=0.9),
                {"product_id": 1},
                "precio del teclado mecánico",
            )
        finally:
            conn.close()
        self.assertEqual(product["id"], 2)
        self.assertEqual(source, "text")

    def test_named_product_resolves_when_intent_producto_is_null(self):
        self._add_product(2, "Teclado Mecánico", 45.0)
        conn = db.connect()
        try:
            product, source = pipeline._resolve_product(
                conn,
                llm.Intent(intencion="consulta_precio", producto=None, confianza=0.5),
                {"product_id": 1},
                "Hola\n¿precio del teclado mecánico?",
            )
        finally:
            conn.close()
        self.assertEqual(product["id"], 2)
        self.assertEqual(source, "text")

    def test_pipeline_answers_about_the_newly_named_product(self):
        self._add_product(2, "Teclado Mecánico", 45.0)
        first = llm.Intent(
            intencion="consulta_precio", producto="Power Bank", confianza=0.95
        )
        with patch("cuadra.ai.llm.classify_light", new=AsyncMock(return_value=first)):
            self._ingest("precio del Power Bank")
            self._make_due()
            self._run_process()

        second = llm.Intent(
            intencion="consulta_precio", producto=None, confianza=0.6
        )
        with patch("cuadra.ai.llm.classify_light", new=AsyncMock(return_value=second)):
            self._ingest("y el precio del teclado mecánico")
            self._make_due()
            self._run_process()

        outbound = self._rows(
            "SELECT body FROM messages WHERE direction = 'OUTBOUND' ORDER BY id"
        )
        self.assertEqual(len(outbound), 2)
        self.assertIn("Teclado Mecánico", outbound[1]["body"])
        self.assertIn("45", outbound[1]["body"])
        self.assertNotIn("Power Bank", outbound[1]["body"])

    def _first_turn(self, intent) -> None:
        with patch("cuadra.ai.llm.classify_light", new=AsyncMock(return_value=intent)):
            self._ingest("precio del Power Bank")
            self._make_due()
            self._run_process()

    def test_unknown_product_does_not_inherit_context_or_escalate(self):
        self._first_turn(
            llm.Intent(
                intencion="consulta_precio", producto="Power Bank", confianza=0.95
            )
        )
        unknown = llm.Intent(
            intencion="consulta_dato",
            producto=None,
            datos_faltantes=["precio", "stock", "garantía", "especificaciones"],
            confianza=0.0,
        )
        with patch("cuadra.ai.llm.classify_light", new=AsyncMock(return_value=unknown)):
            self._ingest("¿Tienes neveras?")
            self._make_due()
            self._run_process()

        outbound = self._rows(
            "SELECT body FROM messages WHERE direction = 'OUTBOUND' ORDER BY id"
        )
        self.assertEqual(len(outbound), 2)
        self.assertEqual(outbound[1]["body"], prompts.TEMPLATES["ask_which_product"])
        self.assertNotIn("Power Bank", outbound[1]["body"])
        self.assertEqual(self._scalar("SELECT COUNT(*) FROM escalations"), 0)
        self.assertEqual(
            self._scalar(
                "SELECT COUNT(*) FROM jobs WHERE type = 'send_escalation'"
            ),
            0,
        )
        event = self._rows(
            "SELECT details_json FROM events WHERE type = 'reply_generated' "
            "ORDER BY id DESC LIMIT 1"
        )[0]
        self.assertIn('"product_id": null', event["details_json"])
        self.assertIn('"escalate": false', event["details_json"])

    def test_named_unknown_product_does_not_use_the_context_product(self):
        """H-10: a named product the catalog lacks must not resolve to the context."""
        self._first_turn(
            llm.Intent(
                intencion="consulta_precio", producto="Power Bank", confianza=0.95
            )
        )
        unknown = llm.Intent(
            intencion="consulta_stock", producto="neveras", confianza=0.95
        )
        with patch("cuadra.ai.llm.classify_light", new=AsyncMock(return_value=unknown)):
            self._ingest("¿Tienes neveras?")
            self._make_due()
            self._run_process()

        outbound = self._rows(
            "SELECT body FROM messages WHERE direction = 'OUTBOUND' ORDER BY id"
        )
        self.assertEqual(len(outbound), 2)
        self.assertEqual(outbound[1]["body"], prompts.TEMPLATES["ask_which_product"])
        self.assertNotIn("Power Bank", outbound[1]["body"])
        self.assertEqual(self._scalar("SELECT COUNT(*) FROM escalations"), 0)

    def test_lexical_support_filters_semantically_close_strangers(self):
        """H-27: "cargador" must not resolve to "Moto Eléctrica Cargo"."""
        self._add_product(2, "Moto Eléctrica Cargo", 1000.0)
        conn = db.connect()
        try:
            moto = catalog.get_product(conn, 2)
            power_bank = catalog.get_product(conn, 1)
            self.assertFalse(pipeline._lexically_supported(conn, "cargador", moto))
            self.assertTrue(
                pipeline._lexically_supported(conn, "power bank", power_bank)
            )
            self.assertTrue(
                pipeline._lexically_supported(conn, "power banks", power_bank)
            )
        finally:
            conn.close()

    def test_semantic_candidate_without_lexical_support_is_ignored(self):
        self._add_product(2, "Moto Eléctrica Cargo", 1000.0)
        conn = db.connect()
        try:
            moto = catalog.get_product(conn, 2)
            with patch(
                "cuadra.ai.pipeline._retrieve_candidates", return_value=[moto]
            ):
                product, source = pipeline._resolve_product(
                    conn,
                    llm.Intent(
                        intencion="consulta_stock",
                        producto="cargador",
                        confianza=0.95,
                    ),
                    {"product_id": 1},
                    "¿Tienes cargadores?",
                )
        finally:
            conn.close()
        self.assertIsNone(product)
        self.assertEqual(source, "")

    def test_high_confidence_followup_keeps_the_context_product(self):
        self._first_turn(
            llm.Intent(
                intencion="consulta_precio", producto="Power Bank", confianza=0.95
            )
        )
        followup = llm.Intent(
            intencion="consulta_dato",
            producto=None,
            datos_faltantes=["garantía"],
            confianza=0.9,
        )
        with patch("cuadra.ai.llm.classify_light", new=AsyncMock(return_value=followup)):
            self._ingest("¿Qué garantía trae?")
            self._make_due()
            self._run_process()

        escalation = self._rows("SELECT * FROM escalations")[0]
        self.assertEqual(escalation["product_id"], 1)
        self.assertEqual(escalation["supplier_id"], 1)
        self.assertIn("garantía", escalation["question"])
        self.assertIn("Power Bank", escalation["question"])
        outbound = self._rows(
            "SELECT body FROM messages WHERE direction = 'OUTBOUND' ORDER BY id"
        )
        self.assertIn("consulto al almacén", outbound[1]["body"])

    def test_sale_capture_keeps_the_context_product(self):
        self._first_turn(
            llm.Intent(
                intencion="consulta_precio", producto="Power Bank", confianza=0.95
            )
        )
        conn = db.connect()
        try:
            stored = conversations.get_context(conn, 1)
            stored["stage"] = "CAPTURE_DATA"
            conversations.set_context(conn, 1, stored)
        finally:
            conn.close()

        noise = llm.Intent(intencion="otro", confianza=0.1)
        with patch("cuadra.ai.llm.classify_light", new=AsyncMock(return_value=noise)):
            self._ingest("calle 10 número 123")
            self._make_due()
            self._run_process()

        outbound = self._rows(
            "SELECT body FROM messages WHERE direction = 'OUTBOUND' ORDER BY id"
        )
        self.assertIn("me falta", outbound[1]["body"])
        self.assertIn("teléfono", outbound[1]["body"])

    def test_comparison_resolves_both_named_products(self):
        self._add_product(2, "Monitor LED 24", 180.0, stock=6)
        self._add_product(3, "Tablet Android 10", 220.0, stock=3)
        conn = db.connect()
        try:
            products = pipeline._comparison_products(
                conn,
                "Compárame el Monitor LED 24 con la Tablet Android 10",
                llm.Intent(intencion="comparar", producto=None, confianza=0.9),
                None,
            )
        finally:
            conn.close()
        ids = [product["id"] for product in products]
        self.assertIn(2, ids)
        self.assertIn(3, ids)
        self.assertNotIn(1, ids)

    def test_fallback_does_not_reuse_a_product_from_the_context(self):
        inherited = {
            "product": {
                "id": 2,
                "name": "Monitor LED 24",
                "price": 180.0,
                "currency": "USD",
                "stock": 6,
                "status": "ACTIVE",
            },
            "products": [],
            "quoted_prices": [],
            "product_from_turn": False,
        }
        body = guardrails.fallback_reply(inherited)
        self.assertEqual(body, prompts.TEMPLATES["ask_which_product"])
        self.assertNotIn("180", body)

    def test_fallback_still_quotes_a_product_resolved_this_turn(self):
        current = {
            "product": {
                "id": 1,
                "name": "Power Bank 10000",
                "price": 12.5,
                "currency": "USD",
                "stock": 5,
                "status": "ACTIVE",
            },
            "products": [],
            "quoted_prices": [],
            "product_from_turn": True,
        }
        body = guardrails.fallback_reply(current)
        self.assertIn("Power Bank 10000", body)
        self.assertIn("12.5", body)


class ReplyWordingTests(Phase1TestCase):
    """H-03 / H-13 / H-26: chat-sized wording and no gendered close."""

    def _add_product(self, product_id: int, name: str, price: float, stock: int = 5):
        conn = db.connect()
        try:
            conn.execute(
                """INSERT INTO products
                   (id, supplier_id, name, price, currency, commission, stock, status)
                   VALUES (?, 1, ?, ?, 'USD', 1, ?, 'ACTIVE')""",
                (product_id, name, price, stock),
            )
            conn.commit()
        finally:
            conn.close()

    def _link_zones(self, names: list[str]) -> None:
        conn = db.connect()
        try:
            for index, name in enumerate(names, start=1):
                conn.execute(
                    "INSERT INTO zones (id, name, active) VALUES (?, ?, 1)",
                    (index, name),
                )
                conn.execute(
                    """INSERT INTO supplier_delivery_zones (supplier_id, zone_id)
                       VALUES (1, ?)""",
                    (index,),
                )
            conn.commit()
        finally:
            conn.close()

    def _outbound(self) -> list[str]:
        return [
            row["body"]
            for row in self._rows(
                "SELECT body FROM messages WHERE direction = 'OUTBOUND' ORDER BY id"
            )
        ]

    def _turn(self, text: str, intent) -> None:
        with patch("cuadra.ai.llm.classify_light", new=AsyncMock(return_value=intent)):
            self._ingest(text)
            self._make_due()
            self._run_process()

    def test_ask_which_product_does_not_confirm_availability(self):
        body = prompts.TEMPLATES["ask_which_product"]
        self.assertIn("Dime cuál producto te interesa", body)
        self.assertNotIn("Claro que sí", body)

    def test_closing_phrases_are_neutral(self):
        self._add_product(2, "Tablet Android 10", 220.0)
        info = self._ingest("precio de la Tablet")
        conn = db.connect()
        try:
            conversation = conversations.get_conversation(
                conn, info["conversation_id"]
            )
            body = pipeline._render_greeting(
                conn, catalog.get_product(conn, 2), conversation
            )
        finally:
            conn.close()
        self.assertIn("Tablet Android 10", body)
        self.assertNotIn("el Tablet", body)
        self.assertNotIn("te la llevo", prompts.SYSTEM_PROMPT.lower())
        self.assertNotIn("te lo llevo", prompts.SYSTEM_PROMPT.lower())

    def test_greeting_detection(self):
        for text in ("Hola", "Hola, buenas", "¡Buenas tardes!", "¿Qué tal?"):
            self.assertTrue(pipeline._is_greeting(text), text)
        for text in ("Hola, ¿tienes el monitor?", "precio del monitor"):
            self.assertFalse(pipeline._is_greeting(text), text)

    def test_bare_greeting_does_not_reuse_the_product(self):
        self._turn(
            "precio del Power Bank",
            llm.Intent(
                intencion="consulta_precio", producto="Power Bank", confianza=0.95
            ),
        )
        self._turn("Hola, buenas", llm.Intent(intencion="otro", confianza=0.9))
        body = self._outbound()[-1]
        # F7 (prueba #2): el saludo recibe un saludo, no la plantilla de producto.
        self.assertEqual(body, prompts.TEMPLATES["greeting"])
        self.assertNotIn("Power Bank", body)

    def test_greeting_does_not_list_every_zone(self):
        self._link_zones([f"Zona {index}" for index in range(1, 5)])
        self._turn(
            "precio del Power Bank",
            llm.Intent(
                intencion="consulta_precio", producto="Power Bank", confianza=0.95
            ),
        )
        body = self._outbound()[-1]
        self.assertIn("Entregamos en tu municipio", body)
        self.assertIn("te confirmo la zona exacta", body)
        self.assertNotIn("Zona 1", body)

    def test_greeting_uses_the_usual_zone(self):
        self._link_zones(["San Miguel", "Vedado"])
        self._ingest("precio del Power Bank")
        conn = db.connect()
        try:
            conn.execute(
                """UPDATE customers SET preferences_json = ?
                   WHERE id = (SELECT customer_id FROM conversations
                               ORDER BY id LIMIT 1)""",
                ('{"usual_zone": "San Miguel"}',),
            )
            conn.commit()
        finally:
            conn.close()
        self._make_due()
        with patch(
            "cuadra.ai.llm.classify_light",
            new=AsyncMock(
                return_value=llm.Intent(
                    intencion="consulta_precio",
                    producto="Power Bank",
                    confianza=0.95,
                )
            ),
        ):
            self._run_process()
        body = self._outbound()[-1]
        self.assertIn("Entregamos en San Miguel", body)
        self.assertNotIn("Vedado", body)


class TransportPolicyTests(Phase1TestCase):
    """H-02: the price reply states the transport policy and never invents it."""

    def _greeting(self, policy: str | None = None) -> str:
        info = self._ingest("hola")
        conn = db.connect()
        try:
            if policy is not None:
                settings.set_transport_policy(conn, policy)
            conversation = conversations.get_conversation(
                conn, info["conversation_id"]
            )
            return pipeline._render_greeting(
                conn, catalog.get_product(conn, 1), conversation
            )
        finally:
            conn.close()

    def test_default_is_coordinated_and_does_not_claim_inclusion(self):
        body = self._greeting()
        self.assertIn("El transporte se coordina al confirmar la entrega.", body)
        self.assertNotIn("Incluye transporte", body)

    def test_included_policy_says_transport_is_included(self):
        body = self._greeting("included")
        self.assertIn("Incluye transporte.", body)
        self.assertNotIn("se coordina", body)

    def test_not_included_policy_says_transport_is_charged_apart(self):
        body = self._greeting("not_included")
        self.assertIn("se cobra aparte", body)

    def test_invalid_policy_is_rejected_and_falls_back_to_default(self):
        conn = db.connect()
        try:
            with self.assertRaises(ValueError):
                settings.set_transport_policy(conn, "free")
            self.assertEqual(settings.transport_policy(conn), "coordinated")
        finally:
            conn.close()

    def test_system_prompt_declares_the_transport_policy(self):
        info = self._ingest("hola")
        conn = db.connect()
        try:
            settings.set_transport_policy(conn, "included")
            conversation = conversations.get_conversation(
                conn, info["conversation_id"]
            )
            builder = context.ContextBuilder(conn, conversation, context={})
            system = builder.build(
                [], product=catalog.get_product(conn, 1)
            )[0]["content"]
        finally:
            conn.close()
        self.assertIn("TRANSPORTE: Incluye transporte.", system)


class _FakeSummarizer:
    def __init__(self, reply: str):
        self.reply = reply
        self.inputs: list[list[str]] = []

    async def summarize(self, previous, rows, *, conversation_id):
        self.inputs.append([row["body"] for row in rows])
        return self.reply


class PromptStateTests(Phase1TestCase):
    """H-25 / H-14: the state mirrors this turn and never leaks internals."""

    def _system(self, context_value: dict, *, product=None) -> str:
        info = self._ingest("hola")
        conn = db.connect()
        try:
            conversation = conversations.get_conversation(
                conn, info["conversation_id"]
            )
            builder = context.ContextBuilder(
                conn, conversation, context=context_value
            )
            messages = builder.build([], product=product)
        finally:
            conn.close()
        return messages[0]["content"]

    def test_state_uses_the_turn_product(self):
        conn = db.connect()
        try:
            product = catalog.get_product(conn, 1)
        finally:
            conn.close()
        system = self._system({"product_id": 99}, product=product)
        self.assertIn("Producto en juego (id 1).", system)
        self.assertNotIn("Producto en juego (id 99).", system)

    def test_state_falls_back_to_the_context_product(self):
        system = self._system({"product_id": 1})
        self.assertIn("Producto en juego (id 1).", system)

    def test_summary_with_internal_terms_is_not_injected(self):
        system = self._system(
            {"summary": "Quiere un monitor. El cliente preguntó por la comisión."}
        )
        self.assertIn("Quiere un monitor", system)
        self.assertNotIn("comisión", system.lower())


class HistoricalPriceTests(Phase1TestCase):
    """H-23: bot history must never reintroduce a superseded price."""

    def _messages(self, history: list[dict]) -> list[dict]:
        info = self._ingest("hola")
        conn = db.connect()
        try:
            conversation = conversations.get_conversation(
                conn, info["conversation_id"]
            )
            product = catalog.get_product(conn, 1)
            builder = context.ContextBuilder(
                conn, conversation, context={"product_id": 1}
            )
            return builder.build(history, product=product)
        finally:
            conn.close()

    def test_assistant_history_drops_a_superseded_price(self):
        messages = self._messages(
            [
                {
                    "direction": "OUTBOUND",
                    "type": "text",
                    "body": "El precio baja de 12.50 a 20 USD.",
                }
            ]
        )
        joined = "\n".join(message["content"] for message in messages)
        self.assertIn("12.50", joined)
        self.assertNotIn("20 USD", joined)
        self.assertIn("[precio anterior]", joined)

    def test_customer_history_keeps_its_words(self):
        messages = self._messages(
            [{"direction": "INBOUND", "type": "text", "body": "me dijiste 20 USD"}]
        )
        joined = "\n".join(message["content"] for message in messages)
        self.assertIn("me dijiste 20 USD", joined)

    def test_system_declares_only_current_prices(self):
        system = self._messages([])[0]["content"]
        self.assertIn("PRECIOS VIGENTES", system)
        self.assertIn("puede haber cambiado", system)


class SummarySanitizationTests(Phase1TestCase):
    """H-14: the rolling summary never carries internal economics."""

    def test_summarizer_never_sees_or_stores_internal_terms(self):
        info = self._ingest("hola")
        conversation_id = info["conversation_id"]
        conn = db.connect()
        try:
            for body in (
                "Quiere el Power Bank.",
                "Preguntó por la comisión del proveedor.",
                "Le interesa el precio.",
                "Pidió entrega en San Miguel.",
                "Espera confirmación.",
            ):
                conversations.add_message(
                    conn, conversation_id, "INBOUND", "customer", body=body
                )
            conversation = conversations.get_conversation(conn, conversation_id)
            summarizer = _FakeSummarizer(
                "El cliente quiere el Power Bank. Preguntó por la comisión interna."
            )
            with (
                patch.object(config, "HISTORY_WINDOW", 2),
                patch.object(config, "SUMMARY_EVERY", 2),
            ):
                asyncio.run(
                    memory.maybe_summarize(
                        conn, conversation, llm_client=summarizer
                    )
                )
            stored = conversations.get_context(conn, conversation_id)
        finally:
            conn.close()

        self.assertTrue(summarizer.inputs)
        self.assertTrue(
            all("comisión" not in body.lower() for body in summarizer.inputs[0])
        )
        self.assertIn("Power Bank", stored["summary"])
        self.assertNotIn("comisión", stored["summary"].lower())


class GuardrailTests(unittest.TestCase):
    def setUp(self):
        self.context = {
            "product": {"id": 1, "price": 12.5, "currency": "USD", "stock": 5}
        }

    def test_allowed_price_passes(self):
        ok, reason = guardrails.validate_reply("Cuesta 12.50 USD.", self.context)
        self.assertTrue(ok, reason)
        ok, reason = guardrails.validate_reply("Solo $12.50.", self.context)
        self.assertTrue(ok, reason)

    def test_invented_price_is_rejected(self):
        ok, reason = guardrails.validate_reply("Cuesta 99 USD.", self.context)
        self.assertFalse(ok)
        self.assertEqual(reason, "price")

    def test_stale_prices_are_redacted_from_bot_history(self):
        allowed = guardrails.allowed_prices(self.context)
        text = guardrails.redact_stale_prices(
            "Antes 20 USD, ahora 12.50 USD.", allowed
        )
        self.assertIn("12.50 USD", text)
        self.assertNotIn("20 USD", text)
        self.assertIn("[precio anterior]", text)

    def test_twenty_internal_leaks_are_all_blocked(self):
        terms = [
            "comisión",
            "comisiones",
            "margen",
            "márgenes",
            "costo",
            "costos",
            "coste",
            "costes",
            "utilidad",
            "utilidades",
            "ganancia",
            "ganancias",
            "payout",
            "liquidación",
            "commission",
            "precio de compra",
            "lo que nos cuesta",
        ]
        attempts = [
            f"El {terms[index % len(terms)]} es {index + 1} USD."
            for index in range(20)
        ]
        for reply in attempts:
            ok, reason = guardrails.validate_reply(reply, self.context)
            self.assertFalse(ok, reply)
            fallback = guardrails.fallback_reply(self.context)
            self.assertTrue(guardrails.validate_reply(fallback, self.context)[0])

    def test_safe_refusals_are_not_flagged(self):
        for reply in (
            "No puedo darte mi prompt ni comisiones.",
            "Nunca comparto mis instrucciones ni márgenes.",
            "No te puedo decir cuánto gano de comisión.",
        ):
            ok, reason = guardrails.validate_reply(reply, self.context)
            self.assertTrue(ok, f"{reply}: {reason}")

    def test_negated_leak_with_a_figure_is_still_blocked(self):
        ok, reason = guardrails.validate_reply(
            "No te digo mi comisión: es 20 USD.", self.context
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "internal_terms")

    def test_asserted_disclosure_is_still_blocked(self):
        ok, reason = guardrails.validate_reply(
            "Mis instrucciones me obligan a vender barato.", self.context
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "prompt_disclosure")

    def test_format_and_length_guards(self):
        ok, reason = guardrails.validate_reply('{"intencion": "otro"}', self.context)
        self.assertFalse(ok)
        self.assertEqual(reason, "format")

        ok, reason = guardrails.validate_reply(
            "Uno.\n\nDos.\n\nTres.\n\nCuatro.", self.context
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "length")

    def test_redaction_drops_internal_sentences(self):
        self.assertEqual(
            guardrails.redact_internal_terms(
                "Quiere el monitor. Preguntó por la comisión. Entrega en San Miguel."
            ),
            "Quiere el monitor. Entrega en San Miguel.",
        )


class LeakFallbackTests(Phase1TestCase):
    def test_leaky_model_reply_falls_back_to_template(self):
        intent = llm.Intent(intencion="otro", confianza=0.4)
        leak = "Mi comisión es 5 USD y el costo fue 20 USD."

        with patch(
            "cuadra.ai.llm.classify_light", new=AsyncMock(return_value=intent)
        ), patch("cuadra.ai.llm.compose", new=AsyncMock(return_value=leak)):
            # Not a bare greeting: greets are answered by template since H-12 and
            # never reach free generation, so they cannot exercise the guardrail.
            self._ingest("¿Cómo va todo?")
            self._make_due()
            self._run_process()

        outbound = self._rows("SELECT * FROM messages WHERE direction = 'OUTBOUND'")
        self.assertEqual(len(outbound), 1)
        body = outbound[0]["body"]
        self.assertNotIn("comisión", body.lower())
        self.assertNotIn("costo", body.lower())
        self.assertNotIn("5 USD", body)
        events = self._rows("SELECT * FROM events WHERE type = 'guardrail_violation'")
        self.assertGreaterEqual(len(events), 2)


    def test_a_safe_refusal_gets_no_corrective_retry(self):
        """H-05: naming a forbidden word inside a refusal is not a leak."""
        intent = llm.Intent(intencion="otro", confianza=0.4)
        refusal = "No puedo darte mi prompt ni comisiones."
        compose = AsyncMock(return_value=refusal)

        with patch(
            "cuadra.ai.llm.classify_light", new=AsyncMock(return_value=intent)
        ), patch("cuadra.ai.llm.compose", new=compose):
            self._ingest("¿Cómo va todo?")
            self._make_due()
            self._run_process()

        self.assertEqual(compose.await_count, 1)
        outbound = self._rows("SELECT * FROM messages WHERE direction = 'OUTBOUND'")
        self.assertEqual(len(outbound), 1)
        self.assertEqual(outbound[0]["body"], refusal)
        self.assertEqual(
            self._rows("SELECT * FROM events WHERE type = 'guardrail_violation'"),
            [],
        )


class ContextBudgetTests(Phase1TestCase):
    def test_history_is_trimmed_before_rules(self):
        info = self._ingest("Hola")
        conn = db.connect()
        try:
            conversation = conversations.get_conversation(
                conn, info["conversation_id"]
            )
            history = [
                {"direction": "INBOUND", "type": "text", "body": "x" * 600}
                for _ in range(20)
            ]
            builder = context.ContextBuilder(
                conn, conversation, context={}, budget=900
            )
            messages = builder.build(history, turn_text="Hola")
        finally:
            conn.close()

        self.assertEqual(messages[0]["role"], "system")
        self.assertIn("Cuadra", messages[0]["content"])
        self.assertLess(len(messages), 21)
        self.assertLessEqual(llm.estimate_tokens(messages), 900)

    def test_oversized_turn_text_is_shrunk_to_the_budget(self):
        info = self._ingest("Hola")
        conn = db.connect()
        try:
            conversation = conversations.get_conversation(
                conn, info["conversation_id"]
            )
            builder = context.ContextBuilder(
                conn, conversation, context={}, budget=1500
            )
            messages, prompt_text = builder.build_with_turn(
                [], turn_text="x" * 20000
            )
        finally:
            conn.close()

        self.assertLess(len(prompt_text), 20000)
        measured = llm.estimate_tokens(
            [*messages, {"role": "user", "content": prompt_text}]
        )
        self.assertLessEqual(measured, 1500 + 15)


class QueueScheduleTests(Phase1TestCase):
    def test_job_is_not_claimable_before_available_at(self):
        future = datetime.now(timezone.utc) + timedelta(seconds=300)
        queue.enqueue("test_future", {}, available_at=future)
        conn = db.connect()
        try:
            self.assertIsNone(queue._claim(conn))
        finally:
            conn.close()

        queue.enqueue("test_now", {}, available_at=_past())
        conn = db.connect()
        try:
            job = queue._claim(conn)
        finally:
            conn.close()
        self.assertEqual(job["type"], "test_now")


class SaleFlowTests(Phase1TestCase):
    """F1/F2 de la prueba #2: la venta no muere y la cantidad no se pierde."""

    def setUp(self):
        super().setUp()
        conn = db.connect()
        try:
            conn.execute(
                "INSERT INTO zones (id, name, active) VALUES (1, 'San Miguel', 1)"
            )
            conn.execute(
                """INSERT INTO supplier_delivery_zones (supplier_id, zone_id)
                   VALUES (1, 1)"""
            )
            conn.commit()
        finally:
            conn.close()

    def _store_facts(self) -> None:
        conn = db.connect()
        try:
            for key, value in (
                (memory.FACT_ZONE, "San Miguel"),
                (memory.FACT_ADDRESS, "Calle 10 numero 123"),
                (memory.FACT_PHONE, "+5355500001"),
                (memory.FACT_CUSTOMER_NAME, "Rolando"),
            ):
                memory.upsert_fact(conn, 1, key, value)
        finally:
            conn.close()

    def _advance(self, text, intent, context, product="resolve"):
        conn = db.connect()
        try:
            conversation = conversations.get_conversation(conn, 1)
            if product == "resolve":
                product, _ = pipeline._resolve_product(conn, intent, context, text)
            return pipeline._maybe_handle_sale(
                conn, conversation, text, intent, product, context
            )
        finally:
            conn.close()

    # ------------------------------------------------------------- F1 (NLU)

    def test_null_string_producto_is_normalized(self):
        self.assertIsNone(llm.Intent(intencion="comprar", producto="null").producto)
        self.assertIsNone(llm.Intent(intencion="comprar", producto=" NULL ").producto)
        self.assertIsNone(llm.Intent(intencion="comprar", producto="").producto)
        self.assertEqual(
            llm.Intent(intencion="comprar", producto="Power Bank").producto,
            "Power Bank",
        )
        parsed = llm.Intent.model_validate_json(
            '{"intencion": "comprar", "producto": "null",'
            ' "datos_faltantes": ["null"], "confianza": 0.9, "cantidad": "null"}'
        )
        self.assertIsNone(parsed.producto)
        self.assertEqual(parsed.datos_faltantes, [])
        self.assertIsNone(parsed.cantidad)

    def test_open_sale_is_not_reset_to_discovery(self):
        intent = llm.Intent(intencion="otro", confianza=0.1)
        self.assertEqual(
            pipeline._stage_for(
                {"stage": "CONFIRM", "product_id": 1}, intent, None, False
            ),
            "CONFIRM",
        )
        self.assertEqual(
            pipeline._stage_for({"stage": "CAPTURE_DATA"}, intent, None, False),
            "CAPTURE_DATA",
        )
        self.assertEqual(
            pipeline._stage_for({"stage": "QUOTE"}, intent, None, False), "DISCOVERY"
        )

    def test_sale_uses_the_context_product_when_the_nlu_forgets_it(self):
        self._ingest("quiero 3 Power Bank")
        self._store_facts()
        context = {"stage": "CONFIRM", "product_id": 1}
        outcome = self._advance(
            "si, dale", llm.Intent(intencion="otro", confianza=0.1), context, product=None
        )
        self.assertIsNotNone(outcome)
        self.assertEqual(outcome["stage"], "DONE")
        self.assertEqual(outcome["sale"]["product_id"], 1)

    def test_a_question_does_not_cancel_the_order(self):
        self._ingest("quiero 3 Power Bank")
        self._store_facts()
        context = {"stage": "CONFIRM", "product_id": 1}
        outcome = self._advance(
            "y no tiene otro tipo de laptop?",
            llm.Intent(intencion="consulta_dato", confianza=0.8),
            context,
        )
        self.assertIsNone(outcome)

    def test_only_a_real_refusal_declines_the_order(self):
        self._ingest("quiero 3 Power Bank")
        self._store_facts()
        context = {"stage": "CONFIRM", "product_id": 1}
        kept = self._advance(
            "quiero 3 no 1",
            llm.Intent(intencion="otro", cantidad=3, confianza=0.5),
            context,
        )
        self.assertEqual(kept["stage"], "CONFIRM")
        self.assertIn("x3", kept["reply"])

        declined = self._advance(
            "no, gracias", llm.Intent(intencion="otro", confianza=0.5), context
        )
        self.assertEqual(declined["stage"], "CLOSE")

    def test_yes_but_i_want_three_creates_the_order(self):
        # m15 de la prueba #2: el "no" corrige la cantidad, no rechaza el pedido.
        self._ingest("quiero 3 Power Bank")
        self._store_facts()
        context = {"stage": "CONFIRM", "product_id": 1, "sale_qty": 3,
                   "sale_qty_product_id": 1}
        outcome = self._advance(
            "Si, pero quiero 3 no 1",
            llm.Intent(intencion="comprar", producto="null", cantidad=3, confianza=0.95),
            context,
        )
        self.assertEqual(outcome["stage"], "DONE")
        self.assertEqual(outcome["sale"]["quantity"], 3)

    # -------------------------------------------------------- F2 (cantidad)

    def test_quantity_draft_belongs_to_one_product(self):
        other = llm.Intent(intencion="otro", confianza=0.1)
        context = {"sale_qty": 3, "sale_qty_product_id": 1}
        self.assertEqual(pipeline._sale_quantity(other, context, {"id": 1}), 3)
        self.assertEqual(pipeline._sale_quantity(other, context, {"id": 2}), 1)
        self.assertEqual(
            pipeline._sale_quantity(
                llm.Intent(intencion="comprar", cantidad=2), context, {"id": 1}
            ),
            2,
        )

    def test_quantity_is_reused_across_turns(self):
        self._ingest("quiero 3 Power Bank")
        self._store_facts()
        context = {
            "stage": "CAPTURE_DATA",
            "product_id": 1,
            "sale_qty": 3,
            "sale_qty_product_id": 1,
        }
        outcome = self._advance(
            "+5355500001", llm.Intent(intencion="otro", confianza=0.1), context
        )
        self.assertEqual(outcome["sale_qty"], 3)
        self.assertIn("x3", outcome["reply"])

    def test_quantity_survives_until_the_sale_is_created(self):
        first = llm.Intent(
            intencion="comprar", producto="Power Bank", cantidad=3, confianza=0.95
        )
        with patch("cuadra.ai.llm.classify_light", new=AsyncMock(return_value=first)):
            self._ingest("quiero 3 Power Bank")
            self._make_due()
            self._run_process()

        data = llm.Intent(intencion="otro", confianza=0.1)
        with patch("cuadra.ai.llm.classify_light", new=AsyncMock(return_value=data)):
            self._ingest(
                "soy de San Miguel, calle 10 numero 123, +5355500001, me llamo Rolando"
            )
            self._make_due()
            self._run_process()

        confirm = llm.Intent(intencion="otro", confianza=0.1)
        with patch("cuadra.ai.llm.classify_light", new=AsyncMock(return_value=confirm)):
            self._ingest("si, dale")
            self._make_due()
            self._run_process()

        sales = self._rows("SELECT quantity, product_id FROM sales")
        self.assertEqual(len(sales), 1)
        self.assertEqual(sales[0]["quantity"], 3)
        self.assertEqual(sales[0]["product_id"], 1)


class CatalogConversationTests(Phase1TestCase):
    """F3/F3b/F4/F5/F7 de la prueba #2 (bloque B)."""

    def _add_product(self, product_id: int, name: str, price: float, stock: int = 5):
        conn = db.connect()
        try:
            conn.execute(
                """INSERT INTO products
                   (id, supplier_id, name, price, currency, commission, stock, status)
                   VALUES (?, 1, ?, ?, 'USD', 1, ?, 'ACTIVE')""",
                (product_id, name, price, stock),
            )
            conn.commit()
        finally:
            conn.close()

    def _turn(self, text: str, intent) -> None:
        with patch("cuadra.ai.llm.classify_light", new=AsyncMock(return_value=intent)):
            self._ingest(text)
            self._make_due()
            self._run_process()

    def _outbound(self) -> list[str]:
        return [
            row["body"]
            for row in self._rows(
                "SELECT body FROM messages WHERE direction = 'OUTBOUND' ORDER BY id"
            )
        ]

    # ------------------------------------------------------------------ F3

    def test_a_category_question_lists_the_family(self):
        self._add_product(2, "Laptop Empresarial", 350.0)
        self._add_product(3, "Laptop Ultraligera", 720.0)
        self._add_product(4, "Monitor LED 24", 180.0)
        self._turn(
            "¿Qué laptops tienes a la venta?",
            llm.Intent(
                intencion="consulta_precio", producto="laptops", confianza=0.95
            ),
        )
        body = self._outbound()[-1]
        self.assertIn("Laptop Empresarial", body)
        self.assertIn("Laptop Ultraligera", body)
        self.assertIn("350 USD", body)
        self.assertNotIn("Monitor LED", body)
        self.assertNotIn(prompts.TEMPLATES["ask_which_product"], body)

    def test_category_matching_is_word_by_word_and_singular(self):
        self._add_product(2, "Laptop Empresarial", 350.0)
        conn = db.connect()
        try:
            for query in (
                "laptops",
                "que laptops tienes a la venta",
                "ando en busca de laptops",
            ):
                names = [p["name"] for p in catalog.category_products(conn, query)]
                self.assertIn("Laptop Empresarial", names, query)
            self.assertEqual(catalog.category_products(conn, "neveras"), [])
        finally:
            conn.close()

    def test_buying_a_category_keeps_the_quantity_for_later(self):
        self._add_product(2, "Laptop Empresarial", 350.0)
        self._turn(
            "Quiero 3 laptops",
            llm.Intent(
                intencion="comprar", producto="laptops", cantidad=3, confianza=0.95
            ),
        )
        conn = db.connect()
        try:
            context = conversations.get_context(conn, 1)
        finally:
            conn.close()
        self.assertEqual(context.get("sale_qty"), 3)
        self.assertIn("Laptop Empresarial", self._outbound()[-1])

    # ----------------------------------------------------------------- F3b

    def test_the_quote_comes_before_asking_for_data(self):
        self._turn(
            "Quiero 3 Power Bank",
            llm.Intent(
                intencion="comprar",
                producto="Power Bank",
                cantidad=3,
                confianza=0.95,
            ),
        )
        body = self._outbound()[-1]
        self.assertIn("37.5", body)
        self.assertIn("me falta", body)

    # ------------------------------------------------------------------ F4

    def test_a_question_about_other_models_lists_them(self):
        self._add_product(2, "Laptop Empresarial", 350.0)
        self._add_product(3, "Laptop Ultraligera", 720.0)
        self._add_product(4, "Monitor LED 24", 180.0)
        self._turn(
            "precio de la Laptop Empresarial",
            llm.Intent(
                intencion="consulta_precio",
                producto="Laptop Empresarial",
                confianza=0.95,
            ),
        )
        self._turn(
            "¿y no tiene otro tipo de laptop?",
            llm.Intent(intencion="consulta_dato", producto=None, confianza=0.9),
        )
        body = self._outbound()[-1]
        self.assertIn("Laptop Ultraligera", body)
        self.assertNotIn("Monitor LED", body)
        self.assertNotIn("Dime cuál producto", body)

    def test_a_unique_product_says_there_is_no_other_model(self):
        self._turn(
            "precio del Power Bank",
            llm.Intent(
                intencion="consulta_precio", producto="Power Bank", confianza=0.95
            ),
        )
        self._turn(
            "¿tiene otro modelo?",
            llm.Intent(intencion="consulta_dato", producto=None, confianza=0.9),
        )
        self.assertIn("no tengo otro modelo", self._outbound()[-1])

    def test_siblings_stay_inside_the_name_family(self):
        self._add_product(2, "Laptop Empresarial", 350.0)
        self._add_product(3, "Laptop Ultraligera", 720.0)
        self._add_product(4, "Monitor LED 24", 180.0)
        conn = db.connect()
        try:
            names = [
                p["name"]
                for p in catalog.siblings(conn, catalog.get_product(conn, 2))
            ]
        finally:
            conn.close()
        self.assertEqual(names, ["Laptop Ultraligera"])

    # ------------------------------------------------------------------ F5

    def test_variants_rotate_through_every_option(self):
        context: dict = {}
        seen = [
            prompts.template_variant("ask_which_product", context) for _ in range(3)
        ]
        self.assertEqual(len(set(seen)), 3)
        self.assertEqual(context["last_template_ask_which_product"], 2)

    def test_the_chosen_variant_is_persisted(self):
        self._turn("Hola", llm.Intent(intencion="otro", confianza=0.0))
        self._turn(
            "¿Tienes neveras?",
            llm.Intent(
                intencion="consulta_stock", producto="neveras", confianza=0.9
            ),
        )
        conn = db.connect()
        try:
            context = conversations.get_context(conn, 1)
        finally:
            conn.close()
        self.assertIn("last_template_greeting", context)
        self.assertIn("last_template_ask_which_product", context)
        bodies = self._outbound()
        self.assertNotEqual(bodies[0], bodies[1])

    # ------------------------------------------------------------------ F7

    def test_a_greeting_gets_a_greeting(self):
        self._turn("buenas noches", llm.Intent(intencion="otro", confianza=0.0))
        body = self._outbound()[-1]
        self.assertEqual(body, prompts.TEMPLATES["greeting"])
        self.assertIn("Buenas", body)

    # ----------------------------------------------------------------- F10

    def _ask_about_the_power_bank(self, reply: str):
        """Turno 1 fija el producto; turno 2 es una pregunta abierta."""
        self._turn(
            "precio del Power Bank",
            llm.Intent(
                intencion="consulta_precio", producto="Power Bank", confianza=0.95
            ),
        )
        with patch("cuadra.ai.llm.compose", new=AsyncMock(return_value=reply)) as compose:
            self._turn(
                "¿de qué color es?",
                llm.Intent(intencion="consulta_dato", confianza=0.9),
            )
        return compose

    def test_an_open_product_question_uses_the_composer(self):
        compose = self._ask_about_the_power_bank("El Power Bank viene en negro.")
        self.assertEqual(compose.await_count, 1)
        self.assertEqual(self._outbound()[-1], "El Power Bank viene en negro.")

    def test_the_composer_keeps_the_guardrails(self):
        # Un término interno invalida la respuesta: un reintento correctivo y,
        # si insiste, la plantilla determinista. Nunca llega al cliente.
        compose = self._ask_about_the_power_bank(
            "El Power Bank cuesta 12 y la comisión es 3."
        )
        self.assertEqual(compose.await_count, 2)
        body = self._outbound()[-1]
        self.assertNotIn("comisión", body)
        self.assertIn("Power Bank", body)

    def test_a_composer_outage_does_not_leave_the_customer_silent(self):
        self._turn(
            "precio del Power Bank",
            llm.Intent(
                intencion="consulta_precio", producto="Power Bank", confianza=0.95
            ),
        )
        with patch(
            "cuadra.ai.llm.compose",
            new=AsyncMock(side_effect=llm.LLMUnavailable("down")),
        ):
            self._turn(
                "¿de qué color es?",
                llm.Intent(intencion="consulta_dato", confianza=0.9),
            )
        body = self._outbound()[-1]
        self.assertIn("Power Bank", body)
        self.assertIn("dato", body)

    # ------------------------------------------------------------------ F8

    def test_one_trace_per_turn_with_duration(self):
        self._turn(
            "precio del Power Bank",
            llm.Intent(
                intencion="consulta_precio", producto="Power Bank", confianza=0.95
            ),
        )
        rows = self._rows(
            "SELECT duration_ms, output_json FROM traces "
            "WHERE method = 'pipeline.handle_turn'"
        )
        self.assertEqual(len(rows), 1)
        self.assertIsNotNone(rows[0]["duration_ms"])
        self.assertIn('"generator"', rows[0]["output_json"])


if __name__ == "__main__":
    unittest.main()
