"""Phase 2: facts, incremental summary, hybrid retrieval and idle closing.

Throwaway SQLite database, no model server, no network. Run from `backend/`:

    .venv\\Scripts\\python.exe -m unittest discover -s tests -v
"""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cuadra import config, db
from cuadra.ai import context, memory, retrieval
from cuadra.domain import catalog, conversations


class _FakeSummarizer:
    def __init__(self, value: str = "Resumen de prueba"):
        self.value = value
        self.calls = []

    async def summarize(self, previous_summary, messages, *, conversation_id=None):
        self.calls.append((previous_summary, messages, conversation_id))
        return self.value


class Phase2TestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_db_path = config.DB_PATH
        config.DB_PATH = Path(self._tmp.name) / "test.db"
        conn = db.connect()
        try:
            db.apply_migrations(conn)
            conn.execute(
                "INSERT INTO categories (id, name) VALUES (1, 'Energía')"
            )
            conn.execute(
                """INSERT INTO suppliers (id, name, delivery_zone)
                   VALUES (1, 'TCP Uno', 'La Habana')"""
            )
            conn.executemany(
                "INSERT INTO zones (id, name, active) VALUES (?, ?, ?)",
                [
                    (1, "San Miguel", 1),
                    (2, "Vedado", 1),
                    (3, "Zona Inactiva", 0),
                    (4, "Regla", 1),
                ],
            )
            conn.execute(
                """INSERT INTO supplier_delivery_zones (supplier_id, zone_id)
                   VALUES (1, 1)"""
            )
            products = [
                (1, 1, 1, "Batería Portátil 20000", "Carga rápida para teléfonos",
                 25.0, "ACTIVE", '{"capacidad": "20000 mAh"}'),
                (2, 1, 1, "Power Bank 10000", "Batería externa compacta",
                 12.5, "ACTIVE", "{}"),
                (3, 1, 1, "Lámpara LED", "Luz para el hogar",
                 8.0, "ACTIVE", "{}"),
                (4, 1, 1, "Batería Pausada", "No debe aparecer en búsquedas",
                 30.0, "PAUSED", "{}"),
                (5, 1, 1, "Cargador Solar", "Carga al sol",
                 18.0, "OUT_OF_STOCK", "{}"),
            ]
            conn.executemany(
                """INSERT INTO products
                   (id, supplier_id, category_id, name, description, price,
                    currency, commission, stock, status, specifications_json)
                   VALUES (?, ?, ?, ?, ?, ?, 'USD', 1, 3, ?, ?)""",
                products,
            )
            conn.execute(
                """INSERT INTO customers (id, name, preferences_json)
                   VALUES (1, '', '{}')"""
            )
            conn.execute(
                """INSERT INTO conversations (id, customer_id, channel, status)
                   VALUES (1, 1, 'telegram', 'ACTIVE')"""
            )
            conn.commit()
        finally:
            conn.close()
        self.conn = db.connect()
        self.conversation = conversations.get_conversation(self.conn, 1)

    def tearDown(self):
        self.conn.close()
        config.DB_PATH = self._old_db_path
        self._tmp.cleanup()


