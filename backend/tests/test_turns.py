"""Phase 1: persistent turns, idempotency, context budget and guardrails.

These tests run against a throwaway SQLite database (never the real
`backend/data/cuadra.db`) and never call a model server.

Run from `backend/`:

    .venv\\Scripts\\python.exe -m unittest discover -s tests -v
"""

import asyncio
import json
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
    offers,
    pipeline,
    prompts,
    queue,
)
from cuadra.domain import catalog, conversations, sales, settings, turns


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
    def test_a_missing_spec_escalates_instead_of_composing(self):
        # F1 (prueba #3): con las especificaciones vacías, el compositor
        # inventó que la GPU era un microprocesador. Debe escalar sin llamar
        # al modelo de redacción.
        intent = llm.Intent(
            intencion="consulta_dato", producto="Power Bank", confianza=0.9
        )
        compose = AsyncMock(return_value="La GPU es un microprocesador.")
        with patch(
            "cuadra.ai.llm.classify_light", new=AsyncMock(return_value=intent)
        ), patch("cuadra.ai.llm.compose", new=compose):
            self._ingest("ID 1, ¿qué microprocesador trae?")
            self._make_due()
            self._run_process()

        self.assertEqual(compose.await_count, 0)
        outbound = self._rows("SELECT * FROM messages WHERE direction = 'OUTBOUND'")
        self.assertEqual(len(outbound), 1)
        self.assertIn("consulto al almacén", outbound[0]["body"])
        escalations_rows = self._rows("SELECT * FROM escalations")
        self.assertEqual(len(escalations_rows), 1)
        self.assertIn("procesador", escalations_rows[0]["question"])

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

    def test_the_same_question_twice_creates_one_escalation(self):
        # F4 (prueba #3): dos turnos seguidos con la misma pregunta abrieron
        # E-0001 y E-0002. El TCP recibió la misma consulta dos veces.
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
            self._ingest("ID 1, ¿qué garantía trae?")
            self._make_due()
            self._run_process()

        self.assertEqual(self._scalar("SELECT COUNT(*) FROM escalations"), 1)
        self.assertEqual(
            self._scalar(
                "SELECT COUNT(*) FROM jobs WHERE type = 'send_escalation'"
            ),
            1,
        )
        self.assertEqual(
            self._scalar(
                "SELECT COUNT(*) FROM events WHERE type = 'escalation_reused'"
            ),
            1,
        )


class PendingOfferTests(Phase1TestCase):
    """F5 (prueba #3): un "Si" suelto se resuelve contra la oferta anterior."""

    def _escalate_with_offer(self) -> None:
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
        self.assertIn(
            "reserve",
            self._rows("SELECT context_json FROM conversations")[0]["context_json"],
        )

    def _reply_to(self, text: str, intent=None) -> str:
        intent = intent or llm.Intent(intencion="otro", confianza=0.1)
        with patch("cuadra.ai.llm.classify_light", new=AsyncMock(return_value=intent)):
            self._ingest(text)
            self._make_due()
            self._run_process()
        return self._rows(
            "SELECT body FROM messages WHERE direction = 'OUTBOUND' ORDER BY id DESC LIMIT 1"
        )[0]["body"]

    def test_a_bare_yes_after_an_offer_starts_the_sale(self):
        self._escalate_with_offer()
        reply = self._reply_to("Si")
        self.assertIn("me falta", reply)
        self.assertEqual(self._scalar("SELECT COUNT(*) FROM escalations"), 1)

    def test_a_bare_no_after_an_offer_declines_it(self):
        self._escalate_with_offer()
        reply = self._reply_to("No, gracias")
        self.assertIn("Sin problema", reply)
        self.assertEqual(self._scalar("SELECT COUNT(*) FROM sales"), 0)

    def test_an_offer_is_replaced_by_the_current_quote(self):
        """La oferta anterior se consume; la ficha hace una oferta nueva."""
        self._escalate_with_offer()
        reply = self._reply_to(
            "¿cuánto cuesta?",
            llm.Intent(
                intencion="consulta_precio", producto="Power Bank", confianza=0.9
            ),
        )
        self.assertNotIn("me falta", reply)
        self.assertEqual(self._scalar("SELECT COUNT(*) FROM sales"), 0)
        context = json.loads(
            self._rows("SELECT context_json FROM conversations")[0]["context_json"]
        )
        offer = context.get("pending_offer")
        self.assertIsNotNone(offer)
        self.assertEqual(offer["kind"], offers.RESERVE)
        self.assertEqual(offer["product_id"], 1)

    def test_offer_answers_require_a_bare_message(self):
        self.assertEqual(offers.answer("Si"), "yes")
        self.assertEqual(offers.answer("si, por favor"), "yes")
        self.assertEqual(offers.answer("No, gracias"), "no")
        self.assertIsNone(offers.answer("Si, ¿qué procesador tiene?"))
        self.assertIsNone(offers.answer("Vale, cuando me la traes"))

    def test_detect_only_matches_the_bot_question(self):
        reserve = prompts.TEMPLATES["escalation_resolved_summary"].format(
            producto="Power Bank", respuesta="Quedan 2."
        )
        self.assertEqual(offers.detect(reserve)["kind"], offers.RESERVE)
        self.assertIsNone(offers.detect("Ya te lo reservé."))


