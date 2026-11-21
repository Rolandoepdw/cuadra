"""Telegram identity: numeric ids are the key, handles are a mutable label.

Customers are already keyed by their numeric Telegram id. Suppliers are stored
by `@usuario`, so these tests cover the backfill paths that learn and protect the
numeric id (`suppliers.telegram_peer_id`).

Everything runs against a throwaway SQLite database and never calls Telegram or a
model server. Run from `backend/`:

    .venv\\Scripts\\python.exe -m unittest discover -s tests -v
"""

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from cuadra import config, db
from cuadra.ai import handlers, ingest
from cuadra.channels import telegram, telegram_user
from cuadra.domain import catalog, conversations


class TelegramIdentityTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_db_path = config.DB_PATH
        config.DB_PATH = Path(self._tmp.name) / "test.db"
        conn = db.connect()
        try:
            db.apply_migrations(conn)
            conn.execute(
                """INSERT INTO suppliers (id, name, telegram_id)
                   VALUES (1, 'Proveedor Uno', 'proveedor_uno')"""
            )
            conn.execute(
                """INSERT INTO suppliers (id, name, telegram_id, telegram_peer_id)
                   VALUES (2, 'Proveedor Dos', '@proveedor_dos', '222333444')"""
            )
            conn.commit()
        finally:
            conn.close()

    def tearDown(self):
        config.DB_PATH = self._old_db_path
        self._tmp.cleanup()

    def _supplier(self, supplier_id: int) -> dict:
        conn = db.connect()
        try:
            return catalog.get_supplier(conn, supplier_id)
        finally:
            conn.close()

    def _events(self, event_type: str) -> list[dict]:
        conn = db.connect()
        try:
            return [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM events WHERE type = ? ORDER BY id", (event_type,)
                )
            ]
        finally:
            conn.close()

    # ------------------------------------------------------------ normalizing

    def test_handle_accepts_bare_handle_at_handle_and_links(self):
        self.assertEqual(catalog.normalize_telegram_handle("proveedor_uno"), "@proveedor_uno")
        self.assertEqual(catalog.normalize_telegram_handle("@proveedor_uno"), "@proveedor_uno")
        self.assertEqual(
            catalog.normalize_telegram_handle("https://t.me/proveedor_uno"), "@proveedor_uno"
        )
        self.assertIsNone(catalog.normalize_telegram_handle("   "))
        self.assertEqual(catalog.normalize_telegram_handle("123456789"), "123456789")

    def test_peer_id_must_be_numeric(self):
        conn = db.connect()
        try:
            with self.assertRaises(catalog.Invalid):
                catalog.update_supplier(conn, 1, {"telegram_peer_id": "@proveedor_uno"})
        finally:
            conn.close()
        self.assertIsNone(self._supplier(1)["telegram_peer_id"])

    def test_create_supplier_normalizes_the_handle(self):
        conn = db.connect()
        try:
            supplier = catalog.create_supplier(
                conn,
                {
                    "name": "Nuevo",
                    "telegram_id": "https://t.me/proveedor_nuevo",
                    "telegram_peer_id": "999888777",
                },
            )
        finally:
            conn.close()
        self.assertEqual(supplier["telegram_id"], "@proveedor_nuevo")
        self.assertEqual(supplier["telegram_peer_id"], "999888777")

    # ------------------------------------------------------- passive learning

    def test_inbound_from_a_known_handle_learns_the_numeric_id(self):
        result = ingest.ingest_incoming(
            channel="telegram",
            identifier="555666777",
            handle="proveedor_uno",
            body="hola, tengo el pedido",
            external_id="tg-learn-1",
        )

        self.assertEqual(result["source"], "supplier")
        self.assertEqual(result["supplier_id"], 1)
        self.assertEqual(self._supplier(1)["telegram_peer_id"], "555666777")
        events = self._events("supplier_peer_resolved")
        self.assertEqual(len(events), 1)
        self.assertIn('"source": "inbound"', events[0]["details_json"])

    def test_next_message_matches_by_numeric_id_even_without_the_handle(self):
        ingest.ingest_incoming(
            channel="telegram",
            identifier="555666777",
            handle="proveedor_uno",
            body="primero",
            external_id="tg-learn-2",
        )

        result = ingest.ingest_incoming(
            channel="telegram",
            identifier="555666777",
            body="segundo",
            external_id="tg-learn-3",
        )

        self.assertEqual(result["source"], "supplier")
        self.assertEqual(result["supplier_id"], 1)

    def test_learned_peer_takes_priority_when_the_handle_is_renamed(self):
        # The supplier renamed themselves: the handle no longer matches, but the
        # numeric id still does.
        result = ingest.ingest_incoming(
            channel="telegram",
            identifier="222333444",
            handle="proveedor_renamed",
            body="sigo siendo yo",
            external_id="tg-rename-1",
        )

        self.assertEqual(result["source"], "supplier")
        self.assertEqual(result["supplier_id"], 2)

    def test_a_handle_taken_over_by_someone_else_is_not_the_supplier(self):
        result = ingest.ingest_incoming(
            channel="telegram",
            identifier="999000111",
            handle="proveedor_dos",
            body="hola, soy nuevo aqui",
            external_id="tg-takeover-1",
        )

        self.assertEqual(result["source"], "customer")
        self.assertEqual(self._supplier(2)["telegram_peer_id"], "222333444")
        events = self._events("supplier_peer_mismatch")
        self.assertEqual(len(events), 1)
        self.assertIn('"actual_peer_id": "999000111"', events[0]["details_json"])

    def test_repeated_mismatch_is_reported_once(self):
        for index in range(3):
            ingest.ingest_incoming(
                channel="telegram",
                identifier="999000111",
                handle="proveedor_dos",
                body=f"spam {index}",
                external_id=f"tg-takeover-{index}",
            )

        self.assertEqual(len(self._events("supplier_peer_mismatch")), 1)

    def test_unknown_handle_stays_a_customer(self):
        result = ingest.ingest_incoming(
            channel="telegram",
            identifier="123123123",
            handle="cliente_nuevo",
            body="busco un power bank",
            external_id="tg-client-1",
        )

        self.assertEqual(result["source"], "customer")
        self.assertIsNone(self._supplier(1)["telegram_peer_id"])

    def test_group_chat_id_is_never_learned_as_a_supplier_peer(self):
        # The bot can be added to a group: its negative chat id is not the TCP's
        # personal id, and escalations must not go to the whole group.
        result = ingest.ingest_incoming(
            channel="telegram",
            identifier="-1001234567890",
            handle="proveedor_uno",
            body="hola desde el grupo",
            external_id="tg-group-1",
        )

        self.assertEqual(result["source"], "customer")
        self.assertIsNone(self._supplier(1)["telegram_peer_id"])
        self.assertEqual(self._events("supplier_peer_resolved"), [])

    # ------------------------------------------------------------- visibility

    def test_missing_peer_list_only_includes_handles_without_a_numeric_id(self):
        conn = db.connect()
        try:
            self.assertEqual(
                [s["id"] for s in catalog.list_suppliers_missing_peer(conn)], [1]
            )
        finally:
            conn.close()

        ingest.ingest_incoming(
            channel="telegram",
            identifier="555666777",
            handle="proveedor_uno",
            body="hola",
            external_id="tg-missing-1",
        )

        conn = db.connect()
        try:
            self.assertEqual(catalog.list_suppliers_missing_peer(conn), [])
        finally:
            conn.close()

    # ------------------------------------------------------ operator shortcuts

    def test_numeric_telegram_id_is_stored_as_the_peer(self):
        conn = db.connect()
        try:
            supplier = catalog.create_supplier(
                conn, {"name": "Pegado", "telegram_id": " 555666777 "}
            )
        finally:
            conn.close()
        self.assertIsNone(supplier["telegram_id"])
        self.assertEqual(supplier["telegram_peer_id"], "555666777")

    def test_numeric_telegram_id_moves_to_the_peer_on_update(self):
        conn = db.connect()
        try:
            catalog.update_supplier(conn, 1, {"telegram_id": "444555666"})
        finally:
            conn.close()
        supplier = self._supplier(1)
        self.assertIsNone(supplier["telegram_id"])
        self.assertEqual(supplier["telegram_peer_id"], "444555666")

    # -------------------------------------------------------- resolve on save

    def test_resolve_job_learns_the_peer(self):
        with (
            patch("cuadra.channels.telegram_user.available", return_value=True),
            patch(
                "cuadra.channels.telegram_user.resolve_peer",
                new=AsyncMock(return_value="777888999"),
            ),
        ):
            asyncio.run(handlers.resolve_supplier_peer({"supplier_id": 1}))

        self.assertEqual(self._supplier(1)["telegram_peer_id"], "777888999")
        events = self._events("supplier_peer_resolved")
        self.assertEqual(len(events), 1)
        self.assertIn('"source": "save"', events[0]["details_json"])

    def test_resolve_job_skips_a_supplier_that_already_has_a_peer(self):
        with (
            patch("cuadra.channels.telegram_user.available", return_value=True),
            patch(
                "cuadra.channels.telegram_user.resolve_peer",
                new=AsyncMock(return_value="000000000"),
            ) as resolve,
        ):
            asyncio.run(handlers.resolve_supplier_peer({"supplier_id": 2}))

        resolve.assert_not_awaited()
        self.assertEqual(self._supplier(2)["telegram_peer_id"], "222333444")

    def test_resolve_job_is_a_noop_without_a_userbot(self):
        with patch("cuadra.channels.telegram_user.available", return_value=False):
            asyncio.run(handlers.resolve_supplier_peer({"supplier_id": 1}))
        self.assertIsNone(self._supplier(1)["telegram_peer_id"])

    # --------------------------------------------------------- bot chat guard

    def test_bot_ignores_group_chats(self):
        with patch("cuadra.channels.telegram.ingest.ingest_incoming") as ingest_mock:
            asyncio.run(
                telegram._handle(
                    None,
                    identifier="-1001234567890",
                    from_user=None,
                    type="text",
                    body="hola grupo",
                    chat_type="supergroup",
                )
            )
        ingest_mock.assert_not_called()

    def test_bot_accepts_private_chats_and_forwards_the_handle(self):
        user = SimpleNamespace(full_name="Cliente", username="cliente_tg")
        with patch("cuadra.channels.telegram.ingest.ingest_incoming") as ingest_mock:
            asyncio.run(
                telegram._handle(
                    None,
                    identifier="123456",
                    from_user=user,
                    type="text",
                    body="hola",
                    chat_type="private",
                )
            )

        ingest_mock.assert_called_once()
        kwargs = ingest_mock.call_args.kwargs
        self.assertEqual(kwargs["identifier"], "123456")
        self.assertEqual(kwargs["handle"], "cliente_tg")
        self.assertEqual(kwargs["identity"], "bot")

    # ---------------------------------------------------------- startup alert

    def test_startup_reports_suppliers_without_a_peer(self):
        from cuadra import main as core_main

        core_main._warn_suppliers_without_peer()

        events = self._events("suppliers_missing_peer")
        self.assertEqual(len(events), 1)
        self.assertIn('"supplier_ids": [1]', events[0]["details_json"])