class RetrievalTests(Phase2TestCase):
    def test_fts_is_filled_by_triggers_and_ignores_accents(self):
        ids = retrieval._fts_search(self.conn, "bateria", limit=10)
        self.assertIn(1, ids)
        self.assertIn(2, ids)
        self.assertNotIn(4, ids)  # PAUSED is never searchable

        self.conn.execute("UPDATE products SET name = 'Batería LED' WHERE id = 3")
        self.conn.commit()
        ids = retrieval._fts_search(self.conn, "bateria led", limit=10)
        self.assertIn(3, ids)

    def test_plural_query_matches_a_singular_product_name(self):
        # Customers say "cargadores"; the catalog stores "Cargador Solar".
        ids = retrieval._fts_search(self.conn, "tienes cargadores disponibles", limit=10)
        self.assertIn(5, ids)

        ids = retrieval._fts_search(self.conn, "quiero dos baterias", limit=10)
        self.assertIn(1, ids)
        self.assertIn(2, ids)

    def test_rrf_preserves_agreement_between_both_lists(self):
        self.assertEqual(
            retrieval.reciprocal_rank_fusion([1, 2, 3], [3, 1], k=60),
            [(1, 1 / 61 + 1 / 62), (3, 1 / 63 + 1 / 61), (2, 1 / 62)],
        )

    def test_fuzzy_fallback_when_both_layers_return_nothing(self):
        with patch.object(retrieval, "_fts_search", return_value=[]), patch.object(
            retrieval, "_vector_search", return_value=[]
        ):
            ids = retrieval.search_ids(self.conn, "Power Bank", top_k=3)
            self.assertEqual(ids, [2])
            results = retrieval.search(self.conn, "Power Bank", top_k=3)
        self.assertEqual(results[0]["id"], 2)
        self.assertIn("match_score", results[0])

    def test_weak_semantic_matches_are_dropped(self):
        # "nevera" is not in this catalog: the dense layer must not return the
        # closest unrelated product just because it is the closest one.
        close = retrieval._pack([1.0, 0.0])
        far = retrieval._pack([0.0, 1.0])
        for product_id, vector in ((2, close), (3, far)):
            self.conn.execute(
                """INSERT INTO product_embeddings
                   (product_id, model, dim, vector, text_hash)
                   VALUES (?, ?, 2, ?, ?)""",
                (product_id, config.EMBEDDING_MODEL, vector, f"hash-{product_id}"),
            )
        self.conn.commit()
        with patch.object(retrieval, "_get_model", return_value=object()), patch.object(
            retrieval, "embed_text", return_value=[0.95, 0.31]
        ):
            # id 2 is above the floor, id 3 (cosine 0.31) is dropped.
            self.assertEqual(retrieval._vector_search(self.conn, "nevera", limit=5), [2])

    def test_index_without_model_is_a_noop(self):
        with patch.object(retrieval, "_get_model", return_value=None):
            self.assertFalse(retrieval.index_product(self.conn, 1))
            self.assertFalse(retrieval.reindex_all(self.conn)["available"])


class ZoneTests(Phase2TestCase):
    def test_long_name_resolves_to_the_real_zone(self):
        zone = catalog.resolve_zone(self.conn, "Vivo en San Miguel del Padrón")
        self.assertIsNotNone(zone)
        self.assertEqual(zone["name"], "San Miguel")

    def test_alias_is_normalised_and_can_be_added(self):
        catalog.add_zone_alias(self.conn, 1, "San Miguel del Padrón")
        zone = catalog.resolve_zone(self.conn, "san miguel del padron")
        self.assertEqual(zone["id"], 1)

    def test_common_word_needs_delivery_context_to_be_a_zone(self):
        self.assertIsNone(
            catalog.resolve_zone(self.conn, "Necesito una regla para medir")
        )
        zone = catalog.resolve_zone(self.conn, "La entrega es en Regla")
        self.assertIsNotNone(zone)
        self.assertEqual(zone["name"], "Regla")

    def test_inactive_or_unknown_zone_is_not_guessed(self):
        self.assertIsNone(catalog.resolve_zone(self.conn, "Zona Inactiva"))
        self.assertIsNone(catalog.resolve_zone(self.conn, "Marianao"))