class SpecTopicTests(unittest.TestCase):
    def test_a_topic_covered_by_the_catalog_is_not_missing(self):
        product = {"specifications": {"procesador": "i5 1250H"}}
        self.assertEqual(
            pipeline._missing_spec_topics(product, "¿qué procesador tiene?"), []
        )

    def test_an_empty_catalog_reports_the_asked_topic(self):
        self.assertEqual(
            pipeline._missing_spec_topics({"specifications": {}}, "¿qué GPU trae?"),
            ["tarjeta gráfica"],
        )

    def test_a_question_without_spec_terms_reports_nothing(self):
        self.assertEqual(
            pipeline._missing_spec_topics({"specifications": {}}, "¿cuánto cuesta?"),
            [],
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
                "cuadra.ai.turn.references._retrieve_candidates", return_value=[moto]
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
            body = pipeline._render_product_card(
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
        # F13 (prueba #4): dentro del cooldown no se repite "¡Buenas!".
        self.assertNotIn("Power Bank", body)
        self.assertNotIn("Buenas", body)
        self.assertIn("ayudo", body)

    def test_the_product_card_never_mentions_the_delivery_zone(self):
        """La zona no se habla hasta que toque la dirección (prueba #6)."""
        self._link_zones([f"Zona {index}" for index in range(1, 5)])
        self._turn(
            "precio del Power Bank",
            llm.Intent(
                intencion="consulta_precio", producto="Power Bank", confianza=0.95
            ),
        )
        body = self._outbound()[-1]
        self.assertNotIn("Entregamos en", body)
        self.assertNotIn("municipio", body)
        self.assertNotIn("zona exacta", body)
        self.assertNotIn("Zona 1", body)
        self.assertIn("Power Bank", body)

    def test_the_product_card_does_not_assume_where_the_customer_lives(self):
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
        self.assertNotIn("San Miguel", body)
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
            return pipeline._render_product_card(
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

    def test_an_invented_spec_is_rejected(self):
        # F1 (prueba #3): el catálogo no dice nada del procesador y el modelo
        # afirmó que la GPU era un microprocesador.
        context = {
            "product": {
                "id": 1,
                "price": 1100,
                "currency": "USD",
                "specifications": {},
            }
        }
        ok, reason = guardrails.validate_reply(
            "La GPU RTX 4060 es un microprocesador especializado.", context
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "spec")

    def test_a_spec_that_is_in_the_catalog_passes(self):
        context = {
            "product": {
                "id": 1,
                "price": 1100,
                "currency": "USD",
                "specifications": {"procesador": "i5 1250H"},
            }
        }
        ok, reason = guardrails.validate_reply(
            "Trae un procesador i5 1250H.", context
        )
        self.assertTrue(ok, reason)

    def test_an_honest_refusal_about_a_missing_spec_passes(self):
        context = {
            "product": {
                "id": 1,
                "price": 1100,
                "currency": "USD",
                "specifications": {},
            }
        }
        ok, reason = guardrails.validate_reply(
            "No tengo ese dato del procesador; lo consulto con el almacén.",
            context,
        )
        self.assertTrue(ok, reason)

    def test_a_reply_without_a_product_skips_the_spec_check(self):
        # El texto que viene del TCP se valida con un contexto sin producto:
        # sus palabras no se bloquean por el catálogo.
        ok, reason = guardrails.validate_reply(
            "Es un i5 y tiene una GPU RTX 4060.", {"quoted_prices": []}
        )
        self.assertTrue(ok, reason)

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


class LogisticsZonePostSaleTests(Phase1TestCase):
    """F6/F8/F9 de la prueba #3: logística, zona sin cobertura y post-venta."""

    def setUp(self):
        super().setUp()
        conn = db.connect()
        try:
            conn.executemany(
                "INSERT INTO zones (id, name, active) VALUES (?, ?, ?)",
                [
                    (1, "San Miguel", 1),
                    (2, "UCI", 0),
                ],
            )
            conn.execute(
                """INSERT INTO supplier_delivery_zones (supplier_id, zone_id)
                   VALUES (1, 1)"""
            )
            conn.commit()
        finally:
            conn.close()

    # --------------------------------------------------------------- helpers

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

    def _conversation(self) -> dict:
        conn = db.connect()
        try:
            customer = conversations.get_or_create_customer(
                conn, "telegram", "55500001", "Cliente"
            )
            return conversations.get_or_create_open_conversation(
                conn, customer["id"], "telegram", identity="bot"
            )
        finally:
            conn.close()

    def _set_context(self, context: dict) -> None:
        conn = db.connect()
        try:
            conversations.set_context(conn, 1, context)
        finally:
            conn.close()

    def _sale(self, status: str = "PENDING_DISPATCH") -> dict:
        conversation = self._conversation()
        conn = db.connect()
        try:
            sale = sales.create_sale(
                conn,
                conversation_id=conversation["id"],
                customer_id=conversation["customer_id"],
                product_id=1,
                quantity=1,
                recipient_name="Rolando",
                delivery_zone="San Miguel",
                delivery_address="Calle 10 numero 123",
                customer_phone="+5355500001",
            )
            if sale["status"] != status:
                conn.execute(
                    "UPDATE sales SET status = ? WHERE id = ?", (status, sale["id"])
                )
                conn.commit()
                sale = sales.get_sale(conn, sale["id"])
            return sale
        finally:
            conn.close()

    def _escalations(self) -> int:
        return self._scalar("SELECT COUNT(*) FROM escalations")

    # ------------------------------------------------------------------ F6

    def test_logistics_is_not_escalated_before_an_order(self):
        self._turn(
            "¿Cuándo me la traes?",
            llm.Intent(
                intencion="consulta_dato", producto="Power Bank", confianza=0.9
            ),
        )
        body = self._outbound()[-1]
        self.assertIn("se coordina al confirmar", body)
        self.assertEqual(self._escalations(), 0)

    def test_a_dispatched_order_is_not_answered_with_a_promise(self):
        sale = self._sale("DISPATCHED")
        conversation = self._conversation()
        conn = db.connect()
        try:
            product = catalog.get_product(conn, 1)
            outcome = pipeline._logistics_outcome(
                conn,
                conversation,
                "¿Cuándo llega?",
                product,
                {"stage": "DONE", "sale_id": sale["id"]},
            )
        finally:
            conn.close()
        self.assertIsNone(outcome)

    # ------------------------------------------------------------------ F8

    def test_post_sale_reply_uses_the_order_status(self):
        sale = self._sale("PENDING_DISPATCH")
        self._set_context(
            {
                "stage": "DONE",
                "sale_id": sale["id"],
                "order_code": sale["order_code"],
                "product_id": 1,
            }
        )
        self._turn(
            "¿Cuándo me la traes?",
            llm.Intent(
                intencion="consulta_dato", producto="Power Bank", confianza=0.9
            ),
        )
        body = self._outbound()[-1]
        self.assertIn(sale["order_code"], body)
        self.assertIn("pendiente", body)
        self.assertNotIn("12.5", body)
        self.assertEqual(self._escalations(), 0)

    def test_post_sale_delivered_order_says_so(self):
        sale = self._sale("DELIVERED")
        self._set_context(
            {
                "stage": "DONE",
                "sale_id": sale["id"],
                "order_code": sale["order_code"],
                "product_id": 1,
            }
        )
        self._turn(
            "¿Ya llegó?",
            llm.Intent(
                intencion="consulta_dato", producto="Power Bank", confianza=0.9
            ),
        )
        body = self._outbound()[-1]
        self.assertIn(sale["order_code"], body)
        self.assertIn("entregado", body.lower())

    def test_a_post_sale_technical_question_keeps_its_route(self):
        sale = self._sale("PENDING_DISPATCH")
        conversation = self._conversation()
        conn = db.connect()
        try:
            product = catalog.get_product(conn, 1)
            outcome = pipeline._post_sale_outcome(
                conn,
                conversation,
                "¿Qué garantía trae?",
                llm.Intent(intencion="consulta_dato", confianza=0.9),
                product,
                {
                    "stage": "DONE",
                    "sale_id": sale["id"],
                    "order_code": sale["order_code"],
                    "product_id": 1,
                },
            )
        finally:
            conn.close()
        self.assertIsNone(outcome)

    # ------------------------------------------------------------------ F9

    def test_an_uncovered_zone_is_rejected_before_the_address(self):
        self._turn(
            "Soy de la UCI, haces domicilio ahi",
            llm.Intent(
                intencion="consulta_precio", producto="Power Bank", confianza=0.9
            ),
        )
        body = self._outbound()[-1]
        self.assertIn("no la cubre", body)
        self.assertEqual(self._escalations(), 0)
        self.assertEqual(self._scalar("SELECT COUNT(*) FROM sales"), 0)
        context = json.loads(
            self._rows("SELECT context_json FROM conversations")[0]["context_json"]
        )
        self.assertEqual(context.get("stage"), "CLOSE")

    def test_a_covered_zone_is_not_rejected(self):
        self._turn(
            "Vivo en San Miguel",
            llm.Intent(
                intencion="consulta_precio", producto="Power Bank", confianza=0.9
            ),
        )
        self.assertNotIn("no la cubre", self._outbound()[-1])

    def test_short_zone_names_resolve_with_a_hint(self):
        conn = db.connect()
        try:
            self.assertIsNone(catalog.resolve_zone(conn, "Vivo en la UCI"))
            zone = catalog.resolve_zone(conn, "Vivo en la UCI", active_only=False)
        finally:
            conn.close()
        self.assertEqual(zone["id"], 2)


class ConversationPolishTests(Phase1TestCase):
    """Prueba #4: saludos repetidos, mensajes banales, stock y alternativas."""

    def setUp(self):
        super().setUp()
        conn = db.connect()
        try:
            conn.executemany(
                "INSERT INTO zones (id, name, active) VALUES (?, ?, ?)",
                [(1, "San Miguel", 1), (2, "UCI", 0)],
            )
            conn.execute(
                """INSERT INTO supplier_delivery_zones (supplier_id, zone_id)
                   VALUES (1, 1)"""
            )
            conn.executemany(
                """INSERT INTO products
                   (id, supplier_id, name, price, currency, commission, stock, status)
                   VALUES (?, 1, ?, ?, 'USD', 1, ?, 'ACTIVE')""",
                [
                    (10, "Laptop Gamer RTX 4060", 1100.0, 1),
                    (11, "Laptop Empresarial", 350.0, 5),
                    (12, "Laptop Ultraligera", 720.0, 5),
                    (13, "Monitor LED 24", 180.0, 5),
                    (14, "Tablet Android 10", 220.0, 5),
                ],
            )
            conn.commit()
        finally:
            conn.close()

    # --------------------------------------------------------------- helpers

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

    def _store_facts(self) -> int:
        conn = db.connect()
        try:
            customer = conversations.get_or_create_customer(
                conn, "telegram", "55500001", "Cliente"
            )
            conversation = conversations.get_or_create_open_conversation(
                conn, customer["id"], "telegram", identity="bot"
            )
            conversation_id = conversation["id"]
            for key, value in (
                (memory.FACT_ZONE, "San Miguel"),
                (memory.FACT_ADDRESS, "Calle 10 numero 123"),
                (memory.FACT_PHONE, "+5355500001"),
                (memory.FACT_CUSTOMER_NAME, "Rolando"),
            ):
                memory.upsert_fact(conn, conversation_id, key, value)
            return conversation_id
        finally:
            conn.close()

    def _conversation_id(self) -> int:
        return self._store_facts()

    def _context(self) -> dict:
        conn = db.connect()
        try:
            return conversations.get_context(conn, self._conversation_id())
        finally:
            conn.close()

    def _sale(self, status: str = "PENDING_DISPATCH") -> dict:
        conversation_id = self._store_facts()
        conn = db.connect()
        try:
            sale = sales.create_sale(
                conn,
                conversation_id=conversation_id,
                customer_id=1,
                product_id=1,
                quantity=1,
                recipient_name="Rolando",
                delivery_zone="San Miguel",
                delivery_address="Calle 10 numero 123",
                customer_phone="+5355500001",
            )
            if sale["status"] != status:
                conn.execute(
                    "UPDATE sales SET status = ? WHERE id = ?", (status, sale["id"])
                )
                conn.commit()
                sale = sales.get_sale(conn, sale["id"])
            return sale
        finally:
            conn.close()

    def _set_context(self, context: dict) -> None:
        conn = db.connect()
        try:
            conversations.set_context(conn, self._conversation_id(), context)
        finally:
            conn.close()

    # ------------------------------------------------------ F12 (banales)

    def test_banal_classifier_only_matches_the_whole_message(self):
        self.assertEqual(pipeline._banal_kind("gracias"), "thanks")
        self.assertEqual(pipeline._banal_kind("muchas gracias!"), "thanks")
        self.assertEqual(pipeline._banal_kind("disculpa"), "sorry")
        self.assertEqual(pipeline._banal_kind("ok, vale"), "ack")
        self.assertIsNone(pipeline._banal_kind("gracias, ¿y el precio?"))
        self.assertIsNone(pipeline._banal_kind("si"))

    def test_thanks_after_a_sale_gets_a_banal_reply(self):
        sale = self._sale()
        self._set_context(
            {
                "stage": "DONE",
                "sale_id": sale["id"],
                "order_code": sale["order_code"],
                "product_id": 1,
            }
        )
        # El NLU real clasificaba "gracias" como comprar; el fast path ni lo llama.
        with patch(
            "cuadra.ai.llm.classify_light",
            new=AsyncMock(
                return_value=llm.Intent(
                    intencion="comprar", producto="Power Bank", confianza=0.95
                )
            ),
        ) as classify_light:
            self._ingest("gracias")
            self._make_due()
            self._run_process()
        body = self._outbound()[-1]
        self.assertIn(body, prompts.TEMPLATE_VARIANTS["banal_thanks"])
        self.assertNotIn(sale["order_code"], body)
        self.assertNotIn("quedó registrado", body)
        self.assertEqual(classify_light.await_count, 0)
        self.assertEqual(self._context().get("stage"), "DONE")

    def test_banal_message_does_not_create_a_sale(self):
        self._store_facts()
        self._turn(
            "ok",
            llm.Intent(intencion="comprar", producto="Power Bank", confianza=0.95),
        )
        self.assertEqual(self._scalar("SELECT COUNT(*) FROM sales"), 0)
        self.assertIn(self._outbound()[-1], prompts.TEMPLATE_VARIANTS["banal_ack"])

    # ------------------------------------------------------ F13 (saludos)

    def test_product_quote_does_not_greet_twice(self):
        self._turn(
            "precio del Power Bank",
            llm.Intent(
                intencion="consulta_precio", producto="Power Bank", confianza=0.95
            ),
        )
        self._turn(
            "precio del Power Bank",
            llm.Intent(
                intencion="consulta_precio", producto="Power Bank", confianza=0.95
            ),
        )
        bodies = self._outbound()
        self.assertTrue(bodies[0].startswith("¡Buenas!"))
        self.assertNotIn("Buenas", bodies[1])

    def test_greeting_typos_are_recognized(self):
        for text in ("buens tardes", "bueas tardes", "buens dias"):
            self.assertTrue(pipeline._is_greeting(text), text)

    def test_second_greeting_within_cooldown_does_not_repeat_buenas(self):
        self._turn("Hola", llm.Intent(intencion="otro", confianza=0.9))
        self._turn("buens tardes", llm.Intent(intencion="otro", confianza=0.9))
        bodies = self._outbound()
        self.assertIn("Buenas", bodies[0])
        self.assertNotIn("Buenas", bodies[1])
        self.assertIn("ayudo", bodies[1])

    # ------------------------------------------------------ F14 (stock)

    def test_quantity_above_stock_says_how_many_remain(self):
        self._store_facts()
        self._turn(
            "quiero 5 laptop gaming",
            llm.Intent(
                intencion="comprar",
                producto="laptop gaming",
                cantidad=5,
                confianza=0.95,
            ),
        )
        body = self._outbound()[-1]
        self.assertIn("solo me queda 1", body)
        self.assertNotIn("Monitor LED", body)
        self.assertNotIn("Tablet Android", body)
        self.assertEqual(self._scalar("SELECT COUNT(*) FROM sales"), 0)
        self.assertEqual(self._context().get("sale_qty"), 1)

    def test_confirming_the_remaining_unit_creates_a_one_unit_sale(self):
        self._store_facts()
        self._turn(
            "quiero 5 laptop gaming",
            llm.Intent(
                intencion="comprar",
                producto="laptop gaming",
                cantidad=5,
                confianza=0.95,
            ),
        )
        self._turn("Si", llm.Intent(intencion="otro", confianza=0.9))
        sales_rows = self._rows("SELECT quantity, product_id FROM sales")
        self.assertEqual(len(sales_rows), 1)
        self.assertEqual(sales_rows[0]["quantity"], 1)
        self.assertEqual(sales_rows[0]["product_id"], 10)

    def test_stock_question_answers_the_available_quantity(self):
        self._store_facts()
        self._turn(
            "quiero 5 laptop gaming",
            llm.Intent(
                intencion="comprar",
                producto="laptop gaming",
                cantidad=5,
                confianza=0.95,
            ),
        )
        self._turn(
            "cuantas puedo comprar",
            llm.Intent(intencion="consulta_stock", confianza=0.95),
        )
        body = self._outbound()[-1]
        self.assertIn("me queda 1", body)
        self.assertIn("Laptop Gamer", body)

    def test_category_plus_qualifier_answers_stock(self):
        self._turn(
            "Quiero comprar laptops gaming, que cantidad tienes en stock",
            llm.Intent(
                intencion="consulta_stock",
                producto="laptops gaming",
                confianza=0.95,
            ),
        )
        body = self._outbound()[-1]
        self.assertIn("Laptop Gamer", body)
        self.assertIn("1 unidad", body)
        self.assertNotIn("Laptop Empresarial", body)

    # ------------------------------------------- F13/F14 (catálogo)

    def test_offers_of_laptops_lists_the_family(self):
        self._turn(
            "Que ofertas de laptops tienes",
            llm.Intent(
                intencion="consulta_precio", producto="laptop", confianza=0.95
            ),
        )
        body = self._outbound()[-1]
        for name in ("Laptop Empresarial", "Laptop Ultraligera", "Laptop Gamer"):
            self.assertIn(name, body)
        self.assertIn("¿Cuál te interesa?", body)

    def test_no_other_model_lists_same_family_siblings(self):
        self._set_context({"stage": "QUOTE", "product_id": 11})
        self._turn(
            "No tienes mas ningun tipo q no sea la empresarial",
            llm.Intent(
                intencion="consulta_stock",
                producto="Laptop Empresarial",
                confianza=0.95,
            ),
        )
        body = self._outbound()[-1]
        self.assertIn("Laptop Ultraligera", body)
        self.assertIn("Laptop Gamer", body)
        self.assertNotIn("Monitor LED", body)
        self.assertNotIn("Tablet Android", body)

    # ------------------------------------------------------ F15 (specs)

    def test_escalation_dedupes_processor_topics(self):
        self._turn(
            "Que microprocesador tiene",
            llm.Intent(
                intencion="consulta_dato",
                producto="Laptop Gamer",
                datos_faltantes=["microprocesador", "procesador"],
                confianza=0.95,
            ),
        )
        question = self._rows("SELECT question FROM escalations")[0]["question"]
        self.assertIn("procesador", question)
        self.assertNotIn("microprocesador", question)
        self.assertEqual(question.count("procesador"), 1)

    # ------------------------------------------- F16 (specs inventadas)

    def test_guard_context_exposes_specs_from_a_search_row(self):
        # `find_product` devuelve la fila SQL cruda: solo trae
        # `specifications_json`. Sin normalizarla, `check_specs` no veía las
        # especificaciones y dejaba pasar datos inventados (mensaje 14).
        conn = db.connect()
        try:
            conn.execute(
                "UPDATE products SET specifications_json = ? WHERE id = 10",
                (json.dumps({"memoria RAM": "16 GB"}),),
            )
            conn.commit()
            row = catalog.find_product(conn, "Laptop Gamer RTX 4060")
            self.assertNotIn("specifications", row)
            guard = pipeline._guard_context(row, None, {})
            self.assertEqual(guard["product"]["specifications"], {"memoria RAM": "16 GB"})
            self.assertTrue(guardrails.check_specs("Tiene 16 GB de RAM.", guard))
            self.assertFalse(
                guardrails.check_specs("Tiene una pantalla de 15 pulgadas.", guard)
            )
        finally:
            conn.close()

    # ------------------------------------------------------ F9 (zona)

    def test_soy_de_an_inactive_zone_is_rejected(self):
        self._turn(
            "Perfecto, soy de la UCI, calle 45 entre 12 y Calixto, numero 1222, 56476666",
            llm.Intent(
                intencion="consulta_precio",
                producto="Laptop Gamer",
                confianza=0.95,
            ),
        )
        body = self._outbound()[-1]
        self.assertIn("no la cubre", body)
        self.assertEqual(self._scalar("SELECT COUNT(*) FROM sales"), 0)


class PruebaCincoTests(Phase1TestCase):
    """Prueba #5: ficha determinista, tabla de catálogo y cobertura de zonas."""

    def setUp(self):
        super().setUp()
        conn = db.connect()
        try:
            conn.execute(
                "UPDATE products SET description = ? WHERE id = 1",
                ("Lenovo ThinkPad\ni5 11va gen\n16GB RAM\n512GB SSD",),
            )
            conn.executemany(
                """INSERT INTO products
                   (id, supplier_id, name, price, currency, commission, stock,
                    status, description)
                   VALUES (?, 1, ?, ?, 'USD', 1, ?, 'ACTIVE', ?)""",
                [
                    (
                        2,
                        "Laptop Ultraligera",
                        720.0,
                        3,
                        "ASUS ZenBook\nRyzen 7\n16GB RAM\n1TB SSD",
                    ),
                    (
                        3,
                        "Laptop Gamer RTX 4060",
                        1100.0,
                        1,
                        "Laptop gaming\nRTX 4060\n16GB RAM\n1TB SSD",
                    ),
                ],
            )
            conn.executemany(
                "INSERT INTO zones (id, name, active) VALUES (?, ?, ?)",
                [(1, "Marianao", 1), (2, "La Lisa", 0), (3, "UCI", 0)],
            )
            conn.execute(
                """INSERT INTO supplier_delivery_zones (supplier_id, zone_id)
                   VALUES (1, 1)"""
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

    def _escalations(self) -> int:
        return self._scalar("SELECT COUNT(*) FROM escalations")

    # --------------------------------------------- contexto y ficha

    def test_history_window_carries_twenty_messages(self):
        self.assertEqual(config.HISTORY_WINDOW, 20)
        self.assertGreaterEqual(config.LLM_MAX_PROMPT_TOKENS, 3200)

    def test_open_details_come_from_the_description_without_the_composer(self):
        compose = AsyncMock(return_value="inventado")
        with patch("cuadra.ai.llm.compose", new=compose):
            self._turn(
                "ID 2, ¿qué propiedades tiene?",
                llm.Intent(intencion="consulta_dato", producto=None, confianza=0.9),
            )
        body = self._outbound()[-1]
        self.assertIn("Ryzen 7", body)
        self.assertIn("16GB RAM", body)
        self.assertIn("1TB SSD", body)
        self.assertEqual(compose.await_count, 0)
        self.assertEqual(self._escalations(), 0)

    def test_a_concrete_detail_present_is_answered_from_the_ficha(self):
        compose = AsyncMock(return_value="inventado")
        with patch("cuadra.ai.llm.compose", new=compose):
            self._turn(
                "ID 2, ¿qué procesador trae?",
                llm.Intent(intencion="consulta_dato", producto=None, confianza=0.9),
            )
        body = self._outbound()[-1]
        self.assertIn("Ryzen 7", body)
        self.assertEqual(compose.await_count, 0)
        self.assertEqual(self._escalations(), 0)

    def test_a_missing_detail_answers_what_it_has_and_escalates_only_that(self):
        compose = AsyncMock(return_value="inventado")
        with patch("cuadra.ai.llm.compose", new=compose):
            self._turn(
                "ID 2, ¿qué garantía trae?",
                llm.Intent(intencion="consulta_dato", producto=None, confianza=0.9),
            )
        body = self._outbound()[-1]
        self.assertIn("Ryzen 7", body)
        self.assertIn("No tengo el dato de garantía", body)
        self.assertEqual(compose.await_count, 0)
        self.assertEqual(self._escalations(), 1)
        question = self._rows("SELECT question FROM escalations")[0]["question"]
        self.assertIn("garantía", question)

    def test_the_guardrail_accepts_specs_from_the_description(self):
        context = {
            "product": {
                "id": 1,
                "price": 12.5,
                "currency": "USD",
                "description": "Ryzen 7\n16GB RAM\n1TB SSD",
                "specifications": {},
            }
        }
        ok, reason = guardrails.validate_reply("Trae Ryzen 7 y 16GB RAM.", context)
        self.assertTrue(ok, reason)
        ok, reason = guardrails.validate_reply(
            "Tiene pantalla OLED de 15 pulgadas.", context
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "spec")

    # --------------------------------------------- tabla de catálogo

    def test_a_bare_category_is_listed_as_a_table_and_never_sold(self):
        self._turn(
            "Quiero ver laptops",
            llm.Intent(intencion="comprar", producto="laptops", confianza=0.9),
        )
        body = self._outbound()[-1]
        self.assertIn("```", body)
        self.assertIn("ID", body)
        self.assertIn("Producto", body)
        self.assertIn("Precio", body)
        self.assertIn("Laptop Ultraligera", body)
        self.assertIn("Dime el ID o el nombre", body)
        self.assertEqual(self._scalar("SELECT COUNT(*) FROM sales"), 0)

    def test_the_table_fence_is_only_allowed_for_the_listing(self):
        table = "Esto es lo que tengo:\n```\nID Producto\n1 Laptop\n```"
        self.assertTrue(guardrails.check_format(table, allow_table=True))
        self.assertFalse(guardrails.check_format(table))

    def test_a_conversational_category_request_lists_the_family(self):
        """\"ando buscando una laptop\" es una familia, no un producto al azar."""
        self._turn(
            "ando buscando una laptop",
            llm.Intent(intencion="consulta_precio", producto=None, confianza=0.9),
        )
        body = self._outbound()[-1]
        self.assertIn("```", body)
        self.assertIn("Laptop Ultraligera", body)
        # La columna de nombre se recorta para caber en el móvil.
        self.assertIn("Laptop Gamer", body)
        self.assertIn("1100 USD", body)
        self.assertNotIn("📦", body)
        self.assertNotIn(prompts.PRODUCT_CARD_CTA, body)
        self.assertEqual(self._scalar("SELECT COUNT(*) FROM sales"), 0)

    def test_the_listing_includes_products_out_of_stock(self):
        conn = db.connect()
        try:
            conn.execute(
                """INSERT INTO products
                   (id, supplier_id, name, price, currency, commission, stock,
                    status, description)
                   VALUES (4, 1, 'Laptop Básica', 300.0, 'USD', 1, 0,
                           'OUT_OF_STOCK', 'Celeron\n8GB RAM\n256GB SSD')"""
            )
            conn.commit()
        finally:
            conn.close()
        self._turn(
            "¿qué laptops tienes?",
            llm.Intent(
                intencion="consulta_precio", producto="laptops", confianza=0.95
            ),
        )
        body = self._outbound()[-1]
        self.assertIn("Laptop Básica", body)
        self.assertIn("agotado", body)

    def test_a_family_with_one_product_answers_with_the_card(self):
        conn = db.connect()
        try:
            conn.execute("INSERT INTO categories (id, name) VALUES (9, 'Tablet')")
            conn.execute(
                """INSERT INTO products
                   (id, supplier_id, category_id, name, price, currency,
                    commission, stock, status, description)
                   VALUES (4, 1, 9, 'Tablet Android 10', 220.0, 'USD', 1, 4,
                           'ACTIVE', 'Android 13\n10 pulgadas\n64GB')"""
            )
            conn.commit()
        finally:
            conn.close()
        self._turn(
            "¿qué tablets tienen?",
            llm.Intent(
                intencion="consulta_precio", producto="tablets", confianza=0.95
            ),
        )
        body = self._outbound()[-1]
        self.assertIn("Tablet Android 10", body)
        self.assertIn("220 USD", body)
        self.assertIn("ID 4", body)
        self.assertIn("4 unidades disponibles", body)
        self.assertIn(prompts.PRODUCT_CARD_CTA, body)
        self.assertNotIn("```", body)

    def test_the_card_brings_every_detail_in_one_message(self):
        self._turn(
            "precio de la Laptop Ultraligera",
            llm.Intent(
                intencion="consulta_precio",
                producto="Laptop Ultraligera",
                confianza=0.95,
            ),
        )
        body = self._outbound()[-1]
        self.assertIn("Laptop Ultraligera", body)
        self.assertIn("720 USD", body)
        self.assertIn("ID 2", body)
        self.assertIn("3 unidades disponibles", body)
        self.assertIn("Ryzen 7", body)
        self.assertIn("16GB RAM", body)
        self.assertIn("El transporte se coordina", body)
        self.assertIn(prompts.PRODUCT_CARD_CTA, body)
        # La zona no aparece hasta que toque la dirección.
        self.assertNotIn("Entregamos en", body)
        self.assertNotIn("Marianao", body)

    def test_a_bare_yes_after_the_card_starts_the_sale(self):
        """Prueba #7: «si» tras la ficha no puede volver a listar la familia.

        El NLU repite el producto de la ficha ("Laptop Ultraligera") y la
        palabra "si" no califica la familia; el turno debe avanzar la venta.
        """
        self._turn(
            "precio de la Laptop Ultraligera",
            llm.Intent(
                intencion="consulta_precio",
                producto="Laptop Ultraligera",
                confianza=0.95,
            ),
        )
        self.assertIn(prompts.PRODUCT_CARD_CTA, self._outbound()[-1])
        self._turn(
            "si",
            llm.Intent(
                intencion="comprar",
                producto="Laptop Ultraligera",
                confianza=0.95,
            ),
        )
        body = self._outbound()[-1]
        self.assertNotIn("```", body)
        self.assertNotIn("Dime el ID o el nombre", body)
        self.assertIn("me falta", body)
        self.assertEqual(self._scalar("SELECT COUNT(*) FROM sales"), 0)

    def test_other_properties_is_not_an_alternatives_request(self):
        self.assertFalse(pipeline._asks_for_alternatives("¿qué otras propiedades tiene?"))
        self.assertTrue(pipeline._asks_for_open_details("¿qué otras propiedades tiene?"))

    # --------------------------------------------- zonas

    def test_a_short_zone_resolves_without_a_delivery_hint(self):
        conn = db.connect()
        try:
            zone = catalog.resolve_zone(conn, "La UCI", active_only=False)
        finally:
            conn.close()
        self.assertIsNotNone(zone)
        self.assertEqual(zone["name"], "UCI")

    def test_a_short_zone_resolves_inside_a_delivery_question(self):
        """\"¿Entregan en Playa?\" es una pregunta de entrega, no la palabra playa."""
        conn = db.connect()
        try:
            conn.execute("INSERT INTO zones (id, name, active) VALUES (5, 'Playa', 1)")
            conn.commit()
            zones = catalog.resolve_zones(conn, "¿Entregan en Playa?", active_only=False)
        finally:
            conn.close()
        self.assertEqual([zone["name"] for zone in zones], ["Playa"])

    def test_multiple_zones_answer_by_coverage_instead_of_escalating(self):
        self._turn(
            "¿Entregan en Marianao o La Lisa?",
            llm.Intent(
                intencion="consulta_precio",
                producto="Laptop Ultraligera",
                confianza=0.95,
            ),
        )
        body = self._outbound()[-1]
        self.assertIn("Marianao", body)
        self.assertIn("La Lisa", body)
        self.assertIn("no llegamos", body)
        self.assertEqual(self._escalations(), 0)

    def test_a_delivery_question_in_a_covered_zone_is_answered_yes(self):
        self._turn(
            "¿Entregan en Marianao?",
            llm.Intent(
                intencion="consulta_dato",
                producto="Laptop Ultraligera",
                confianza=0.95,
            ),
        )
        body = self._outbound()[-1]
        self.assertIn("sí entregamos", body.lower())
        self.assertEqual(self._escalations(), 0)

    def test_a_delivery_question_without_a_zone_lists_the_real_zones(self):
        self._turn(
            "ID 2, ¿puedes entregar en mi zona?",
            llm.Intent(intencion="consulta_dato", producto=None, confianza=0.9),
        )
        body = self._outbound()[-1]
        self.assertIn("Marianao", body)
        self.assertEqual(self._escalations(), 0)

    def _restrict_product_to_playa(self, product_id: int) -> None:
        """El TCP cubre Marianao y Playa; el producto solo se entrega en Playa."""
        conn = db.connect()
        try:
            conn.execute(
                "INSERT INTO zones (id, name, active) VALUES (4, 'Playa', 1)"
            )
            conn.execute(
                """INSERT INTO supplier_delivery_zones (supplier_id, zone_id)
                   VALUES (1, 4)"""
            )
            catalog.set_product_zones(conn, product_id, [4])
            conn.commit()
        finally:
            conn.close()

    def test_a_product_zone_question_respects_its_own_coverage(self):
        """El TCP llega a Marianao, pero este producto no."""
        self._restrict_product_to_playa(2)
        self._turn(
            "¿Entregan en Marianao?",
            llm.Intent(
                intencion="consulta_dato",
                producto="Laptop Ultraligera",
                confianza=0.95,
            ),
        )
        body = self._outbound()[-1]
        self.assertIn("no la cubre", body)
        self.assertNotIn("sí entregamos", body.lower())
        self.assertEqual(self._escalations(), 0)

    def test_a_zone_question_without_a_zone_lists_the_product_zones(self):
        self._restrict_product_to_playa(2)
        self._turn(
            "ID 2, ¿puedes entregar en mi zona?",
            llm.Intent(intencion="consulta_dato", producto=None, confianza=0.9),
        )
        body = self._outbound()[-1]
        self.assertIn("Playa", body)
        self.assertNotIn("Marianao", body)
        self.assertEqual(self._escalations(), 0)


class PruebaOchoTests(Phase1TestCase):
    """Prueba #8: una petición de catálogo no es un detalle ni una venta."""

    def setUp(self):
        super().setUp()
        conn = db.connect()
        try:
            conn.execute("INSERT INTO categories (id, name) VALUES (1, 'tecnología')")
            conn.execute("INSERT INTO categories (id, name) VALUES (3, 'accesorios')")
            conn.executemany(
                """INSERT INTO products
                   (id, supplier_id, category_id, name, price, currency,
                    commission, stock, status, description)
                   VALUES (?, 1, ?, ?, ?, 'USD', 1, ?, 'ACTIVE', ?)""",
                [
                    (
                        2,
                        1,
                        "Laptop Ultraligera",
                        720.0,
                        1,
                        "ASUS ZenBook\nRyzen 7\n16GB RAM\n1TB SSD",
                    ),
                    (3, 3, "Mouse Inalámbrico", 15.0, 25, ""),
                    (4, 3, "Teclado Mecánico", 45.0, 10, ""),
                    (5, 3, "Audífonos Bluetooth", 35.0, 8, ""),
                ],
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

    def _conversation(self) -> dict:
        conn = db.connect()
        try:
            customer = conversations.get_or_create_customer(
                conn, "telegram", "55500001", "Cliente"
            )
            return conversations.get_or_create_open_conversation(
                conn, customer["id"], "telegram", identity="bot"
            )
        finally:
            conn.close()

    def _set_context(self, context: dict) -> None:
        conversation = self._conversation()
        conn = db.connect()
        try:
            conversations.set_context(conn, conversation["id"], context)
        finally:
            conn.close()

    def _context(self) -> dict:
        conversation = self._conversation()
        conn = db.connect()
        try:
            return conversations.get_context(conn, conversation["id"])
        finally:
            conn.close()

    # ------------------------------------------------- ruta de catálogo

    def test_only_explicit_catalog_requests_take_the_listing_path(self):
        self.assertTrue(
            pipeline._catalog_request(
                "Que productos tienes de accesorios para laptop"
            )
        )
        self.assertTrue(pipeline._catalog_request("¿qué laptops tienes?"))
        self.assertTrue(pipeline._catalog_request("dame un listado de laptops"))
        # Un dato concreto no es un listado: sigue siendo detalle del producto.
        self.assertFalse(pipeline._catalog_request("¿qué procesador trae la laptop?"))
        self.assertFalse(
            pipeline._catalog_request("¿qué características tiene la laptop?")
        )
        self.assertFalse(pipeline._catalog_request("¿cuántas laptops tienes?"))
        self.assertFalse(pipeline._catalog_request("precio de la Laptop Ultraligera"))

    def test_a_catalog_question_after_a_done_sale_lists_the_accessories(self):
        self._set_context(
            {
                "stage": "DONE",
                "sale_id": 7,
                "order_code": "V-000001",
                "product_id": 2,
            }
        )
        compose = AsyncMock(return_value="Cuadra no tiene accesorios")
        with patch("cuadra.ai.llm.compose", new=compose):
            self._turn(
                "Que productos tienes de accesorios para laptop",
                llm.Intent(
                    intencion="consulta_dato", producto=None, confianza=0.95
                ),
            )
        body = self._outbound()[-1]
        for name in ("Mouse Inalámbrico", "Teclado Mecánico", "Audífonos Bluetooth"):
            self.assertIn(name, body)
        self.assertIn("```", body)
        self.assertNotIn("Laptop Ultraligera", body)
        self.assertNotIn("no tiene accesorios", body)
        # El listado se responde con el catálogo real, no con el compositor.
        self.assertEqual(compose.await_count, 0)
        # Una consulta nueva vuelve a DISCOVERY y suelta el producto de la venta.
        context = self._context()
        self.assertEqual(context.get("stage"), "DISCOVERY")
        self.assertNotIn("product_id", context)
        self.assertEqual(context.get("sale_id"), 7)
        self.assertEqual(context.get("order_code"), "V-000001")

    def test_a_catalog_question_does_not_inherit_the_previous_product(self):
        self._set_context({"stage": "PRODUCT_MATCH", "product_id": 2})
        self._turn(
            "¿Qué accesorios tienes?",
            llm.Intent(intencion="consulta_dato", producto=None, confianza=0.95),
        )
        body = self._outbound()[-1]
        self.assertIn("Mouse Inalámbrico", body)
        self.assertNotIn("Laptop Ultraligera", body)
        context = self._context()
        self.assertNotIn("product_id", context)
        self.assertEqual(context.get("stage"), "DISCOVERY")

    def test_a_detail_question_still_answers_the_product(self):
        self._set_context({"stage": "PRODUCT_MATCH", "product_id": 2})
        compose = AsyncMock(return_value="inventado")
        with patch("cuadra.ai.llm.compose", new=compose):
            self._turn(
                "¿Qué procesador trae la laptop?",
                llm.Intent(intencion="consulta_dato", producto=None, confianza=0.95),
            )
        body = self._outbound()[-1]
        self.assertIn("Ryzen 7", body)
        self.assertNotIn("```", body)
        self.assertEqual(compose.await_count, 0)

    # ------------------------------------------------ L3.1 referencias
    def test_a_listing_is_remembered_for_the_next_turn(self):
        self._turn(
            "¿Qué accesorios tienes?",
            llm.Intent(intencion="consulta_dato", confianza=0.95),
        )
        listing = self._context().get("last_listing")
        self.assertIsNotNone(listing)
        self.assertEqual(listing["product_ids"], [3, 4, 5])
        self.assertEqual(listing["query"], "accesorios")

    def test_el_segundo_uses_the_listing_over_the_nlu_product(self):
        self._turn(
            "¿Qué accesorios tienes?",
            llm.Intent(intencion="consulta_dato", confianza=0.95),
        )
        self._turn(
            "el segundo",
            llm.Intent(
                intencion="consulta_precio",
                producto="Laptop Ultraligera",
                confianza=0.95,
            ),
        )
        body = self._outbound()[-1]
        self.assertIn("Teclado Mecánico", body)
        self.assertNotIn("Laptop Ultraligera", body)

    def test_esos_relists_the_same_products_without_inventing(self):
        self._turn(
            "¿Qué accesorios tienes?",
            llm.Intent(intencion="consulta_dato", confianza=0.95),
        )
        self._turn(
            "¿cuánto cuestan esos?",
            llm.Intent(intencion="consulta_precio", confianza=0.95),
        )
        body = self._outbound()[-1]
        for name in ("Mouse Inalámbrico", "Teclado Mecánico", "Audífonos Bluetooth"):
            self.assertIn(name, body)
        self.assertNotIn("Laptop Ultraligera", body)

    def test_a_catalog_question_labelled_otro_still_lists(self):
        # El NLU puede devolver `otro` para "¿qué accesorios tienes?". El
        # catálogo es una lectura real: no debe caer al compositor.
        compose = AsyncMock(return_value="Cuadra no tiene accesorios")
        with patch("cuadra.ai.llm.compose", new=compose):
            self._turn(
                "¿Qué accesorios tienes?",
                llm.Intent(intencion="otro", confianza=0.9),
            )
        body = self._outbound()[-1]
        for name in ("Mouse Inalámbrico", "Teclado Mecánico", "Audífonos Bluetooth"):
            self.assertIn(name, body)
        self.assertNotIn("no tiene accesorios", body)
        self.assertEqual(compose.await_count, 0)
        self.assertEqual(self._context().get("last_listing")["product_ids"], [3, 4, 5])

    def test_el_segundo_labelled_otro_uses_the_listing(self):
        self._turn(
            "¿Qué accesorios tienes?",
            llm.Intent(intencion="otro", confianza=0.9),
        )
        compose = AsyncMock(return_value="inventado")
        with patch("cuadra.ai.llm.compose", new=compose):
            self._turn(
                "El segundo me interesa",
                llm.Intent(intencion="otro", confianza=0.9),
            )
        body = self._outbound()[-1]
        self.assertIn("Teclado Mecánico", body)
        self.assertNotIn("Mouse Inalámbrico", body)
        self.assertEqual(compose.await_count, 0)

    def test_a_catalog_request_during_a_sale_keeps_the_sale(self):
        self._set_context(
            {
                "stage": "CAPTURE_DATA",
                "product_id": 2,
                "sale_qty": 1,
                "sale_qty_product_id": 2,
            }
        )
        self._turn(
            "¿Qué accesorios tienes?",
            llm.Intent(intencion="consulta_dato", confianza=0.95),
        )
        body = self._outbound()[-1]
        self.assertIn("Mouse Inalámbrico", body)
        context = self._context()
        self.assertEqual(context.get("stage"), "CAPTURE_DATA")
        self.assertEqual(context.get("product_id"), 2)
        self.assertEqual(context.get("sale_qty"), 1)


if __name__ == "__main__":
    unittest.main()