class OperatorMessageTests(unittest.TestCase):
    """F11 (prueba #3): las salidas manuales del userbot se registran."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_db_path = config.DB_PATH
        config.DB_PATH = Path(self._tmp.name) / "test.db"
        telegram_user._programmatic_sends.clear()
        conn = db.connect()
        try:
            db.apply_migrations(conn)
            customer = conversations.get_or_create_customer(
                conn, "telegram", "55500001", "Cliente"
            )
            self.conversation = conversations.get_or_create_open_conversation(
                conn, customer["id"], "telegram", identity="userbot"
            )
        finally:
            conn.close()

    def tearDown(self):
        telegram_user._programmatic_sends.clear()
        config.DB_PATH = self._old_db_path
        self._tmp.cleanup()

    def _messages(self) -> list[dict]:
        conn = db.connect()
        try:
            return [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM messages ORDER BY id"
                ).fetchall()
            ]
        finally:
            conn.close()

    def _operator_events(self) -> list[dict]:
        conn = db.connect()
        try:
            return [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM events WHERE type = 'operator_message'"
                ).fetchall()
            ]
        finally:
            conn.close()

    def test_a_manual_reply_is_stored_as_operator(self):
        message = telegram_user.record_operator_message(
            "55500001", "Ya te lo reservo", "77"
        )
        self.assertIsNotNone(message)
        self.assertEqual(message["direction"], "OUTBOUND")
        self.assertEqual(message["author"], "operator")
        self.assertEqual(message["body"], "Ya te lo reservo")
        self.assertEqual(len(self._operator_events()), 1)

    def test_a_programmatic_send_is_not_duplicated(self):
        telegram_user._mark_programmatic("55500001", "Tu pedido V-000001 quedó registrado")
        result = telegram_user.record_operator_message(
            "55500001", "Tu pedido V-000001 quedó registrado", "78"
        )
        self.assertIsNone(result)
        self.assertEqual(self._messages(), [])

    def test_the_same_telegram_id_is_not_stored_twice(self):
        telegram_user.record_operator_message("55500001", "Hola", "79")
        result = telegram_user.record_operator_message("55500001", "Hola", "79")
        self.assertIsNone(result)
        self.assertEqual(len(self._messages()), 1)

    def test_writing_to_a_stranger_creates_nothing(self):
        result = telegram_user.record_operator_message("99900001", "Hola", "80")
        self.assertIsNone(result)
        self.assertEqual(self._messages(), [])


if __name__ == "__main__":
    unittest.main()