class MemoryTests(Phase2TestCase):
    def test_facts_are_updated_in_place_and_profile_survives(self):
        product = catalog.get_product(self.conn, 1)
        memory.capture_facts(
            self.conn,
            self.conversation,
            "Vivo en San Miguel del Padrón, mi nombre es Carlos, "
            "tengo 50 USD de presupuesto y mi teléfono es 71234567",
            product=product,
        )
        facts = memory.facts_map(self.conn, 1)
        self.assertEqual(facts[memory.FACT_ZONE], "San Miguel")
        self.assertEqual(facts[memory.FACT_CUSTOMER_NAME], "Carlos")
        self.assertEqual(facts[memory.FACT_BUDGET], "50 USD")
        self.assertEqual(facts[memory.FACT_PHONE], "+53 71234567")
        self.assertEqual(facts[memory.FACT_PRODUCT_NAME], "Batería Portátil 20000")

        before = len(memory.list_facts(self.conn, 1))
        memory.capture_facts(self.conn, self.conversation, "Mi presupuesto es 80 USD")
        self.assertEqual(len(memory.list_facts(self.conn, 1)), before)
        self.assertEqual(memory.facts_map(self.conn, 1)[memory.FACT_BUDGET], "80 USD")

        profile = memory.customer_profile(self.conn, 1)
        self.assertEqual(profile["nombre"], "Carlos")
        self.assertEqual(profile["zona_habitual"], "San Miguel")
        self.assertEqual(profile["telefono"], "+53 71234567")

    def test_phone_normalisation_rejects_short_numbers(self):
        self.assertEqual(memory.compose_phone("+53 7 123 4567"), "+53 71234567")
        self.assertEqual(memory.compose_phone("71234567"), "+53 71234567")
        self.assertIsNone(memory.compose_phone("123"))

    def test_incremental_summary_advances_by_message_id_not_count(self):
        old_window, old_every = config.HISTORY_WINDOW, config.SUMMARY_EVERY
        config.HISTORY_WINDOW, config.SUMMARY_EVERY = 4, 2
        try:
            message_ids = []
            for index in range(10):
                message = conversations.add_message(
                    self.conn, 1, "INBOUND", "customer", body=f"mensaje {index}"
                )
                message_ids.append(message["id"])
            expected_through = message_ids[5]  # 10 rows, delta = first 6

            summarizer = _FakeSummarizer()
            result = asyncio.run(
                memory.maybe_summarize(
                    self.conn, self.conversation, llm_client=summarizer
                )
            )
            self.assertTrue(result)
            stored = conversations.get_context(self.conn, 1)
            self.assertEqual(stored["summary"], "Resumen de prueba")
            self.assertEqual(stored["summary_through_message_id"], expected_through)
            sent_ids = [row["id"] for row in summarizer.calls[0][1]]
            self.assertEqual(sent_ids, message_ids[:6])

            # Four old + two new unseen messages: still not enough to advance.
            for index in range(2):
                conversations.add_message(
                    self.conn, 1, "INBOUND", "customer", body=f"nuevo {index}"
                )
            self.assertFalse(
                asyncio.run(
                    memory.maybe_summarize(
                        self.conn, self.conversation, llm_client=_FakeSummarizer()
                    )
                )
            )
        finally:
            config.HISTORY_WINDOW, config.SUMMARY_EVERY = old_window, old_every

    def test_facts_survive_fifty_messages_and_a_summary(self):
        old_window, old_every = config.HISTORY_WINDOW, config.SUMMARY_EVERY
        config.HISTORY_WINDOW, config.SUMMARY_EVERY = 8, 10
        try:
            memory.capture_facts(
                self.conn,
                self.conversation,
                "Vivo en San Miguel, mi nombre es Carlos y tengo 50 USD "
                "de presupuesto para una bater\u00eda",
                product=catalog.get_product(self.conn, 1),
            )
            for index in range(50):
                conversations.add_message(
                    self.conn, 1, "INBOUND", "customer", body=f"mensaje {index}"
                )
            self.assertTrue(
                asyncio.run(
                    memory.maybe_summarize(
                        self.conn, self.conversation, llm_client=_FakeSummarizer()
                    )
                )
            )
            facts = memory.facts_map(self.conn, 1)
            self.assertEqual(facts[memory.FACT_PRODUCT_NAME], "Batería Portátil 20000")
            self.assertEqual(facts[memory.FACT_ZONE], "San Miguel")
            self.assertEqual(facts[memory.FACT_BUDGET], "50 USD")
            system = context.ContextBuilder(
                self.conn,
                conversations.get_conversation(self.conn, 1),
            )._system(None, [])
            for expected in ("Batería Portátil 20000", "San Miguel", "50 USD"):
                self.assertIn(expected, system)
        finally:
            config.HISTORY_WINDOW, config.SUMMARY_EVERY = old_window, old_every

    def test_summary_is_not_taken_while_a_turn_is_processing(self):
        old_window, old_every = config.HISTORY_WINDOW, config.SUMMARY_EVERY
        config.HISTORY_WINDOW, config.SUMMARY_EVERY = 2, 1
        try:
            for index in range(6):
                conversations.add_message(
                    self.conn, 1, "INBOUND", "customer", body=f"msg {index}"
                )
            self.conn.execute(
                """INSERT INTO turns
                   (conversation_id, status, due_at, message_ids_json)
                   VALUES (1, 'PROCESSING', datetime('now'), '[]')"""
            )
            self.conn.commit()
            summarizer = _FakeSummarizer("no debería ejecutarse")
            self.assertFalse(
                asyncio.run(
                    memory.maybe_summarize(
                        self.conn, self.conversation, llm_client=summarizer
                    )
                )
            )
            self.assertEqual(summarizer.calls, [])
        finally:
            config.HISTORY_WINDOW, config.SUMMARY_EVERY = old_window, old_every

    def test_context_always_contains_facts_and_profile(self):
        memory.capture_facts(
            self.conn,
            self.conversation,
            "Vivo en Vedado y mi nombre es Ana",
        )
        system = context.ContextBuilder(
            self.conn, self.conversation, budget=700
        )._system(None, [])
        self.assertIn("HECHOS FIJADOS", system)
        self.assertIn("Vedado", system)
        self.assertIn("PERFIL DEL CLIENTE", system)
        self.assertIn("Ana", system)


