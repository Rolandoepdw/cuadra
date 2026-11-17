"""Phase 4: TCP triage, catalog mutations, undo and audit.

Throwaway SQLite database, no model server, no network. The extractor is
replaced by a deterministic fake in the handler test. Run from `backend/`:

    .venv\\Scripts\\python.exe -m unittest discover -s tests -v
"""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from cuadra import config, db
from cuadra.ai import handlers, ingest, tcp_extractor, tcp_triage
from cuadra.domain import catalog, catalog_mutations
from cuadra.domain import escalations
from cuadra.domain.errors import Conflict, Invalid


class Phase4Fixture(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_db_path = config.DB_PATH
        config.DB_PATH = Path(self._tmp.name) / "test.db"
        self.conn = db.connect()
        db.apply_migrations(self.conn)
        self._seed()

    def tearDown(self):
        self.conn.close()
        config.DB_PATH = self._old_db_path
        self._tmp.cleanup()

    def _seed(self):
        conn = self.conn
        conn.execute("INSERT INTO categories (id, name) VALUES (1, 'Energía')")
        conn.executemany(
            """INSERT INTO suppliers (id, name, telegram_id, delivery_zone)
               VALUES (?, ?, ?, 'La Habana')""",
            [
                (1, "TCP Uno", "111222333"),
                (2, "TCP Dos", "444555666"),
            ],
        )
        conn.executemany(
            "INSERT INTO zones (id, name, active) VALUES (?, ?, ?)",
            [
                (1, "San Miguel", 1),
                (2, "Vedado", 1),
                (3, "Centro Habana", 1),
                (4, "Zona Inactiva", 0),
            ],
        )
        conn.executemany(
            "INSERT INTO supplier_delivery_zones (supplier_id, zone_id) VALUES (?, ?)",
            [(1, 1), (1, 2), (2, 3)],
        )
        products = [
            # id, supplier, name, price, stock, reserved, status
            (1, 1, "Power Bank 10000mAh", 25.0, 5, 1, "ACTIVE"),
            (2, 2, "Cargador USB C", 30.0, 10, 0, "ACTIVE"),
            (3, 1, "Cable HDMI 2m", 100.0, 1, 0, "PAUSED"),
            (4, 1, "Batería Portátil", 40.0, 4, 0, "ACTIVE"),
        ]
        conn.executemany(
            """INSERT INTO products
               (id, supplier_id, category_id, name, description, price,
                currency, commission, commission_currency, commission_type,
                stock, stock_reserved, status, specifications_json)
               VALUES (?, ?, 1, ?, 'Producto de prueba', ?, 'USD', 5.0, 'USD',
                       'FIXED', ?, ?, ?, '{"garantia": "3 meses"}')""",
            [
                (pid, supplier, name, price, stock, reserved, status)
                for pid, supplier, name, price, stock, reserved, status in products
            ],
        )
        conn.executemany(
            "INSERT INTO customers (id, name, preferences_json) VALUES (?, ?, '{}')",
            [(1, "Ana"), (2, "Luis"), (3, "TCP Uno"), (4, "TCP Dos")],
        )
        conn.executemany(
            """INSERT INTO conversations (id, customer_id, channel, status)
               VALUES (?, ?, 'telegram', ?)""",
            [
                (1, 1, "WAITING_TCP"),
                (2, 2, "WAITING_TCP"),
                (3, 3, "ACTIVE"),
                (4, 4, "ACTIVE"),
            ],
        )
        conn.executemany(
            """INSERT INTO escalations
               (id, conversation_id, product_id, supplier_id, question, status)
               VALUES (?, ?, ?, ?, ?, 'PENDING')""",
            [
                (1, 1, 1, 1, "¿Tienes stock?",),
                (2, 2, 1, 1, "¿Garantía?",),
                (3, 3, 2, 2, "¿Stock?",),
                (4, 1, 4, 1, "¿Precio?",),
            ],
        )
        conn.execute(
            """INSERT INTO messages (id, conversation_id, direction, author, type, body)
               VALUES (10, 3, 'INBOUND', 'tcp', 'text', 'mensaje inicial')"""
        )
        conn.commit()

    def product(self, product_id: int = 1) -> dict:
        return catalog.get_product(self.conn, product_id)

    def extraction(self, **values):
        base = {
            "has_catalog_update": True,
            "confidence": 0.95,
            "new_specifications": {},
            "delivery_zone_names": [],
            "customer_answer_summary": "",
        }
        base.update(values)
        return base

    def apply(self, **values):
        return catalog_mutations.apply_tcp_update(
            self.conn,
            product_id=values.pop("product_id", 1),
            supplier_id=values.pop("supplier_id", 1),
            escalation_id=values.pop("escalation_id", None),
            source_message_id=values.pop("source_message_id", None),
            source_text=values.pop("source_text", None),
            extraction=self.extraction(**values),
        )


class TestExtractionAndCorrelation(Phase4Fixture):
    def test_parse_case_ids_accepts_whisper_forms(self):
        ids, codes = tcp_triage.parse_case_ids("ref [E-0104] y E 12, venta v-42")
        self.assertEqual(ids, [104, 12])
        self.assertEqual(codes, ["V-000042"])

    def test_classify(self):
        self.assertEqual(tcp_triage.classify("ref [E-0003]"), "ESCALATION_ANSWER")
        self.assertEqual(tcp_triage.classify("eso ya se entregó"), "ORDER_DELIVERED")
        self.assertEqual(tcp_triage.classify("me quedan 2"), "CATALOG_UPDATE")
        self.assertEqual(tcp_triage.classify("¿qué te parece?"), "QUESTION")

    def test_explicit_code_wins(self):
        result = tcp_triage.correlate(self.conn, supplier_id=1, text="[E-0004] precio 45")
        self.assertEqual(result.escalation["id"], 4)
        self.assertEqual(result.product_id, 4)
        self.assertEqual(result.method, "case_id")
        self.assertFalse(result.ambiguous)

    def test_case_of_another_supplier_is_not_found(self):
        result = tcp_triage.correlate(self.conn, supplier_id=2, text="[E-0001]")
        self.assertFalse(result.found)
        self.assertEqual(result.reason, "case_not_found")

    def test_single_pending(self):
        result = tcp_triage.correlate(self.conn, supplier_id=2, text="me quedan 3")
        self.assertEqual(result.escalation["id"], 3)
        self.assertEqual(result.method, "single_pending")

    def test_multiple_pending_clear_winner(self):
        result = tcp_triage.correlate(
            self.conn, supplier_id=1, text="del power bank me quedan 2"
        )
        self.assertEqual(result.product_id, 1)
        self.assertEqual(result.method, "heuristic")
        self.assertFalse(result.ambiguous)

    def test_multiple_pending_ambiguous(self):
        result = tcp_triage.correlate(
            self.conn, supplier_id=1, text="me quedan 2 unidades"
        )
        self.assertTrue(result.ambiguous)
        self.assertIsNotNone(result.escalation)
        self.assertFalse(result.reason == "")

    def test_proactive_update_matches_product(self):
        self.conn.execute(
            "UPDATE escalations SET status = 'RESOLVED' WHERE supplier_id = 1"
        )
        self.conn.commit()
        result = tcp_triage.correlate(
            self.conn, supplier_id=1, text="bajé el precio de la Batería Portátil"
        )
        self.assertEqual(result.product_id, 4)
        self.assertFalse(result.ambiguous)


class TestCatalogMutations(Phase4Fixture):
    def test_price_within_range_applies_and_reverts(self):
        mutation = self.apply(price=28.0)
        self.assertEqual(mutation["status"], "APPLIED")
        self.assertEqual(mutation["mutation_type"], "PRICE")
        self.assertEqual(self.product()["price"], 28.0)

        reverted = catalog_mutations.revert(self.conn, mutation["id"], reason="error")
        self.assertEqual(reverted["status"], "REVERTED")
        self.assertEqual(self.product()["price"], 25.0)

    def test_price_out_of_range_goes_to_review_then_approves(self):
        mutation = self.apply(price=300.0)
        self.assertEqual(mutation["status"], "PENDING_REVIEW")
        self.assertIn("variación de precio", mutation["notes"])
        self.assertEqual(self.product()["price"], 25.0)

        approved = catalog_mutations.approve(self.conn, mutation["id"])
        self.assertEqual(approved["status"], "APPLIED")
        self.assertEqual(approved["applied_by"], "operator")
        self.assertEqual(self.product()["price"], 300.0)

    def test_low_confidence_goes_to_review(self):
        mutation = self.apply(stock=2, confidence=0.5)
        self.assertEqual(mutation["status"], "PENDING_REVIEW")
        self.assertEqual(self.product()["stock"], 5)

    def test_mlc_is_never_stored(self):
        mutation = self.apply(currency="MLC", price=28.0)
        self.assertEqual(mutation["status"], "PENDING_REVIEW")
        self.assertIn("MLC", mutation["notes"])
        product = self.product()
        self.assertEqual(product["currency"], "USD")
        self.assertEqual(product["price"], 25.0)

    def test_zone_change_always_requires_review(self):
        mutation = self.apply(delivery_zone_names=["Vedado", "Centro Habana"])
        self.assertEqual(mutation["status"], "PENDING_REVIEW")
        self.assertIn("zona", mutation["notes"].lower())
        self.assertEqual(
            [z["id"] for z in catalog.list_supplier_zones(self.conn, 1)], [1, 2]
        )

    def test_stock_zero_forces_out_of_stock(self):
        mutation = self.apply(stock=0)
        self.assertEqual(mutation["status"], "APPLIED")
        self.assertEqual(self.product()["status"], "OUT_OF_STOCK")
        self.assertEqual(self.product()["stock"], 0)

    def test_stock_back_activates_non_paused_product(self):
        self.conn.execute(
            "UPDATE products SET status = 'OUT_OF_STOCK', stock = 0 WHERE id = 1"
        )
        self.conn.commit()
        mutation = self.apply(stock=3)
        self.assertEqual(mutation["status"], "APPLIED")
        self.assertEqual(self.product()["status"], "ACTIVE")

    def test_paused_product_stays_paused(self):
        mutation = self.apply(stock=2, product_id=3)
        self.assertEqual(mutation["status"], "APPLIED")
        self.assertEqual(self.product(3)["status"], "PAUSED")

    def test_ambiguous_correlation_is_not_applied(self):
        mutation = catalog_mutations.apply_tcp_update(
            self.conn,
            product_id=1,
            supplier_id=1,
            extraction=self.extraction(stock=2),
            correlation_method="heuristic",
            ambiguous=True,
        )
        self.assertEqual(mutation["status"], "PENDING_REVIEW")
        self.assertTrue(mutation["ambiguous"])
        self.assertEqual(self.product()["stock"], 5)

    def test_undo_of_old_mutation_conflicts(self):
        first = self.apply(price=28.0)
        self.apply(stock=3)
        with self.assertRaises(Conflict):
            catalog_mutations.revert(self.conn, first["id"])
        self.assertEqual(self.product()["price"], 28.0)
        self.assertEqual(self.product()["stock"], 3)

    def test_states_match_ignores_reserved_stock(self):
        mutation = self.apply(stock=2)
        self.conn.execute("UPDATE products SET stock_reserved = 1 WHERE id = 1")
        self.conn.commit()
        reverted = catalog_mutations.revert(self.conn, mutation["id"])
        self.assertEqual(reverted["status"], "REVERTED")

    def test_approve_detects_later_change(self):
        mutation = self.apply(price=300.0)
        self.conn.execute("UPDATE products SET price = 26 WHERE id = 1")
        self.conn.commit()
        with self.assertRaises(Conflict):
            catalog_mutations.approve(self.conn, mutation["id"])

    def test_reject(self):
        mutation = self.apply(price=300.0)
        rejected = catalog_mutations.reject(self.conn, mutation["id"], reason="caro")
        self.assertEqual(rejected["status"], "REJECTED")
        self.assertEqual(rejected["notes"], "caro")
        self.assertEqual(self.product()["price"], 25.0)

    def test_source_message_id_is_idempotent(self):
        first = catalog_mutations.apply_tcp_update(
            self.conn,
            product_id=1,
            supplier_id=1,
            source_message_id=10,
            extraction=self.extraction(stock=2),
        )
        second = catalog_mutations.apply_tcp_update(
            self.conn,
            product_id=1,
            supplier_id=1,
            source_message_id=10,
            extraction=self.extraction(stock=1),
        )
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(
            catalog_mutations.count_mutations(self.conn), 1
        )
        self.assertEqual(self.product()["stock"], 2)

    def test_specs_merge_without_overwriting(self):
        mutation = self.apply(new_specifications={"garantia": "6 meses"})
        self.assertEqual(mutation["status"], "APPLIED")
        specs = self.product()["specifications"]
        self.assertEqual(specs["garantia"], "6 meses")
        self.assertEqual(self.product()["specifications"], specs)

    def test_oversold_flags_product_and_sales(self):
        self.conn.execute(
            """INSERT INTO sales
               (id, order_code, conversation_id, product_id, supplier_id,
                customer_id, quantity, amount, currency, commission,
                commission_currency, status)
               VALUES (1, 'V-000001', 1, 1, 1, 1, 2, 50.0, 'USD', 5.0, 'USD',
                       'DISPATCHED')"""
        )
        self.conn.commit()
        mutation = self.apply(stock=0)
        self.assertEqual(mutation["status"], "APPLIED")
        self.assertEqual(self.product()["status"], "OUT_OF_STOCK")
        sale = self.conn.execute("SELECT status FROM sales WHERE id = 1").fetchone()
        self.assertEqual(sale["status"], "NEEDS_OPERATOR")

    def test_list_filters_and_parsed_states(self):
        self.apply(price=28.0)
        self.apply(price=300.0)
        page = catalog_mutations.list_mutations(self.conn, status="PENDING_REVIEW")
        self.assertEqual(len(page), 1)
        self.assertIn("price", page[0]["previous_state"])
        self.assertIn("price", page[0]["new_state"])
        self.assertEqual(
            catalog_mutations.count_mutations(self.conn, status="APPLIED"), 1
        )
        self.assertEqual(catalog_mutations.pending_review_count(self.conn), 1)


class TestResumption(Phase4Fixture):
    def test_resume_all_waiting_conversations_once_each(self):
        sends = tcp_triage.resume_escalations(
            self.conn,
            product_id=1,
            supplier_id=1,
            answer="me quedan 2",
            summary="Me quedan 2 unidades.",
        )
        self.assertEqual(len(sends), 2)  # conversations 1 and 2
        conversation_ids = {cid for cid, _ in sends}
        self.assertEqual(conversation_ids, {1, 2})
        for escalation in escalations.open_for_product(self.conn, 1, 1):
            self.fail(f"escalation {escalation['id']} was not resolved")
        for conversation_id in (1, 2):
            row = self.conn.execute(
                "SELECT status FROM conversations WHERE id = ?", (conversation_id,)
            ).fetchone()
            self.assertEqual(row["status"], "ACTIVE")

    def test_resume_does_not_reopen_closed_conversation(self):
        self.conn.execute("UPDATE conversations SET status = 'CLOSED' WHERE id = 2")
        self.conn.commit()
        sends = tcp_triage.resume_escalations(
            self.conn, product_id=1, supplier_id=1, answer="sí hay"
        )
        self.assertEqual(len(sends), 1)
        row = self.conn.execute(
            "SELECT status FROM conversations WHERE id = 2"
        ).fetchone()
        self.assertEqual(row["status"], "CLOSED")


class TestCustomerAnswer(Phase4Fixture):
    """H-22: the client receives every detail the TCP actually answered."""

    def test_full_tcp_answer_wins_over_the_short_summary(self):
        body = tcp_triage._customer_reply(
            self.product(1),
            "Tiene 6 meses de garantía.",
            answer=(
                "[E-0001] El Power Bank 10000mAh viene con maletín y tiene "
                "6 meses de garantía."
            ),
        )
        self.assertIn("maletín", body)
        self.assertIn("garantía", body)
        self.assertNotIn("E-0001", body)
        self.assertIn("¿Te reservo", body)

    def test_internal_terms_are_redacted_from_the_answer(self):
        body = tcp_triage._customer_reply(
            self.product(1),
            "Me quedan 2 unidades.",
            answer="[E-0001] Me quedan 2. Mi comisión es 5 USD. Trae cable USB.",
        )
        self.assertIn("Me quedan 2", body)
        self.assertIn("Trae cable USB", body)
        self.assertNotIn("comisión", body.lower())

    def test_foreign_currency_falls_back_to_the_summary(self):
        body = tcp_triage._customer_reply(
            self.product(1),
            "Me quedan 2 unidades.",
            answer="[E-0001] Cuesta 4200 MLC y me quedan 2 unidades.",
        )
        self.assertNotIn("4200", body)
        self.assertNotIn("MLC", body)
        self.assertIn("Me quedan 2 unidades", body)

    def test_out_of_stock_answer_does_not_repeat_the_phrase(self):
        self.conn.execute(
            "UPDATE products SET stock = 0, status = 'OUT_OF_STOCK' WHERE id = 1"
        )
        self.conn.commit()
        body = tcp_triage._customer_reply(
            self.product(1), answer="[E-0001] Se agotó: quedan 0 unidades."
        )
        self.assertIn("quedan 0 unidades", body)
        self.assertNotIn("Por ahora no tenemos unidades", body)
        self.assertNotIn("¿Te reservo", body)
        self.assertIn("te avise cuando vuelva a entrar", body)

    def test_out_of_stock_without_stock_details_still_says_it(self):
        self.conn.execute(
            "UPDATE products SET stock = 0, status = 'OUT_OF_STOCK' WHERE id = 1"
        )
        self.conn.commit()
        body = tcp_triage._customer_reply(
            self.product(1), "Trae estuche y tiene 6 meses de garantía."
        )
        self.assertIn("no tenemos unidades", body)


class TestTcpHandler(Phase4Fixture):
    def _message(self, body: str, message_id: int = 20) -> int:
        self.conn.execute(
            """INSERT INTO messages (id, conversation_id, direction, author, type, body)
               VALUES (?, 3, 'INBOUND', 'tcp', 'text', ?)""",
            (message_id, body),
        )
        self.conn.commit()
        return message_id

    def _tcp_process(self, message_id: int):
        return asyncio.run(
            handlers.tcp_process(
                {
                    "conversation_id": 3,
                    "message_id": message_id,
                    "supplier_id": 1,
                }
            )
        )

    def test_handler_applies_stock_and_resumes_clients(self):
        message_id = self._message("[E-0001] me quedan 2 y subió el precio a 30")
        extraction = tcp_extractor.TCPUpdateExtraction(
            has_catalog_update=True,
            stock=2,
            price=30.0,
            confidence=0.92,
            customer_answer_summary="Me quedan 2 unidades y el precio es 30 USD.",
        )
        with patch(
            "cuadra.ai.handlers.tcp_extractor.extract",
            new=AsyncMock(return_value=extraction),
        ):
            self._tcp_process(message_id)

        product = self.product()
        self.assertEqual(product["stock"], 2)
        self.assertEqual(product["price"], 30.0)
        mutation = catalog_mutations.list_mutations(self.conn)[0]
        self.assertEqual(mutation["status"], "APPLIED")
        self.assertEqual(mutation["escalation_id"], 1)
        self.assertEqual(mutation["correlation_method"], "case_id")
        self.assertEqual(escalations.get(self.conn, 1)["status"], "RESOLVED")
        self.assertEqual(escalations.get(self.conn, 2)["status"], "RESOLVED")
        self.assertEqual(
            self.conn.execute(
                "SELECT status FROM conversations WHERE id = 1"
            ).fetchone()["status"],
            "ACTIVE",
        )

    def test_handler_sends_the_complete_tcp_answer(self):
        message_id = self._message(
            "[E-0001] Trae maletín y 6 meses de garantía. Me quedan 2 unidades."
        )
        extraction = tcp_extractor.TCPUpdateExtraction(
            has_catalog_update=True,
            stock=2,
            confidence=0.92,
            customer_answer_summary="Me quedan 2 unidades.",
        )
        with patch(
            "cuadra.ai.handlers.tcp_extractor.extract",
            new=AsyncMock(return_value=extraction),
        ):
            self._tcp_process(message_id)

        body = self.conn.execute(
            """SELECT body FROM messages
               WHERE conversation_id = 1 AND direction = 'OUTBOUND'
               ORDER BY id DESC LIMIT 1"""
        ).fetchone()["body"]
        self.assertIn("maletín", body)
        self.assertIn("garantía", body)
        self.assertIn("Me quedan 2 unidades", body)
        self.assertNotIn("E-0001", body)

    def test_handler_is_idempotent_on_retry(self):
        message_id = self._message("[E-0001] me quedan 2")
        extraction = tcp_extractor.TCPUpdateExtraction(
            has_catalog_update=True, stock=2, confidence=0.92
        )
        with patch(
            "cuadra.ai.handlers.tcp_extractor.extract",
            new=AsyncMock(return_value=extraction),
        ):
            self._tcp_process(message_id)
            self._tcp_process(message_id)

        self.assertEqual(catalog_mutations.count_mutations(self.conn), 1)
        outbound = self.conn.execute(
            """SELECT COUNT(*) FROM messages
               WHERE conversation_id = 1 AND direction = 'OUTBOUND'"""
        ).fetchone()[0]
        self.assertEqual(outbound, 1)

    def test_handler_falls_back_when_model_is_down(self):
        message_id = self._message("[E-0001] garantía de 6 meses")
        with patch(
            "cuadra.ai.handlers.tcp_extractor.extract",
            new=AsyncMock(return_value=None),
        ):
            self._tcp_process(message_id)

        self.assertEqual(catalog_mutations.count_mutations(self.conn), 0)
        escalation = escalations.get(self.conn, 1)
        self.assertEqual(escalation["status"], "RESOLVED")
        product = self.product()
        self.assertEqual(product["specifications"]["dato_proveedor"], "garantía de 6 meses")

    def test_a_delivery_ack_is_never_an_escalation_answer(self):
        # Prueba #3: "Entregado" cayó en la correlación de escalaciones y se
        # atribuyó a un producto cualquiera. Un ack sin venta no toca nada.
        message_id = self._message("Entregado")

        self._tcp_process(message_id)

        for escalation_id in (1, 2, 4):
            self.assertEqual(
                escalations.get(self.conn, escalation_id)["status"], "PENDING"
            )
        outbound = self.conn.execute(
            "SELECT COUNT(*) FROM messages WHERE direction = 'OUTBOUND'"
        ).fetchone()[0]
        self.assertEqual(outbound, 0)

    def test_an_ambiguous_answer_does_not_resolve_any_escalation(self):
        # Varias consultas abiertas y ninguna referencia: se pide la
        # referencia al TCP en vez de elegir por parecido.
        message_id = self._message("me quedan 2 unidades")

        self._tcp_process(message_id)

        for escalation_id in (1, 2, 4):
            self.assertEqual(
                escalations.get(self.conn, escalation_id)["status"], "PENDING"
            )
        job = self.conn.execute(
            "SELECT type FROM jobs WHERE type = 'ask_tcp_reference'"
        ).fetchone()
        self.assertIsNotNone(job)

    def test_audio_supplier_message_queues_transcription(self):
        result = ingest.ingest_incoming(
            channel="telegram",
            identifier="111222333",
            type="audio",
            file_path="notas/audio.ogg",
            external_id="tcp-audio-1",
        )
        job = self.conn.execute(
            "SELECT type, payload_json FROM jobs WHERE id = ?", (result["job_id"],)
        ).fetchone()
        self.assertEqual(job["type"], "transcribe_audio")
        self.assertIn('"next_job": "tcp_process"', job["payload_json"])
        self.assertEqual(result["source"], "supplier")


class TestOperatorNotifications(Phase4Fixture):
    """H-20 / H-21: out-of-stock wording and reject/revert notifications."""

    def test_mlc_price_never_reaches_the_proposed_state(self):
        mutation = self.apply(price=4200.0, currency="MLC")
        self.assertEqual(mutation["status"], "PENDING_REVIEW")
        self.assertEqual(mutation["new_state"]["price"], 25.0)
        self.assertEqual(mutation["new_state"]["currency"], "USD")
        self.assertIn("MLC", mutation["notes"])
        self.assertEqual(self.product()["price"], 25.0)

    def test_approve_refuses_a_legacy_incoherent_state(self):
        mutation = self.apply(price=30.0)
        self.assertEqual(mutation["status"], "APPLIED")
        # Simulate a row created before the H-21 fix: price + MLC currency.
        catalog_mutations.revert(self.conn, mutation["id"])
        state = dict(mutation["previous_state"])
        state.update({"price": 4200.0, "currency": "MLC"})
        cur = self.conn.execute(
            """INSERT INTO catalog_mutation_log (
                   product_id, supplier_id, mutation_type, previous_state_json,
                   new_state_json, previous_updated_at, confidence,
                   correlation_method, ambiguous, status, applied_by
               ) VALUES (1, 1, 'PRICE', ?, ?, ?, 0.95, 'case_id', 0,
                         'PENDING_REVIEW', 'bot')""",
            (
                mutation["previous_state_json"],
                json.dumps(state),
                mutation["previous_updated_at"],
            ),
        )
        self.conn.commit()
        with self.assertRaises(Invalid):
            catalog_mutations.approve(self.conn, int(cur.lastrowid))
        self.assertEqual(self.product()["price"], 25.0)

    def test_out_of_stock_summary_does_not_offer_a_reservation(self):
        self.conn.execute(
            "UPDATE products SET stock = 0, status = 'OUT_OF_STOCK' WHERE id = 2"
        )
        self.conn.commit()
        body = tcp_triage._customer_reply(
            self.product(2), "Trae estuche y tiene 6 meses de garantía."
        )
        self.assertIn("no tenemos unidades", body)
        self.assertNotIn("¿Te reservo", body)

    def test_available_product_still_offers_a_reservation(self):
        body = tcp_triage._customer_reply(self.product(1), "Me quedan 2 unidades.")
        self.assertIn("¿Te reservo", body)

    def test_reject_notifies_the_waiting_client(self):
        escalations.set_answer(self.conn, 4, "cuesta 4200 MLC")
        mutation = self.apply(price=300.0, escalation_id=4)
        self.assertEqual(mutation["status"], "PENDING_REVIEW")

        sends = tcp_triage.notify_mutation_outcome(
            self.conn, mutation, kind="rejected"
        )
        self.assertEqual([conversation for conversation, _ in sends], [1])
        body = self.conn.execute(
            "SELECT body FROM messages WHERE id = ?", (sends[0][1],)
        ).fetchone()["body"]
        self.assertNotIn("4200", body)
        self.assertIn("No pude confirmar", body)
        self.assertEqual(escalations.get(self.conn, 4)["status"], "RESOLVED")

    def test_revert_tells_the_client_the_restored_price(self):
        mutation = self.apply(price=30.0, escalation_id=1)
        self.assertEqual(mutation["status"], "APPLIED")
        escalations.set_answer(self.conn, 1, "subió a 30")
        catalog_mutations.revert(self.conn, mutation["id"])

        sends = tcp_triage.notify_mutation_outcome(
            self.conn,
            catalog_mutations.get(self.conn, mutation["id"]),
            kind="reverted",
        )
        self.assertEqual([conversation for conversation, _ in sends], [1])
        body = self.conn.execute(
            "SELECT body FROM messages WHERE id = ?", (sends[0][1],)
        ).fetchone()["body"]
        self.assertIn("25 USD", body)


if __name__ == "__main__":
    unittest.main()
