"""L3.0 TurnFrame: assemble, commit and the single context write point.

Run from `backend/`:

    .venv\\Scripts\\python.exe -m unittest tests.test_frame -v
"""

import tempfile
import unittest
from pathlib import Path

from cuadra import config, db
from cuadra.ai import llm
from cuadra.ai.turn import frame as turn_frame
from cuadra.domain import conversations


class FrameTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_db_path = config.DB_PATH
        config.DB_PATH = Path(self._tmp.name) / "test.db"
        conn = db.connect()
        try:
            db.apply_migrations(conn)
            conn.execute(
                "INSERT INTO customers (id, name) VALUES (1, 'Cliente')"
            )
            conn.execute(
                """INSERT INTO conversations (id, customer_id, channel)
                   VALUES (1, 1, 'telegram')"""
            )
            conn.execute(
                """INSERT INTO suppliers (id, name, delivery_zone)
                   VALUES (1, 'TCP Uno', 'La Habana')"""
            )
            conn.execute(
                """INSERT INTO products
                   (id, supplier_id, name, price, currency, commission, stock,
                    status)
                   VALUES (1, 1, 'Power Bank 10000', 12.5, 'USD', 2, 5,
                           'ACTIVE')"""
            )
            conn.commit()
        finally:
            conn.close()

    def tearDown(self):
        config.DB_PATH = self._old_db_path
        self._tmp.cleanup()

    def _conn(self):
        return db.connect()

    def _conversation(self, conn):
        return conversations.get_conversation(conn, 1)

    def _product(self, conn):
        from cuadra.domain import catalog

        return catalog.get_product(conn, 1)

    def _frame(self, conn, context=None):
        conversation = self._conversation(conn)
        context = context if context is not None else conversations.get_context(conn, 1)
        return turn_frame.build_frame(
            conn,
            conversation,
            {"id": 7, "last_message_id": 1},
            turn_messages=[],
            text="hola",
            context=context,
            history=[],
        )

    def test_build_frame_reads_the_persisted_context_without_changing_it(self):
        conn = self._conn()
        try:
            context = {
                "stage": "discovery",
                "product_id": 1,
                "last_listing": {
                    "product_ids": [1],
                    "labels": ["Power Bank 10000"],
                    "query": "power bank",
                    "kind": "category",
                },
                "pending_offer": {"kind": "reserve", "product_id": 1},
            }
            conversations.set_context(conn, 1, context)
            frame = self._frame(conn, context)
            self.assertEqual(frame.stage, "DISCOVERY")
            self.assertEqual(frame.focus_product, None)
            self.assertEqual(frame.last_listing.product_ids, [1])
            self.assertEqual(frame.pending_offer["kind"], "reserve")
            self.assertEqual(frame.turn_id, 7)
            self.assertEqual(conversations.get_context(conn, 1), context)
        finally:
            conn.close()

    def test_build_frame_survives_a_corrupt_listing(self):
        conn = self._conn()
        try:
            frame = self._frame(conn, {"last_listing": {"product_ids": []}})
            self.assertIsNone(frame.last_listing)
        finally:
            conn.close()

    def test_update_context_writes_the_same_dict_it_mutated(self):
        conn = self._conn()
        try:
            context = {"stage": "DISCOVERY"}
            conversation = self._conversation(conn)
            turn_frame._update_context(
                conn,
                conversation,
                llm.Intent(intencion="consulta_precio", confianza=0.9),
                self._product(conn),
                "llm",
                False,
                stage="PRESENTING",
                context=context,
            )
            stored = conversations.get_context(conn, 1)
            self.assertEqual(stored["stage"], "PRESENTING")
            self.assertEqual(stored["product_id"], 1)
            self.assertEqual(stored["last_intent"], "consulta_precio")
            self.assertFalse(stored["pending_escalation"])
        finally:
            conn.close()

    def test_clear_product_keeps_the_sale_for_post_sale_questions(self):
        conn = self._conn()
        try:
            context = {"product_id": 1, "sale_id": 3, "order_code": "CU-3"}
            conversation = self._conversation(conn)
            turn_frame._update_context(
                conn,
                conversation,
                llm.Intent(intencion="otro", confianza=0.5),
                None,
                "template",
                False,
                context=context,
                clear_product=True,
            )
            stored = conversations.get_context(conn, 1)
            self.assertNotIn("product_id", stored)
            self.assertEqual(stored["sale_id"], 3)
            self.assertEqual(stored["order_code"], "CU-3")
        finally:
            conn.close()

    def test_commit_persists_the_listing_added_in_this_turn(self):
        conn = self._conn()
        try:
            frame = self._frame(conn, {})
            frame.last_listing = turn_frame.Listing(
                product_ids=[1], labels=["Power Bank 10000"], kind="category"
            )
            turn_frame.commit(
                frame,
                intent=llm.Intent(intencion="otro", confianza=0.9),
                product=self._product(conn),
                generator="table",
                escalate=False,
                stage="PRESENTING",
            )
            stored = conversations.get_context(conn, 1)
            self.assertEqual(stored["last_listing"]["product_ids"], [1])
            self.assertEqual(stored["stage"], "PRESENTING")
            self.assertEqual(frame.stage, "PRESENTING")
        finally:
            conn.close()

    def test_commit_without_listing_does_not_create_the_key(self):
        conn = self._conn()
        try:
            frame = self._frame(conn, {})
            turn_frame.commit(
                frame,
                intent=llm.Intent(intencion="otro", confianza=0.9),
                product=None,
                generator="template",
                escalate=False,
                stage="DISCOVERY",
            )
            stored = conversations.get_context(conn, 1)
            self.assertNotIn("last_listing", stored)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