class CatalogZonesTests(Phase2TestCase):
    def test_product_json_exposes_zone_list_and_legacy_fallback(self):
        product = catalog.get_product(self.conn, 1)
        payload = catalog.llm_product_json(self.conn, product)
        self.assertEqual(payload["zonas_entrega"], ["San Miguel"])
        self.assertEqual(payload["zona_entrega"], "San Miguel")

        self.conn.execute("DELETE FROM supplier_delivery_zones")
        self.conn.commit()
        payload = catalog.llm_product_json(self.conn, catalog.get_product(self.conn, 1))
        self.assertEqual(payload["zonas_entrega"], ["La Habana"])

    def test_delete_zone_only_deactivates_it(self):
        result = catalog.delete_zone(self.conn, 2)  # Vedado
        self.assertEqual(result["id"], 2)
        self.assertEqual(result["active"], 0)

        zones = {zone["name"]: zone for zone in catalog.list_zones(self.conn)}
        self.assertIn("Vedado", zones)  # the row is preserved
        self.assertEqual(zones["Vedado"]["active"], 0)
        self.assertIsNone(catalog.resolve_zone(self.conn, "Vedado"))


class IdleClosingTests(Phase2TestCase):
    def test_idle_conversation_is_closed_and_a_new_one_is_created(self):
        self.conn.execute(
            "UPDATE conversations SET updated_at = datetime('now', '-100 hours') WHERE id = 1"
        )
        self.conn.commit()
        self.assertEqual(conversations.close_idle(self.conn, hours=72), 1)
        closed = conversations.get_conversation(self.conn, 1)
        self.assertEqual(closed["status"], "CLOSED")
        self.assertIsNotNone(closed["closed_at"])

        fresh = conversations.get_or_create_open_conversation(
            self.conn, 1, "telegram"
        )
        self.assertNotEqual(fresh["id"], 1)
        self.assertEqual(fresh["status"], "ACTIVE")

    def test_recent_conversation_stays_open(self):
        self.assertEqual(conversations.close_idle(self.conn, hours=72), 0)
        self.assertEqual(
            conversations.get_conversation(self.conn, 1)["status"], "ACTIVE"
        )


if __name__ == "__main__":
    unittest.main()
